'''
Shared Flask application setup for BringMail.

Do not execute this file directly. Use bringmail_flask_dev.py or
bringmail_flask_prod.py.
'''
import flask
from flask import request
import functools
import json
import queue
import threading
import time
import traceback

from werkzeug.datastructures import MultiDict
from voussoirkit import flasktools
from voussoirkit import pathclass
from voussoirkit import sentinel
from voussoirkit import vlogging

log = vlogging.get_logger(__name__)

import bringmail

from . import jinja_filters

# Flask init #######################################################################################

# __file__ = .../bringmail_flask/backend/common.py
# root_dir = .../bringmail_flask
root_dir = pathclass.Path(__file__).parent.parent
TEMPLATE_DIR = root_dir.with_child('templates')
STATIC_DIR = root_dir.with_child('static')
BROWSER_CACHE_DURATION = 180

site = flask.Flask(
    __name__,
    template_folder=TEMPLATE_DIR.absolute_path,
    static_folder=STATIC_DIR.absolute_path,
)
site.config.update(
    SEND_FILE_MAX_AGE_DEFAULT=BROWSER_CACHE_DURATION,
    TEMPLATES_AUTO_RELOAD=True,
)
site.jinja_env.add_extension('jinja2.ext.do')
site.jinja_env.globals['INF'] = float('inf')
site.jinja_env.trim_blocks = True
site.jinja_env.lstrip_blocks = True
jinja_filters.register_all(site)
site.localhost_only = False

# This is assigned by init_bringdb at launcher startup.
bringdb = None

# Response wrappers ################################################################################

def catch_bringmail_exception(endpoint):
    '''
    Convert BringException into a JSON response instead of error 500.
    '''
    @functools.wraps(endpoint)
    def wrapped(*args, **kwargs):
        try:
            return endpoint(*args, **kwargs)
        except bringmail.exceptions.BringException as exc:
            if isinstance(exc, bringmail.exceptions.NoSuch):
                status = 404
            else:
                status = 400
            response = flasktools.json_response(exc.jsonify(), status=status)
            flask.abort(response)
    return wrapped


@site.before_request
def before_request():
    request.is_localhost = (request.remote_addr == '127.0.0.1')
    if site.localhost_only and not request.is_localhost:
        flask.abort(403)

    # A custom header on every mutating request blocks cross-site form
    # submissions, since browsers will not attach it to a plain HTML form.
    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        if request.headers.get('X-BringMail-Request') != '1':
            flask.abort(403)


@site.after_request
def after_request(response):
    response = flasktools.gzip_response(request, response)
    return response


DEFAULT_LAYOUT = 'ltb'
LAYOUTS = {'ltb', 'lmr'}


def give_layout_cookie(function, *, cookie_name, default_layout):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        old_layout = request.cookies.get(cookie_name, None)
        new_layout = request.args.get('layout', None)
        if new_layout is not None and new_layout not in LAYOUTS:
            new_layout = None
        layout = new_layout or old_layout or default_layout
        if layout not in LAYOUTS:
            layout = default_layout

        request.cookies = MultiDict(request.cookies)
        request.cookies[cookie_name] = layout

        response = function(*args, **kwargs)

        if new_layout is None:
            pass
        elif new_layout == '':
            response.set_cookie(cookie_name, value='', expires=0)
        elif new_layout != old_layout:
            response.set_cookie(cookie_name, value=new_layout, expires=2147483647)

        return response
    return wrapped


site.route = flasktools.decorate_and_route(
    flask_app=site,
    decorators=[
        flasktools.ensure_response_type,
        functools.partial(
            give_layout_cookie,
            cookie_name='bringmail_layout',
            default_layout=DEFAULT_LAYOUT,
        ),
        functools.partial(
            flasktools.give_theme_cookie,
            cookie_name='bringmail_theme',
            default_theme='slate',
        ),
        catch_bringmail_exception,
    ],
)

# Get functions ####################################################################################

def getter_wrapper(getter_function):
    def getter_wrapped(thing_id, response_type):
        if response_type not in {'html', 'json'}:
            raise TypeError(f'response_type should be html or json, not {response_type}.')

        try:
            return getter_function(thing_id)

        except bringmail.exceptions.BringException as exc:
            if isinstance(exc, bringmail.exceptions.NoSuch):
                status = 404
            else:
                status = 400

            if response_type == 'html':
                flask.abort(status, exc.error_message)
            else:
                response = exc.jsonify()
                response = flasktools.json_response(response, status=status)
                flask.abort(response)

        except Exception:
            traceback.print_exc()
            if response_type == 'html':
                flask.abort(500)
            else:
                flask.abort(flasktools.json_response({}, status=500))

    return getter_wrapped


@getter_wrapper
def get_account(account_id):
    return bringdb.get_account(account_id)


@getter_wrapper
def get_folder(folder_id):
    return bringdb.get_folder(folder_id)


@getter_wrapper
def get_message(message_id):
    return bringdb.get_message(message_id)


@getter_wrapper
def get_messages(message_ids):
    return bringdb.get_objects_by_id(
        bringmail.objects.Message,
        message_ids,
        raise_for_missing=True,
    )


# Other functions ##################################################################################

def get_layout():
    layout = request.args.get('layout', None)
    if layout not in LAYOUTS:
        layout = request.cookies.get('bringmail_layout', DEFAULT_LAYOUT)
    if layout not in LAYOUTS:
        layout = DEFAULT_LAYOUT
    return layout


def render_template(request, template_name, **kwargs):
    theme = request.cookies.get('bringmail_theme', None)
    response = flask.render_template(
        template_name,
        site=site,
        request=request,
        theme=theme,
        layout=get_layout(),
        **kwargs,
    )
    return response


# Background threads ###############################################################################

QUIT_EVENT = sentinel.Sentinel('quit')

AUTOREFRESH_THREAD_EVENTS = queue.Queue()

####################################################################################################

REFRESH_QUEUE = queue.Queue()
_REFRESH_QUEUE_SET = set()
_REFRESH_QUEUE_LOCK = threading.Lock()


def add_account_to_refresh_queue(account):
    if not isinstance(account, bringmail.objects.Account):
        account = bringdb.get_account(account)

    with _REFRESH_QUEUE_LOCK:
        if account.id in _REFRESH_QUEUE_SET:
            return
        _REFRESH_QUEUE_SET.add(account.id)

    log.debug('Adding %s to refresh queue.', account)
    REFRESH_QUEUE.put(account.id)


def clear_refresh_queue():
    with _REFRESH_QUEUE_LOCK:
        while not REFRESH_QUEUE.empty():
            try:
                REFRESH_QUEUE.get_nowait()
            except queue.Empty:
                break
        _REFRESH_QUEUE_SET.clear()


def autorefresh_thread():
    '''
    Watch each account's last_refresh_attempt and autorefresh_interval, and
    enqueue accounts that are due.
    '''
    log.info('Starting autorefresh thread.')
    while True:
        if not REFRESH_QUEUE.empty():
            try:
                event = AUTOREFRESH_THREAD_EVENTS.get(timeout=10)
                if event is QUIT_EVENT:
                    break
            except queue.Empty:
                pass
            continue

        now = bringmail.helpers.now()
        soonest = now + 3600
        for account in list(bringdb.get_accounts()):
            next_refresh = account.next_refresh
            if now > next_refresh:
                add_account_to_refresh_queue(account)
                if account.autorefresh_interval > 0:
                    next_refresh = now + account.autorefresh_interval
                else:
                    next_refresh = float('inf')
            soonest = min(soonest, next_refresh)

        now = bringmail.helpers.now()
        sleepy = soonest - now
        sleepy = max(sleepy, 30)
        sleepy = min(sleepy, 3600)
        sleepy = int(sleepy)
        log.info('Sleeping %s until next account refresh.', sleepy)
        try:
            event = AUTOREFRESH_THREAD_EVENTS.get(timeout=sleepy)
            if event is QUIT_EVENT:
                break
        except queue.Empty:
            pass


def refresh_queue_thread():
    '''
    One worker performs all account syncs so SQLite writes are serialized and
    only one network-heavy refresh runs at a time.
    '''
    log.info('Starting refresh queue thread.')
    while True:
        account_id = REFRESH_QUEUE.get()
        if account_id is QUIT_EVENT:
            break

        account = None
        try:
            account = bringdb.get_account(account_id)
            flasktools.send_sse(
                event='account_refresh_started',
                data=json.dumps(account.jsonify()),
            )

            def progress(payload):
                flasktools.send_sse(
                    event='account_refresh_progress',
                    data=json.dumps(payload),
                )

            error = account.refresh(progress=progress)
            flasktools.send_sse(
                event='account_refresh_finished',
                data=json.dumps({
                    'account_id': account.id,
                    'error': error,
                    'last_refresh': account.last_refresh,
                    'last_refresh_attempt': account.last_refresh_attempt,
                }),
            )
        except Exception:
            log.warning('Refreshing account %s raised:\n%s', account_id, traceback.format_exc())
            flasktools.send_sse(
                event='account_refresh_finished',
                data=json.dumps({
                    'account_id': account_id,
                    'error': traceback.format_exc(),
                }),
            )
        finally:
            with _REFRESH_QUEUE_LOCK:
                _REFRESH_QUEUE_SET.discard(account_id)
            if REFRESH_QUEUE.empty():
                flasktools.send_sse(event='account_refresh_queue_finished', data='')


def sse_keepalive_thread():
    log.info('Starting SSE keepalive thread.')
    while True:
        flasktools.send_sse(event='keepalive', data=bringmail.helpers.now())
        time.sleep(60)


def init_bringdb(*args, **kwargs):
    global bringdb
    bringdb = bringmail.bringmaildb.BringMailDB.closest_bringmaildb(*args, **kwargs)
    return bringdb


def start_background_threads():
    threading.Thread(target=autorefresh_thread, daemon=True).start()
    threading.Thread(target=refresh_queue_thread, daemon=True).start()
    threading.Thread(target=sse_keepalive_thread, daemon=True).start()
