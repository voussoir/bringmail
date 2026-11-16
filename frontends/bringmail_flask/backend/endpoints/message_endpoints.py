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
    message.set_seen(seen)
    return flasktools.json_response(message.jsonify())


@site.route('/message/<message_id>/set_flagged', methods=['POST'])
@flasktools.required_fields(['flagged'], forbid_whitespace=True)
def post_message_set_flagged(message_id):
    message = common.get_message(message_id, response_type='json')
    try:
        flagged = stringtools.truthystring(request.form['flagged'])
    except ValueError:
        return flasktools.json_response({}, status=400)
    message.set_flagged(flagged)
    return flasktools.json_response(message.jsonify())


@site.route('/message/<message_id>/move', methods=['POST'])
@flasktools.required_fields(['folder_id'])
def post_message_move(message_id):
    message = common.get_message(message_id, response_type='json')
    folder = common.get_folder(request.form['folder_id'], response_type='json')
    message.move_to(folder)
    if message.deleted:
        return flasktools.json_response({'type': 'message', 'id': message.id, 'deleted': True})
    return flasktools.json_response(message.jsonify())


@site.route('/message/<message_id>/delete', methods=['POST'])
def post_message_delete(message_id):
    message = common.get_message(message_id, response_type='json')
    message.delete()
    if message.deleted:
        return flasktools.json_response({'type': 'message', 'id': message.id, 'deleted': True})
    return flasktools.json_response(message.jsonify())


def _parse_message_ids():
    return [int(value) for value in stringtools.comma_space_split(request.form['message_ids'])]


@site.route('/batch/messages/set_seen', methods=['POST'])
@flasktools.required_fields(['message_ids', 'seen'], forbid_whitespace=True)
def post_batch_messages_set_seen():
    message_ids = _parse_message_ids()
    seen = stringtools.truthystring(request.form['seen'])
    messages = list(common.get_messages(message_ids, response_type='json'))
    for message in messages:
        message.set_seen(seen)
    return flasktools.json_response([message.id for message in messages])


@site.route('/batch/messages/set_flagged', methods=['POST'])
@flasktools.required_fields(['message_ids', 'flagged'], forbid_whitespace=True)
def post_batch_messages_set_flagged():
    message_ids = _parse_message_ids()
    flagged = stringtools.truthystring(request.form['flagged'])
    messages = list(common.get_messages(message_ids, response_type='json'))
    for message in messages:
        message.set_flagged(flagged)
    return flasktools.json_response([message.id for message in messages])


@site.route('/batch/messages/move', methods=['POST'])
@flasktools.required_fields(['message_ids', 'folder_id'])
def post_batch_messages_move():
    message_ids = _parse_message_ids()
    folder = common.get_folder(request.form['folder_id'], response_type='json')
    messages = list(common.get_messages(message_ids, response_type='json'))
    for message in messages:
        message.move_to(folder)
    return flasktools.json_response([message.id for message in messages])


@site.route('/batch/messages/delete', methods=['POST'])
@flasktools.required_fields(['message_ids'])
def post_batch_messages_delete():
    message_ids = _parse_message_ids()
    messages = list(common.get_messages(message_ids, response_type='json'))
    for message in messages:
        message.delete()
    return flasktools.json_response([message.id for message in messages])
