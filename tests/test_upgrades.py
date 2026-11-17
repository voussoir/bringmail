import os
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

    def test_upgrade_1_to_2_creates_pending_queue(self):
        self.make_database()

        # Reshape the current database back to the version 1 schema so the
        # real migration can be exercised.
        db = self.open_database()
        try:
            with db.transaction:
                db.execute('DROP TABLE IF EXISTS pending_operations')
                db.execute('ALTER TABLE messages DROP COLUMN local_deleted')
                db.pragma_write('user_version', 1)
        finally:
            db.close()

        report = upgrades.upgrade_all(data_directory=self.tempdir.name)
        self.assertEqual(report.pending, (2,))
        self.assertEqual(report.applied, (2,))
        self.assertIsNotNone(report.backup)

        db = self.open_database()
        try:
            self.assertEqual(db.pragma_read('user_version'), 2)
            tables = [
                row['name']
                for row in db.select("SELECT name FROM sqlite_master WHERE type == 'table'")
            ]
            self.assertIn('pending_operations', tables)
            columns = [
                row['name']
                for row in db.sql_write.execute('PRAGMA table_xinfo("messages")').fetchall()
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
            self.assertEqual(db.pragma_read('user_version'), target)
            rows = db.sql_write.execute('PRAGMA table_xinfo("accounts")').fetchall()
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
            self.assertEqual(db.pragma_read('user_version'), second)
            rows = db.sql_write.execute('PRAGMA table_xinfo("accounts")').fetchall()
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
            self.assertEqual(db.pragma_read('user_version'), start)
            rows = db.sql_write.execute('PRAGMA table_xinfo("accounts")').fetchall()
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
            self.assertEqual(db.pragma_read('user_version'), start)
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
            db.sql_write.execute('PRAGMA foreign_keys = OFF')
            with db.transaction:
                db.execute('''
                CREATE TABLE parent (
                    id INTEGER PRIMARY KEY,
                    name TEXT
                )
                ''')
                db.execute('''
                CREATE TABLE child (
                    id INTEGER PRIMARY KEY,
                    parent_id INTEGER REFERENCES parent(id)
                )
                ''')
                db.execute("INSERT INTO parent VALUES (1, 'one')")
                db.execute('INSERT INTO child VALUES (10, 1)')

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

            db.sql_write.execute('PRAGMA foreign_keys = ON')
            violations = db.sql_write.execute('PRAGMA foreign_key_check').fetchall()
            self.assertEqual(violations, [])
            row = db.sql_write.execute('SELECT parent_id FROM child').fetchone()
            self.assertEqual(row[0], 1)
        finally:
            db.close()

    def test_rebuild_table_preserves_data_and_generated_columns(self):
        db = bringmaildb.BringMailDB(
            data_directory=self.tempdir.name,
            create=True,
        )
        try:
            with db.transaction:
                db.execute('''
                CREATE TABLE sample (
                    id INTEGER PRIMARY KEY,
                    value TEXT,
                    upper_value TEXT GENERATED ALWAYS AS (upper(value)) VIRTUAL
                )
                ''')
                db.execute("INSERT INTO sample (value) VALUES ('abc')")

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

            row = db.sql_write.execute(
                'SELECT id, value, upper_value, extra FROM sample'
            ).fetchone()
            self.assertEqual(tuple(row), (1, 'abc', 'ABC', 'x'))

            tables = [
                row['name']
                for row in db.sql_write.execute(
                    "SELECT name FROM sqlite_master WHERE type == 'table'"
                )
            ]
            self.assertIn('sample', tables)
            self.assertNotIn('sample__new', tables)
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
