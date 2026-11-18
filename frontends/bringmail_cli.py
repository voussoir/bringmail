import argparse
import getpass
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from voussoirkit import pipeable
from voussoirkit import vlogging

import bringmail
from bringmail import objects

log = vlogging.getLogger(__name__, 'bringmail_cli')

bringdb = None


def load_bringdb():
    global bringdb
    if bringdb is None:
        bringdb = bringmail.bringmaildb.BringMailDB.closest_bringmaildb()
    return bringdb


def init_argparse(args):
    bringmail.bringmaildb.BringMailDB(create=True)
    return 0


def add_account_argparse(args):
    database = load_bringdb()

    if args.offline:
        if not args.label:
            pipeable.stderr('An offline account requires --label.')
            return 1
        with database.transaction:
            account = database.add_account(
                label=args.label,
                offline=True,
            )
        pipeable.stdout(f'Added {account}.')
        return 0

    if not args.host or not args.username:
        pipeable.stderr('An online account requires --host and --username.')
        return 1

    password = args.password
    if password is None:
        password = getpass.getpass(f'Password for {args.username}: ')

    with database.transaction:
        account = database.add_account(
            label=args.label,
            host=args.host,
            port=args.port,
            ssl=args.ssl,
            starttls=args.starttls,
            username=args.username,
            password=password,
            autorefresh_interval=args.autorefresh_interval,
        )
    pipeable.stdout(f'Added {account}.')
    return 0


def list_accounts_argparse(args):
    database = load_bringdb()
    for account in database.get_accounts():
        unread = account.get_unread_count()
        if account.offline:
            pipeable.stdout(
                f'{account.id} | {account.display_name} | offline | '
                f'unread={unread}'
            )
            continue
        pipeable.stdout(
            f'{account.id} | {account.display_name} | {account.username} | '
            f'{account.host}:{account.port} | interval={account.autorefresh_interval} | '
            f'unread={unread}'
        )
    return 0


def refresh_argparse(args):
    database = load_bringdb()
    now = bringmail.helpers.now()
    did_refresh = False
    for account in list(database.get_accounts()):
        if now > account.next_refresh:
            error = account.refresh()
            did_refresh = True
            if error:
                pipeable.stderr(f'{account}: {error.splitlines()[-1]}')
    if not did_refresh:
        pipeable.stdout('No accounts are due for refresh.')
    return 0


def refresh_all_argparse(args):
    database = load_bringdb()
    for account in list(database.get_accounts()):
        error = account.refresh()
        if error:
            pipeable.stderr(f'{account}: {error.splitlines()[-1]}')
    return 0


def main(argv):
    parser = argparse.ArgumentParser(
        description='''
        Command-line management for the BringMail mail cache.
        ''',
    )
    subparsers = parser.add_subparsers()

    p_init = subparsers.add_parser('init', description='Create a new BringMail database in the current directory.')
    p_init.set_defaults(func=init_argparse)

    p_add = subparsers.add_parser('add_account', aliases=['add-account'])
    p_add.add_argument('--offline', action='store_true')
    p_add.add_argument('--host', default=None)
    p_add.add_argument('--username', default=None)
    p_add.add_argument('--password', default=None)
    p_add.add_argument('--label', default=None)
    p_add.add_argument('--port', type=int, default=993)
    p_add.add_argument('--ssl', action='store_true', default=True)
    p_add.add_argument('--no-ssl', dest='ssl', action='store_false')
    p_add.add_argument('--starttls', action='store_true')
    p_add.add_argument('--autorefresh_interval', type=int, default=bringmail.constants.DEFAULT_AUTOREFRESH_INTERVAL)
    p_add.set_defaults(func=add_account_argparse)

    p_list = subparsers.add_parser('accounts', description='List accounts.')
    p_list.set_defaults(func=list_accounts_argparse)

    p_refresh = subparsers.add_parser('refresh', description='Refresh accounts whose interval has elapsed.')
    p_refresh.set_defaults(func=refresh_argparse)

    p_refresh_all = subparsers.add_parser('refresh_all', aliases=['refresh-all'], description='Refresh every account now.')
    p_refresh_all.set_defaults(func=refresh_all_argparse)

    args = parser.parse_args(argv)
    if not hasattr(args, 'func'):
        parser.print_help()
        return 1
    return args.func(args)


@vlogging.main_decorator(log=log)
def wrapped_main(argv):
    return main(argv)


if __name__ == '__main__':
    raise SystemExit(wrapped_main(sys.argv[1:]))
