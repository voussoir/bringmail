import flask
from flask import request
import sqlite3

from voussoirkit import flasktools
from voussoirkit import stringtools

import bringmail

from .. import common

site = common.site


def _form_bool(name, default):
    value = request.form.get(name)
    if value is None:
        return default
    return stringtools.truthystring(value)


def _form_int(name, default):
    value = request.form.get(name)
    if value is None or value.strip() == '':
        return default
    return int(value)


def _invalid(message):
    return flasktools.json_response(
        {
            'type': 'error',
            'error_type': 'INVALID_INPUT',
            'error_message': message,
        },
        status=400,
    )


@site.route('/accounts.json')
@flasktools.cached_endpoint(max_age=0, etag_function=lambda: common.bringdb.last_commit_id)
def get_accounts_json():
    accounts = []
    for account in common.bringdb.get_accounts():
        accounts.append(account.jsonify(folders=True, unread_count=True, pending_count=True))
    return flasktools.json_response(accounts)


@site.route('/accounts/refresh_all', methods=['POST'])
def post_accounts_refresh_all():
    for account in common.bringdb.get_accounts():
        if account.offline:
            continue
        common.add_account_to_refresh_queue(account, manual=True)
    return flasktools.json_response({})


@site.route('/accounts/reorder', methods=['POST'])
def post_accounts_reorder():
    raw_ids = request.form.get('account_ids', '')
    try:
        account_ids = [int(value) for value in raw_ids.split(',') if value.strip()]
        with common.bringdb.transaction:
            accounts = common.bringdb.reorder_accounts(account_ids)
    except bringmail.exceptions.InvalidAccount as exc:
        return _invalid(exc.error_message)
    except (TypeError, ValueError) as exc:
        return _invalid(str(exc))

    return flasktools.json_response([
        account.jsonify(folders=True, unread_count=True, pending_count=True)
        for account in accounts
    ])


@site.route('/account/<account_id>.json')
def get_account_json(account_id):
    account = common.get_account(account_id, response_type='json')
    return flasktools.json_response(account.jsonify(folders=True, unread_count=True, pending_count=True))


@site.route('/account/add')
def get_account_add():
    return common.render_template(
        request,
        'account_settings.html',
        account=None,
        folders=[],
    )


@site.route('/account/<account_id>/settings')
def get_account_settings(account_id):
    account = common.get_account(account_id, response_type='html')
    folders = list(account.get_folders())
    return common.render_template(
        request,
        'account_settings.html',
        account=account,
        folders=folders,
    )


@site.route('/accounts/add', methods=['POST'])
def post_accounts_add():
    try:
        offline = _form_bool('offline', False)
    except (TypeError, ValueError) as exc:
        return _invalid(str(exc))

    label = request.form.get('label')

    if offline:
        supplied = [
            name for name in ('host', 'port', 'username', 'password')
            if (request.form.get(name) or '').strip()
        ]
        if supplied:
            return _invalid('Offline accounts cannot have IMAP connection settings.')

        try:
            with common.bringdb.transaction:
                account = common.bringdb.add_account(
                    label=label,
                    offline=True,
                )
        except bringmail.exceptions.InvalidAccount as exc:
            return _invalid(exc.error_message)
        except (TypeError, ValueError) as exc:
            return _invalid(str(exc))

        common.AUTOREFRESH_THREAD_EVENTS.put('account changed')
        common.reconfigure_idle_supervisor()
        return flasktools.json_response(
            account.jsonify(folders=True, unread_count=True, pending_count=True)
        )

    try:
        port = _form_int('port', 993)
        ssl = _form_bool('ssl', True)
        starttls = _form_bool('starttls', False)
        autorefresh_interval = _form_int('autorefresh_interval', bringmail.constants.DEFAULT_AUTOREFRESH_INTERVAL)
    except (TypeError, ValueError) as exc:
        return _invalid(str(exc))

    host = request.form.get('host')
    username = request.form.get('username')
    password = request.form.get('password')
    if not host:
        return _invalid('Host is required.')
    if not username:
        return _invalid('Username is required.')
    if not password:
        return _invalid('Password is required.')

    with common.bringdb.transaction:
        try:
            account = common.bringdb.add_account(
                label=label,
                host=host,
                port=port,
                ssl=ssl,
                starttls=starttls,
                username=username,
                password=password,
                autorefresh_interval=autorefresh_interval,
            )
        except sqlite3.IntegrityError:
            return _invalid(f'An account for {username} at {host}:{port} already exists.')
        except (TypeError, ValueError) as exc:
            return _invalid(str(exc))

    common.AUTOREFRESH_THREAD_EVENTS.put('account changed')
    common.reconfigure_idle_supervisor()
    return flasktools.json_response(account.jsonify(folders=True, unread_count=True, pending_count=True))


@site.route('/account/<account_id>/update', methods=['POST'])
def post_account_update(account_id):
    account = common.get_account(account_id, response_type='json')

    try:
        offline = _form_bool('offline', False) if 'offline' in request.form else None
    except (TypeError, ValueError) as exc:
        return _invalid(str(exc))

    if account.offline:
        if offline is True:
            return _invalid('An account cannot be converted between online and offline.')
        unsupported = [
            name for name in (
                'host',
                'port',
                'ssl',
                'starttls',
                'username',
                'password',
                'autorefresh_interval',
            )
            if name in request.form
        ]
        if unsupported:
            return _invalid('Offline accounts only support label changes.')
        try:
            label = request.form.get('label')
            with common.bringdb.transaction:
                account.set_label(label)
        except (TypeError, ValueError, bringmail.exceptions.InvalidAccount) as exc:
            account.__reinit__()
            if isinstance(exc, bringmail.exceptions.InvalidAccount):
                return _invalid(exc.error_message)
            return _invalid(str(exc))

        common.AUTOREFRESH_THREAD_EVENTS.put('account changed')
        return flasktools.json_response(account.jsonify(folders=True, unread_count=True, pending_count=True))

    if offline is True:
        return _invalid('An account cannot be converted between online and offline.')

    try:
        if 'label' in request.form:
            label = request.form.get('label')
        else:
            label = account.label

        if 'host' in request.form:
            host = request.form.get('host')
            port = _form_int('port', account.port)
            ssl = _form_bool('ssl', account.ssl)
            starttls = _form_bool('starttls', account.starttls)
        else:
            host = account.host
            port = account.port
            ssl = account.ssl
            starttls = account.starttls

        if 'username' in request.form:
            username = request.form.get('username')
        else:
            username = account.username

        password = request.form.get('password') or None

        if 'autorefresh_interval' in request.form:
            autorefresh_interval = _form_int('autorefresh_interval', account.autorefresh_interval)
        else:
            autorefresh_interval = account.autorefresh_interval
    except (TypeError, ValueError) as exc:
        return _invalid(str(exc))

    try:
        with common.bringdb.transaction:
            if label != account.label:
                account.set_label(label)
            if (host, port, ssl, starttls) != (account.host, account.port, account.ssl, account.starttls):
                account.set_server(host=host, port=port, ssl=ssl, starttls=starttls)
            if username != account.username:
                account.set_username(username)
            if password:
                account.set_password(password)
            if autorefresh_interval != account.autorefresh_interval:
                account.set_autorefresh_interval(autorefresh_interval)
    except (TypeError, ValueError) as exc:
        account.__reinit__()
        return _invalid(str(exc))
    except sqlite3.IntegrityError:
        account.__reinit__()
        return _invalid('Another account already uses that host, port, and username.')

    # Log out any reusable session that was created with the old credentials.
    bringmail.helpers.drop_imap_session(account.id)
    common.AUTOREFRESH_THREAD_EVENTS.put('account changed')
    common.reconfigure_idle_supervisor()

    return flasktools.json_response(account.jsonify(folders=True, unread_count=True, pending_count=True))


@site.route('/account/<account_id>/delete', methods=['POST'])
def post_account_delete(account_id):
    account = common.get_account(account_id, response_type='json')
    with common.bringdb.transaction:
        account.delete()
    bringmail.helpers.drop_imap_session(account.id)
    common.AUTOREFRESH_THREAD_EVENTS.put('account changed')
    common.reconfigure_idle_supervisor()
    return flasktools.json_response({})


@site.route('/account/<account_id>/refresh', methods=['POST'])
def post_account_refresh(account_id):
    account = common.get_account(account_id, response_type='json')
    common.add_account_to_refresh_queue(account, manual=True)
    return flasktools.json_response({})


@site.route('/account/<account_id>/test', methods=['POST'])
def post_account_test(account_id):
    account = common.get_account(account_id, response_type='json')
    folder_count = account.test_connection()
    return flasktools.json_response({'folder_count': folder_count})


@site.route('/account/<account_id>/folders.json')
def get_account_folders_json(account_id):
    account = common.get_account(account_id, response_type='json')
    counts = common.bringdb.get_account_unread_counts(account)
    folders = [
        folder.jsonify(
            unread_count=counts.get(folder.id, 0),
            message_count=common.bringdb.get_folder_message_count(folder),
        )
        for folder in account.get_folders()
    ]
    return flasktools.json_response(folders)
