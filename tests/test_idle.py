import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from bringmail import exceptions
from bringmail import idle


class FakeIdleClient:
    def __init__(self):
        self.capabilities = {'IDLE'}
        self.selected = []
        self.select_kwargs = None
        self.idle_calls = 0
        self.idle_done_calls = 0
        self.idle_responses = []
        self.idle_done_responses = []
        self.timeouts = []
        self.logged_out = False
        self.shutdown_called = False

    def has_capability(self, capability):
        return capability.upper() in self.capabilities

    def select_folder(self, name, readonly=False):
        self.selected.append(name)
        self.select_kwargs = {'name': name, 'readonly': readonly}
        return {}

    def idle(self):
        self.idle_calls += 1

    def idle_check(self, timeout=None):
        self.timeouts.append(timeout)
        responses = self.idle_responses
        self.idle_responses = []
        return responses

    def idle_done(self):
        self.idle_done_calls += 1
        responses = self.idle_done_responses
        self.idle_done_responses = []
        return (b'Idle terminated', responses)

    def logout(self):
        self.logged_out = True

    def shutdown(self):
        self.shutdown_called = True


class IdleSessionTest(unittest.TestCase):
    def setUp(self):
        self.account = mock.Mock()
        self.account.id = 7
        self.client = FakeIdleClient()
        self.connect_patch = mock.patch(
            'bringmail.helpers.connect_imap',
            return_value=self.client,
        )
        self.connect = self.connect_patch.start()
        self.addCleanup(self.connect_patch.stop)

    def make_session(self, **kwargs):
        return idle.IdleSession(self.account, 'INBOX', **kwargs)

    def test_open_checks_capability_and_selects_readonly(self):
        session = self.make_session()
        session.open()

        self.assertTrue(session.is_open)
        self.assertFalse(session.is_idling)
        self.assertEqual(self.client.select_kwargs, {'name': 'INBOX', 'readonly': True})
        self.assertFalse(self.client.logged_out)
        self.assertFalse(self.client.shutdown_called)

    def test_open_unsupported(self):
        self.client.capabilities = set()
        session = self.make_session()

        with self.assertRaises(exceptions.IdleUnsupported):
            session.open()

        self.assertFalse(session.is_open)
        self.assertIsNone(self.client.select_kwargs)
        self.assertTrue(self.client.shutdown_called)
        self.assertFalse(self.client.logged_out)

    def test_poll_starts_idle_once(self):
        session = self.make_session()
        session.open()
        self.client.idle_responses = [(1, b'EXISTS'), (b'OK', b'Still here')]

        events = session.poll(timeout=2)

        self.assertEqual(self.client.idle_calls, 1)
        self.assertEqual(self.client.timeouts, [2])
        self.assertEqual(
            [event.kind for event in events],
            [idle.EVENT_EXISTS, idle.EVENT_OK],
        )
        self.assertEqual(events[0].count, 1)
        self.assertTrue(session.is_idling)

        session.poll(timeout=1)
        self.assertEqual(self.client.idle_calls, 1)

    def test_poll_uses_default_timeout(self):
        session = self.make_session(poll_seconds=3)
        session.open()
        session.poll()
        self.assertEqual(self.client.timeouts, [3])

    def test_poll_requires_open(self):
        session = self.make_session()
        with self.assertRaises(RuntimeError):
            session.poll()

    def test_poll_renews_after_deadline(self):
        session = self.make_session(renew_seconds=10, poll_seconds=1)
        session.open()
        now = [0.0]

        with mock.patch('bringmail.idle.time.monotonic', side_effect=lambda: now[0]):
            session.poll()
            self.assertEqual(self.client.idle_calls, 1)

            now[0] = 11.0
            self.client.idle_done_responses = [(4, b'EXISTS')]
            events = session.poll()

        self.assertEqual(self.client.idle_done_calls, 1)
        self.assertEqual(self.client.idle_calls, 2)
        self.assertIn(idle.EVENT_EXISTS, [event.kind for event in events])
        self.assertTrue(session.is_idling)

    def test_close_graceful(self):
        session = self.make_session()
        session.open()
        session.poll()

        session.close()

        self.assertFalse(session.is_open)
        self.assertEqual(self.client.idle_done_calls, 1)
        self.assertTrue(self.client.logged_out)
        self.assertFalse(self.client.shutdown_called)

    def test_close_after_idle_done_failure_shuts_down(self):
        session = self.make_session()
        session.open()
        session.poll()

        self.client.idle_done = mock.Mock(side_effect=RuntimeError('broken'))
        session.close()

        self.assertFalse(session.is_open)
        self.assertTrue(self.client.shutdown_called)
        self.assertFalse(self.client.logged_out)

    def test_parse_response_kinds(self):
        exists = idle.parse_response((3, b'EXISTS'))
        self.assertEqual(exists.kind, idle.EVENT_EXISTS)
        self.assertEqual(exists.count, 3)

        expunge = idle.parse_response((2, b'EXPUNGE'))
        self.assertEqual(expunge.kind, idle.EVENT_EXPUNGE)
        self.assertEqual(expunge.count, 2)

        flags = idle.parse_response(
            (1, b'FETCH', (b'FLAGS', (b'\\Seen', b'\\Flagged')))
        )
        self.assertEqual(flags.kind, idle.EVENT_FLAGS)
        self.assertEqual(flags.count, 1)
        self.assertEqual(flags.flags, ('\\Seen', '\\Flagged'))

        ok = idle.parse_response((b'OK', b'Still here'))
        self.assertEqual(ok.kind, idle.EVENT_OK)
        self.assertEqual(ok.text, 'Still here')

        bye = idle.parse_response((b'BYE', b'Autologout'))
        self.assertEqual(bye.kind, idle.EVENT_BYE)
        self.assertEqual(bye.text, 'Autologout')

        other = idle.parse_response((1, b'RECENT'))
        self.assertEqual(other.kind, idle.EVENT_OTHER)


if __name__ == '__main__':
    unittest.main()
