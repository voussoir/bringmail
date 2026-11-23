'''
Apply pending schema upgrades to a BringMail database.

Run this from the directory containing _bringmail, or pass the data directory
as an argument. BringMail must not be running while the upgrade is applied.
'''
import argparse
import pathlib
import sqlite3
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from voussoirkit import vlogging

log = vlogging.getLogger(__name__, 'database_upgrader')

import bringmail

def _print_error(exc):
    if hasattr(exc, 'error_message'):
        message = exc.error_message
    else:
        message = str(exc)

    print(message, file=sys.stderr)

    if isinstance(exc, sqlite3.OperationalError):
        if 'locked' in str(exc).lower() or 'busy' in str(exc).lower():
            print('Stop the BringMail server and try again.', file=sys.stderr)

def _print_plan(plan):
    print(f'Database: {plan.database_path}')
    print(f'Version: {plan.start_version} -> {plan.target_version}')
    if plan.pending:
        versions = ', '.join(str(version) for version in plan.pending)
        print(f'Pending migrations: {versions}')
    else:
        print('Nothing to do.')

def database_upgrader_argparse(args):
    try:
        plan = bringmail.upgrades.upgrade_all(
            data_directory=args.data_directory,
            dry_run=True,
        )
    except (bringmail.exceptions.BringException,
            sqlite3.Error,
            OSError) as exc:
        _print_error(exc)
        return 1

    _print_plan(plan)
    if not plan.pending:
        return 0
    if args.dry_run:
        print('Dry run; no changes made.')
        return 0

    print('BringMail must not be running while the upgrade is applied.')
    if not args.yes:
        try:
            answer = input('Apply these upgrades? [y/N] ')
        except EOFError:
            answer = ''
        if answer.strip().lower() not in {'y', 'yes'}:
            print('Aborted.')
            return 1

    try:
        report = bringmail.upgrades.upgrade_all(
            data_directory=plan.data_directory,
            backup=not args.no_backup,
        )
    except (bringmail.exceptions.BringException,
            sqlite3.Error,
            OSError) as exc:
        _print_error(exc)
        return 1

    if not report.applied:
        print('Nothing to do.')
        return 0

    print(
        f'Upgraded database from version {report.start_version} '
        f'to version {report.target_version}.'
    )
    if report.backup:
        print(f'Backup: {report.backup}')
    return 0

@vlogging.main_decorator(log=log)
def main(argv):
    parser = argparse.ArgumentParser(
        description='''
        Apply pending schema upgrades to a BringMail database.
        BringMail must not be running while the upgrade is applied.
        ''',
    )
    parser.add_argument(
        'data_directory',
        nargs='?',
        default=None,
        help='''
        The directory containing bringmail.db. When omitted, the closest
        _bringmail directory is found from the current working directory.
        ''',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show pending migrations without changing the database.',
    )
    parser.add_argument(
        '--yes',
        action='store_true',
        help='Apply the migrations without asking for confirmation.',
    )
    parser.add_argument(
        '--no-backup',
        action='store_true',
        help='Do not copy the database before applying migrations.',
    )
    parser.set_defaults(func=database_upgrader_argparse)

    args = parser.parse_args(argv)
    return args.func(args)

if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
