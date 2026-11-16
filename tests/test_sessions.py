import os
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from bringmail import bringmaildb
from bringmail import constants
from bringmail import helpers


class FakeSessionClient:
    def __init__(self):
        self.selected = None
        self.added_flags = []
        self.removed_flags = []
        self.moved = []
        self.logged_out = False
        self.noops = 0

    def select_folder(self, name, readonly=False):
        self.selected = name
        return {b'UIDVALIDITY': 1, b'UIDNEXT': 2}

    def add_flags(self, messages, flags):
        self.added_flags.append((list(messages), list(flags)))

    def remove_flags(self, messages, flags):
        self.removed_flags.append((list(messages), list(flags)))

    def move(self, messages, folder):
        self.moved.append((list(messages), folder))
        return b'OK'

    def search(self, criteria):
        if criteria and criteria[0].upper() == 'HEADER':
            return [42]
        return []

    def noop(self):
        self.noops += 1

    def logout(self):
        self.logged_out = True

    def shutdown(self):
        self.logged_out = True


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = bringmaildb.BringMailDB(data_directory=self.tempdir.name, create=True)
        with self.db.transaction:
            self.account = self.db.add_account(
                host='imap.example.com',
                port=993,
                ssl=True,
                starttls=False,
                username='user@example.com',
                password='hunter2',
                autorefresh_interval=900,
            )
            self.folder = self.db.add_folder(
                account=self.account,
                name='INBOX',
                display_name='Inbox',
                delimiter='/',
                special_use=constants.SPECIAL_USE_INBOX,
            )
            self.message = self.db.add_message(
                account=self.account,
                folder=self.folder,
                uid=1,
                uidvalidity=1,
                raw=None,
                meta={
                    'subject': 'test',
                    'from': [],
                    'to': [],
                    'cc': [],
                    'date_unix': 1,
                    'message_id': '<test@example.com>',
                    'has_attachments': False,
                    'snippet': '',
                },
                size=0,
                flags={},
            )
        self.client = FakeSessionClient()

    def tearDown(self):
        helpers.drop_imap_session(self.account.id)
        self.db.close()
        self.tempdir.cleanup()

    def test_session_reuse_and_flags(self):
        with mock.patch('bringmail.helpers.connect_imap', return_value=self.client) as connect:
            self.message.set_seen(True)
            self.message.set_flagged(True)

        self.assertEqual(connect.call_count, 1)
        self.assertEqual(self.client.selected, 'INBOX')
        self.assertEqual(self.client.added_flags[0], ([1], [b'\\Seen']))
        self.assertEqual(self.client.added_flags[1], ([1], [b'\\Flagged']))
        self.assertTrue(self.message.seen)
        self.assertTrue(self.message.flagged)
        self.assertEqual(
            self.db.select_one_value('SELECT seen FROM messages WHERE id == ?', [self.message.id]),
            1,
        )

    def test_session_discarded_after_error(self):
        def boom(client):
            raise RuntimeError('boom')

        with mock.patch('bringmail.helpers.connect_imap', return_value=self.client) as connect:
            with self.assertRaises(RuntimeError):
                helpers.imap_call(self.account, boom)
            helpers.imap_call(self.account, lambda client: client.select_folder('INBOX'))

        self.assertEqual(connect.call_count, 2)

    def test_move_and_delete(self):
        with self.db.transaction:
            archive = self.db.add_folder(
                account=self.account,
                name='Archive',
                display_name='Archive',
                delimiter='/',
            )
            self.db.add_folder(
                account=self.account,
                name='Trash',
                display_name='Trash',
                delimiter='/',
                special_use=constants.SPECIAL_USE_TRASH,
            )

        with mock.patch('bringmail.helpers.connect_imap', return_value=self.client):
            self.message.move_to(archive)
            self.assertEqual(self.message.folder_id, archive.id)
            self.assertEqual(self.message.uid, 42)
            self.assertEqual(self.message.uidvalidity, 1)
            self.assertEqual(self.client.moved[-1], ([1], 'Archive'))

            self.message.delete()
            trash = self.account.get_trash_folder()
            self.assertIsNotNone(trash)
            self.assertEqual(self.message.folder_id, trash.id)
            self.assertEqual(self.message.uid, 42)
            self.assertEqual(self.client.moved[-1], ([42], 'Trash'))

        row = self.db.select_one(
            'SELECT folder_id, uid FROM messages WHERE id == ?',
            [self.message.id],
        )
        self.assertEqual(row['folder_id'], trash.id)
        self.assertEqual(row['uid'], 42)


if __name__ == '__main__':
    unittest.main()
