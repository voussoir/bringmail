import flask
from flask import request
import urllib.parse

from werkzeug.utils import secure_filename

from voussoirkit import flasktools
from voussoirkit import stringtools

import bringmail

from .. import common

site = common.site


def _message_response(message):
    body = message.get_body()
    return flasktools.json_response(body)


def _jsonify_with_pending(message):
    pending = common.pending_state_for(message)
    return flasktools.json_response(message.jsonify(pending=pending))


def _queue_batch(messages, action):
    '''
    Apply a queued action to every message, then ask for one flush per
    account involved. Messages can span accounts in the All view.
    '''
    accounts = {}
    for message in messages:
        action(message)
        accounts[message.account_id] = message.account
    for account in accounts.values():
        common.add_flush_to_queue(account)
    return flasktools.json_response([message.id for message in messages])


@site.route('/message/<message_id>.json')
def get_message_json(message_id):
    message = common.get_message(message_id, response_type='json')
    return _message_response(message)


@site.route('/message/<message_id>/headers')
def get_message_headers(message_id):
    message = common.get_message(message_id, response_type='html')
    text = message.get_headers_text()
    return flask.Response(text, mimetype='text/plain')


@site.route('/message/<message_id>/part/<int:part_index>')
def get_message_part(message_id, part_index):
    message = common.get_message(message_id, response_type='json')
    part = message.get_attachment(part_index)

    response = flask.Response(part['bytes'], mimetype=part['content_type'])
    response.headers['Cache-Control'] = f'private, max-age={common.BROWSER_CACHE_DURATION}'

    filename = part.get('filename')
    if filename:
        filename = secure_filename(filename) or 'attachment'
        quoted = urllib.parse.quote(filename)
        disposition = 'inline' if part.get('content_id') else 'attachment'
        response.headers['Content-Disposition'] = (
            f"{disposition}; filename=\"{filename}\"; filename*=UTF-8''{quoted}"
        )
    return response


@site.route('/message/<message_id>/set_seen', methods=['POST'])
@flasktools.required_fields(['seen'], forbid_whitespace=True)
def post_message_set_seen(message_id):
    message = common.get_message(message_id, response_type='json')
    try:
        seen = stringtools.truthystring(request.form['seen'])
    except ValueError:
        return flasktools.json_response({}, status=400)
    message.queue_seen(seen)
    common.add_flush_to_queue(message.account)
    return _jsonify_with_pending(message)


@site.route('/message/<message_id>/set_flagged', methods=['POST'])
@flasktools.required_fields(['flagged'], forbid_whitespace=True)
def post_message_set_flagged(message_id):
    message = common.get_message(message_id, response_type='json')
    try:
        flagged = stringtools.truthystring(request.form['flagged'])
    except ValueError:
        return flasktools.json_response({}, status=400)
    message.queue_flagged(flagged)
    common.add_flush_to_queue(message.account)
    return _jsonify_with_pending(message)


@site.route('/message/<message_id>/move', methods=['POST'])
@flasktools.required_fields(['folder_id'])
def post_message_move(message_id):
    message = common.get_message(message_id, response_type='json')
    folder = common.get_folder(request.form['folder_id'], response_type='json')
    message.queue_move(folder)
    common.add_flush_to_queue(message.account)
    return _jsonify_with_pending(message)


@site.route('/message/<message_id>/delete', methods=['POST'])
def post_message_delete(message_id):
    message = common.get_message(message_id, response_type='json')
    message.queue_delete()
    common.add_flush_to_queue(message.account)
    return _jsonify_with_pending(message)


@site.route('/message/<message_id>/restore', methods=['POST'])
def post_message_restore(message_id):
    message = common.get_message(message_id, response_type='json')
    message.queue_restore()
    common.add_flush_to_queue(message.account)
    return _jsonify_with_pending(message)


def _parse_message_ids():
    return [int(value) for value in stringtools.comma_space_split(request.form['message_ids'])]


@site.route('/batch/messages/set_seen', methods=['POST'])
@flasktools.required_fields(['message_ids', 'seen'], forbid_whitespace=True)
def post_batch_messages_set_seen():
    message_ids = _parse_message_ids()
    seen = stringtools.truthystring(request.form['seen'])
    messages = list(common.get_messages(message_ids, response_type='json'))
    return _queue_batch(messages, lambda message: message.queue_seen(seen))


@site.route('/batch/messages/set_flagged', methods=['POST'])
@flasktools.required_fields(['message_ids', 'flagged'], forbid_whitespace=True)
def post_batch_messages_set_flagged():
    message_ids = _parse_message_ids()
    flagged = stringtools.truthystring(request.form['flagged'])
    messages = list(common.get_messages(message_ids, response_type='json'))
    return _queue_batch(messages, lambda message: message.queue_flagged(flagged))


@site.route('/batch/messages/move', methods=['POST'])
@flasktools.required_fields(['message_ids', 'folder_id'])
def post_batch_messages_move():
    message_ids = _parse_message_ids()
    folder = common.get_folder(request.form['folder_id'], response_type='json')
    messages = list(common.get_messages(message_ids, response_type='json'))
    return _queue_batch(messages, lambda message: message.queue_move(folder))


@site.route('/batch/messages/move_to_account', methods=['POST'])
@flasktools.required_fields(['message_ids', 'folder_id'])
def post_batch_messages_move_to_account():
    message_ids = _parse_message_ids()
    folder = common.get_folder(request.form['folder_id'], response_type='json')
    messages = list(common.get_messages(message_ids, response_type='json'))
    if not messages:
        return flasktools.json_response(
            {
                'type': 'error',
                'error_type': 'INVALID_INPUT',
                'error_message': 'No messages were selected.',
            },
            status=400,
        )

    source_account_ids = {message.account_id for message in messages}
    if len(source_account_ids) != 1:
        raise bringmail.exceptions.InvalidFolder(
            'Move requires messages from one account.'
        )
    if folder.account_id == messages[0].account_id:
        raise bringmail.exceptions.InvalidFolder(
            'The destination folder belongs to the source account.'
        )

    entries = []
    for message in messages:
        result = message.queue_move_to_account(folder)
        entries.append({
            'id': message.id,
            'copy_only': result.copy_only,
            'pending': common.pending_state_for(message),
        })

    common.add_flush_to_queue(messages[0].account)
    return flasktools.json_response({
        'type': 'batch_move_to_account',
        'folder_id': folder.id,
        'messages': entries,
    })


@site.route('/batch/messages/delete', methods=['POST'])
@flasktools.required_fields(['message_ids'])
def post_batch_messages_delete():
    message_ids = _parse_message_ids()
    messages = list(common.get_messages(message_ids, response_type='json'))
    return _queue_batch(messages, lambda message: message.queue_delete())


@site.route('/batch/messages/restore', methods=['POST'])
@flasktools.required_fields(['message_ids'])
def post_batch_messages_restore():
    message_ids = _parse_message_ids()
    messages = list(common.get_messages(message_ids, response_type='json'))
    return _queue_batch(messages, lambda message: message.queue_restore())
