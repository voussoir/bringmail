import dataclasses
import datetime
import json
import math
import typing

from . import constants
from . import exceptions
from . import helpers
from . import outbox

from voussoirkit import sentinel
from voussoirkit import vlogging
from voussoirkit import worms

log = vlogging.get_logger(__name__)


def _row_get(row, key, default=None):
    try:
        return row[key]
    except (KeyError, IndexError):
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


def move_message_on_server(client, *, source_name, destination_name, uid, message_id=None):
    '''
    Move one message between two folders on an already-authenticated client.

    This is shared by the direct move path (Message.move_to, used by the CLI
    and REPL) and by the outbox flush. Returns (new_uid, new_uidvalidity,
    uid_source), where uid_source is 'copyuid' or 'search'. new_uid may be
    None when the new UID could not be discovered.
    '''
    client.select_folder(source_name, readonly=False)

    # A move/copy assigns the message a new UID in the destination folder.
    # UIDPLUS servers report that UID in a COPYUID response code, which lets
    # the caller follow the message directly.
    before_ok = helpers.untagged_ok_responses(client)
    before_copyuid = helpers.untagged_copyuid_responses(client)
    try:
        response = client.move([uid], destination_name)
    except Exception:
        response = client.copy([uid], destination_name)
        copyuid = helpers.copyuid_from_response(
            client,
            response,
            before=before_ok,
            before_copyuid=before_copyuid,
        )
        client.add_flags([uid], [b'\\Deleted'])
        client.expunge([uid])
    else:
        copyuid = helpers.copyuid_from_response(
            client,
            response,
            before=before_ok,
            before_copyuid=before_copyuid,
        )

    if copyuid is not None:
        (new_uidvalidity, uid_mapping) = copyuid
        moved_uid = uid_mapping.get(uid)
        if moved_uid is not None:
            log.debug(
                'COPYUID for uid %s -> "%s": destination uid %s, '
                'destination uidvalidity %s.',
                uid,
                destination_name,
                moved_uid,
                new_uidvalidity,
            )
            return (moved_uid, new_uidvalidity, 'copyuid')
    else:
        log.debug(
            'The move response for uid %s to "%s" did not include a '
            'usable COPYUID: %r',
            uid,
            destination_name,
            response,
        )
        try:
            capabilities = client.capabilities()
        except Exception:
            capabilities = ()
        log.debug('IMAP capabilities: %s', capabilities)

    # Servers without UIDPLUS (or a usable COPYUID) require a search in the
    # destination folder to discover the new UID.
    new_uid = None
    new_uidvalidity = None
    if not message_id:
        log.warning(
            'Message uid %s has no Message-ID header; cannot search for its '
            'new UID in "%s".',
            uid,
            destination_name,
        )
    else:
        try:
            selected = client.select_folder(destination_name, readonly=False)
            new_uidvalidity = selected.get(b'UIDVALIDITY')
            found = [
                int(found_uid)
                for found_uid in client.search(['HEADER', 'Message-ID', message_id])
            ]
            log.debug(
                'Message-ID search for uid %s in "%s" returned UIDs %s.',
                uid,
                destination_name,
                found,
            )
            if len(found) == 1:
                new_uid = found[0]
            elif len(found) > 1:
                # A new copy is assigned the highest UID in the mailbox. If
                # the destination already held another copy of this message,
                # the moved copy is the newest.
                new_uid = max(found)
                log.warning(
                    'Multiple copies of uid %s match in "%s"; '
                    'using the newest UID %s.',
                    uid,
                    destination_name,
                    new_uid,
                )
        except Exception:
            log.warning(
                'Could not search for uid %s in "%s".',
                uid,
                destination_name,
                exc_info=True,
            )
    return (new_uid, new_uidvalidity, 'search')


class ObjectBase(worms.Object):
    def __init__(self, bringmaildb):
        super().__init__(bringmaildb)
        self.bringmaildb = bringmaildb


class Account(ObjectBase):
    table = 'accounts'
    no_such_exception = exceptions.NoSuchAccount

    def __init__(self, bringmaildb, db_row):
        super().__init__(bringmaildb)

        self.id = db_row['id']
        self.label = db_row['label']
        self.host = db_row['host']
        self.port = db_row['port']
        self.ssl = bool(db_row['ssl'])
        self.starttls = bool(db_row['starttls'])
        self.username = db_row['username']
        self.password = db_row['password']
        self.autorefresh_interval = db_row['autorefresh_interval']
        self.ui_order_rank = db_row['ui_order_rank']
        self.created = db_row['created']
        self.last_refresh = db_row['last_refresh']
        self.last_refresh_attempt = db_row['last_refresh_attempt']
        self.last_refresh_error = db_row['last_refresh_error']

    def __repr__(self):
        return f'Account:{self.id}:{self.display_name}'

    @property
    def display_name(self):
        return self.label or self.username or self.host or str(self.id)

    @staticmethod
    def normalize_autorefresh_interval(autorefresh_interval):
        return helpers.normalize_int(autorefresh_interval, minimum=0)

    @staticmethod
    def normalize_host(host):
        return helpers.normalize_string_not_blank(host)

    normalize_label = staticmethod(helpers.normalize_string_blank_to_none)

    @staticmethod
    def normalize_password(password):
        if password is None:
            raise ValueError('password is required')
        if not isinstance(password, str):
            raise TypeError(password)
        if password == '':
            raise ValueError('password is required')
        return password

    @staticmethod
    def normalize_port(port):
        return helpers.normalize_int(port, minimum=1, maximum=65535)

    normalize_ssl = staticmethod(helpers.normalize_bool)
    normalize_starttls = staticmethod(helpers.normalize_bool)

    normalize_username = staticmethod(helpers.normalize_string_not_blank)

    @staticmethod
    def normalize_ui_order_rank(ui_order_rank):
        value = helpers.normalize_int(ui_order_rank, minimum=0)
        return float(value)

    @worms.atomic
    def delete(self):
        self.assert_not_deleted()
        log.info('Deleting %s.', self)
        self.bringmaildb.delete(table=Account, pairs={'id': self.id})
        self.bringmaildb.clear_object_caches()
        self.deleted = True

    def get_folders(self):
        return self.bringmaildb.get_folders(account=self)

    @staticmethod
    def _aggregate_folders_from(folders):
        '''
        The account-level view is the union of INBOX and SENT and their
        descendants. Junk, Trash, Archive, and unrelated folders are excluded.
        '''
        roots = {}
        for folder in folders:
            special_use = folder.special_use
            if not special_use:
                special_use = helpers.folder_special_use(folder.attributes, folder.name)
            if special_use in (constants.SPECIAL_USE_INBOX, constants.SPECIAL_USE_SENT):
                roots.setdefault(special_use, folder)

        if not roots:
            return []

        selected = []
        seen = set()
        for folder in folders:
            for root in roots.values():
                if folder.id == root.id:
                    in_subtree = True
                else:
                    delimiter = root.delimiter or folder.delimiter
                    in_subtree = bool(
                        delimiter and
                        folder.name.startswith(root.name + delimiter)
                    )
                if in_subtree:
                    if folder.id not in seen:
                        seen.add(folder.id)
                        selected.append(folder)
                    break
        return selected

    def get_aggregate_folders(self):
        return self._aggregate_folders_from(self.get_folders())

    def get_trash_folder(self):
        for folder in self.get_folders():
            if folder.special_use == constants.SPECIAL_USE_TRASH:
                return folder
        return None

    def get_unread_count(self):
        folder_ids = [folder.id for folder in self.get_aggregate_folders()]
        if not folder_ids:
            return 0
        placeholders = ','.join('?' for _ in folder_ids)
        query = f'''
        SELECT COUNT(id) FROM messages
        WHERE folder_id IN ({placeholders}) AND seen == 0 AND imap_deleted == 0
        '''
        return self.bringmaildb.select_one_value(query, folder_ids, fallback=0)

    @property
    def next_refresh(self):
        if self.autorefresh_interval < 1:
            return float('inf')
        return self.last_refresh_attempt + self.autorefresh_interval

    def jsonify(self, *, folders=False, unread_count=False, pending_count=False):
        self.assert_not_deleted()
        j = {
            'type': 'account',
            'id': self.id,
            'label': self.label,
            'display_name': self.display_name,
            'host': self.host,
            'port': self.port,
            'ssl': self.ssl,
            'starttls': self.starttls,
            'username': self.username,
            'autorefresh_interval': self.autorefresh_interval,
            'ui_order_rank': self.ui_order_rank,
            'created': self.created,
            'last_refresh': self.last_refresh,
            'last_refresh_attempt': self.last_refresh_attempt,
            'last_refresh_error': self.last_refresh_error,
            'next_refresh': self.next_refresh if self.next_refresh != float('inf') else None,
        }
        if pending_count:
            summary = self.bringmaildb.get_pending_summary(self)
            j['pending_count'] = summary['pending']
            j['conflict_count'] = summary['conflicted'] + summary['failed']
        if folders or unread_count:
            account_folders = self.get_folders()
            counts = self.bringmaildb.get_account_unread_counts(self)
        if unread_count:
            aggregate_ids = {
                folder.id
                for folder in self._aggregate_folders_from(account_folders)
            }
            j['unread_count'] = sum(
                counts.get(folder_id, 0) for folder_id in aggregate_ids
            )
        if folders:
            j['folders'] = [
                folder.jsonify(
                    unread_count=counts.get(folder.id, 0),
                    message_count=self.bringmaildb.get_folder_message_count(folder),
                )
                for folder in account_folders
            ]
        return j

    def refresh(self, progress=None):
        '''
        Synchronize this account with the mail server. Returns None on success
        or the traceback string on failure.
        '''
        self.assert_not_deleted()
        return self.bringmaildb.sync_account(self, progress=progress)

    def test_connection(self):
        '''
        Connect, authenticate, and list folders without modifying the cache.
        Returns the number of folders found.
        '''
        self.assert_not_deleted()
        with helpers.imap_connection(self) as client:
            folders = client.list_folders()
        return len(folders)

    @worms.atomic
    def set_autorefresh_interval(self, autorefresh_interval):
        self.assert_not_deleted()
        autorefresh_interval = self.normalize_autorefresh_interval(autorefresh_interval)
        pairs = {'id': self.id, 'autorefresh_interval': autorefresh_interval}
        self.bringmaildb.update(table=Account, pairs=pairs, where_key='id')
        self.autorefresh_interval = autorefresh_interval

    @worms.atomic
    def set_label(self, label):
        self.assert_not_deleted()
        label = self.normalize_label(label)
        pairs = {'id': self.id, 'label': label}
        self.bringmaildb.update(table=Account, pairs=pairs, where_key='id')
        self.label = label

    @worms.atomic
    def set_password(self, password):
        self.assert_not_deleted()
        password = self.normalize_password(password)
        pairs = {'id': self.id, 'password': password}
        self.bringmaildb.update(table=Account, pairs=pairs, where_key='id')
        self.password = password

    @worms.atomic
    def set_server(self, *, host, port, ssl, starttls):
        self.assert_not_deleted()
        host = self.normalize_host(host)
        port = self.normalize_port(port)
        ssl = self.normalize_ssl(ssl)
        starttls = self.normalize_starttls(starttls)
        pairs = {
            'id': self.id,
            'host': host,
            'port': port,
            'ssl': ssl,
            'starttls': starttls,
        }
        self.bringmaildb.update(table=Account, pairs=pairs, where_key='id')
        self.host = host
        self.port = port
        self.ssl = ssl
        self.starttls = starttls

    @worms.atomic
    def set_username(self, username):
        self.assert_not_deleted()
        username = self.normalize_username(username)
        pairs = {'id': self.id, 'username': username}
        self.bringmaildb.update(table=Account, pairs=pairs, where_key='id')
        self.username = username


class Folder(ObjectBase):
    table = 'folders'
    no_such_exception = exceptions.NoSuchFolder

    def __init__(self, bringmaildb, db_row):
        super().__init__(bringmaildb)

        self.id = db_row['id']
        self.account_id = db_row['account_id']
        self.name = helpers.decode_text(db_row['name'])
        self.display_name = helpers.decode_text(db_row['display_name'])
        self.delimiter = helpers.decode_text(db_row['delimiter'])
        self.attributes = [
            helpers.decode_text(attribute)
            for attribute in _loads_json(db_row['attributes'], [])
        ]
        self.special_use = helpers.decode_text(db_row['special_use'])
        self.uidvalidity = db_row['uidvalidity']
        self.uidnext = db_row['uidnext']
        self.highestmodseq = db_row['highestmodseq']
        self.last_sync = db_row['last_sync']
        self.last_sync_error = db_row['last_sync_error']
        self.sync_enabled = bool(db_row['sync_enabled'])
        self.ui_order_rank = db_row['ui_order_rank']

        self._account = None

    def __repr__(self):
        return f'Folder:{self.id}:{self.name}'

    @property
    def account(self):
        if self._account is None:
            self._account = self.bringmaildb.get_account(self.account_id)
        return self._account

    @property
    def is_selectable(self):
        return '\\Noselect' not in self.attributes

    @property
    def label(self):
        return self.display_name or self.name

    @property
    def depth(self):
        return helpers.folder_depth(self.name, self.delimiter)

    def get_message_count(self):
        return self.bringmaildb.get_folder_message_count(self)

    def get_messages(self, *, limit=constants.DEFAULT_PAGE_SIZE, before_date=None, before_id=None):
        return self.bringmaildb.get_messages(
            folder=self,
            limit=limit,
            before_date=before_date,
            before_id=before_id,
        )

    def get_unread_count(self):
        return self.bringmaildb.get_folder_unread_count(self)

    def jsonify(self, *, unread_count=None, message_count=None):
        self.assert_not_deleted()
        if unread_count is None:
            unread_count = self.get_unread_count()
        if message_count is None:
            message_count = self.get_message_count()
        return {
            'type': 'folder',
            'id': self.id,
            'account_id': self.account_id,
            'name': self.name,
            'display_name': self.label,
            'delimiter': self.delimiter,
            'depth': self.depth,
            'special_use': self.special_use,
            'uidvalidity': self.uidvalidity,
            'uidnext': self.uidnext,
            'last_sync': self.last_sync,
            'last_sync_error': self.last_sync_error,
            'sync_enabled': self.sync_enabled,
            'selectable': self.is_selectable,
            'unread_count': unread_count,
            'message_count': message_count,
        }

    @worms.atomic
    def set_sync_enabled(self, sync_enabled):
        self.assert_not_deleted()
        sync_enabled = helpers.normalize_bool(sync_enabled)
        pairs = {'id': self.id, 'sync_enabled': sync_enabled}
        self.bringmaildb.update(table=Folder, pairs=pairs, where_key='id')
        self.sync_enabled = sync_enabled


@dataclasses.dataclass(frozen=True)
class MoveToAccountResult:
    '''
    The outcome of queuing a cross-account transfer for one message.
    '''
    queued: bool
    copy_only: bool
    operation: str


class Message(ObjectBase):
    table = 'messages'
    no_such_exception = exceptions.NoSuchMessage

    def __init__(self, bringmaildb, db_row):
        super().__init__(bringmaildb)

        self.id = db_row['id']
        self.account_id = db_row['account_id']
        self.folder_id = db_row['folder_id']
        self.uid = db_row['uid']
        self.uidvalidity = db_row['uidvalidity']
        self.raw = db_row['raw']
        self.raw_codec = db_row['raw_codec']
        self.body_fetched = bool(db_row['body_fetched'])
        self.meta = _loads_json(db_row['meta'], {})
        self.meta_version = db_row['meta_version']
        self.seen = bool(db_row['seen'])
        self.flagged = bool(db_row['flagged'])
        self.answered = bool(db_row['answered'])
        self.imap_deleted = bool(db_row['imap_deleted'])
        self.draft = bool(db_row['draft'])
        self.keywords = _loads_json(db_row['keywords'], [])
        self.modseq = db_row['modseq']
        self.size = db_row['size']
        self.local_deleted = bool(_row_get(db_row, 'local_deleted', 0))

        self.subject = _row_get(db_row, 'subject', self.meta.get('subject'))
        self.from_name = _row_get(db_row, 'from_name', None)
        self.from_addr = _row_get(db_row, 'from_addr', None)
        self.date_unix = _row_get(db_row, 'date_unix', self.meta.get('date_unix'))
        self.message_id = _row_get(db_row, 'message_id', self.meta.get('message_id'))
        self.has_attachments = bool(
            _row_get(db_row, 'has_attachments', self.meta.get('has_attachments'))
        )

        self._account = None
        self._folder = None

    def __repr__(self):
        return f'Message:{self.id}:{self.subject}'

    @property
    def account(self):
        if self._account is None:
            self._account = self.bringmaildb.get_account(self.account_id)
        return self._account

    @property
    def folder(self):
        if self._folder is None:
            self._folder = self.bringmaildb.get_folder(self.folder_id)
        return self._folder

    @property
    def from_display(self):
        if self.from_name:
            return self.from_name
        if self.from_addr:
            return self.from_addr
        return ''

    @property
    def display_subject(self):
        return self.subject or '(no subject)'

    @property
    def date_datetime(self):
        if self.date_unix is None:
            return None
        return datetime.datetime.fromtimestamp(self.date_unix, datetime.timezone.utc)

    def get_attachment(self, part_index):
        self.assert_not_deleted()
        if not self.body_fetched or self.raw is None:
            raise exceptions.MessageNotCached(self.id)
        raw = self.bringmaildb.decompress_raw(self.raw, self.raw_codec)
        return helpers.part_payload(raw, part_index)

    def get_body(self):
        self.assert_not_deleted()
        if not self.body_fetched or self.raw is None:
            raise exceptions.MessageNotCached(self.id)
        raw = self.bringmaildb.decompress_raw(self.raw, self.raw_codec)
        body = helpers.body_for_message(raw)
        body.update({
            'type': 'message_body',
            'id': self.id,
            'account_id': self.account_id,
            'folder_id': self.folder_id,
            'subject': self.display_subject,
            'from': self.meta.get('from', []),
            'to': self.meta.get('to', []),
            'cc': self.meta.get('cc', []),
            'date_unix': self.date_unix,
            'message_id': self.message_id,
            'seen': self.seen,
            'flagged': self.flagged,
            'body_fetched': self.body_fetched,
        })
        return body

    def get_headers_text(self):
        self.assert_not_deleted()
        if not self.body_fetched or self.raw is None:
            raise exceptions.MessageNotCached(self.id)
        raw = self.bringmaildb.decompress_raw(self.raw, self.raw_codec)
        (message, meta) = helpers.parse_full_message(raw)
        return helpers.headers_text(message)

    def jsonify(self, *, pending=None):
        self.assert_not_deleted()
        return {
            'type': 'message',
            'id': self.id,
            'account_id': self.account_id,
            'folder_id': self.folder_id,
            'uid': self.uid,
            'subject': self.display_subject,
            'from': self.meta.get('from', []),
            'from_name': self.from_name,
            'from_addr': self.from_addr,
            'from_display': self.from_display,
            'to': self.meta.get('to', []),
            'cc': self.meta.get('cc', []),
            'date_unix': self.date_unix,
            'message_id': self.message_id,
            'size': self.size,
            'snippet': self.meta.get('snippet', ''),
            'seen': self.seen,
            'flagged': self.flagged,
            'answered': self.answered,
            'deleted': self.imap_deleted,
            'draft': self.draft,
            'has_attachments': self.has_attachments,
            'attachment_count': len(self.meta.get('attachments', [])),
            'body_fetched': self.body_fetched,
            'local_deleted': self.local_deleted,
            'pending': pending,
        }

    def _set_local_flag(self, column, value):
        with self.bringmaildb.transaction:
            pairs = {'id': self.id, column: value}
            self.bringmaildb.update(table=Message, pairs=pairs, where_key='id')
            setattr(self, column, value)

    def _imap_flag(self, flag, enabled):
        account = self.account
        folder = self.folder
        if not folder.is_selectable:
            raise exceptions.FolderNotSelectable(folder.name)

        def operation(client):
            client.select_folder(folder.name, readonly=False)
            if enabled:
                client.add_flags([self.uid], [flag])
            else:
                client.remove_flags([self.uid], [flag])

        try:
            helpers.imap_call(account, operation)
        except exceptions.BringException:
            raise
        except Exception as exc:
            raise exceptions.MailServerActionError(str(exc))

    # Pending (offline) queue ######################################################################

    def _flag_snapshot(self):
        '''
        The pre-state recorded with a queued operation. The flush compares
        the server against this to decide whether the user's action is still
        meaningful.
        '''
        return {
            'seen': bool(self.seen),
            'flagged': bool(self.flagged),
            'answered': bool(self.answered),
            'keywords': list(self.keywords or []),
            'modseq': self.modseq,
            'folder_id': self.folder_id,
            'uid': self.uid,
        }

    def _apply_local(self, pairs):
        '''
        Update the local row and this instance. The caller must already hold
        a database transaction.
        '''
        data = {'id': self.id}
        data.update(pairs)
        self.bringmaildb.update(table=Message, pairs=data, where_key='id')
        for (key, value) in pairs.items():
            setattr(self, key, value)

    def _pending_folder_operations(self):
        return self.bringmaildb.get_pending_for_message(
            self.id,
            states=(constants.PENDING_STATE_PENDING,),
        )

    def _virtual_folder_id(self):
        return outbox.virtual_folder_id(
            self.folder_id,
            self._pending_folder_operations(),
        )

    def _queue_flag(self, operation, enabled):
        if operation not in constants.OPERATION_PRIVATE_FLAGS:
            raise ValueError(operation)
        enabled = helpers.normalize_bool(enabled)
        current = bool(getattr(self, operation))
        if enabled == current:
            return False

        self.assert_not_deleted()
        folder = self.folder
        if not folder.is_selectable:
            raise exceptions.FolderNotSelectable(folder.name)

        snapshot = self._flag_snapshot()
        with self.bringmaildb.transaction:
            self.bringmaildb.add_pending_operation(
                account=self.account,
                folder=folder,
                operation=operation,
                message=self,
                enabled=enabled,
                base_folder_id=self.folder_id,
                observed=snapshot,
                base_modseq=self.modseq,
            )
            self._apply_local({operation: enabled})
        return True

    def queue_seen(self, seen):
        return self._queue_flag(constants.OPERATION_SEEN, seen)

    def queue_flagged(self, flagged):
        return self._queue_flag(constants.OPERATION_FLAGGED, flagged)

    def queue_answered(self, answered):
        return self._queue_flag(constants.OPERATION_ANSWERED, answered)

    def queue_keywords(self, keywords):
        '''
        Queue an exact keyword (custom flag) set. Keywords are shared
        metadata, so the flush uses a conditional write when it can.
        '''
        self.assert_not_deleted()
        target = sorted({str(word) for word in (keywords or [])})
        current = sorted({str(word) for word in (self.keywords or [])})
        if target == current:
            return False

        folder = self.folder
        if not folder.is_selectable:
            raise exceptions.FolderNotSelectable(folder.name)

        snapshot = self._flag_snapshot()
        with self.bringmaildb.transaction:
            self.bringmaildb.add_pending_operation(
                account=self.account,
                folder=folder,
                operation=constants.OPERATION_KEYWORDS,
                message=self,
                target_keywords=target,
                base_folder_id=self.folder_id,
                observed=snapshot,
                base_modseq=self.modseq,
            )
            self._apply_local({
                'keywords': json.dumps(target) if target else None,
            })
        self.keywords = target
        return True

    def queue_move(self, folder):
        '''
        Queue a move without touching the server or the local folder. The
        row keeps its current UID until the flush learns the destination UID
        from COPYUID (or a Message-ID search).

        A destination in another account becomes a cross-account transfer:
        the message is copied with APPEND and then the source is moved to
        Trash when that is possible.
        '''
        self.assert_not_deleted()
        if not isinstance(folder, Folder):
            raise TypeError(folder)
        folder.assert_not_deleted()
        if not folder.is_selectable:
            raise exceptions.FolderNotSelectable(folder.name)
        if folder.account_id != self.account_id:
            return self.queue_move_to_account(folder).queued

        base_folder_id = self._virtual_folder_id()
        if folder.id == base_folder_id:
            return False

        snapshot = self._flag_snapshot()
        with self.bringmaildb.transaction:
            self.bringmaildb.add_pending_operation(
                account=self.account,
                folder=self.folder,
                operation=constants.OPERATION_MOVE,
                message=self,
                base_folder_id=base_folder_id,
                target_folder_id=folder.id,
                observed=snapshot,
                base_modseq=self.modseq,
            )
        return True

    def queue_move_to_account(self, folder):
        '''
        Queue a transfer to a folder in another account.

        The source is not changed locally at enqueue time. The flush copies
        the raw message with APPEND and only then moves the source to Trash.
        When the source account has no usable Trash folder, the operation is
        copy-only and the source is left in place.
        '''
        self.assert_not_deleted()
        if not isinstance(folder, Folder):
            raise TypeError(folder)
        folder.assert_not_deleted()
        if not folder.is_selectable:
            raise exceptions.FolderNotSelectable(folder.name)

        if folder.account_id == self.account_id:
            queued = self.queue_move(folder)
            return MoveToAccountResult(
                queued=queued,
                copy_only=False,
                operation=constants.OPERATION_MOVE,
            )

        source = self.folder
        if not source.is_selectable:
            raise exceptions.FolderNotSelectable(source.name)

        pending = self._pending_folder_operations()
        if any(operation.is_cross_account for operation in pending):
            raise exceptions.InvalidFolder(
                'A cross-account transfer is already queued for this message.'
            )

        base_folder_id = self._virtual_folder_id()
        trash = self.account.get_trash_folder()
        copy_only = (
            trash is None or
            not trash.is_selectable or
            base_folder_id == trash.id
        )
        operation_kind = (
            constants.OPERATION_COPY_ACCOUNT
            if copy_only
            else constants.OPERATION_MOVE_ACCOUNT
        )

        snapshot = self._flag_snapshot()
        with self.bringmaildb.transaction:
            self.bringmaildb.add_pending_operation(
                account=self.account,
                folder=source,
                operation=operation_kind,
                message=self,
                base_folder_id=base_folder_id,
                target_folder_id=folder.id,
                observed=snapshot,
                base_modseq=self.modseq,
            )
        return MoveToAccountResult(
            queued=True,
            copy_only=copy_only,
            operation=operation_kind,
        )

    def queue_delete(self):
        '''
        Queue a delete, which is a move to the Trash folder. The local row
        stays where it is and is marked local_deleted so the UI can show it
        struck through and offer an undo.
        '''
        self.assert_not_deleted()
        trash = self.account.get_trash_folder()
        if trash is None:
            raise exceptions.TrashNotFound()

        base_folder_id = self._virtual_folder_id()
        if base_folder_id == trash.id:
            return False

        snapshot = self._flag_snapshot()
        with self.bringmaildb.transaction:
            self.bringmaildb.add_pending_operation(
                account=self.account,
                folder=self.folder,
                operation=constants.OPERATION_DELETE,
                message=self,
                base_folder_id=base_folder_id,
                target_folder_id=trash.id,
                observed=snapshot,
                base_modseq=self.modseq,
            )
            self._apply_local({'local_deleted': True})
        return True

    def queue_restore(self):
        '''
        Queue the inverse of a delete. While the delete is still pending the
        two cancel; after the delete has reached the server this becomes a
        real move back to the original folder.
        '''
        self.assert_not_deleted()
        trash = self.account.get_trash_folder()
        if trash is None:
            raise exceptions.TrashNotFound()

        delete_operation = self.bringmaildb.get_latest_delete_operation(self.id)
        if delete_operation is None or delete_operation.base_folder_id is None:
            raise exceptions.NothingToRestore()

        target_folder_id = delete_operation.base_folder_id
        base_folder_id = self._virtual_folder_id()
        if base_folder_id == target_folder_id and not self.local_deleted:
            return False
        if not self.local_deleted and self.folder_id != trash.id:
            raise exceptions.NothingToRestore()

        target = self.bringmaildb.get_folder(target_folder_id)
        if not target.is_selectable:
            raise exceptions.FolderNotSelectable(target.name)

        snapshot = self._flag_snapshot()
        with self.bringmaildb.transaction:
            self.bringmaildb.add_pending_operation(
                account=self.account,
                folder=self.folder,
                operation=constants.OPERATION_RESTORE,
                message=self,
                base_folder_id=base_folder_id,
                target_folder_id=target_folder_id,
                observed=snapshot,
                base_modseq=self.modseq,
            )
            self._apply_local({'local_deleted': False})
        return True

    def move_to(self, folder):
        self.assert_not_deleted()
        if not isinstance(folder, Folder):
            raise TypeError(folder)
        folder.assert_not_deleted()
        if folder.account_id != self.account_id:
            raise exceptions.InvalidFolder('Cannot move a message to another account.')
        if not folder.is_selectable:
            raise exceptions.FolderNotSelectable(folder.name)
        if folder.id == self.folder_id:
            return

        account = self.account
        source = self.folder
        previous_uid = self.uid
        previous_uidvalidity = self.uidvalidity

        def operation(client):
            return move_message_on_server(
                client,
                source_name=source.name,
                destination_name=folder.name,
                uid=self.uid,
                message_id=self.message_id,
            )

        try:
            (new_uid, new_uidvalidity, uid_source) = helpers.imap_call(account, operation)
        except exceptions.BringException:
            raise
        except Exception as exc:
            raise exceptions.MailServerActionError(str(exc))

        with self.bringmaildb.transaction:
            if new_uid is not None:
                pairs = {
                    'id': self.id,
                    'folder_id': folder.id,
                    'uid': new_uid,
                    'uidvalidity': new_uidvalidity,
                }
                self.bringmaildb.update(table=Message, pairs=pairs, where_key='id')
                self.folder_id = folder.id
                self.uid = new_uid
                self.uidvalidity = new_uidvalidity
                self._folder = folder
                log.debug(
                    'Moved message %s from "%s" to "%s": uid %s -> %s, '
                    'uidvalidity %s -> %s (uid discovered by %s).',
                    self.id,
                    source.name,
                    folder.name,
                    previous_uid,
                    new_uid,
                    previous_uidvalidity,
                    new_uidvalidity,
                    uid_source,
                )
            else:
                # We could not discover the new UID. Remove the local row and
                # let the next destination-folder sync download it again.
                log.warning(
                    'Could not determine the destination UID for message %s '
                    'after moving it to "%s"; deleting the local row.',
                    self.id,
                    folder.name,
                )
                self.bringmaildb.delete(table=Message, pairs={'id': self.id})
                self.deleted = True
                self.bringmaildb.clear_object_caches()

    def delete(self):
        folder = self.account.get_trash_folder()
        if folder is None:
            raise exceptions.TrashNotFound()
        self.move_to(folder)

    def set_flagged(self, flagged):
        self.assert_not_deleted()
        flagged = helpers.normalize_bool(flagged)
        if flagged == self.flagged:
            return
        self._imap_flag(b'\\Flagged', flagged)
        self._set_local_flag('flagged', flagged)

    def set_seen(self, seen):
        self.assert_not_deleted()
        seen = helpers.normalize_bool(seen)
        if seen == self.seen:
            return
        self._imap_flag(b'\\Seen', seen)
        self._set_local_flag('seen', seen)


