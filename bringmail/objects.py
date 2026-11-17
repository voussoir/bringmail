import datetime
import json
import math
import typing

from . import constants
from . import exceptions
from . import helpers

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

    def jsonify(self, *, folders=False, unread_count=False):
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

    def jsonify(self):
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
            client.select_folder(source.name, readonly=False)

            # A move/copy assigns the message a new UID in the destination
            # folder. UIDPLUS servers report that UID in a COPYUID response
            # code, which lets the local row follow the message directly.
            before_ok = helpers.untagged_ok_responses(client)
            before_copyuid = helpers.untagged_copyuid_responses(client)
            try:
                response = client.move([self.uid], folder.name)
            except Exception:
                response = client.copy([self.uid], folder.name)
                copyuid = helpers.copyuid_from_response(
                    client,
                    response,
                    before=before_ok,
                    before_copyuid=before_copyuid,
                )
                client.add_flags([self.uid], [b'\\Deleted'])
                client.expunge([self.uid])
            else:
                copyuid = helpers.copyuid_from_response(
                    client,
                    response,
                    before=before_ok,
                    before_copyuid=before_copyuid,
                )

            if copyuid is not None:
                (new_uidvalidity, uid_mapping) = copyuid
                moved_uid = uid_mapping.get(self.uid)
                if moved_uid is not None:
                    log.debug(
                        'COPYUID for message %s: source uid %s -> destination uid %s, '
                        'destination uidvalidity %s.',
                        self.id,
                        self.uid,
                        moved_uid,
                        new_uidvalidity,
                    )
                    return (moved_uid, new_uidvalidity, 'copyuid')
            else:
                log.debug(
                    'The move response for message %s to "%s" did not include a '
                    'usable COPYUID: %r',
                    self.id,
                    folder.name,
                    response,
                )
                try:
                    capabilities = client.capabilities()
                except Exception:
                    capabilities = ()
                log.debug('IMAP capabilities: %s', capabilities)

            # Servers without UIDPLUS (or a usable COPYUID) require a search
            # in the destination folder to discover the new UID.
            new_uid = None
            new_uidvalidity = None
            if not self.message_id:
                log.warning(
                    'Message %s has no Message-ID header; cannot search for its '
                    'new UID in "%s".',
                    self.id,
                    folder.name,
                )
            else:
                try:
                    selected = client.select_folder(folder.name, readonly=False)
                    new_uidvalidity = selected.get(b'UIDVALIDITY')
                    found = [
                        int(uid)
                        for uid in client.search(['HEADER', 'Message-ID', self.message_id])
                    ]
                    log.debug(
                        'Message-ID search for message %s in "%s" returned UIDs %s.',
                        self.id,
                        folder.name,
                        found,
                    )
                    if len(found) == 1:
                        new_uid = found[0]
                    elif len(found) > 1:
                        # A new copy is assigned the highest UID in the
                        # mailbox. If the destination already held another
                        # copy of this message, the moved copy is the newest.
                        new_uid = max(found)
                        log.warning(
                            'Multiple copies of message %s match in "%s"; '
                            'using the newest UID %s.',
                            self.id,
                            folder.name,
                            new_uid,
                        )
                except Exception:
                    log.warning(
                        'Could not search for message %s in "%s".',
                        self.id,
                        folder.name,
                        exc_info=True,
                    )
            return (new_uid, new_uidvalidity, 'search')

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


