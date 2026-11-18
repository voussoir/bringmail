import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from bringmail import bringmaildb
from bringmail import constants
from bringmail import idle_supervisor


class FakeWatcher:
    instances = []

    def __init__(self, snapshot, on_change, on_state, **kwargs):
        self.snapshot = snapshot
        self.on_change = on_change
        self.on_state = on_state
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.delay = None
        self.state = idle_supervisor.STATE_STOPPED
        self.detail = None
        FakeWatcher.instances.append(self)

    def start(self, delay=0):
        self.started = True
        self.delay = delay

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        pass


class IdleSupervisorTest(unittest.TestCase):
    def setUp(self):
        FakeWatcher.instances = []
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = bringmaildb.BringMailDB(
            data_directory=self.tempdir.name,
            create=True,
        )
        with self.db.transaction:
            self.account = self.db.add_account(
                label='Test',
                host='imap.example.com',
                port=993,
                ssl=True,
                starttls=False,
                username='user@example.com',
                password='hunter2',
                autorefresh_interval=900,
            )
            self.inbox = self.db.add_folder(
                account=self.account,
                name='INBOX',
                display_name='Inbox',
                delimiter='/',
                special_use=constants.SPECIAL_USE_INBOX,
            )

        self.supervisor = idle_supervisor.IdleSupervisor(
            self.db,
            watcher_class=FakeWatcher,
            startup_stagger_seconds=0,
        )

    def tearDown(self):
        self.supervisor.stop()
        self.db.close()
        self.tempdir.cleanup()

    def test_start_creates_watcher_for_inbox(self):
        self.supervisor.start()

        self.assertEqual(len(FakeWatcher.instances), 1)
        watcher = FakeWatcher.instances[0]
        self.assertTrue(watcher.started)
        self.assertEqual(watcher.snapshot.id, self.account.id)
        self.assertEqual(watcher.snapshot.folder_id, self.inbox.id)
        self.assertEqual(watcher.snapshot.folder_name, 'INBOX')
        self.assertEqual(watcher.snapshot.password, 'hunter2')

    def test_skips_account_without_inbox(self):
        with self.db.transaction:
            other = self.db.add_account(
                label='Other',
                host='imap.example.com',
                port=993,
                ssl=True,
                starttls=False,
                username='other@example.com',
                password='hunter2',
                autorefresh_interval=900,
            )
            self.db.add_folder(
                account=other,
                name='Archive',
                display_name='Archive',
                delimiter='/',
            )

        self.supervisor.start()

        self.assertEqual(
            [watcher.snapshot.id for watcher in FakeWatcher.instances],
            [self.account.id],
        )

    def test_skips_offline_account(self):
        with self.db.transaction:
            self.db.add_account(label='Offline', offline=True)

        self.supervisor.start()

        self.assertEqual(
            [watcher.snapshot.id for watcher in FakeWatcher.instances],
            [self.account.id],
        )

    def test_reconfigure_keeps_matching_watcher(self):
        self.supervisor.start()
        watcher = FakeWatcher.instances[0]

        self.supervisor.reconfigure()

        self.assertEqual(len(FakeWatcher.instances), 1)
        self.assertFalse(watcher.stopped)
        self.assertIs(FakeWatcher.instances[0], watcher)

    def test_reconfigure_restarts_after_password_change(self):
        self.supervisor.start()
        old_watcher = FakeWatcher.instances[0]

        with self.db.transaction:
            self.account.set_password('new password')

        self.supervisor.reconfigure()

        self.assertTrue(old_watcher.stopped)
        self.assertEqual(len(FakeWatcher.instances), 2)
        new_watcher = FakeWatcher.instances[1]
        self.assertFalse(new_watcher.stopped)
        self.assertEqual(new_watcher.snapshot.password, 'new password')

    def test_reconfigure_stops_deleted_account(self):
        self.supervisor.start()
        watcher = FakeWatcher.instances[0]

        with self.db.transaction:
            self.account.delete()

        self.supervisor.reconfigure()

        self.assertTrue(watcher.stopped)
        self.assertEqual(self.supervisor.status(), {})

    def test_stop_stops_all_watchers(self):
        self.supervisor.start()
        watcher = FakeWatcher.instances[0]

        self.supervisor.stop()

        self.assertTrue(watcher.stopped)
        self.assertFalse(self.supervisor.is_started)
        self.assertEqual(self.supervisor.status(), {})

    def test_status_reports_watcher_state(self):
        self.supervisor.start()
        watcher = FakeWatcher.instances[0]
        watcher.state = idle_supervisor.STATE_CONNECTED
        watcher.detail = 'watching'

        status = self.supervisor.status()[self.account.id]

        self.assertEqual(status['state'], idle_supervisor.STATE_CONNECTED)
        self.assertEqual(status['detail'], 'watching')
        self.assertEqual(status['folder_name'], 'INBOX')

    def test_find_inbox_folder(self):
        folder = idle_supervisor.find_inbox_folder(self.account)
        self.assertEqual(folder.id, self.inbox.id)


if __name__ == '__main__':
    unittest.main()
