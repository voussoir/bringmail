'''
Pending (offline) operation queue helpers.

This module contains the parts of the offline queue that do not touch the
network or the database: describing queued rows, choosing which of several
queued operations for the same message actually need to be sent, and the
backoff schedule. Keeping these rules pure makes the cancellation and
supersede behaviour testable without an IMAP server.

A queued row records the operation the user asked for *and* the state they
were looking at when they asked (the `observed` snapshot). Consolidation uses
that snapshot to cancel an operation whose net effect is nothing, and the
flush uses it to decide whether the server has moved on since.
'''
import dataclasses
import json
import typing

from . import constants


def _row_get(row, key, default=None):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _loads_json(value, default):
    if not value:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


@dataclasses.dataclass(frozen=True)
class Operation:
    '''
    A normalized view of one pending_operations row.

    Rows are read as dict-like sqlite rows; this dataclass gives the rest of
    the queue a typed object and normalizes the JSON columns.
    '''
    id: int
    account_id: int
    folder_id: int
    message_id: int | None
    message_uid: int
    message_uidvalidity: int | None
    operation: str
    enabled: bool | None
    base_folder_id: int | None
    target_folder_id: int | None
    target_keywords: list[str]
    base_modseq: int | None
    observed: dict
    state: str
    attempts: int
    next_attempt: int
    last_error: str | None
    created: int
    updated: int

    @classmethod
    def from_row(cls, row):
        keywords = _loads_json(_row_get(row, 'target_keywords'), [])
        if isinstance(keywords, str):
            keywords = [keywords]
        return cls(
            id=int(_row_get(row, 'id')),
            account_id=int(_row_get(row, 'account_id')),
            folder_id=int(_row_get(row, 'folder_id')),
            message_id=(
                int(_row_get(row, 'message_id'))
                if _row_get(row, 'message_id') is not None
                else None
            ),
            message_uid=int(_row_get(row, 'message_uid')),
            message_uidvalidity=(
                int(_row_get(row, 'message_uidvalidity'))
                if _row_get(row, 'message_uidvalidity') is not None
                else None
            ),
            operation=str(_row_get(row, 'operation')),
            enabled=(
                bool(_row_get(row, 'enabled'))
                if _row_get(row, 'enabled') is not None
                else None
            ),
            base_folder_id=(
                int(_row_get(row, 'base_folder_id'))
                if _row_get(row, 'base_folder_id') is not None
                else None
            ),
            target_folder_id=(
                int(_row_get(row, 'target_folder_id'))
                if _row_get(row, 'target_folder_id') is not None
                else None
            ),
            target_keywords=[str(word) for word in keywords or []],
            base_modseq=(
                int(_row_get(row, 'base_modseq'))
                if _row_get(row, 'base_modseq') is not None
                else None
            ),
            observed=_loads_json(_row_get(row, 'observed'), {}),
            state=str(_row_get(row, 'state')),
            attempts=int(_row_get(row, 'attempts') or 0),
            next_attempt=int(_row_get(row, 'next_attempt') or 0),
            last_error=_row_get(row, 'last_error'),
            created=int(_row_get(row, 'created') or 0),
            updated=int(_row_get(row, 'updated') or 0),
        )

    @property
    def is_flag(self):
        return self.operation in constants.OPERATION_FLAG_KINDS

    @property
    def is_folder(self):
        return self.operation in constants.OPERATION_FOLDER_KINDS

    @property
    def is_cross_account(self):
        return self.operation in constants.OPERATION_CROSS_ACCOUNT_KINDS

    @property
    def is_private_flag(self):
        return self.operation in constants.OPERATION_PRIVATE_FLAGS

    @property
    def subject_key(self):
        '''
        Identity of the thing this operation acts on.

        message_id is stable across a move, so it is preferred. The
        (folder, uid) fallback exists for rows whose message was already
        removed locally.
        '''
        if self.message_id is not None:
            return ('message', self.message_id)
        return ('uid', self.folder_id, self.message_uid)

    def base_flag_value(self):
        '''
        The flag value the user saw before any queued operation for this
        message. The earliest row's snapshot is authoritative.
        '''
        if self.operation == constants.OPERATION_KEYWORDS:
            return set(str(word) for word in self.observed.get('keywords', []))
        return bool(self.observed.get(self.operation, False))

    def desired_flag_value(self):
        if self.operation == constants.OPERATION_KEYWORDS:
            return set(self.target_keywords)
        return bool(self.enabled)

    def label(self):
        return describe(self.operation, self.enabled)


def describe(operation, enabled=None):
    '''
    A short, user-facing description of a queued operation.
    '''
    if operation == constants.OPERATION_SEEN:
        return 'Marked read' if enabled else 'Marked unread'
    if operation == constants.OPERATION_FLAGGED:
        return 'Flagged' if enabled else 'Unflagged'
    if operation == constants.OPERATION_ANSWERED:
        return 'Marked answered' if enabled else 'Unanswered'
    if operation == constants.OPERATION_KEYWORDS:
        return 'Updated keywords'
    if operation == constants.OPERATION_MOVE:
        return 'Moved'
    if operation == constants.OPERATION_DELETE:
        return 'Deleted'
    if operation == constants.OPERATION_RESTORE:
        return 'Restored'
    if operation == constants.OPERATION_COPY_ACCOUNT:
        return 'Copied to another account'
    if operation == constants.OPERATION_MOVE_ACCOUNT:
        return 'Moved to another account'
    return operation


@dataclasses.dataclass(frozen=True)
class Plan:
    '''
    The result of consolidating the pending rows for one account.

    execute:
        Operations that still need to reach the server, in execution order.

    superseded:
        Rows that must be retired as superseded. This includes rows replaced
        by a later operation on the same subject and rows whose net effect is
        nothing (for example read -> unread -> read).
    '''
    execute: tuple[Operation, ...]
    superseded: tuple[int, ...]

    def all_superseded_ids(self):
        return set(self.superseded)


def _group_by_subject(operations):
    groups = {}
    for operation in operations:
        groups.setdefault(operation.subject_key, []).append(operation)
    for group in groups.values():
        group.sort(key=lambda operation: operation.id)
    return groups


def _consolidate_flags(group):
    '''
    Return (to_execute, superseded_ids) for the flag operations in one group.

    Flag changes are absolute per message, so only the latest value for each
    kind matters. If the latest value equals the value the user saw before
    the first queued operation, the whole stack is cancelled.
    '''
    by_kind = {}
    for operation in group:
        if operation.is_flag:
            by_kind.setdefault(operation.operation, []).append(operation)

    to_execute = []
    superseded = []
    for (kind, rows) in by_kind.items():
        rows.sort(key=lambda operation: operation.id)
        latest = rows[-1]
        base_value = rows[0].base_flag_value()
        desired = latest.desired_flag_value()
        if desired == base_value:
            superseded.extend(operation.id for operation in rows)
            continue
        superseded.extend(operation.id for operation in rows[:-1])
        to_execute.append(latest)
    return (to_execute, superseded)


def _consolidate_folders(group):
    '''
    Return (to_execute, superseded_ids) for the folder operations in one group.

    The same-account chain is simulated from the first operation's base folder.
    Deletions and restores participate because delete is a move to Trash and
    restore is a move back. A move followed by its inverse is cancelled while
    the first one is still unsent; anything longer is kept in order.

    Cross-account operations are opaque to the same-account chain: they do not
    change the local source folder until they are flushed, and they have no
    inverse operation. They are kept and executed in id order with the rest of
    the folder operations.
    '''
    cross_ops = [operation for operation in group if operation.is_cross_account]
    folder_ops = [
        operation for operation in group
        if operation.is_folder and not operation.is_cross_account
    ]
    folder_ops.sort(key=lambda operation: operation.id)
    if not folder_ops:
        return (cross_ops, [])

    simulated_folder = folder_ops[0].base_folder_id
    accepted = []
    superseded = []

    for operation in folder_ops:
        destination = operation.target_folder_id
        if destination is None:
            superseded.append(operation.id)
            continue
        if operation.base_folder_id is not None and operation.base_folder_id != simulated_folder:
            # The chain is inconsistent with what we know. Trust the
            # simulation rather than reordering, and keep the operation.
            simulated_folder = operation.base_folder_id
        if destination == simulated_folder:
            superseded.append(operation.id)
            continue

        if (
                accepted and
                operation.base_folder_id == simulated_folder and
                operation.target_folder_id == accepted[-1].base_folder_id and
                simulated_folder == accepted[-1].target_folder_id
        ):
            # A -> B followed by B -> A with nothing sent in between. Cancel
            # both and return the simulated state to the original folder.
            cancelled = accepted.pop()
            superseded.append(cancelled.id)
            superseded.append(operation.id)
            simulated_folder = cancelled.base_folder_id
            continue

        accepted.append(operation)
        simulated_folder = destination

    return (accepted + cross_ops, superseded)


def consolidate(operations):
    '''
    Turn the unsent rows for one account into a Plan.

    The input must already be filtered to unsent rows; the function does not
    look at the database and does not consider sent rows, so a queued
    operation that has already reached the server can never be cancelled.
    '''
    operations = [
        operation if isinstance(operation, Operation) else Operation.from_row(operation)
        for operation in operations
    ]
    groups = _group_by_subject(operations)
    to_execute = []
    superseded = []
    for group in groups.values():
        (flag_execute, flag_superseded) = _consolidate_flags(group)
        (folder_execute, folder_superseded) = _consolidate_folders(group)
        to_execute.extend(folder_execute)
        to_execute.extend(flag_execute)
        superseded.extend(flag_superseded)
        superseded.extend(folder_superseded)

    # Folder operations first because they change the message's UID, then
    # flags, which are re-targeted at the message's live row anyway.
    to_execute.sort(key=lambda operation: (0 if operation.is_folder else 1, operation.id))
    return Plan(execute=tuple(to_execute), superseded=tuple(superseded))


def backoff_seconds(attempts):
    '''
    Delay before the next retry of a row that has failed `attempts` times.

    A connection failure should retry quickly at first and settle down to a
    slow probe so a server that is down for hours is not hammered.
    '''
    attempts = max(0, int(attempts or 0))
    delay = constants.OUTBOX_RETRY_BASE_SECONDS * (2 ** attempts)
    return int(min(delay, constants.OUTBOX_RETRY_MAX_SECONDS))


def is_expired(operation, now):
    '''
    True when a row has been retried for longer than the queue allows.
    '''
    if operation.state != constants.PENDING_STATE_PENDING:
        return False
    return (now - operation.created) >= constants.OUTBOX_RETRY_EXPIRE_SECONDS


def summarize(operations):
    '''
    Counts for the pending list and the account tree.

    Returns a dict of {state: count} like {'pending': 2, 'conflicted': 1}.
    '''
    counts = {}
    for operation in operations:
        if not isinstance(operation, Operation):
            operation = Operation.from_row(operation)
        counts[operation.state] = counts.get(operation.state, 0) + 1
    return counts


def pending_message_states(operations):
    '''
    Map message_id -> {operation, state} for the newest unsent row per
    message. Used to annotate message lists.
    '''
    states = {}
    for operation in operations:
        if not isinstance(operation, Operation):
            operation = Operation.from_row(operation)
        if operation.message_id is None:
            continue
        if operation.state == constants.PENDING_STATE_SUPERSEDED:
            continue
        states[operation.message_id] = {
            'operation': operation.operation,
            'label': operation.label(),
            'state': operation.state,
        }
    return states


def operation_payload(operation):
    '''
    JSON-ready description of one row for the pending actions endpoint.
    '''
    if not isinstance(operation, Operation):
        operation = Operation.from_row(operation)
    return {
        'type': 'pending_operation',
        'id': operation.id,
        'account_id': operation.account_id,
        'folder_id': operation.folder_id,
        'message_id': operation.message_id,
        'message_uid': operation.message_uid,
        'operation': operation.operation,
        'label': operation.label(),
        'state': operation.state,
        'enabled': operation.enabled,
        'target_folder_id': operation.target_folder_id,
        'base_folder_id': operation.base_folder_id,
        'base_modseq': operation.base_modseq,
        'observed': operation.observed,
        'attempts': operation.attempts,
        'last_error': operation.last_error,
        'created': operation.created,
        'updated': operation.updated,
        'next_attempt': operation.next_attempt,
    }


def virtual_folder_id(current_folder_id, operations):
    '''
    The folder a message is going to be in once every queued folder
    operation has been applied.

    The local row keeps its current folder while a delete is only queued, so
    the enqueue code needs this to chain a restore or a second move. Cross-
    account operations are ignored because the source folder is not changed
    until their flush.
    '''
    folder_id = current_folder_id
    for operation in operations:
        if not isinstance(operation, Operation):
            operation = Operation.from_row(operation)
        if not operation.is_folder or operation.is_cross_account:
            continue
        if operation.base_folder_id is not None and operation.base_folder_id != folder_id:
            folder_id = operation.base_folder_id
        folder_id = operation.target_folder_id
    return folder_id


def flag_value_label(operation):
    '''
    Human wording for a conflict notice.
    '''
    if operation.operation == constants.OPERATION_KEYWORDS:
        return 'keywords'
    return operation.operation
