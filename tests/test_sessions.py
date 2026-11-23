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
        self.copied = []
        self.expunged = []
        self.searches = []
        self.logged_out = False
        self.noops = 0
        self.move_supported = True
        self.copyuid = True
        self.header_search_results = [42]

    def select_folder(self, name, readonly=False):
        self.selected = name
        return {b'UIDVALIDITY': 1, b'UIDNEXT': 2}

    def capabilities(self):
        return (b'IMAP4rev1', b'MOVE')

    def add_flags(self, messages, flags):
        self.added_flags.append((list(messages), list(flags)))

    def remove_flags(self, messages, flags):
        self.removed_flags.append((list(messages), list(flags)))

    def move(self, messages, folder):
        self.moved.append((list(messages), folder))
        if not self.move_supported:
            raise RuntimeError('The server does not support MOVE.')
        if not self.copyuid:
            return b'Move completed.'
        source = ','.join(str(message) for message in messages)
        return f'[COPYUID 1 {source} 42] Move completed.'.encode('ascii')

    def copy(self, messages, folder):
        self.copied.append((list(messages), folder))
        if not self.copyuid:
            return b'Copy completed.'
        source = ','.join(str(message) for message in messages)
        return f'[COPYUID 1 {source} 42] Copy completed.'.encode('ascii')

    def expunge(self, messages):
        self.expunged.append(list(messages))

    def search(self, criteria):
        self.searches.append(criteria)
        if criteria and criteria[0].upper() == 'HEADER':
            return list(self.header_search_results)
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

    def make_archive(self):
        with self.db.transaction:
            return self.db.add_folder(
                account=self.account,
                name='Archive',
                display_name='Archive',
                delimiter='/',
            )

    def test_move_uses_copyuid_without_searching(self):
        archive = self.make_archive()

        with mock.patch('bringmail.helpers.connect_imap', return_value=self.client):
            self.message.move_to(archive)

        self.assertEqual(self.message.folder_id, archive.id)
        self.assertEqual(self.message.uid, 42)
        self.assertEqual(self.message.uidvalidity, 1)
        self.assertEqual(self.client.searches, [])

    def test_move_falls_back_to_search_without_copyuid(self):
        self.client.copyuid = False
        archive = self.make_archive()

        with mock.patch('bringmail.helpers.connect_imap', return_value=self.client):
            self.message.move_to(archive)

        self.assertEqual(self.message.folder_id, archive.id)
        self.assertEqual(self.message.uid, 42)
        self.assertTrue(self.client.searches)

    def test_move_uses_newest_uid_when_message_id_matches_multiple(self):
        self.client.copyuid = False
        self.client.header_search_results = [42, 57]
        archive = self.make_archive()

        with mock.patch('bringmail.helpers.connect_imap', return_value=self.client):
            self.message.move_to(archive)

        self.assertEqual(self.message.folder_id, archive.id)
        self.assertEqual(self.message.uid, 57)
        self.assertEqual(self.message.uidvalidity, 1)

    def test_move_falls_back_to_copy_when_move_is_unsupported(self):
        self.client.move_supported = False
        archive = self.make_archive()

        with mock.patch('bringmail.helpers.connect_imap', return_value=self.client):
            self.message.move_to(archive)

        self.assertEqual(self.client.copied, [([1], 'Archive')])
        self.assertEqual(self.client.expunged, [[1]])
        self.assertEqual(self.message.folder_id, archive.id)
        self.assertEqual(self.message.uid, 42)
        self.assertEqual(self.message.uidvalidity, 1)
        self.assertEqual(self.client.searches, [])

    def test_copyuid_from_untagged_ok(self):
        class StubIMAP:
            untagged_responses = {'OK': [b'[COPYUID 9 5 90] Done']}

        class StubClient:
            _imap = StubIMAP()

        copyuid = helpers.copyuid_from_response(StubClient(), b'Done')
        self.assertEqual(copyuid, (9, {5: 90}))

    def test_copyuid_from_tagged_response_code(self):
        # imapclient's move() returns None, and imaplib keeps the contents of
        # a tagged [COPYUID ...] response code in its own response map.
        class StubIMAP:
            untagged_responses = {'COPYUID': [b'9 5 90']}

        class StubClient:
            _imap = StubIMAP()

        copyuid = helpers.copyuid_from_response(StubClient(), None)
        self.assertEqual(copyuid, (9, {5: 90}))

if __name__ == '__main__':
    unittest.main()
