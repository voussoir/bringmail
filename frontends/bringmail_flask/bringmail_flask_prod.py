'''
This file is the WSGI entrypoint for production use.

If you are using Gunicorn, for example:
gunicorn bringmail_flask_prod:site --bind "127.0.0.1:PORT" --worker-class gevent
'''
import pathlib
import sys

THIS_DIR = pathlib.Path(__file__).resolve().parent
FRONTENDS_DIR = THIS_DIR.parent
REPO_ROOT = FRONTENDS_DIR.parent
for path in (str(REPO_ROOT), str(FRONTENDS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from bringmail_flask import backend

site = backend.site
site.debug = False

backend.common.init_bringdb()
backend.common.start_background_threads()
