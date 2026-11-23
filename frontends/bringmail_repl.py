import argparse
import code
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from voussoirkit import interactive
from voussoirkit import pipeable

import bringmail

def bringmail_repl_argparse(args):
    global B
    try:
        B = bringmail.bringmaildb.BringMailDB.closest_bringmaildb()
    except bringmail.exceptions.NoClosestBringMailDB as exc:
        pipeable.stderr(exc.error_message)
        pipeable.stderr('Try `bringmail_cli.py init` to create the database.')
        return 1

    if args.exec_statement:
        with B.transaction:
            exec(args.exec_statement)
    else:
        while True:
            try:
                code.interact(banner='', local=dict(globals(), **locals()))
            except SystemExit:
                pass
            if len(B.savepoints) == 0:
                break
            print('You have uncommitted changes. Are you sure you want to quit?')
            if interactive.getpermission():
                break
    return 0

def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument('--exec', dest='exec_statement', default=None)
    parser.set_defaults(func=bringmail_repl_argparse)
    args = parser.parse_args(argv)
    return args.func(args)

if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
