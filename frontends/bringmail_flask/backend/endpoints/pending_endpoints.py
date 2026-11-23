import flask

from voussoirkit import flasktools

import bringmail

from .. import common

site = common.site

VISIBLE_STATES = (
    bringmail.constants.PENDING_STATE_PENDING,
    bringmail.constants.PENDING_STATE_CONFLICTED,
    bringmail.constants.PENDING_STATE_FAILED,
)

@site.route('/account/<account_id>/pending.json')
def get_account_pending_json(account_id):
    account = common.get_account(account_id, response_type='json')
    operations = common.bringdb.get_pending_operations(
        account=account,
        states=VISIBLE_STATES,
    )
    return flasktools.json_response({
        'type': 'pending_list',
        'account_id': account.id,
        'counts': common.bringdb.get_pending_summary(account),
        'operations': [
            bringmail.outbox.operation_payload(operation)
            for operation in operations
        ],
    })

@site.route('/account/<account_id>/flush', methods=['POST'])
def post_account_flush(account_id):
    account = common.get_account(account_id, response_type='json')
    common.add_flush_to_queue(account, manual=True)
    return flasktools.json_response({
        'type': 'flush_queued',
        'account_id': account.id,
    })

@site.route('/account/<account_id>/pending/discard', methods=['POST'])
def post_account_pending_discard(account_id):
    account = common.get_account(account_id, response_type='json')
    common.add_discard_to_queue(account, manual=True)
    return flasktools.json_response({
        'type': 'discard_queued',
        'account_id': account.id,
    })

@site.route('/pending/<operation_id>/dismiss', methods=['POST'])
def post_pending_dismiss(operation_id):
    operation = common.bringdb.get_pending_operation(operation_id)
    if operation is None:
        return flasktools.json_response({}, status=404)
    if operation.state not in (
            bringmail.constants.PENDING_STATE_CONFLICTED,
            bringmail.constants.PENDING_STATE_FAILED):
        return flasktools.json_response(
            {
                'type': 'error',
                'error_type': 'INVALID_INPUT',
                'error_message': 'Only conflicted or failed actions can be dismissed.',
            },
            status=400,
        )

    with common.bringdb.transaction:
        common.bringdb.set_pending_state(
            operation.id,
            bringmail.constants.PENDING_STATE_SUPERSEDED,
            last_error='Dismissed without retrying.',
        )
    return flasktools.json_response({
        'type': 'pending_operation',
        'id': operation.id,
        'state': bringmail.constants.PENDING_STATE_SUPERSEDED,
    })
