from flask import request

from voussoirkit import flasktools
from voussoirkit import stringtools

import bringmail

from .. import common

site = common.site


def _pagination_args():
    try:
        limit = int(request.args.get('limit', bringmail.constants.DEFAULT_PAGE_SIZE))
    except ValueError:
        limit = bringmail.constants.DEFAULT_PAGE_SIZE
    limit = max(1, min(limit, bringmail.constants.MAX_PAGE_SIZE))

    before_date = request.args.get('before_date')
    before_id = request.args.get('before_id')
    if before_date is not None and before_id is not None:
        try:
            before_date = int(before_date)
            before_id = int(before_id)
        except ValueError:
            before_date = None
            before_id = None
    else:
        before_date = None
        before_id = None

    return (limit, before_date, before_id)


def _message_list_response(*, folder=None, account=None, all_accounts=False):
    (limit, before_date, before_id) = _pagination_args()
    if all_accounts:
        folder_ids = []
        seen = set()
        for each_account in common.bringdb.get_accounts():
            for each_folder in each_account.get_aggregate_folders():
                if each_folder.id not in seen:
                    seen.add(each_folder.id)
                    folder_ids.append(each_folder.id)
    elif account is not None:
        folder_ids = [folder.id for folder in account.get_aggregate_folders()]
    else:
        folder_ids = None
    messages = common.bringdb.get_messages(
        folder=folder,
        folder_ids=folder_ids,
        limit=min(limit + 1, bringmail.constants.MAX_PAGE_SIZE),
        before_date=before_date,
        before_id=before_id,
    )
    has_more = len(messages) > limit
    messages = messages[:limit]
    pending = common.pending_states_for_messages(message.id for message in messages)
    response = {
        'type': 'message_list',
        'messages': [
            message.jsonify(pending=pending.get(message.id))
            for message in messages
        ],
        'has_more': has_more,
    }
    if folder is not None:
        response['folder'] = folder.jsonify()
    elif account is not None:
        response['account'] = account.jsonify(unread_count=True, pending_count=True)
    return flasktools.json_response(response)


@site.route('/folder/<folder_id>.json')
def get_folder_json(folder_id):
    folder = common.get_folder(folder_id, response_type='json')
    return flasktools.json_response(folder.jsonify())


@site.route('/folder/<folder_id>/messages.json')
@flasktools.cached_endpoint(max_age=0, etag_function=lambda: common.bringdb.last_commit_id, max_urls=100)
def get_folder_messages_json(folder_id):
    folder = common.get_folder(folder_id, response_type='json')
    return _message_list_response(folder=folder)


@site.route('/account/<account_id>/messages.json')
@flasktools.cached_endpoint(max_age=0, etag_function=lambda: common.bringdb.last_commit_id, max_urls=50)
def get_account_messages_json(account_id):
    account = common.get_account(account_id, response_type='json')
    return _message_list_response(account=account)


@site.route('/all/messages.json')
@flasktools.cached_endpoint(max_age=0, etag_function=lambda: common.bringdb.last_commit_id, max_urls=10)
def get_all_messages_json():
    return _message_list_response(all_accounts=True)


@site.route('/folder/<folder_id>/refresh', methods=['POST'])
def post_folder_refresh(folder_id):
    folder = common.get_folder(folder_id, response_type='json')
    if not folder.is_selectable:
        return flasktools.json_response({}, status=400)
    common.add_folder_to_refresh_queue(folder, manual=True)
    return flasktools.json_response({})


@site.route('/folder/<folder_id>/set_sync_enabled', methods=['POST'])
@flasktools.required_fields(['sync_enabled'])
def post_folder_set_sync_enabled(folder_id):
    folder = common.get_folder(folder_id, response_type='json')
    try:
        sync_enabled = stringtools.truthystring(request.form['sync_enabled'])
    except ValueError:
        return flasktools.json_response({}, status=400)
    with common.bringdb.transaction:
        folder.set_sync_enabled(sync_enabled)
    return flasktools.json_response(folder.jsonify())
