import os
import sys
import threading
import time
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from bringmail import exceptions
from bringmail import idle
from bringmail import idle_supervisor

def make_snapshot():
    return idle_supervisor.AccountSnapshot(
        id=1,
        display_name='Test',
        host='imap.example.com',
        port=993,
        ssl=True,
        starttls=False,
        username='user@example.com',
        password='hunter2',
        folder_id=10,
        folder_name='INBOX',
    )

class FakeSession:
    def __init__(self, snapshot, folder_name, **kwargs):
        self.snapshot = snapshot
        self.folder_name = folder_name
        self.kwargs = kwargs
        self.closed = []
        self.open_error = None
        self.poll_error = None
        self.poll_count = 0
        self.events = []

    def open(self):
        if self.open_error is not None:
            raise self.open_error

    def poll(self, timeout=None):
        self.poll_count += 1
        time.sleep(0.01)
        if self.poll_error is not None:
            raise self.poll_error
        events = self.events
        self.events = []
        return events

    def close(self, graceful=True):
        self.closed.append(graceful)

class RecordingFactory:
    def __init__(self, session_class=FakeSession):
        self.session_class = session_class
        self.sessions = []
        self.calls = 0

    def __call__(self, snapshot, folder_name, **kwargs):
        self.calls += 1
        session = self.session_class(snapshot, folder_name, **kwargs)
        self.sessions.append(session)
        return session

class OneEventSession(FakeSession):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent = False

    def poll(self, timeout=None):
        self.poll_count += 1
        time.sleep(0.01)
        if not self.sent:
            self.sent = True
            return [idle.IdleEvent(idle.EVENT_EXISTS, count=1)]
        return []

class UnsupportedSession(FakeSession):
    def open(self):
        raise exceptions.IdleUnsupported()

class DisconnectOnceSession(FakeSession):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.disconnected = False

    def poll(self, timeout=None):
        if not self.disconnected:
            self.disconnected = True
            time.sleep(0.01)
            raise exceptions.IdleDisconnected('socket error: EOF')
        return super().poll(timeout=timeout)

class IdleWatcherTest(unittest.TestCase):
    def test_connected_catch_up_and_event(self):
        changes = []
        states = []
        done = threading.Event()

        def on_change(snapshot, events, reason):
            changes.append((reason, events))
            if reason == 'idle':
                done.set()

        def on_state(snapshot, state, detail):
            states.append(state)

        factory = RecordingFactory(session_class=OneEventSession)
        watcher = idle_supervisor.IdleWatcher(
            make_snapshot(),
            on_change,
            on_state,
            poll_seconds=0.01,
            session_factory=factory,
        )
        watcher.start()
        try:
            self.assertTrue(done.wait(2))
        finally:
            watcher.stop()
            watcher.join(timeout=2)

        reasons = [reason for (reason, _) in changes]
        self.assertIn('connected', reasons)
        self.assertIn('idle', reasons)

        idle_events = [events for (reason, events) in changes if reason == 'idle']
        self.assertEqual(idle_events[0][0].kind, idle.EVENT_EXISTS)
        self.assertIn(idle_supervisor.STATE_CONNECTED, states)
        self.assertEqual(factory.sessions[0].closed, [True])

    def test_unsupported_state(self):
        states = []
        changes = []

        def on_state(snapshot, state, detail):
            states.append(state)

        def on_change(snapshot, events, reason):
            changes.append((reason, events))

        watcher = idle_supervisor.IdleWatcher(
            make_snapshot(),
            on_change,
            on_state,
            poll_seconds=0.01,
            unsupported_retry_seconds=60,
            session_factory=RecordingFactory(session_class=UnsupportedSession),
        )
        watcher.start()
        try:
            deadline = time.time() + 2
            while (
                time.time() < deadline and
                idle_supervisor.STATE_UNSUPPORTED not in states
            ):
                time.sleep(0.01)
        finally:
            watcher.stop()
            watcher.join(timeout=2)

        self.assertIn(idle_supervisor.STATE_UNSUPPORTED, states)
        self.assertEqual(changes, [])

    def test_reconnects_after_open_error(self):
        states = []
        connected = threading.Event()
        factory = RecordingFactory()

        def on_state(snapshot, state, detail):
            states.append(state)
            if state == idle_supervisor.STATE_CONNECTED:
                connected.set()

        first_session = None

        def flaky_factory(snapshot, folder_name, **kwargs):
            nonlocal first_session
            session = factory(snapshot, folder_name, **kwargs)
            if first_session is None:
                first_session = session
                session.open_error = exceptions.MailServerConnectionError('nope')
            return session

        watcher = idle_supervisor.IdleWatcher(
            make_snapshot(),
            None,
            on_state,
            poll_seconds=0.01,
            reconnect_min_seconds=0.01,
            reconnect_max_seconds=0.02,
            session_factory=flaky_factory,
        )
        watcher.start()
        try:
            self.assertTrue(connected.wait(2))
        finally:
            watcher.stop()
            watcher.join(timeout=2)

        self.assertIn(idle_supervisor.STATE_RETRYING, states)
        self.assertGreaterEqual(factory.calls, 2)

    def test_expected_disconnect_logged_at_debug(self):
        retrying = threading.Event()
        connected = threading.Event()

        def on_state(snapshot, state, detail):
            if state == idle_supervisor.STATE_RETRYING:
                retrying.set()
            elif state == idle_supervisor.STATE_CONNECTED:
                connected.set()

        watcher = idle_supervisor.IdleWatcher(
            make_snapshot(),
            None,
            on_state,
            poll_seconds=0.01,
            reconnect_min_seconds=0.01,
            reconnect_max_seconds=0.02,
            session_factory=RecordingFactory(session_class=DisconnectOnceSession),
        )

        with self.assertLogs('bringmail.idle_supervisor', level='DEBUG') as captured:
            watcher.start()
            try:
                self.assertTrue(retrying.wait(2))
                self.assertTrue(connected.wait(2))
            finally:
                watcher.stop()
                watcher.join(timeout=2)

        messages = [record.getMessage() for record in captured.records]
        self.assertTrue(any('disconnected' in message for message in messages))
        self.assertTrue(any('socket error: EOF' in message for message in messages))
        self.assertFalse(any('failed' in message for message in messages))

    def test_debounce_coalesces_events(self):
        changes = []

        def on_change(snapshot, events, reason):
            changes.append(events)

        watcher = idle_supervisor.IdleWatcher(
            make_snapshot(),
            on_change,
            None,
            debounce_seconds=10,
        )
        first = idle.IdleEvent(idle.EVENT_EXISTS, count=1)
        second = idle.IdleEvent(idle.EVENT_EXISTS, count=2)
        clock = [0.0]

        with mock.patch(
                'bringmail.idle_supervisor.time.monotonic',
                side_effect=lambda: clock[0],
            ):
            watcher._collect([first])
            watcher._collect([second])
            self.assertEqual(len(changes), 1)

            clock[0] = 11.0
            watcher._collect([])

        self.assertEqual(len(changes), 2)
        self.assertEqual(changes[1], [second])

if __name__ == '__main__':
    unittest.main()
