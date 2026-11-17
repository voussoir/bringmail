'''
Database schema upgrades for BringMail.

BringMail stores its schema version in SQLite's PRAGMA user_version. This
module applies the version-to-version migrations that bring an existing
database up to constants.DATABASE_VERSION.

Adding a migration
------------------
1. Change the schema in constants.DB_INIT.
2. Increment constants.DATABASE_VERSION.
3. Add a function here, for example:

    @upgrade_function(1, 2)
    def upgrade_1_to_2(migrator):
        migrator.execute('CREATE INDEX ...')

Use migrator.add_column() for simple column additions. Use
migrator.rebuild_table() when SQLite cannot perform the change in place, such
as adding a STORED generated column, changing a column type, or changing a
collation. Use the Migrator's SQL helpers rather than ORM object methods,
because the database's column index is loaded before migrations run. Add a
test in tests/test_upgrades.py for every migration.

The upgrade functions run with foreign key enforcement disabled and inside one
transaction per version. Indices from constants.DB_INIT are recreated and
PRAGMA foreign_key_check is run before each version is committed. A migration
function must not commit or start its own transaction.
'''
import dataclasses
import os
import sqlite3
import time
import typing

from voussoirkit import vlogging

from . import bringmaildb
from . import constants
from . import exceptions

log = vlogging.get_logger(__name__)

UpgradeFunction = typing.Callable[['Migrator'], None]

UPGRADES: dict[tuple[int, int], UpgradeFunction] = {}


def upgrade_function(from_version, to_version):
    '''
    Register a version-to-version migration function.
    '''
    def register(function):
        key = (int(from_version), int(to_version))
        if key[1] != key[0] + 1:
            raise ValueError('Upgrade functions must advance exactly one version.')
        if key in UPGRADES:
            raise ValueError(f'An upgrade for {key[0]} -> {key[1]} is already registered.')
        UPGRADES[key] = function
        return function
    return register


def _assert_identifier(name):
    if not isinstance(name, str) or not name.isidentifier():
        raise ValueError(f'Invalid SQL identifier: {name!r}')
    return name


@dataclasses.dataclass(frozen=True)
class UpgradeReport:
    data_directory: str
    database_path: str
    start_version: int
    target_version: int
    pending: tuple[int, ...]
    applied: tuple[int, ...]
    backup: str | None
    dry_run: bool

    @property
    def upgraded(self):
        return bool(self.applied)


class Migrator:
    '''
    Helpers for writing one migration.

    A Migrator does not start or commit transactions. upgrade_all wraps each
    version in a transaction and disables foreign key enforcement around the
    whole chain.
    '''
    def __init__(self, db):
        if not isinstance(db, bringmaildb.BringMailDB):
            raise TypeError(db)
        self.db = db

    def execute(self, sql, bindings=None):
        if bindings is None:
            return self.db.execute(sql)
        return self.db.execute(sql, bindings)

    def executescript(self, script):
        '''
        Execute a DDL script inside the migration transaction.
        '''
        self.db.executescript(script)

    def add_column(self, table, column, definition):
        '''
        Add a column with ALTER TABLE ADD COLUMN.

        SQLite can add VIRTUAL generated columns this way, but not STORED
        generated columns. Use rebuild_table for those.
        '''
        _assert_identifier(table)
        _assert_identifier(column)
        upper = definition.upper()
        if 'PRIMARY KEY' in upper or 'UNIQUE' in upper:
            raise ValueError(
                'SQLite cannot add a PRIMARY KEY or UNIQUE column with '
                'ALTER TABLE. Use rebuild_table instead.'
            )
        if 'GENERATED ALWAYS' in upper and 'STORED' in upper:
            raise ValueError(
                'SQLite cannot add a STORED generated column with ALTER TABLE. '
                'Use rebuild_table instead.'
            )
        self.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')

    def create_index(self, name, table, columns, *, unique=False):
        '''
        Create an index from a list of column names.
        '''
        _assert_identifier(name)
        _assert_identifier(table)
        if isinstance(columns, str):
            columns = [columns]
        columns = [_assert_identifier(column) for column in columns]
        columns_sql = ', '.join(f'"{column}"' for column in columns)
        unique_sql = 'UNIQUE ' if unique else ''
        self.execute(
            f'CREATE {unique_sql}INDEX IF NOT EXISTS "{name}" '
            f'ON "{table}" ({columns_sql})'
        )

    def drop_index(self, name):
        _assert_identifier(name)
        self.execute(f'DROP INDEX IF EXISTS "{name}"')

    def _table_columns(self, table):
        '''
        Return (name, hidden) pairs from PRAGMA table_xinfo.

        hidden is 0 for ordinary columns, 2 for VIRTUAL generated columns and
        3 for STORED generated columns. Generated columns must be omitted from
        INSERT statements.
        '''
        rows = self.db.sql_write.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
        return [(row['name'], row['hidden']) for row in rows]

    def _row_count(self, table):
        row = self.db.sql_write.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
        return int(row[0])

    def rebuild_table(
            self,
            table,
            *,
            create_sql,
            transfer_sql=None,
            expect_same_rowcount=True,
    ):
        '''
        Rebuild a table using SQLite's recommended create-copy-drop-rename
        procedure.

        create_sql:
            The complete CREATE TABLE statement for the new table, using
            {table} wherever the table name belongs. The Migrator substitutes a
            temporary name. Foreign key clauses, including self-references,
            should use the table's final name.

        transfer_sql:
            Optional INSERT ... SELECT statement. It may use {table} for the
            temporary table, {old_table} for the existing table, and {columns}
            for the comma-separated intersection of ordinary columns. When
            omitted, the Migrator copies the intersection of ordinary columns
            automatically. Provide this explicitly when a column changes
            between generated and ordinary, or when columns are renamed.

        expect_same_rowcount:
            When True, raise if the transfer does not preserve the row count.
            Set False for migrations that intentionally delete rows.

        Indexes on the rebuilt table are dropped with the old table. upgrade_all
        recreates the indices from constants.DB_INIT after the migration.
        Recreate any custom indices that are not declared in DB_INIT in the
        migration itself. This helper does not recreate triggers or views;
        BringMail currently has none.
        '''
        _assert_identifier(table)
        if '{table}' not in create_sql:
            raise ValueError('create_sql must contain the {table} placeholder.')

        temp_table = f'{table}__new'
        self.execute(f'DROP TABLE IF EXISTS "{temp_table}"')
        self.execute(create_sql.replace('{table}', temp_table))

        old_columns = [
            name for (name, hidden) in self._table_columns(table) if hidden == 0
        ]
        new_columns = [
            name for (name, hidden) in self._table_columns(temp_table) if hidden == 0
        ]
        new_column_set = set(new_columns)
        common_columns = [name for name in old_columns if name in new_column_set]

        if transfer_sql is None:
            if not common_columns:
                if self._row_count(table) > 0:
                    raise ValueError(
                        f'Rebuilding "{table}" has no columns in common with '
                        'the new table.'
                    )
                transfer_sql = None
            else:
                columns_sql = ', '.join(f'"{name}"' for name in common_columns)
                transfer_sql = (
                    f'INSERT INTO "{temp_table}" ({columns_sql}) '
                    f'SELECT {columns_sql} FROM "{table}"'
                )
        else:
            transfer_sql = transfer_sql.replace('{table}', temp_table)
            transfer_sql = transfer_sql.replace('{old_table}', table)
            transfer_sql = transfer_sql.replace(
                '{columns}',
                ', '.join(f'"{name}"' for name in common_columns),
            )

        if transfer_sql is not None:
            self.execute(transfer_sql)

        old_count = self._row_count(table)
        new_count = self._row_count(temp_table)
        if expect_same_rowcount and old_count != new_count:
            raise exceptions.DatabaseUpgradeError(
                f'Rebuilding "{table}" preserved {new_count} of {old_count} rows.'
            )

        self.execute(f'DROP TABLE "{table}"')
        self.execute(f'ALTER TABLE "{temp_table}" RENAME TO "{table}"')

    def recreate_indices(self):
        '''
        Recreate every index declared in constants.DB_INIT.

        The statements use IF NOT EXISTS, so this is safe to run after every
        migration.
        '''
        for statement in constants.DB_INIT.split(';'):
            statement = statement.strip()
            if statement.upper().startswith(('CREATE INDEX', 'CREATE UNIQUE INDEX')):
                self.execute(statement)


def _set_foreign_keys(db, enabled):
    '''
    Set PRAGMA foreign_keys and verify that it took effect.

    SQLite silently ignores changes to this pragma while a transaction is open,
    which would make a table rebuild cascade-delete child rows.
    '''
    value = 'ON' if enabled else 'OFF'
    db.sql_write.execute(f'PRAGMA foreign_keys = {value}')
    row = db.sql_write.execute('PRAGMA foreign_keys').fetchone()
    if bool(row[0]) != bool(enabled):
        action = 'enable' if enabled else 'disable'
        raise exceptions.DatabaseUpgradeError(
            f'Could not {action} foreign key enforcement. '
            'Close any other BringMail processes and try again.'
        )


def _backup_database(db: bringmaildb.BringMailDB, version: int):
    source = db.database_filepath
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime())
    base_name = f'{source.basename}.bak-v{version}-{stamp}-{os.getpid()}'
    backup = source.parent.with_child(base_name)
    suffix = 1
    while backup.exists:
        backup = source.parent.with_child(f'{base_name}-{suffix}')
        suffix += 1

    log.info('Backing up database to "%s".', backup.absolute_path)
    destination = sqlite3.connect(backup.absolute_path)
    try:
        db.sql_write.backup(destination)
    finally:
        destination.close()
    return backup.absolute_path


def _open_database(data_directory=None):
    if data_directory is None:
        return bringmaildb.BringMailDB.closest_bringmaildb(skip_version_check=True)
    return bringmaildb.BringMailDB(
        data_directory=data_directory,
        create=False,
        skip_version_check=True,
    )


@upgrade_function(1, 2)
def upgrade_1_to_2(migrator):
    '''
    Add the offline pending-operations queue and the messages.local_deleted
    marker. The queue records the operation and the pre-state the user was
    looking at so a later flush can detect server divergence.
    '''
    migrator.add_column(
        'messages',
        'local_deleted',
        'BOOLEAN NOT NULL DEFAULT 0',
    )
    migrator.execute('''
    CREATE TABLE IF NOT EXISTS pending_operations(
        id INTEGER PRIMARY KEY,
        account_id INT NOT NULL,
        folder_id INT NOT NULL,
        message_id INT,
        message_uid INT NOT NULL,
        message_uidvalidity INT,
        operation TEXT NOT NULL,
        enabled INT,
        base_folder_id INT,
        target_folder_id INT,
        target_keywords TEXT,
        base_modseq INT,
        observed TEXT,
        state TEXT NOT NULL,
        attempts INT NOT NULL,
        next_attempt INT NOT NULL,
        last_error TEXT,
        created INT NOT NULL,
        updated INT NOT NULL,
        FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE,
        FOREIGN KEY(folder_id) REFERENCES folders(id) ON DELETE CASCADE
    )
    ''')


def upgrade_all(
        data_directory=None,
        *,
        target_version=None,
        dry_run=False,
        backup=True,
        upgrades=None,
) -> UpgradeReport:
    '''
    Upgrade a database from its current version to target_version, which
    defaults to constants.DATABASE_VERSION.

    data_directory:
        The directory containing bringmail.db. When omitted, the closest
        _bringmail directory is found from the current working directory.

    target_version:
        The destination version. Defaults to constants.DATABASE_VERSION;
        callers normally leave it unset.

    dry_run:
        Report pending migrations without changing anything.

    backup:
        Copy the database before applying migrations. The copy uses SQLite's
        backup API, so it is consistent even when the database uses WAL.

    upgrades:
        Override the migration registry. This is intended for tests; normal
        callers should leave it as None.
    '''
    if upgrades is None:
        upgrades = UPGRADES
    target_version = (
        constants.DATABASE_VERSION
        if target_version is None
        else int(target_version)
    )

    db = _open_database(data_directory)
    try:
        start_version = int(db.pragma_read('user_version') or 0)
        if start_version > target_version:
            raise exceptions.DatabaseTooNew(
                existing=start_version,
                new=target_version,
            )

        pending = tuple(range(start_version + 1, target_version + 1))
        steps = []
        for version in pending:
            function = upgrades.get((version - 1, version))
            if function is None:
                raise exceptions.DatabaseUpgradeError(
                    f'No upgrade function is registered for version {version}.'
                )
            steps.append((version, function))

        report = UpgradeReport(
            data_directory=db.data_directory.absolute_path,
            database_path=db.database_filepath.absolute_path,
            start_version=start_version,
            target_version=target_version,
            pending=pending,
            applied=tuple(),
            backup=None,
            dry_run=dry_run,
        )

        if dry_run or not pending:
            return report

        backup_path = None
        if backup:
            backup_path = _backup_database(db, start_version)

        applied = []
        _set_foreign_keys(db, False)
        try:
            for (version, function) in steps:
                try:
                    with db.transaction:
                        current_version = int(
                            db.sql_write.execute('PRAGMA user_version').fetchone()[0]
                        )
                        if current_version != version - 1:
                            raise exceptions.DatabaseUpgradeError(
                                f'Expected database version {version - 1} before '
                                f'upgrading to {version}, found {current_version}.'
                            )

                        migrator = Migrator(db)
                        function(migrator)
                        migrator.recreate_indices()

                        violations = db.sql_write.execute(
                            'PRAGMA foreign_key_check'
                        ).fetchall()
                        if violations:
                            raise exceptions.DatabaseUpgradeError(
                                f'Upgrade to version {version} left '
                                f'{len(violations)} foreign key violation(s).'
                            )

                        db.pragma_write('user_version', version)
                except Exception:
                    log.warning('Upgrade to version %s failed.', version, exc_info=True)
                    raise

                applied.append(version)
                log.info('Upgraded database to version %s.', version)
        finally:
            try:
                _set_foreign_keys(db, True)
            except Exception:
                log.warning('Could not re-enable foreign key enforcement.', exc_info=True)

        final_version = int(
            db.sql_write.execute('PRAGMA user_version').fetchone()[0]
        )
        if final_version != target_version:
            raise exceptions.DatabaseUpgradeError(
                f'Database version is {final_version} after upgrading, '
                f'expected {target_version}.'
            )

        return dataclasses.replace(
            report,
            applied=tuple(applied),
            backup=backup_path,
        )
    finally:
        try:
            db.close()
        except Exception:
            log.warning('Could not close the database cleanly.', exc_info=True)
