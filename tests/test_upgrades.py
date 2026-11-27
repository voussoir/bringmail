import os
import sqlite3
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from bringmail import bringmaildb
from bringmail import constants
from bringmail import exceptions
from bringmail import upgrades

class UpgradeTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

    def make_database(self):
        db = bringmaildb.BringMailDB(
            data_directory=self.tempdir.name,
            create=True,
        )
        try:
            with db.transaction:
                account = db.add_account(
                    label='Test',
                    host='imap.example.com',
                    port=993,
                    ssl=True,
                    starttls=False,
                    username='user@example.com',
                    password='hunter2',
                )
                folder = db.add_folder(
                    account=account,
                    name='INBOX',
                    display_name='Inbox',
                    delimiter='/',
                    special_use=constants.SPECIAL_USE_INBOX,
                )
                db.add_message(
                    account=account,
                    folder=folder,
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
                )
        finally:
            db.close()

    def open_database(self):
        return bringmaildb.BringMailDB(
            data_directory=self.tempdir.name,
            create=False,
            skip_version_check=True,
        )

    def test_database_version_matches_constants(self):
        # upgrades keeps its own copy so it does not import the head schema.
        # Both values have to move together.
        self.assertEqual(upgrades.DATABASE_VERSION, constants.DATABASE_VERSION)

    def test_upgrade_1_to_2_creates_pending_queue(self):
        self.make_database()

        # Reshape the current database back to the version 1 schema so the
        # real migration can be exercised.
        db = self.open_database()
        try:
            with db.transaction:
                db.execute_write('DROP TABLE IF EXISTS pending_operations')
                db.execute_write('ALTER TABLE messages DROP COLUMN local_deleted')
                db.pragma_write('user_version', 1)
        finally:
            db.close()

        report = upgrades.upgrade_all(data_directory=self.tempdir.name)
        self.assertEqual(report.pending, (2, 3))
        self.assertEqual(report.applied, (2, 3))
        self.assertIsNotNone(report.backup)

        db = self.open_database()
        try:
            self.assertEqual(db.pragma_read_one('user_version'), 3)
            tables = [
                row['name']
                for row in db.select("SELECT name FROM sqlite_master WHERE type == 'table'")
            ]
            self.assertIn('pending_operations', tables)
            columns = [
                row['name']
                for row in db.pragma_read_all('table_xinfo("messages")')
            ]
            self.assertIn('local_deleted', columns)
            indexes = [
                row['name']
                for row in db.select("SELECT name FROM sqlite_master WHERE type == 'index'")
            ]
            self.assertIn('index_pending_ops_account_state', indexes)
            self.assertIn('index_pending_ops_message', indexes)
            self.assertEqual(db.select_one_value('SELECT COUNT(id) FROM messages'), 1)
            self.assertEqual(db.select_one_value('SELECT COUNT(id) FROM pending_operations'), 0)
        finally:
            db.close()

    def test_upgrade_2_to_3_adds_offline_account_support(self):
        self.make_database()

        db = self.open_database()
        try:
            account_id = db.select_one_value('SELECT id FROM accounts')
            db.pragma_write('foreign_keys', 'off')
            with db.transaction:
                db.execute_write('DROP TABLE accounts')
                db.execute_write('''
                CREATE TABLE accounts(
                    id INTEGER PRIMARY KEY,
                    label TEXT,
                    host TEXT NOT NULL,
                    port INT NOT NULL,
                    ssl BOOLEAN NOT NULL,
                    starttls BOOLEAN NOT NULL,
                    username TEXT NOT NULL,
                    password TEXT NOT NULL,
                    autorefresh_interval INT NOT NULL,
                    ui_order_rank REAL NOT NULL,
                    created INT NOT NULL,
                    last_refresh INT NOT NULL,
                    last_refresh_attempt INT NOT NULL,
                    last_refresh_error TEXT
                )
                ''')
                db.execute_write(
                    '''
                    INSERT INTO accounts (
                        id, label, host, port, ssl, starttls, username,
                        password, autorefresh_interval, ui_order_rank, created,
                        last_refresh, last_refresh_attempt, last_refresh_error
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    [
                        account_id,
                        'Test',
                        'imap.example.com',
                        993,
                        1,
                        0,
                        'user@example.com',
                        'hunter2',
                        900,
                        1,
                        1,
                        0,
                        0,
                        None,
                    ],
                )
                db.pragma_write('user_version', 2)
        finally:
            db.close()

        report = upgrades.upgrade_all(data_directory=self.tempdir.name)
        self.assertEqual(report.pending, (3,))
        self.assertEqual(report.applied, (3,))

        db = self.open_database()
        try:
            self.assertEqual(db.pragma_read_one('user_version'), 3)
            columns = {
                row['name']: row
                for row in db.pragma_read_all('table_xinfo("accounts")')
            }
            self.assertIn('offline', columns)
            self.assertEqual(columns['offline']['dflt_value'], '0')
            self.assertEqual(columns['host']['notnull'], 0)
            self.assertEqual(columns['port']['notnull'], 0)
            self.assertEqual(columns['username']['notnull'], 0)
            self.assertEqual(columns['password']['notnull'], 0)

            triggers = [
                row['name']
                for row in db.select("SELECT name FROM sqlite_master WHERE type == 'trigger'")
            ]
            self.assertIn('accounts_offline_immutable', triggers)

            indexes = [
                row['name']
                for row in db.select("SELECT name FROM sqlite_master WHERE type == 'index'")
            ]
            self.assertIn('index_accounts_endpoint', indexes)

            account = db.get_account(account_id)
            self.assertFalse(account.offline)
            self.assertEqual(account.host, 'imap.example.com')
            self.assertEqual(account.username, 'user@example.com')
            self.assertEqual(db.select_one_value('SELECT COUNT(id) FROM folders'), 1)
            self.assertEqual(db.select_one_value('SELECT COUNT(id) FROM messages'), 1)

            with self.assertRaises(sqlite3.IntegrityError):
                with db.transaction:
                    db.execute_write(
                        'UPDATE accounts SET offline = 1 WHERE id == ?',
                        [account_id],
                    )
        finally:
            db.close()

    def test_upgrade_all_is_a_noop_for_current_database(self):
        self.make_database()
        report = upgrades.upgrade_all(data_directory=self.tempdir.name)

        self.assertFalse(report.upgraded)
        self.assertEqual(report.start_version, constants.DATABASE_VERSION)
        self.assertEqual(report.target_version, constants.DATABASE_VERSION)
        self.assertEqual(report.pending, ())
        self.assertEqual(report.applied, ())
        self.assertIsNone(report.backup)

    def test_upgrade_chain_applies_and_backs_up(self):
        self.make_database()
        start = constants.DATABASE_VERSION
        target = start + 1

        def add_column(migrator):
            migrator.add_column('accounts', 'test_upgrade_column', 'TEXT')

        report = upgrades.upgrade_all(
            data_directory=self.tempdir.name,
            target_version=target,
            upgrades={(start, target): add_column},
        )

        self.assertTrue(report.upgraded)
        self.assertEqual(report.start_version, start)
        self.assertEqual(report.target_version, target)
        self.assertEqual(report.pending, (target,))
        self.assertEqual(report.applied, (target,))
        self.assertIsNotNone(report.backup)
        self.assertTrue(os.path.isfile(report.backup))

        db = self.open_database()
        try:
            self.assertEqual(db.pragma_read_one('user_version'), target)
            rows = db.pragma_read_all('table_xinfo("accounts")')
            names = [row['name'] for row in rows]
            self.assertIn('test_upgrade_column', names)
            self.assertEqual(db.select_one_value('SELECT COUNT(id) FROM accounts'), 1)
            self.assertEqual(db.select_one_value('SELECT COUNT(id) FROM folders'), 1)
            self.assertEqual(db.select_one_value('SELECT COUNT(id) FROM messages'), 1)
        finally:
            db.close()

    def test_multiple_versions_are_applied_in_order(self):
        self.make_database()
        start = constants.DATABASE_VERSION
        first = start + 1
        second = start + 2

        def upgrade_a(migrator):
            migrator.add_column('accounts', 'upgrade_a', 'TEXT')

        def upgrade_b(migrator):
            migrator.add_column('accounts', 'upgrade_b', 'TEXT')

        report = upgrades.upgrade_all(
            data_directory=self.tempdir.name,
            target_version=second,
            upgrades={
                (start, first): upgrade_a,
                (first, second): upgrade_b,
            },
        )

        self.assertEqual(report.applied, (first, second))

        db = self.open_database()
        try:
            self.assertEqual(db.pragma_read_one('user_version'), second)
            rows = db.pragma_read_all('table_xinfo("accounts")')
            names = [row['name'] for row in rows]
            self.assertIn('upgrade_a', names)
            self.assertIn('upgrade_b', names)
        finally:
            db.close()

    def test_dry_run_reports_without_changing_database(self):
        self.make_database()
        start = constants.DATABASE_VERSION
        target = start + 1

        def add_column(migrator):
            migrator.add_column('accounts', 'test_upgrade_column', 'TEXT')

        report = upgrades.upgrade_all(
            data_directory=self.tempdir.name,
            target_version=target,
            upgrades={(start, target): add_column},
            dry_run=True,
        )

        self.assertFalse(report.upgraded)
        self.assertTrue(report.dry_run)
        self.assertEqual(report.pending, (target,))
        self.assertEqual(report.applied, ())
        self.assertIsNone(report.backup)

        db = self.open_database()
        try:
            self.assertEqual(db.pragma_read_one('user_version'), start)
            rows = db.pragma_read_all('table_xinfo("accounts")')
            names = [row['name'] for row in rows]
            self.assertNotIn('test_upgrade_column', names)
        finally:
            db.close()

    def test_missing_upgrade_function_is_rejected(self):
        self.make_database()
        target = constants.DATABASE_VERSION + 1

        with self.assertRaises(exceptions.DatabaseUpgradeError):
            upgrades.upgrade_all(
                data_directory=self.tempdir.name,
                target_version=target,
                upgrades={},
            )

    def test_newer_database_is_rejected(self):
        db = bringmaildb.BringMailDB(
            data_directory=self.tempdir.name,
            create=True,
        )
        try:
            with db.transaction:
                db.pragma_write('user_version', constants.DATABASE_VERSION + 10)
        finally:
            db.close()

        db = self.open_database()
        try:
            with self.assertRaises(exceptions.DatabaseTooNew):
                db._check_version()
        finally:
            db.close()

        with self.assertRaises(exceptions.DatabaseTooNew):
            upgrades.upgrade_all(data_directory=self.tempdir.name)

    def test_failed_upgrade_rolls_back(self):
        self.make_database()
        start = constants.DATABASE_VERSION
        target = start + 1

        def break_foreign_keys(migrator):
            migrator.execute('DELETE FROM folders')

        with self.assertRaises(exceptions.DatabaseUpgradeError):
            upgrades.upgrade_all(
                data_directory=self.tempdir.name,
                target_version=target,
                upgrades={(start, target): break_foreign_keys},
            )

        db = self.open_database()
        try:
            self.assertEqual(db.pragma_read_one('user_version'), start)
            self.assertEqual(db.select_one_value('SELECT COUNT(id) FROM folders'), 1)
            self.assertEqual(db.select_one_value('SELECT COUNT(id) FROM messages'), 1)
        finally:
            db.close()

    def test_rebuild_table_preserves_foreign_keys(self):
        db = bringmaildb.BringMailDB(
            data_directory=self.tempdir.name,
            create=True,
        )
        try:
            # A table rebuild must be done with foreign key enforcement off,
            # which is what upgrade_all does around the whole chain.
            db.pragma_write('foreign_keys', 'OFF')
            with db.transaction:
                db.execute_write('''
                CREATE TABLE parent (
                    id INTEGER PRIMARY KEY,
                    name TEXT
                )
                ''')
                db.execute_write('''
                CREATE TABLE child (
                    id INTEGER PRIMARY KEY,
                    parent_id INTEGER REFERENCES parent(id)
                )
                ''')
                db.execute_write("INSERT INTO parent VALUES (1, 'one')")
                db.execute_write('INSERT INTO child VALUES (10, 1)')

            with db.transaction:
                migrator = upgrades.Migrator(db)
                migrator.rebuild_table(
                    'parent',
                    create_sql='''
                    CREATE TABLE {table} (
                        id INTEGER PRIMARY KEY,
                        name TEXT,
                        note TEXT
                    )
                    ''',
                )

            db.pragma_write('foreign_keys', 'ON')
            violations = db.pragma_read_all('foreign_key_check')
            self.assertEqual(violations, [])
            row = db.select_one_value('SELECT parent_id FROM child')
            self.assertEqual(row, 1)
        finally:
            db.close()

    def test_rebuild_table_preserves_data_and_generated_columns(self):
        db = bringmaildb.BringMailDB(
            data_directory=self.tempdir.name,
            create=True,
        )
        try:
            with db.transaction:
                db.execute_write('''
                CREATE TABLE sample (
                    id INTEGER PRIMARY KEY,
                    value TEXT,
                    upper_value TEXT GENERATED ALWAYS AS (upper(value)) VIRTUAL
                )
                ''')
                db.execute_write("INSERT INTO sample (value) VALUES ('abc')")

            with db.transaction:
                migrator = upgrades.Migrator(db)
                migrator.rebuild_table(
                    'sample',
                    create_sql='''
                    CREATE TABLE {table} (
                        id INTEGER PRIMARY KEY,
                        value TEXT,
                        upper_value TEXT GENERATED ALWAYS AS (upper(value)) VIRTUAL,
                        extra TEXT DEFAULT 'x'
                    )
                    ''',
                )

            row = db.select_one(
                'SELECT id, value, upper_value, extra FROM sample'
            )
            self.assertEqual(tuple(row), (1, 'abc', 'ABC', 'x'))

            tables = db.get_tables()
            self.assertIn('sample', tables)
            self.assertNotIn('sample__new', tables)
        finally:
            db.close()

if __name__ == '__main__':
    unittest.main()
