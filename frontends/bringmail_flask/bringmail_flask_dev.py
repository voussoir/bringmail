'''
This file is the gevent launcher for local / development use.
'''
import gevent.monkey; gevent.monkey.patch_all()

import argparse
import gevent.pywsgi
import os
import pathlib
import sys
import traceback

THIS_DIR = pathlib.Path(__file__).resolve().parent
FRONTENDS_DIR = THIS_DIR.parent
REPO_ROOT = FRONTENDS_DIR.parent
for path in (str(REPO_ROOT), str(FRONTENDS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from voussoirkit import vlogging

log = vlogging.getLogger(__name__, 'bringmail_flask_dev')

import bringmail
from bringmail_flask import backend

site = backend.site

def bringmail_flask_dev(
        *,
        host,
        port,
        init,
        localhost_only,
    ):
    if localhost_only:
        log.info('Setting localhost_only = True')
        site.localhost_only = True

    if init:
        bringmail.bringmaildb.BringMailDB(create=True)

    try:
        backend.common.init_bringdb()
    except bringmail.exceptions.NoClosestBringMailDB as exc:
        log.error(exc.error_message)
        log.error('Try adding --init to create the database.')
        return 1
    except (bringmail.exceptions.SQLiteTooOld,
            bringmail.exceptions.DatabaseOutOfDate,
            bringmail.exceptions.DatabaseTooNew,
            bringmail.exceptions.BadDataDirectory):
        log.error(traceback.format_exc())
        return 1
    except FileNotFoundError as exc:
        log.error(str(exc))
        log.error('Try adding --init to create the database.')
        return 1

    message = f'Starting server on {host}:{port}, pid={os.getpid()}.'
    log.info(message)

    backend.common.start_background_threads()

    http = gevent.pywsgi.WSGIServer(
        listener=(host, port),
        application=site,
    )
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        log.info('Goodbye')
        return 0
    finally:
        backend.common.stop_background_threads()

def bringmail_flask_dev_argparse(args):
    return bringmail_flask_dev(
        host=args.host,
        port=args.port,
        init=args.init,
        localhost_only=args.localhost_only,
    )

@vlogging.main_decorator(log=log)
def main(argv):
    parser = argparse.ArgumentParser(
        description='''
        Run the BringMail web interface locally.
        ''',
    )
    parser.add_argument(
        'port',
        nargs='?',
        type=int,
        default=27465,
        help='Port number on which to run the server. Default 27465.',
    )
    parser.add_argument(
        '--host',
        default='127.0.0.1',
        help='Host / interface to bind. Default 127.0.0.1.',
    )
    parser.add_argument(
        '--init',
        action='store_true',
        help='Create a new BringMail database in the current folder.',
    )
    parser.add_argument(
        '--localhost_only',
        '--localhost-only',
        action='store_true',
        help='Reject requests whose remote address is not 127.0.0.1.',
    )
    parser.set_defaults(func=bringmail_flask_dev_argparse)

    args = parser.parse_args(argv)
    return args.func(args)

if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
