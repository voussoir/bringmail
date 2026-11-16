import datetime
import os
import sqlite3
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import bringmail
from bringmail import bringmaildb
from bringmail import constants
from bringmail import exceptions
from bringmail import helpers


FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'simple.eml')


class DatabaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(FIXTURE, 'rb') as handle:
            cls.raw = handle.read()

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = bringmaildb.BringMailDB(
            data_directory=self.tempdir.name,
            create=True,
        )

    def tearDown(self):
        self.db.close()
        self.tempdir.cleanup()

    def make_account(self, **kwargs):
        values = {
            'label': 'Test account',
            'host': 'imap.example.com',
            'port': 993,
            'ssl': True,
            'starttls': False,
            'username': 'user@example.com',
            'password': 'hunter2',
            'autorefresh_interval': 900,
        }
        values.update(kwargs)
        with self.db.transaction:
            return self.db.add_account(**values)

    def make_folder(self, account, **kwargs):
        values = {
            'account': account,
            'name': 'INBOX',
            'display_name': 'Inbox',
            'delimiter': '/',
            'special_use': constants.SPECIAL_USE_INBOX,
        }
        values.update(kwargs)
        with self.db.transaction:
            return self.db.add_folder(**values)

    def make_message(self, account, folder, meta, **kwargs):
        values = {
            'account': account,
            'folder': folder,
            'uid': 1,
            'uidvalidity': 1,
            'raw': None,
            'meta': meta,
            'size': 0,
            'flags': {},
            'keywords': None,
            'modseq': None,
        }
        values.update(kwargs)
        with self.db.transaction:
            return self.db.add_message(**values)

    def test_sqlite_version(self):
        self.assertGreaterEqual(sqlite3.sqlite_version_info, constants.MINIMUM_SQLITE)
        self.assertEqual(self.db.pragma_read('user_version'), constants.DATABASE_VERSION)

    def test_generated_columns_and_body(self):
        account = self.make_account()
        folder = self.make_folder(account)
        (message, meta) = helpers.parse_full_message(self.raw, size=len(self.raw))

        with self.db.transaction:
            stored = self.db.add_message(
                account=account,
                folder=folder,
                uid=42,
                uidvalidity=1,
                raw=self.raw,
                meta=meta,
                size=len(self.raw),
                flags={'seen': False, 'flagged': True},
            )

        row = self.db.select_one(
            '''
            SELECT id, subject, from_name, from_addr, date_unix, message_id, has_attachments
            FROM messages WHERE folder_id == ? AND uid == ?
            ''',
            [folder.id, 42],
        )
        self.assertIsNotNone(row)
        self.assertEqual(row['subject'], 'Hello ✓')
        self.assertEqual(row['from_name'], 'Alice Example')
        self.assertEqual(row['from_addr'], 'alice@example.com')
        self.assertEqual(
            row['date_unix'],
            int(datetime.datetime(2026, 1, 1, 12, 34, 56, tzinfo=datetime.timezone.utc).timestamp()),
        )
        self.assertTrue(row['has_attachments'])

        body = stored.get_body()
        self.assertIn('Hello <b>HTML</b> text.', body['html'])
        self.assertNotIn('<script', body['html'])
        self.assertEqual(len(body['attachments']), 1)

        account_json = account.jsonify(folders=True, unread_count=True)
        self.assertNotIn('password', account_json)
        self.assertEqual(account_json['folders'][0]['unread_count'], 1)
        self.assertEqual(account_json['folders'][0]['message_count'], 1)
        self.assertEqual(account_json['unread_count'], 1)

        messages = self.db.get_messages(folder=folder, limit=10)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].id, stored.id)

    def test_flags_and_unread_counts(self):
        account = self.make_account()
        folder = self.make_folder(account)
        (message, meta) = helpers.parse_full_message(self.raw)
        with self.db.transaction:
            stored = self.db.add_message(
                account=account,
                folder=folder,
                uid=7,
                uidvalidity=1,
                raw=self.raw,
                meta=meta,
                size=len(self.raw),
                flags={'seen': False},
            )

        self.assertEqual(account.get_unread_count(), 1)
        self.assertEqual(folder.get_unread_count(), 1)

        stored._set_local_flag('seen', True)
        self.assertEqual(account.get_unread_count(), 0)
        self.assertEqual(folder.get_unread_count(), 0)

        stored._set_local_flag('seen', False)
        self.assertEqual(account.get_unread_count(), 1)

    def test_pagination(self):
        account = self.make_account()
        folder = self.make_folder(account)
        meta_template = {
            'subject': 'message',
            'from': [{'name': 'Alice', 'addr': 'alice@example.com'}],
            'to': [],
            'cc': [],
            'date_unix': 0,
            'message_id': '<x@example.com>',
            'has_attachments': False,
            'snippet': '',
        }
        stored = []
        for index in range(3):
            meta = dict(meta_template)
            meta['subject'] = f'message {index}'
            meta['date_unix'] = 100 * (index + 1)
            message = self.make_message(
                account,
                folder,
                meta,
                uid=10 + index,
                size=10,
            )
            stored.append(message)

        first_page = self.db.get_messages(folder=folder, limit=2)
        self.assertEqual([message.id for message in first_page], [stored[2].id, stored[1].id])

        second_page = self.db.get_messages(
            folder=folder,
            limit=2,
            before_date=first_page[-1].date_unix,
            before_id=first_page[-1].id,
        )
        self.assertEqual([message.id for message in second_page], [stored[0].id])

    def test_duplicate_account(self):
        self.make_account()
        with self.assertRaises(exceptions.DuplicateAccount):
            with self.db.transaction:
                self.db.add_account(
                    host='imap.example.com',
                    port=993,
                    ssl=True,
                    starttls=False,
                    username='user@example.com',
                    password='different',
                )

    def test_account_delete_cascades(self):
        account = self.make_account()
        folder = self.make_folder(account)
        meta = {
            'subject': 'message',
            'from': [],
            'to': [],
            'cc': [],
            'date_unix': 1,
            'message_id': None,
            'has_attachments': False,
            'snippet': '',
        }
        self.make_message(account, folder, meta, uid=1)

        with self.db.transaction:
            account.delete()

        self.assertEqual(self.db.select_one_value('SELECT COUNT(id) FROM accounts'), 0)
        self.assertEqual(self.db.select_one_value('SELECT COUNT(id) FROM folders'), 0)
        self.assertEqual(self.db.select_one_value('SELECT COUNT(id) FROM messages'), 0)
        with self.assertRaises(exceptions.NoSuchAccount):
            self.db.get_account(account.id)


if __name__ == '__main__':
    unittest.main()
