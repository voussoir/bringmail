import datetime
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
from bringmail import exceptions

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'simple.eml')


class FakeIMAPClient:
    def __init__(self, raws, uidvalidity=100, flags=None):
        self.raws = dict(raws)
        self.uidvalidity = uidvalidity
        self.flags = flags or {uid: set() for uid in raws}
        self.fetch_calls = []
        self.selected = None
        self.internaldate = datetime.datetime(2026, 1, 1, 12, 34, 56, tzinfo=datetime.timezone.utc)
        self.logged_out = False

    def list_folders(self):
        # imapclient returns the delimiter as bytes, the name as str.
        return [((b'\\HasNoChildren',), b'/', 'INBOX')]

    def has_capability(self, capability):
        return False

    def select_folder(self, name, readonly=False):
        self.selected = name
        return {
            b'UIDVALIDITY': self.uidvalidity,
            b'UIDNEXT': max(self.raws, default=0) + 1,
            b'HIGHESTMODSEQ': 1,
        }

    def search(self, criteria):
        return sorted(self.raws)

    def fetch(self, messages, data, modifiers=None):
        if isinstance(messages, str):
            uids = sorted(self.raws)
        else:
            uids = [int(message) for message in messages]
        self.fetch_calls.append((uids, list(data), modifiers))

        result = {}
        for uid in uids:
            if uid not in self.raws:
                continue
            raw = self.raws[uid]
            item = {
                b'FLAGS': tuple(self.flags.get(uid, set())),
                b'INTERNALDATE': self.internaldate,
                b'RFC822.SIZE': len(raw),
            }
            if 'BODY.PEEK[]' in data:
                item[b'BODY[]'] = raw
            elif 'BODY.PEEK[HEADER]' in data:
                header = raw.split(b'\r\n\r\n', 1)[0] + b'\r\n\r\n'
                item[b'BODY[HEADER]'] = header
            result[uid] = item
        return result

    def logout(self):
        self.logged_out = True


class SyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(FIXTURE, 'rb') as handle:
            cls.raw = handle.read()

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = bringmaildb.BringMailDB(data_directory=self.tempdir.name, create=True)
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

    def tearDown(self):
        self.db.close()
        self.tempdir.cleanup()

    def sync(self, client):
        with mock.patch('bringmail.helpers.connect_imap', return_value=client):
            return self.account.refresh()

    def get_folder(self):
        folders = self.db.get_folders(self.account)
        self.assertEqual(len(folders), 1)
        return folders[0]

    def add_folder(self, name, display_name=None, special_use=None, attributes=None):
        with self.db.transaction:
            return self.db.add_folder(
                account=self.account,
                name=name,
                display_name=display_name or name,
                delimiter='/',
                attributes=attributes,
                special_use=special_use,
            )

    def test_initial_sync_and_incremental(self):
        client = FakeIMAPClient({1: self.raw, 2: self.raw})
        error = self.sync(client)
        self.assertIsNone(error)
        self.assertTrue(client.logged_out)

        folder = self.get_folder()
        messages = self.db.get_messages(folder=folder, limit=10)
        self.assertEqual(len(messages), 2)
        self.assertEqual({message.uid for message in messages}, {1, 2})
        self.assertTrue(all(message.body_fetched for message in messages))
        self.assertEqual(folder.uidvalidity, 100)
        self.assertEqual(folder.uidnext, 3)
        self.assertEqual(folder.delimiter, '/')
        self.assertEqual(folder.display_name, 'Inbox')
        self.assertIn('Hello', messages[0].get_body()['html'])

        # A second refresh with one new message and a flag change must not
        # re-download the bodies of UIDs 1 and 2.
        client.raws[3] = self.raw
        client.flags[1] = {b'\\Seen'}
        client.fetch_calls = []
        error = self.sync(client)
        self.assertIsNone(error)

        body_calls = [
            (uids, data) for (uids, data, modifiers) in client.fetch_calls
            if any('BODY.PEEK[]' in item for item in data)
        ]
        self.assertEqual(len(body_calls), 1)
        self.assertEqual(set(body_calls[0][0]), {3})

        messages = self.db.get_messages(folder=folder, limit=10)
        self.assertEqual(len(messages), 3)
        by_uid = {message.uid: message for message in messages}
        self.assertTrue(by_uid[1].seen)
        self.assertFalse(by_uid[2].seen)
        self.assertFalse(by_uid[3].seen)

    def test_header_first_import(self):
        client = FakeIMAPClient({1: self.raw, 2: self.raw})
        with mock.patch.object(constants, 'HEADER_FIRST_THRESHOLD', 1):
            self.assertIsNone(self.sync(client))

        header_calls = [
            data for (uids, data, modifiers) in client.fetch_calls
            if 'BODY.PEEK[HEADER]' in data
        ]
        body_calls = [
            data for (uids, data, modifiers) in client.fetch_calls
            if 'BODY.PEEK[]' in data
        ]
        self.assertTrue(header_calls)
        self.assertTrue(body_calls)

        folder = self.get_folder()
        messages = self.db.get_messages(folder=folder, limit=10)
        self.assertEqual(len(messages), 2)
        self.assertTrue(all(message.body_fetched for message in messages))
        self.assertTrue(all(message.raw is not None for message in messages))

    def test_uidvalidity_reset(self):
        client = FakeIMAPClient({1: self.raw, 2: self.raw})
        self.assertIsNone(self.sync(client))
        folder = self.get_folder()

        client.uidvalidity = 200
        client.fetch_calls = []
        self.assertIsNone(self.sync(client))

        body_calls = [
            (uids, data) for (uids, data, modifiers) in client.fetch_calls
            if any('BODY.PEEK[]' in item for item in data)
        ]
        self.assertEqual(len(body_calls), 1)
        self.assertEqual(set(body_calls[0][0]), {1, 2})
        folder = self.get_folder()
        self.assertEqual(folder.uidvalidity, 200)
        self.assertEqual(len(self.db.get_messages(folder=folder, limit=10)), 2)

    def test_sync_folder_only_targets_requested_folder(self):
        inbox = self.add_folder('INBOX', 'Inbox', constants.SPECIAL_USE_INBOX)
        sent = self.add_folder('Sent', 'Sent', constants.SPECIAL_USE_SENT)

        meta = {
            'subject': 'sent-only',
            'from': [],
            'to': [],
            'cc': [],
            'date_unix': 1,
            'message_id': '<sent@example.com>',
            'has_attachments': False,
            'snippet': '',
        }
        with self.db.transaction:
            self.db.add_message(
                account=self.account,
                folder=sent,
                uid=99,
                uidvalidity=1,
                raw=None,
                meta=meta,
                size=0,
            )
            self.db.execute(
                'UPDATE accounts SET last_refresh_attempt = ? WHERE id == ?',
                [1234, self.account.id],
            )
        self.account.last_refresh_attempt = 1234

        client = FakeIMAPClient({1: self.raw})
        with mock.patch('bringmail.helpers.connect_imap', return_value=client):
            error = self.db.sync_folder(self.account, inbox)

        self.assertIsNone(error)
        self.assertTrue(client.logged_out)
        self.assertEqual(client.selected, 'INBOX')
        self.assertEqual(
            {message.uid for message in self.db.get_messages(folder=inbox, limit=10)},
            {1},
        )
        self.assertEqual(
            {message.uid for message in self.db.get_messages(folder=sent, limit=10)},
            {99},
        )
        self.assertEqual(self.account.last_refresh_attempt, 1234)

        row = self.db.select_one(
            'SELECT last_refresh, last_refresh_attempt FROM accounts WHERE id == ?',
            [self.account.id],
        )
        self.assertEqual(row['last_refresh'], 0)
        self.assertEqual(row['last_refresh_attempt'], 1234)

    def test_sync_folder_records_error_on_folder_only(self):
        inbox = self.add_folder('INBOX', 'Inbox', constants.SPECIAL_USE_INBOX)
        client = FakeIMAPClient({1: self.raw})
        client.search = mock.Mock(side_effect=RuntimeError('search exploded'))

        with mock.patch('bringmail.helpers.connect_imap', return_value=client):
            with mock.patch.object(bringmaildb.log, 'warning') as warning:
                error = self.db.sync_folder(self.account, inbox)

        self.assertTrue(warning.called)
        self.assertIsNotNone(error)
        self.assertIn('search exploded', error)
        self.assertIn('search exploded', inbox.last_sync_error)

        row = self.db.select_one(
            'SELECT last_sync_error FROM folders WHERE id == ?',
            [inbox.id],
        )
        self.assertIn('search exploded', row['last_sync_error'])
        self.assertEqual(self.account.last_refresh_attempt, 0)
        self.assertEqual(self.account.last_refresh_error, None)

    def test_sync_folder_rejects_folder_from_another_account(self):
        with self.db.transaction:
            other = self.db.add_account(
                host='imap.example.com',
                port=993,
                ssl=True,
                starttls=False,
                username='other@example.com',
                password='hunter2',
            )
            other_folder = self.db.add_folder(
                account=other,
                name='INBOX',
                display_name='Inbox',
                delimiter='/',
                special_use=constants.SPECIAL_USE_INBOX,
            )

        with self.assertRaises(exceptions.InvalidFolder):
            self.db.sync_folder(self.account, other_folder)

    def test_sync_folder_rejects_nonselectable_folder(self):
        folder = self.add_folder('Parent', 'Parent', attributes=['\\Noselect'])

        with self.assertRaises(exceptions.FolderNotSelectable):
            self.db.sync_folder(self.account, folder)

    def test_sync_flags_do_not_overwrite_pending_flag(self):
        client = FakeIMAPClient({1: self.raw})
        self.assertIsNone(self.sync(client))

        folder = self.get_folder()
        message = self.db.get_messages(folder=folder, limit=10)[0]
        self.assertTrue(message.queue_seen(True))
        self.assertTrue(message.seen)

        # The server still reports the message as unseen. The pending change
        # must survive the refresh so the flush can decide its fate.
        client.flags[1] = set()
        self.assertIsNone(self.sync(client))

        row = self.db.select_one(
            'SELECT seen FROM messages WHERE id == ?',
            [message.id],
        )
        self.assertEqual(row['seen'], 1)

    def test_sync_keeps_message_with_pending_delete(self):
        client = FakeIMAPClient({1: self.raw})
        self.assertIsNone(self.sync(client))

        folder = self.get_folder()
        message = self.db.get_messages(folder=folder, limit=10)[0]
        self.add_folder('Trash', 'Trash', constants.SPECIAL_USE_TRASH)
        self.assertTrue(message.queue_delete())
        self.assertTrue(message.local_deleted)

        # The server no longer reports the UID. Sync must not hard-delete the
        # row while a queued operation still refers to it.
        client.raws = {}
        with mock.patch('bringmail.helpers.connect_imap', return_value=client):
            self.assertIsNone(self.db.sync_folder(self.account, folder))

        self.assertEqual(self.db.get_folder_uids(folder), {1})

    def test_offline_account_sync_never_opens_a_network_connection(self):
        with self.db.transaction:
            offline = self.db.add_account(label='Offline', offline=True)

        with mock.patch(
                'bringmail.helpers.imapclient.IMAPClient',
                side_effect=AssertionError('offline account opened a socket')):
            self.assertIsNone(offline.refresh())

        self.assertEqual(
            {folder.name for folder in offline.get_folders()},
            {'INBOX', 'Trash'},
        )
        self.assertGreater(offline.last_refresh, 0)
        self.assertIsNone(offline.last_refresh_error)

        with mock.patch(
                'bringmail.helpers.imapclient.IMAPClient',
                side_effect=AssertionError('offline account opened a socket')):
            self.assertEqual(offline.test_connection(), 2)

    def test_offline_account_folder_sync_is_local(self):
        with self.db.transaction:
            offline = self.db.add_account(label='Offline', offline=True)
        inbox = offline.get_folders()[0]

        with mock.patch(
                'bringmail.helpers.imapclient.IMAPClient',
                side_effect=AssertionError('offline account opened a socket')):
            self.assertIsNone(self.db.sync_folder(offline, inbox))

        inbox = self.db.get_folder(inbox.id)
        self.assertGreater(inbox.last_sync, 0)
        self.assertIsNone(inbox.last_sync_error)


if __name__ == '__main__':
    unittest.main()
