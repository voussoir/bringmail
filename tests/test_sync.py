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
        return [((b'\\HasNoChildren',), '/', 'INBOX')]

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


if __name__ == '__main__':
    unittest.main()
