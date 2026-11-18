import datetime
import json
import random
import sqlite3
import traceback
import typing
import zlib

from . import constants
from . import exceptions
from . import helpers
from . import objects
from . import outbox

from voussoirkit import cacheclass
from voussoirkit import pathclass
from voussoirkit import vlogging
from voussoirkit import worms

log = vlogging.get_logger(__name__)

RNG = random.SystemRandom()


def _placeholders(count):
    return '(' + ', '.join('?' * count) + ')'


def _chunks(items, size):
    items = list(items)
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _split_imap_flags(flags):
    '''
    Return (standard_flag_booleans, custom_keyword_list).
    '''
    if flags is None:
        flags = []
    elif isinstance(flags, (bytes, str)):
        flags = [flags]
    decoded = [helpers.decode_flag(flag) for flag in flags]
    standard = {
        'seen': '\\Seen' in decoded,
        'flagged': '\\Flagged' in decoded,
        'answered': '\\Answered' in decoded,
        'imap_deleted': '\\Deleted' in decoded,
        'draft': '\\Draft' in decoded,
    }
    keywords = [
        flag for flag in decoded
        if not flag.startswith('\\') and flag.lower() != '$recent'
    ]
    return (standard, sorted(set(keywords)))


def _response_body_bytes(data):
    for key in (b'BODY[]', b'BODY.PEEK[]'):
        if key in data:
            return data[key]
    for (key, value) in data.items():
        if isinstance(key, bytes) and key.startswith(b'BODY['):
            return value
    return None


def _response_header_bytes(data):
    for (key, value) in data.items():
        if isinstance(key, bytes) and key.startswith(b'BODY[HEADER'):
            return value
    return None


def _response_flag_info(data):
    flags = data.get(b'FLAGS', ())
    if flags is None:
        flags = ()
    (standard, keywords) = _split_imap_flags(flags)
    modseq = data.get(b'MODSEQ')
    if isinstance(modseq, (list, tuple)):
        modseq = modseq[0] if modseq else None
    try:
        modseq = int(modseq) if modseq is not None else None
    except (TypeError, ValueError):
        modseq = None
    return (standard, keywords, modseq)


def _client_has_capability(client, capability):
    '''
    Best-effort capability check for both real and test clients.
    '''
    try:
        return bool(client.has_capability(capability))
    except Exception:
        return False


class BDBAccountMixin:
    def __init__(self):
        super().__init__()

    @worms.atomic
    def add_account(
            self,
            *,
            label=None,
            offline=False,
            host=None,
            password=None,
            port=None,
            autorefresh_interval=constants.DEFAULT_AUTOREFRESH_INTERVAL,
            ssl=True,
            starttls=False,
            username=None,
        ):
        offline = helpers.normalize_bool(offline)

        if offline:
            if any(value is not None for value in (host, port, username, password)):
                raise exceptions.InvalidAccount(
                    'Offline accounts do not have IMAP connection settings.'
                )
            if not isinstance(label, str) or not label.strip():
                raise exceptions.InvalidAccount(
                    'An offline account requires a label.'
                )
            label = label.strip()
            host = None
            port = None
            username = None
            password = None
            ssl = False
            starttls = False
            autorefresh_interval = 0
        else:
            label = objects.Account.normalize_label(label)
            host = objects.Account.normalize_host(host)
            port = objects.Account.normalize_port(port if port is not None else 993)
            ssl = helpers.normalize_bool(ssl)
            starttls = helpers.normalize_bool(starttls)
            username = objects.Account.normalize_username(username)
            password = objects.Account.normalize_password(password)
            autorefresh_interval = objects.Account.normalize_autorefresh_interval(autorefresh_interval)

            existing = self.select_one(
                'SELECT id FROM accounts WHERE host == ? AND port == ? AND username == ?',
                [host, port, username],
            )
            if existing is not None:
                raise exceptions.DuplicateAccount(host=host, port=port, username=username)

        data = {
            'id': self.generate_id(objects.Account),
            'offline': offline,
            'label': label,
            'host': host,
            'port': port,
            'ssl': ssl,
            'starttls': starttls,
            'username': username,
            'password': password,
            'autorefresh_interval': autorefresh_interval,
            'ui_order_rank': self.get_last_account_rank() + 1,
            'created': int(helpers.now()),
            'last_refresh': 0,
            'last_refresh_attempt': 0,
            'last_refresh_error': None,
        }
        self.insert(table=objects.Account, pairs=data)
        account = self.get_cached_instance(
            objects.Account,
            self.select_one('SELECT * FROM accounts WHERE id == ?', [data['id']]),
        )
        if offline:
            self.ensure_offline_folders(account)
        return account

    def ensure_offline_folders(self, account):
        '''
        Ensure an offline account has its fixed Inbox and Trash folders.

        Offline accounts have no server to discover folders from, so this is
        the local equivalent of the folder list. It is idempotent and is
        called both at account creation and by the offline IMAP mock. The
        caller must provide an active transaction.
        '''
        if not isinstance(account, objects.Account):
            raise TypeError(account)
        if not account.offline:
            raise exceptions.InvalidAccount(
                'Only offline accounts have local folders.'
            )

        specs = (
            (
                constants.OFFLINE_INBOX_NAME,
                'Inbox',
                constants.SPECIAL_USE_INBOX,
            ),
            (
                constants.OFFLINE_TRASH_NAME,
                'Trash',
                constants.SPECIAL_USE_TRASH,
            ),
        )
        for (index, (name, display_name, special_use)) in enumerate(specs):
            folder = self.get_folder_by_name(account, name)
            if folder is None:
                folder = self.add_folder(
                    account=account,
                    name=name,
                    display_name=display_name,
                    delimiter=constants.OFFLINE_FOLDER_DELIMITER,
                    attributes=[],
                    special_use=special_use,
                    ui_order_rank=index + 1,
                )

            pairs = {}
            if folder.uidvalidity is None:
                pairs['uidvalidity'] = constants.OFFLINE_UIDVALIDITY
            if folder.uidnext is None:
                pairs['uidnext'] = constants.OFFLINE_UIDNEXT
            if not pairs:
                continue
            pairs['id'] = folder.id
            self.update(table=objects.Folder, pairs=pairs, where_key='id')
            if 'uidvalidity' in pairs:
                folder.uidvalidity = pairs['uidvalidity']
            if 'uidnext' in pairs:
                folder.uidnext = pairs['uidnext']

        return self.get_folders(account)

    def get_account(self, account_id) -> objects.Account:
        return self.get_object_by_id(objects.Account, account_id)

    def get_account_count(self):
        return self.select_one_value('SELECT COUNT(id) FROM accounts', fallback=0)

    def get_accounts(self):
        query = 'SELECT * FROM accounts ORDER BY ui_order_rank ASC, id ASC'
        return self.get_objects_by_sql(objects.Account, query)

    def get_account_unread_counts(self, account):
        query = '''
        SELECT folder_id, COUNT(id) FROM messages
        WHERE account_id == ? AND seen == 0 AND imap_deleted == 0
        GROUP BY folder_id
        '''
        return {row[0]: row[1] for row in self.select(query, [account.id])}

    def get_last_account_rank(self):
        rank = self.select_one_value(
            'SELECT ui_order_rank FROM accounts ORDER BY ui_order_rank DESC LIMIT 1',
            fallback=0,
        )
        return rank or 0

    @worms.atomic
    def reorder_accounts(self, account_ids):
        '''
        Assign each account a contiguous ui_order_rank that matches the given
        order, and return the accounts in that new order.

        account_ids must contain every existing account id exactly once. A
        stale client that does not know about a newer account is rejected
        instead of letting that account silently fall to the bottom of the
        list.
        '''
        account_ids = [int(account_id) for account_id in account_ids]
        if not account_ids:
            raise exceptions.InvalidAccount('No account ids were provided.')
        if len(set(account_ids)) != len(account_ids):
            raise exceptions.InvalidAccount('The account ids contain duplicates.')

        accounts = {account.id: account for account in self.get_accounts()}
        if set(account_ids) != set(accounts):
            raise exceptions.InvalidAccount(
                'The account ids must contain every account exactly once.'
            )

        ordered = [accounts[account_id] for account_id in account_ids]
        for (index, account) in enumerate(ordered):
            account.set_ui_order_rank(index + 1)
        return ordered


class BDBFolderMixin:
    def __init__(self):
        super().__init__()

    @worms.atomic
    def add_folder(
            self,
            *,
            account,
            name,
            display_name=None,
            delimiter=None,
            attributes=None,
            special_use=None,
            ui_order_rank=None,
        ):
        if not isinstance(account, objects.Account):
            raise TypeError(account)
        name = helpers.normalize_string_not_blank(helpers.decode_text(name))
        display_name = helpers.normalize_string_blank_to_none(helpers.decode_text(display_name))
        if delimiter is not None:
            delimiter = helpers.decode_text(delimiter)
        if delimiter == '':
            delimiter = None
        attributes = attributes or []
        if not isinstance(attributes, (list, tuple)):
            raise TypeError(attributes)
        if special_use is not None:
            special_use = helpers.decode_text(special_use) or None
        if ui_order_rank is None:
            ui_order_rank = self.get_last_folder_rank(account) + 1

        data = {
            'id': self.generate_id(objects.Folder),
            'account_id': account.id,
            'name': name,
            'display_name': display_name,
            'delimiter': delimiter,
            'attributes': json.dumps(list(attributes)),
            'special_use': special_use,
            'uidvalidity': None,
            'uidnext': None,
            'highestmodseq': None,
            'last_sync': 0,
            'last_sync_error': None,
            'sync_enabled': True,
            'ui_order_rank': float(ui_order_rank),
        }
        self.insert(table=objects.Folder, pairs=data)
        return self.get_cached_instance(
            objects.Folder,
            self.select_one('SELECT * FROM folders WHERE id == ?', [data['id']]),
        )

    def get_folder(self, folder_id) -> objects.Folder:
        return self.get_object_by_id(objects.Folder, folder_id)

    def get_folder_by_name(self, account, name):
        row = self.select_one(
            'SELECT * FROM folders WHERE account_id == ? AND name == ?',
            [account.id, name],
        )
        if row is None:
            return None
        return self.get_cached_instance(objects.Folder, row)

    def get_folder_count(self):
        return self.select_one_value('SELECT COUNT(id) FROM folders', fallback=0)

    def get_folder_message_count(self, folder):
        query = '''
        SELECT COUNT(id) FROM messages
        WHERE folder_id == ? AND imap_deleted == 0
        '''
        return self.select_one_value(query, [folder.id], fallback=0)

    def get_folder_unread_count(self, folder):
        query = '''
        SELECT COUNT(id) FROM messages
        WHERE folder_id == ? AND seen == 0 AND imap_deleted == 0
        '''
        return self.select_one_value(query, [folder.id], fallback=0)

    def get_folders(self, account=None):
        if account is None:
            query = 'SELECT * FROM folders ORDER BY ui_order_rank ASC, name ASC'
            return self.get_objects_by_sql(objects.Folder, query)

        query = 'SELECT * FROM folders WHERE account_id == ? ORDER BY ui_order_rank ASC, name ASC'
        folders = list(self.get_objects_by_sql(objects.Folder, query, [account.id]))
        folders.sort(key=lambda folder: helpers.folder_sort_key(folder.name, folder.special_use))
        return folders

    def get_last_folder_rank(self, account):
        rank = self.select_one_value(
            'SELECT ui_order_rank FROM folders WHERE account_id == ? ORDER BY ui_order_rank DESC LIMIT 1',
            [account.id],
            fallback=0,
        )
        return rank or 0


class BDBMessageMixin:
    def __init__(self):
        super().__init__()

    def _insert_message(
            self,
            account,
            folder,
            *,
            uid,
            uidvalidity,
            raw,
            meta,
            size=None,
            flags=None,
            keywords=None,
            modseq=None,
        ):
        flags = flags or {}
        if raw is not None:
            compressed = zlib.compress(raw, 6)
            raw_codec = constants.RAW_CODEC_ZLIB
            body_fetched = True
        else:
            compressed = None
            raw_codec = constants.RAW_CODEC_ZLIB
            body_fetched = False

        data = {
            'id': self.generate_id(objects.Message),
            'account_id': account.id,
            'folder_id': folder.id,
            'uid': int(uid),
            'uidvalidity': uidvalidity,
            'raw': compressed,
            'raw_codec': raw_codec,
            'body_fetched': body_fetched,
            'meta': json.dumps(meta, ensure_ascii=False, separators=(',', ':')),
            'meta_version': constants.META_VERSION,
            'seen': bool(flags.get('seen', False)),
            'flagged': bool(flags.get('flagged', False)),
            'answered': bool(flags.get('answered', False)),
            'imap_deleted': bool(flags.get('imap_deleted', False)),
            'draft': bool(flags.get('draft', False)),
            'keywords': json.dumps(keywords) if keywords else None,
            'modseq': modseq,
            'size': size,
        }
        self.insert(table=objects.Message, pairs=data, ignore_duplicate=True)

    def add_message(
            self,
            *,
            account,
            folder,
            uid,
            uidvalidity,
            raw,
            meta,
            size=None,
            flags=None,
            keywords=None,
            modseq=None,
        ):
        self._insert_message(
            account,
            folder,
            uid=uid,
            uidvalidity=uidvalidity,
            raw=raw,
            meta=meta,
            size=size,
            flags=flags,
            keywords=keywords,
            modseq=modseq,
        )
        row = self.select_one(
            'SELECT * FROM messages WHERE folder_id == ? AND uid == ?',
            [folder.id, uid],
        )
        if row is None:
            return None
        return self.get_cached_instance(objects.Message, row)

    def decompress_raw(self, raw, raw_codec):
        if raw is None:
            return None
        if raw_codec == constants.RAW_CODEC_ZLIB:
            return zlib.decompress(raw)
        return raw

    def get_message(self, message_id) -> objects.Message:
        return self.get_object_by_id(objects.Message, message_id)

    def get_message_count(self, folder=None, account=None):
        wheres = []
        bindings = []
        if folder is not None:
            wheres.append('folder_id == ?')
            bindings.append(folder.id)
        if account is not None:
            wheres.append('account_id == ?')
            bindings.append(account.id)
        wheres.append('imap_deleted == 0')
        query = 'SELECT COUNT(id) FROM messages WHERE ' + ' AND '.join(wheres)
        return self.select_one_value(query, bindings, fallback=0)

    def get_messages(
            self,
            *,
            folder=None,
            folder_ids=None,
            account=None,
            limit=constants.DEFAULT_PAGE_SIZE,
            before_date=None,
            before_id=None,
        ):
        limit = helpers.normalize_int(limit, minimum=1, maximum=constants.MAX_PAGE_SIZE)
        wheres = []
        bindings = []

        if folder is not None:
            wheres.append('folder_id == ?')
            bindings.append(folder.id)
        elif folder_ids is not None:
            folder_ids = [int(folder_id) for folder_id in folder_ids]
            if not folder_ids:
                return []
            placeholders = _placeholders(len(folder_ids))
            wheres.append(f'folder_id IN {placeholders}')
            bindings.extend(folder_ids)
        elif account is not None:
            wheres.append('account_id == ?')
            bindings.append(account.id)
        else:
            raise ValueError('Either folder, folder_ids, or account is required.')

        wheres.append('imap_deleted == 0')

        if before_date is not None and before_id is not None:
            wheres.append('(date_unix < ? OR (date_unix == ? AND id < ?))')
            bindings.extend([int(before_date), int(before_date), int(before_id)])

        query = 'SELECT * FROM messages WHERE ' + ' AND '.join(wheres)
        query += ' ORDER BY date_unix DESC, id DESC LIMIT ?'
        bindings.append(limit)
        return [self.get_cached_instance(objects.Message, row) for row in self.select(query, bindings)]

    def get_message_rows_for_folder(self, folder):
        query = 'SELECT * FROM messages WHERE folder_id == ?'
        return list(self.select(query, [folder.id]))

    def delete_messages_for_folder(self, folder):
        self.delete(table=objects.Message, pairs={'folder_id': folder.id})

    def delete_messages_by_uid(self, folder, uids):
        uids = [int(uid) for uid in uids]
        for batch in _chunks(uids, 500):
            qmarks = _placeholders(len(batch))
            query = f'DELETE FROM messages WHERE folder_id == ? AND uid IN {qmarks}'
            self.execute(query, [folder.id] + batch)

    def get_folder_uids(self, folder):
        query = 'SELECT uid FROM messages WHERE folder_id == ?'
        return set(self.select_column(query, [folder.id]))

    def update_message_body(self, message_id, *, raw, meta, size=None, flags=None, keywords=None, modseq=None, update_flags=True):
        flags = flags or {}
        data = {
            'id': message_id,
            'raw': zlib.compress(raw, 6),
            'raw_codec': constants.RAW_CODEC_ZLIB,
            'body_fetched': True,
            'meta': json.dumps(meta, ensure_ascii=False, separators=(',', ':')),
            'meta_version': constants.META_VERSION,
            'size': size,
            'modseq': modseq,
        }
        if update_flags:
            data.update({
                'seen': bool(flags.get('seen', False)),
                'flagged': bool(flags.get('flagged', False)),
                'answered': bool(flags.get('answered', False)),
                'imap_deleted': bool(flags.get('imap_deleted', False)),
                'draft': bool(flags.get('draft', False)),
                'keywords': json.dumps(keywords) if keywords else None,
            })
        self.update(table=objects.Message, pairs=data, where_key='id')

    def get_pending_body_rows(self, folder, limit=100):
        query = '''
        SELECT * FROM messages
        WHERE folder_id == ? AND body_fetched == 0
        ORDER BY uid DESC
        LIMIT ?
        '''
        return list(self.select(query, [folder.id, limit]))

    def get_local_flag_map(self, folder):
        query = '''
        SELECT id, uid, seen, flagged, answered, imap_deleted, draft, keywords, modseq
        FROM messages WHERE folder_id == ?
        '''
        return {row['uid']: row for row in self.select(query, [folder.id])}


class BDBSyncMixin:
    def __init__(self):
        super().__init__()

    def _progress(self, callback, **payload):
        if callback is None:
            return
        try:
            callback(payload)
        except Exception:
            log.debug('Progress callback raised.', exc_info=True)

    def sync_account(self, account, progress=None):
        '''
        Synchronize one account. Returns None on success, or a traceback string
        on failure. Per-folder errors are recorded on the folder and do not
        abort the other folders.
        '''
        account.assert_not_deleted()
        started = int(helpers.now())
        account.last_refresh_attempt = started
        with self.transaction:
            self.update(
                table=objects.Account,
                pairs={'id': account.id, 'last_refresh_attempt': started},
                where_key='id',
            )

        self._progress(progress, stage='account_start', account_id=account.id)
        error = None
        try:
            client = helpers.connect_imap(account)
            try:
                helpers.enable_condstore(client)
                self._sync_account_with_client(client, account, progress=progress)
            finally:
                try:
                    client.logout()
                except Exception:
                    pass
        except Exception:
            error = traceback.format_exc()
            log.warning('Refreshing %s raised:\n%s', account, error)

        finished = int(helpers.now())
        with self.transaction:
            pairs = {
                'id': account.id,
                'last_refresh_attempt': started,
                'last_refresh_error': error,
            }
            if error is None:
                pairs['last_refresh'] = finished
            self.update(table=objects.Account, pairs=pairs, where_key='id')

        account.last_refresh_attempt = started
        account.last_refresh_error = error
        if error is None:
            account.last_refresh = finished

        self._progress(
            progress,
            stage='account_finished',
            account_id=account.id,
            error=error,
        )
        return error

    def sync_folder(self, account, folder, progress=None):
        '''
        Synchronize one folder. Returns None on success, or a traceback string
        on failure.

        This is the targeted counterpart to sync_account. It deliberately does
        not update account.last_refresh or account.last_refresh_attempt, so an
        IDLE-triggered sync does not postpone the account's scheduled refresh.
        Callers must serialize syncs; the frontend uses one refresh worker.
        '''
        if not isinstance(account, objects.Account):
            raise TypeError(account)
        if not isinstance(folder, objects.Folder):
            raise TypeError(folder)
        account.assert_not_deleted()
        folder.assert_not_deleted()

        if folder.account_id != account.id:
            raise exceptions.InvalidFolder('Folder does not belong to this account.')
        if not folder.is_selectable:
            raise exceptions.FolderNotSelectable(folder.name)

        self._progress(
            progress,
            stage='folder_start',
            account_id=account.id,
            folder_id=folder.id,
            folder_name=folder.name,
            index=0,
            total=1,
        )

        error = None
        try:
            client = helpers.connect_imap(account)
            try:
                helpers.enable_condstore(client)
                self._sync_folder(client, folder, account, progress=progress)
            finally:
                try:
                    client.logout()
                except Exception:
                    pass
        except Exception:
            error = traceback.format_exc()
            log.warning('Refreshing %s raised:\n%s', folder, error)
            with self.transaction:
                self.update(
                    table=objects.Folder,
                    pairs={'id': folder.id, 'last_sync_error': error},
                    where_key='id',
                )
            folder.last_sync_error = error

        self._progress(
            progress,
            stage='folder_finished',
            account_id=account.id,
            folder_id=folder.id,
            folder_name=folder.name,
            index=0,
            total=1,
            error=error,
        )
        return error

    def _sync_account_with_client(self, client, account, progress=None):
        server_folders = client.list_folders()
        self._progress(progress, stage='folders', account_id=account.id, total=len(server_folders))

        existing = {folder.name: folder for folder in self.get_folders(account)}
        present_names = set()
        max_rank = 0
        for folder in existing.values():
            max_rank = max(max_rank, folder.ui_order_rank or 0)

        specs = []
        for (index, (flags, delimiter, name)) in enumerate(server_folders):
            delimiter = helpers.decode_text(delimiter)
            name = helpers.decode_text(name)
            if flags is None:
                flags = []
            elif isinstance(flags, (bytes, str)):
                flags = [flags]
            decoded_flags = sorted({helpers.decode_flag(flag) for flag in flags})
            special_use = helpers.folder_special_use(flags, name)
            display_name = helpers.folder_display_name(name, delimiter)
            specs.append({
                'name': name,
                'delimiter': delimiter,
                'attributes': decoded_flags,
                'special_use': special_use,
                'display_name': display_name,
                'ui_order_rank': index + 1,
            })
            present_names.add(name)

        with self.transaction:
            for spec in specs:
                folder = existing.get(spec['name'])
                if folder is None:
                    self.add_folder(
                        account=account,
                        name=spec['name'],
                        display_name=spec['display_name'],
                        delimiter=spec['delimiter'],
                        attributes=spec['attributes'],
                        special_use=spec['special_use'],
                        ui_order_rank=max_rank + spec['ui_order_rank'],
                    )
                else:
                    pairs = {
                        'id': folder.id,
                        'display_name': spec['display_name'],
                        'delimiter': spec['delimiter'],
                        'attributes': json.dumps(spec['attributes']),
                        'special_use': spec['special_use'],
                    }
                    self.update(table=objects.Folder, pairs=pairs, where_key='id')

            for folder in existing.values():
                if folder.name not in present_names:
                    log.info('Folder %s no longer exists on the server; dropping local cache.', folder.name)
                    self.delete(table=objects.Folder, pairs={'id': folder.id})

        self.caches[objects.Folder].clear()
        self.caches[objects.Message].clear()

        folders = [folder for folder in self.get_folders(account) if folder.sync_enabled and folder.is_selectable]
        total = len(folders)
        for (index, folder) in enumerate(folders):
            self._progress(
                progress,
                stage='folder_start',
                account_id=account.id,
                folder_id=folder.id,
                folder_name=folder.name,
                index=index,
                total=total,
            )
            try:
                self._sync_folder(client, folder, account, progress=progress)
            except Exception:
                error = traceback.format_exc()
                log.warning('Syncing %s raised:\n%s', folder, error)
                with self.transaction:
                    self.update(
                        table=objects.Folder,
                        pairs={'id': folder.id, 'last_sync_error': error},
                        where_key='id',
                    )
                folder.last_sync_error = error
            else:
                self._progress(
                    progress,
                    stage='folder_finished',
                    account_id=account.id,
                    folder_id=folder.id,
                    folder_name=folder.name,
                    index=index,
                    total=total,
                )

    def _sync_folder(self, client, folder, account, progress=None):
        selected = client.select_folder(folder.name, readonly=True)
        uidvalidity = selected.get(b'UIDVALIDITY')
        uidnext = selected.get(b'UIDNEXT')
        highestmodseq = selected.get(b'HIGHESTMODSEQ')

        if (
                folder.uidvalidity is not None and
                uidvalidity is not None and
                folder.uidvalidity != uidvalidity):
            log.warning(
                'UIDVALIDITY changed for %s (%s -> %s); discarding local messages.',
                folder,
                folder.uidvalidity,
                uidvalidity,
            )
            with self.transaction:
                self.delete(table=objects.Message, pairs={'folder_id': folder.id})
                self.update(
                    table=objects.Folder,
                    pairs={
                        'id': folder.id,
                        'uidvalidity': uidvalidity,
                        'uidnext': uidnext,
                        'highestmodseq': highestmodseq,
                    },
                    where_key='id',
                )
            folder.uidvalidity = uidvalidity
            folder.uidnext = uidnext
            folder.highestmodseq = highestmodseq
            self.caches[objects.Message].clear()

        if folder.uidvalidity is None and uidvalidity is not None:
            with self.transaction:
                self.update(
                    table=objects.Folder,
                    pairs={'id': folder.id, 'uidvalidity': uidvalidity},
                    where_key='id',
                )
            folder.uidvalidity = uidvalidity

        server_uids = {int(uid) for uid in client.search(['ALL'])}
        local_uids = self.get_folder_uids(folder)
        # A message with an unsent operation is never deleted by sync. The
        # flush decides whether the operation was satisfied (its intended
        # destination) or became a conflict because the server lost the
        # message. Deleting the row here would discard the queued intent.
        guarded_uids = self.get_pending_uids_for_folder(folder)
        removed_uids = (local_uids - server_uids) - guarded_uids
        new_uids = sorted(server_uids - local_uids)

        if removed_uids:
            log.warning(
                'Folder "%s": local UIDs %s are not present on the server; deleting their rows.',
                folder.name,
                sorted(removed_uids),
            )
            with self.transaction:
                self.delete_messages_by_uid(folder, removed_uids)
            self.caches[objects.Message].clear()

        self._sync_folder_flags(client, folder, highestmodseq)

        if new_uids:
            log.debug(
                'Folder "%s": %d UID(s) present on the server but not locally: %s.',
                folder.name,
                len(new_uids),
                new_uids[:20],
            )
            header_first = len(new_uids) > constants.HEADER_FIRST_THRESHOLD
            if header_first:
                self._fetch_headers(client, folder, account, new_uids, progress=progress)
            else:
                self._fetch_full(client, folder, account, new_uids, progress=progress)

        # Fill in bodies for rows that were created by an earlier header-only
        # pass or an interrupted sync.
        self._fetch_pending_bodies(client, folder, progress=progress)

        finished = int(helpers.now())
        pairs = {
            'id': folder.id,
            'last_sync': finished,
            'last_sync_error': None,
        }
        if uidvalidity is not None:
            pairs['uidvalidity'] = uidvalidity
        if uidnext is not None:
            pairs['uidnext'] = uidnext
        if highestmodseq is not None:
            pairs['highestmodseq'] = highestmodseq
        with self.transaction:
            self.update(table=objects.Folder, pairs=pairs, where_key='id')
        if uidvalidity is not None:
            folder.uidvalidity = uidvalidity
        if uidnext is not None:
            folder.uidnext = uidnext
        if highestmodseq is not None:
            folder.highestmodseq = highestmodseq
        folder.last_sync = finished
        folder.last_sync_error = None

    def _sync_folder_flags(self, client, folder, highestmodseq):
        local = self.get_local_flag_map(folder)
        if not local:
            return

        # Messages with an unsent flag operation keep their local value. The
        # remote flags are still fetched; the flush compares them when it
        # decides whether the queued operation is still valid.
        guarded_ids = self.get_pending_flag_message_ids(folder)

        response = None
        if (
                folder.highestmodseq is not None and
                highestmodseq is not None and
                client.has_capability('CONDSTORE')):
            try:
                response = client.fetch(
                    '1:*',
                    ['FLAGS'],
                    modifiers=[f'CHANGEDSINCE {folder.highestmodseq}'],
                )
            except Exception:
                log.debug('CHANGEDSINCE fetch failed for %s; using full flag sync.', folder)
                response = None

        if response is None:
            response = {}
            for batch in _chunks(local.keys(), constants.FETCH_BATCH_FLAGS):
                response.update(client.fetch(batch, ['FLAGS']))

        if not response:
            return

        updates = []
        for (uid, data) in response.items():
            uid = int(uid)
            if uid not in local:
                continue
            (flags, keywords, modseq) = _response_flag_info(data)
            current = local[uid]
            if current['id'] in guarded_ids:
                continue
            if (
                    bool(current['seen']) == flags['seen'] and
                    bool(current['flagged']) == flags['flagged'] and
                    bool(current['answered']) == flags['answered'] and
                    bool(current['imap_deleted']) == flags['imap_deleted'] and
                    bool(current['draft']) == flags['draft']):
                continue
            updates.append((uid, flags, keywords, modseq))

        if not updates:
            return

        with self.transaction:
            for (uid, flags, keywords, modseq) in updates:
                self.update(
                    table=objects.Message,
                    pairs={
                        'id': local[uid]['id'],
                        'seen': flags['seen'],
                        'flagged': flags['flagged'],
                        'answered': flags['answered'],
                        'imap_deleted': flags['imap_deleted'],
                        'draft': flags['draft'],
                        'keywords': json.dumps(keywords) if keywords else None,
                        'modseq': modseq,
                    },
                    where_key='id',
                )
        self.caches[objects.Message].clear()

    def _fetch_headers(self, client, folder, account, uids, progress=None):
        total = len(uids)
        done = 0
        for batch in _chunks(uids, constants.FETCH_BATCH_HEADERS):
            response = client.fetch(
                batch,
                ['FLAGS', 'INTERNALDATE', 'RFC822.SIZE', 'BODY.PEEK[HEADER]'],
            )
            with self.transaction:
                for (uid, data) in response.items():
                    uid = int(uid)
                    header_bytes = _response_header_bytes(data)
                    internaldate = data.get(b'INTERNALDATE')
                    size = helpers.maybe_int(data.get(b'RFC822.SIZE'))
                    (flags, keywords, modseq) = _response_flag_info(data)
                    if header_bytes is None:
                        meta = helpers.empty_meta(
                            internaldate=internaldate,
                            size=size,
                            error='Server did not return headers.',
                        )
                    else:
                        try:
                            (message, meta) = helpers.parse_header_message(
                                header_bytes,
                                internaldate=internaldate,
                                size=size,
                            )
                        except Exception as exc:
                            log.warning('Could not parse headers for %s uid %s.', folder, uid, exc_info=True)
                            meta = helpers.empty_meta(
                                internaldate=internaldate,
                                size=size,
                                error=exc,
                            )
                    self._insert_message(
                        account,
                        folder,
                        uid=uid,
                        uidvalidity=folder.uidvalidity,
                        raw=None,
                        meta=meta,
                        size=size,
                        flags=flags,
                        keywords=keywords,
                        modseq=modseq,
                    )
            done += len(batch)
            self._progress(
                progress,
                stage='headers',
                account_id=folder.account_id,
                folder_id=folder.id,
                folder_name=folder.name,
                done=done,
                total=total,
            )

    def _fetch_full(self, client, folder, account, uids, progress=None):
        total = len(uids)
        done = 0
        for batch in _chunks(uids, constants.FETCH_BATCH_FULL):
            response = client.fetch(
                batch,
                ['FLAGS', 'INTERNALDATE', 'RFC822.SIZE', 'BODY.PEEK[]'],
            )
            with self.transaction:
                for (uid, data) in response.items():
                    uid = int(uid)
                    raw = _response_body_bytes(data)
                    internaldate = data.get(b'INTERNALDATE')
                    size = helpers.maybe_int(data.get(b'RFC822.SIZE'))
                    (flags, keywords, modseq) = _response_flag_info(data)
                    if raw is None:
                        meta = helpers.empty_meta(
                            internaldate=internaldate,
                            size=size,
                            error='Server did not return the message body.',
                        )
                    else:
                        try:
                            (message, meta) = helpers.parse_full_message(
                                raw,
                                internaldate=internaldate,
                                size=size,
                            )
                        except Exception as exc:
                            log.warning('Could not parse message for %s uid %s.', folder, uid, exc_info=True)
                            meta = helpers.empty_meta(
                                internaldate=internaldate,
                                size=size,
                                error=exc,
                            )
                    self._insert_message(
                        account,
                        folder,
                        uid=uid,
                        uidvalidity=folder.uidvalidity,
                        raw=raw,
                        meta=meta,
                        size=size,
                        flags=flags,
                        keywords=keywords,
                        modseq=modseq,
                    )
            done += len(batch)
            self._progress(
                progress,
                stage='bodies',
                account_id=folder.account_id,
                folder_id=folder.id,
                folder_name=folder.name,
                done=done,
                total=total,
            )

    def _fetch_pending_bodies(self, client, folder, progress=None):
        pending = self.get_pending_body_rows(folder, limit=100000)
        if not pending:
            return

        guarded_ids = self.get_pending_flag_message_ids(folder)
        total = len(pending)
        done = 0
        # Fetch newest first so the reader becomes useful as soon as possible.
        pending_uids = [row['uid'] for row in pending]
        for batch in _chunks(pending_uids, constants.FETCH_BATCH_FULL):
            response = client.fetch(
                batch,
                ['FLAGS', 'INTERNALDATE', 'RFC822.SIZE', 'BODY.PEEK[]'],
            )
            with self.transaction:
                for (uid, data) in response.items():
                    uid = int(uid)
                    row = self.select_one(
                        'SELECT id FROM messages WHERE folder_id == ? AND uid == ?',
                        [folder.id, uid],
                    )
                    if row is None:
                        continue
                    raw = _response_body_bytes(data)
                    if raw is None:
                        continue
                    internaldate = data.get(b'INTERNALDATE')
                    size = helpers.maybe_int(data.get(b'RFC822.SIZE'))
                    (flags, keywords, modseq) = _response_flag_info(data)
                    try:
                        (message, meta) = helpers.parse_full_message(
                            raw,
                            internaldate=internaldate,
                            size=size,
                        )
                    except Exception as exc:
                        log.warning('Could not parse pending body for %s uid %s.', folder, uid, exc_info=True)
                        meta = helpers.empty_meta(
                            internaldate=internaldate,
                            size=size,
                            error=exc,
                        )
                    self.update_message_body(
                        row['id'],
                        raw=raw,
                        meta=meta,
                        size=size,
                        flags=flags,
                        keywords=keywords,
                        modseq=modseq,
                        update_flags=row['id'] not in guarded_ids,
                    )
            self.caches[objects.Message].clear()
            done += len(batch)
            self._progress(
                progress,
                stage='bodies',
                account_id=folder.account_id,
                folder_id=folder.id,
                folder_name=folder.name,
                done=done,
                total=total,
            )


class BDBPendingMixin:
    '''
    Storage for the offline pending-operations queue.

    Every queued row keeps the operation and the pre-state the user was
    looking at. Storage is deliberately append-only apart from state
    transitions, so the queue doubles as a record of what the user asked for
    while disconnected.
    '''

    def __init__(self):
        super().__init__()

    def add_pending_operation(
            self,
            *,
            account,
            folder,
            operation,
            message=None,
            enabled=None,
            base_folder_id=None,
            target_folder_id=None,
            target_keywords=None,
            observed=None,
            base_modseq=None,
            message_uid=None,
            message_uidvalidity=None,
        ):
        if operation not in constants.OPERATION_KINDS:
            raise ValueError(f'Unknown pending operation: {operation!r}')

        if message is not None:
            message_id = message.id
            message_uid = message.uid
            message_uidvalidity = message.uidvalidity
        else:
            message_id = None

        if message_uid is None:
            raise ValueError('message or message_uid is required')
        if message_uidvalidity is None and folder is not None:
            message_uidvalidity = folder.uidvalidity

        now = int(helpers.now())
        self.execute(
            '''
            INSERT INTO pending_operations (
                account_id, folder_id, message_id, message_uid,
                message_uidvalidity, operation, enabled, base_folder_id,
                target_folder_id, target_keywords, base_modseq, observed,
                state, attempts, next_attempt, last_error, created, updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            [
                account.id,
                folder.id,
                message_id,
                int(message_uid),
                message_uidvalidity,
                operation,
                None if enabled is None else int(bool(enabled)),
                base_folder_id,
                target_folder_id,
                (
                    json.dumps(list(target_keywords))
                    if target_keywords is not None
                    else None
                ),
                base_modseq,
                json.dumps(observed or {}, ensure_ascii=False, separators=(',', ':')),
                constants.PENDING_STATE_PENDING,
                0,
                now,
                None,
                now,
                now,
            ],
        )
        row = self.sql_write.execute('SELECT last_insert_rowid()').fetchone()
        return int(row[0])

    def get_pending_operations(
            self,
            *,
            account=None,
            folder=None,
            message_ids=None,
            states=None,
            operations=None,
            due_before=None,
            limit=None,
        ):
        wheres = []
        bindings = []
        if account is not None:
            wheres.append('account_id == ?')
            bindings.append(account.id)
        if folder is not None:
            wheres.append('folder_id == ?')
            bindings.append(folder.id)
        if message_ids is not None:
            message_ids = [int(message_id) for message_id in message_ids]
            if not message_ids:
                return []
            placeholders = _placeholders(len(message_ids))
            wheres.append(f'message_id IN {placeholders}')
            bindings.extend(message_ids)
        if states is not None:
            states = list(states)
            if not states:
                return []
            placeholders = _placeholders(len(states))
            wheres.append(f'state IN {placeholders}')
            bindings.extend(states)
        if operations is not None:
            operations = list(operations)
            if not operations:
                return []
            placeholders = _placeholders(len(operations))
            wheres.append(f'operation IN {placeholders}')
            bindings.extend(operations)
        if due_before is not None:
            wheres.append('next_attempt <= ?')
            bindings.append(int(due_before))

        query = 'SELECT * FROM pending_operations'
        if wheres:
            query += ' WHERE ' + ' AND '.join(wheres)
        query += ' ORDER BY id ASC'
        if limit is not None:
            query += ' LIMIT ?'
            bindings.append(int(limit))

        rows = self.select(query, bindings) if bindings else self.select(query)
        return [outbox.Operation.from_row(row) for row in rows]

    def get_pending_for_message(self, message_id, *, states=None):
        return self.get_pending_operations(message_ids=[message_id], states=states)

    def get_pending_operation(self, operation_id):
        row = self.select_one(
            'SELECT * FROM pending_operations WHERE id == ?',
            [int(operation_id)],
        )
        if row is None:
            return None
        return outbox.Operation.from_row(row)

    def get_latest_delete_operation(self, message_id):
        row = self.select_one(
            '''
            SELECT * FROM pending_operations
            WHERE message_id == ? AND operation == ? AND state IN (?, ?)
            ORDER BY id DESC LIMIT 1
            ''',
            [
                int(message_id),
                constants.OPERATION_DELETE,
                constants.PENDING_STATE_PENDING,
                constants.PENDING_STATE_SENT,
            ],
        )
        if row is None:
            return None
        return outbox.Operation.from_row(row)

    def get_pending_count(self, account):
        return self.select_one_value(
            'SELECT COUNT(id) FROM pending_operations WHERE account_id == ? AND state == ?',
            [account.id, constants.PENDING_STATE_PENDING],
            fallback=0,
        )

    def get_pending_summary(self, account):
        rows = self.get_pending_operations(
            account=account,
            states=(
                constants.PENDING_STATE_PENDING,
                constants.PENDING_STATE_CONFLICTED,
                constants.PENDING_STATE_FAILED,
            ),
        )
        counts = outbox.summarize(rows)
        return {
            'pending': counts.get(constants.PENDING_STATE_PENDING, 0),
            'conflicted': counts.get(constants.PENDING_STATE_CONFLICTED, 0),
            'failed': counts.get(constants.PENDING_STATE_FAILED, 0),
        }

    def get_pending_counts_by_account(self):
        '''
        Return {account_id: {state: count}} for rows the UI should mention.

        Superseded and sent rows are history rather than outstanding work and
        are excluded.
        '''
        rows = self.select(
            '''
            SELECT account_id, state, COUNT(id) AS count
            FROM pending_operations
            WHERE state IN (?, ?, ?)
            GROUP BY account_id, state
            ''',
            [
                constants.PENDING_STATE_PENDING,
                constants.PENDING_STATE_CONFLICTED,
                constants.PENDING_STATE_FAILED,
            ],
        )
        counts = {}
        for row in rows:
            counts.setdefault(row['account_id'], {})[row['state']] = row['count']
        return counts

    def get_pending_message_states(self, account, *, states=None):
        if states is None:
            states = (
                constants.PENDING_STATE_PENDING,
                constants.PENDING_STATE_CONFLICTED,
                constants.PENDING_STATE_FAILED,
            )
        rows = self.get_pending_operations(account=account, states=states)
        return outbox.pending_message_states(rows)

    def get_pending_message_ids(self, *, folder=None, operations=None, states=None):
        wheres = ['message_id IS NOT NULL']
        bindings = []
        if folder is not None:
            wheres.append('folder_id == ?')
            bindings.append(folder.id)
        if operations is not None:
            operations = list(operations)
            if not operations:
                return set()
            placeholders = _placeholders(len(operations))
            wheres.append(f'operation IN {placeholders}')
            bindings.extend(operations)
        if states is not None:
            states = list(states)
            if not states:
                return set()
            placeholders = _placeholders(len(states))
            wheres.append(f'state IN {placeholders}')
            bindings.extend(states)

        query = 'SELECT DISTINCT message_id FROM pending_operations WHERE ' + ' AND '.join(wheres)
        rows = self.select(query, bindings) if bindings else self.select(query)
        return {int(row['message_id']) for row in rows}

    def get_pending_flag_message_ids(self, folder):
        return self.get_pending_message_ids(
            folder=folder,
            operations=constants.OPERATION_FLAG_KINDS,
            states=(constants.PENDING_STATE_PENDING,),
        )

    def get_pending_uids_for_folder(self, folder):
        rows = self.select(
            '''
            SELECT DISTINCT message_uid FROM pending_operations
            WHERE folder_id == ? AND state == ?
            ''',
            [folder.id, constants.PENDING_STATE_PENDING],
        )
        return {int(row['message_uid']) for row in rows}

    def has_due_pending(self, account, now=None):
        if now is None:
            now = int(helpers.now())
        count = self.select_one_value(
            '''
            SELECT COUNT(id) FROM pending_operations
            WHERE account_id == ? AND state == ? AND next_attempt <= ?
            ''',
            [account.id, constants.PENDING_STATE_PENDING, int(now)],
            fallback=0,
        )
        return count > 0

    def get_next_pending_attempt(self, account):
        '''
        The earliest time an account's pending queue wants to be retried, or
        None when there is nothing pending. The autorefresh thread uses this
        so backoff delays are honoured instead of waiting for the next
        scheduled account refresh.
        '''
        value = self.select_one_value(
            '''
            SELECT MIN(next_attempt) AS next_attempt FROM pending_operations
            WHERE account_id == ? AND state == ?
            ''',
            [account.id, constants.PENDING_STATE_PENDING],
            fallback=None,
        )
        if value is None:
            return None
        return int(value)

    def set_pending_state(
            self,
            operation_id,
            state,
            *,
            last_error=None,
            attempts=None,
            next_attempt=None,
        ):
        pairs = {
            'state': state,
            'updated': int(helpers.now()),
        }
        if last_error is not None:
            pairs['last_error'] = str(last_error)
        if attempts is not None:
            pairs['attempts'] = int(attempts)
        if next_attempt is not None:
            pairs['next_attempt'] = int(next_attempt)
        columns = ', '.join(f'{column} = ?' for column in pairs)
        bindings = list(pairs.values()) + [int(operation_id)]
        self.execute(
            f'UPDATE pending_operations SET {columns} WHERE id == ?',
            bindings,
        )


def _outbox_message_flags(message):
    '''
    Flags to carry across an account boundary. \\Deleted is deliberately not
    copied because the destination copy is a new live message.
    '''
    flags = [
        flag.decode('ascii')
        for flag in helpers.booleans_to_flags(
            seen=message.seen,
            flagged=message.flagged,
            answered=message.answered,
            draft=message.draft,
        )
    ]
    for keyword in message.keywords or []:
        keyword = str(keyword)
        if keyword:
            flags.append(keyword)
    return flags


def _outbox_message_time(message):
    '''
    The source INTERNALDATE as a UTC datetime, falling back to the Date header.
    '''
    value = message.meta.get('internaldate_unix')
    if value is None:
        value = message.meta.get('date_unix')
    if value is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(int(value), datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


class BDBOutboxMixin:
    '''
    Flush the pending queue against the mail server.

    The flush always reconciles the affected folders first, then revalidates
    each row against the fresh server state. Conditional STORE is used where
    the server supports CONDSTORE; otherwise shared operations fall back to a
    read-compare-write with a small unavoidable race.
    '''

    def __init__(self):
        super().__init__()

    def _outbox_progress(self, callback, **payload):
        if callback is None:
            return
        try:
            callback(payload)
        except Exception:
            log.debug('Outbox progress callback raised.', exc_info=True)

    def _outbox_get_message(self, operation):
        if operation.message_id is None:
            return None
        try:
            return self.get_message(operation.message_id)
        except exceptions.NoSuchMessage:
            return None

    def _outbox_mark(self, operation_id, state, *, error=None, attempts=None, next_attempt=None):
        with self.transaction:
            self.set_pending_state(
                operation_id,
                state,
                last_error=error,
                attempts=attempts,
                next_attempt=next_attempt,
            )

    def _outbox_backoff(self, operations, error):
        now = int(helpers.now())
        for operation in operations:
            # A row may have been resolved by an earlier step (for example a
            # conflict recorded while another folder's reconcile failed).
            # Only genuinely pending rows are retried.
            current = self.get_pending_operation(operation.id)
            if current is None or current.state != constants.PENDING_STATE_PENDING:
                continue
            attempts = current.attempts + 1
            self._outbox_mark(
                operation.id,
                constants.PENDING_STATE_PENDING,
                error=error,
                attempts=attempts,
                next_attempt=now + outbox.backoff_seconds(attempts),
            )

    def _outbox_has_newer_pending_flag(self, operation):
        '''
        True when the user queued another flag change for this message while
        the current one was being sent. The newer local value must win over
        whatever the server returned for the older operation.
        '''
        if not operation.is_flag or operation.message_id is None:
            return False
        rows = self.get_pending_operations(
            message_ids=[operation.message_id],
            states=(constants.PENDING_STATE_PENDING,),
            operations=constants.OPERATION_FLAG_KINDS,
        )
        return any(row.id > operation.id for row in rows)

    def _outbox_revert_local(self, operation):
        '''
        Put the local row back to the pre-state recorded with an operation
        that will not be applied. This keeps the cache from claiming a
        change that the server never accepted.
        '''
        message = self._outbox_get_message(operation)
        if message is None:
            return

        if operation.is_flag:
            if self._outbox_has_newer_pending_flag(operation):
                return
            if operation.operation == constants.OPERATION_KEYWORDS:
                keywords = list(operation.observed.get('keywords', []))
                pairs = {'keywords': json.dumps(keywords) if keywords else None}
                message.keywords = keywords
            else:
                value = bool(operation.observed.get(operation.operation, False))
                pairs = {operation.operation: value}
                setattr(message, operation.operation, value)
            with self.transaction:
                pairs = dict(pairs)
                pairs['id'] = message.id
                self.update(table=objects.Message, pairs=pairs, where_key='id')
            self.caches[objects.Message].clear()
            return

        if operation.operation in (
                constants.OPERATION_DELETE,
                constants.OPERATION_MOVE,
                constants.OPERATION_RESTORE):
            # A folder operation never changes the local row at enqueue time,
            # except for the delete marker.
            if operation.operation == constants.OPERATION_DELETE:
                with self.transaction:
                    self.update(
                        table=objects.Message,
                        pairs={'id': message.id, 'local_deleted': False},
                        where_key='id',
                    )
                message.local_deleted = False
                self.caches[objects.Message].clear()

    def discard_pending(self, account, *, states=None):
        '''
        Abandon visible queued operations for an account without sending them.

        The local flag values and delete markers are restored to the state
        recorded by the earliest discarded operation for each message. Rows are
        marked superseded rather than deleted, so the queue keeps its history.
        '''
        if states is None:
            states = (
                constants.PENDING_STATE_PENDING,
                constants.PENDING_STATE_CONFLICTED,
                constants.PENDING_STATE_FAILED,
            )
        rows = self.get_pending_operations(account=account, states=states)
        if not rows:
            return {'discarded': 0}

        groups = {}
        for row in rows:
            if row.message_id is None:
                continue
            groups.setdefault(row.message_id, []).append(row)

        with self.transaction:
            for (message_id, group) in groups.items():
                message = self._outbox_get_message(group[0])
                if message is None:
                    continue

                pairs = {}
                for operation in (
                        constants.OPERATION_SEEN,
                        constants.OPERATION_FLAGGED,
                        constants.OPERATION_ANSWERED):
                    kind_rows = [
                        row for row in group
                        if row.operation == operation
                    ]
                    if not kind_rows:
                        continue
                    value = bool(kind_rows[0].observed.get(
                        operation,
                        getattr(message, operation),
                    ))
                    pairs[operation] = value

                keyword_rows = [
                    row for row in group
                    if row.operation == constants.OPERATION_KEYWORDS
                ]
                if keyword_rows:
                    keywords = list(keyword_rows[0].observed.get('keywords') or [])
                    pairs['keywords'] = json.dumps(keywords) if keywords else None

                if any(row.operation == constants.OPERATION_DELETE for row in group):
                    pairs['local_deleted'] = False

                if not pairs:
                    continue
                pairs['id'] = message_id
                self.update(table=objects.Message, pairs=pairs, where_key='id')

            for row in rows:
                self.set_pending_state(
                    row.id,
                    constants.PENDING_STATE_SUPERSEDED,
                    last_error='Discarded by user.',
                )

        self.caches[objects.Message].clear()
        return {'discarded': len(rows)}

    def _outbox_summary(self, account):
        return self.get_pending_summary(account)

    def flush_pending(self, account, progress=None):
        '''
        Drain this account's queue. Returns a summary dict.

        Connection failures leave rows pending with exponential backoff;
        conflicts and refused actions move to terminal states and become
        visible to the user. Nothing is ever silently discarded.
        '''
        account.assert_not_deleted()
        now = int(helpers.now())
        summary = {
            'account_id': account.id,
            'sent': 0,
            'failed': 0,
            'conflicted': 0,
            'superseded': 0,
            'pending': 0,
            'not_due': 0,
            'error': None,
            'refresh_folder_ids': [],
        }

        rows = self.get_pending_operations(
            account=account,
            states=(constants.PENDING_STATE_PENDING,),
        )
        if not rows:
            return summary

        due = []
        for row in rows:
            if row.next_attempt > now:
                summary['not_due'] += 1
                continue
            if outbox.is_expired(row, now):
                # The action will never be sent. Put the local row back to
                # the state the user saw so the cache does not claim a change
                # the server never received.
                self._outbox_revert_local(row)
                self._outbox_mark(
                    row.id,
                    constants.PENDING_STATE_FAILED,
                    error='Gave up retrying this action.',
                )
                summary['failed'] += 1
                continue
            due.append(row)

        if not due:
            summary['pending'] = self.get_pending_count(account)
            return summary

        self._outbox_progress(
            progress,
            stage='outbox_start',
            account_id=account.id,
            total=len(due),
        )

        client = None
        try:
            client = helpers.connect_imap(account)
            helpers.enable_condstore(client)
        except Exception as exc:
            log.warning('Outbox could not connect to %s: %s', account, exc)
            self._outbox_backoff(due, str(exc))
            summary['error'] = f'Could not connect to the mail server: {exc}'
            summary.update(self._outbox_summary(account))
            self._outbox_progress(
                progress,
                stage='outbox_finished',
                **summary,
            )
            return summary

        try:
            try:
                self._outbox_reconcile(client, account, due, progress=progress)
            except Exception as exc:
                log.warning('Outbox reconcile for %s failed: %s', account, exc)
                self._outbox_backoff(due, str(exc))
                summary['error'] = f'Could not synchronize before sending: {exc}'
                summary.update(self._outbox_summary(account))
                self._outbox_progress(
                    progress,
                    stage='outbox_finished',
                    **summary,
                )
                return summary

            self.caches[objects.Message].clear()
            fresh = self.get_pending_operations(
                account=account,
                states=(constants.PENDING_STATE_PENDING,),
            )
            fresh_due = [row for row in fresh if row.next_attempt <= now]
            plan = outbox.consolidate(fresh_due)

            if plan.superseded:
                with self.transaction:
                    for operation_id in plan.superseded:
                        self.set_pending_state(
                            operation_id,
                            constants.PENDING_STATE_SUPERSEDED,
                        )
                summary['superseded'] = len(plan.superseded)

            self._outbox_refresh_folder_ids = set()
            for (index, operation) in enumerate(plan.execute):
                # An earlier operation in this plan may have retired this row
                # (for example a cross-account move deletes an offline source
                # message and supersedes its later flag operations).
                current = self.get_pending_operation(operation.id)
                if (
                        current is None or
                        current.state != constants.PENDING_STATE_PENDING):
                    continue
                try:
                    refresh_folder_id = self._outbox_execute(client, operation)
                    if refresh_folder_id is not None:
                        self._outbox_refresh_folder_ids.add(refresh_folder_id)
                except exceptions.PendingConflict as exc:
                    log.info('Pending operation %s became a conflict: %s', operation, exc)
                    self._outbox_revert_local(operation)
                    self._outbox_mark(
                        operation.id,
                        constants.PENDING_STATE_CONFLICTED,
                        error=exc.error_message,
                    )
                    summary['conflicted'] += 1
                except exceptions.BringException as exc:
                    log.warning('Pending operation %s failed: %s', operation, exc)
                    self._outbox_revert_local(operation)
                    self._outbox_mark(
                        operation.id,
                        constants.PENDING_STATE_FAILED,
                        error=str(exc),
                    )
                    summary['failed'] += 1
                except Exception as exc:
                    # Anything unexpected is treated as a transport problem:
                    # keep the row and retry rather than dropping the user's
                    # action. The remaining plan is left for the next flush.
                    log.warning(
                        'Pending operation %s raised; backing off: %s',
                        operation,
                        exc,
                    )
                    self._outbox_backoff([operation], str(exc))
                    summary['error'] = str(exc)
                    break
                else:
                    summary['sent'] += 1

                self._outbox_progress(
                    progress,
                    stage='outbox_progress',
                    account_id=account.id,
                    done=index + 1,
                    total=len(plan.execute),
                )
        finally:
            try:
                client.logout()
            except Exception:
                pass

        summary['refresh_folder_ids'] = sorted(
            getattr(self, '_outbox_refresh_folder_ids', set())
        )
        summary.update(self._outbox_summary(account))
        self._outbox_progress(
            progress,
            stage='outbox_finished',
            **summary,
        )
        return summary

    def _outbox_reconcile(self, client, account, rows, progress=None):
        '''
        Sync every folder the queued operations will write to or read from,
        before any write is attempted. This is what turns "another client
        changed things while we were offline" into a decision instead of a
        surprise.
        '''
        folder_ids = set()
        for row in rows:
            # Only the folder the operation was created in needs a sync. The
            # destination folder is opened by the move itself, which also
            # returns its current UIDVALIDITY.
            folder_ids.add(row.folder_id)

        for folder_id in sorted(folder_ids):
            try:
                folder = self.get_folder(folder_id)
            except exceptions.NoSuchFolder:
                self._outbox_fail_folder(rows, folder_id, 'The folder no longer exists.')
                continue
            if not folder.is_selectable:
                self._outbox_fail_folder(rows, folder_id, 'The folder is not selectable.')
                continue
            self._sync_folder(client, folder, account, progress=progress)

    def _outbox_fail_folder(self, rows, folder_id, error):
        for row in rows:
            if row.folder_id != folder_id and row.target_folder_id != folder_id:
                continue
            if row.folder_id != folder_id:
                # The folder is only the destination; the operation itself
                # still has a valid source. Leave it for revalidation.
                continue
            self._outbox_revert_local(row)
            self._outbox_mark(
                row.id,
                constants.PENDING_STATE_CONFLICTED,
                error=error,
            )

    def _outbox_execute(self, client, operation):
        folder = self.get_folder(operation.folder_id)
        message = self._outbox_get_message(operation)
        # A queued flag operation can be re-targeted by an earlier move in
        # the same plan, in which case operation.folder_id is no longer the
        # message's live folder and its UIDVALIDITY is not comparable.
        if message is not None and message.folder_id == folder.id:
            if (
                    operation.message_uidvalidity is not None and
                    folder.uidvalidity is not None and
                    int(operation.message_uidvalidity) != int(folder.uidvalidity)):
                raise exceptions.PendingConflict(
                    'The folder was renumbered after this action was queued.'
                )

        if operation.is_flag:
            self._outbox_execute_flag(client, operation)
            return None
        return self._outbox_execute_folder(client, operation)

    def _outbox_flag_changes(self, operation, message):
        '''
        Compute the flag delta relative to the state the user saw, not the
        local row. The local row may already show the desired value while the
        server still holds the pre-state (for example when another client
        changed the message and the sync guard preserved the local value).
        '''
        if operation.operation == constants.OPERATION_KEYWORDS:
            target = set(operation.target_keywords)
            current = set(operation.observed.get('keywords', message.keywords or []))
            return (sorted(target - current), sorted(current - target))

        flag = {
            constants.OPERATION_SEEN: b'\\Seen',
            constants.OPERATION_FLAGGED: b'\\Flagged',
            constants.OPERATION_ANSWERED: b'\\Answered',
        }[operation.operation]
        current = bool(operation.observed.get(
            operation.operation,
            getattr(message, operation.operation),
        ))
        if operation.enabled and not current:
            return ([flag], [])
        if not operation.enabled and current:
            return ([], [flag])
        return ([], [])

    def _outbox_plain_store(self, client, message, add, remove):
        if add:
            client.add_flags([message.uid], add)
        if remove:
            client.remove_flags([message.uid], remove)

    def _outbox_refresh_message_flags(self, client, message, operation):
        # MODSEQ is only meaningful when the server supports CONDSTORE and we
        # actually have a mod-sequence to compare with. Some servers treat an
        # unsupported MODSEQ item as a malformed body section, so do not ask
        # for it on plain IMAP servers.
        data_items = ['FLAGS']
        if operation.base_modseq is not None and client.has_capability('CONDSTORE'):
            data_items.append('MODSEQ')

        try:
            response = client.fetch([int(message.uid)], data_items)
        except Exception:
            if 'MODSEQ' not in data_items:
                raise
            # A server that advertises CONDSTORE but rejects MODSEQ without an
            # explicit enablement still gives us the flags.
            log.debug('MODSEQ fetch failed; retrying with FLAGS only.', exc_info=True)
            data_items = ['FLAGS']
            response = client.fetch([int(message.uid)], data_items)

        data = response.get(int(message.uid)) or {}
        if not data:
            return
        (flags, keywords, modseq) = _response_flag_info(data)
        pairs = {'id': message.id}
        if modseq is not None:
            pairs['modseq'] = modseq
        if not self._outbox_has_newer_pending_flag(operation):
            pairs.update({
                'seen': flags['seen'],
                'flagged': flags['flagged'],
                'answered': flags['answered'],
                'imap_deleted': flags['imap_deleted'],
                'draft': flags['draft'],
                'keywords': json.dumps(keywords) if keywords else None,
            })
        if len(pairs) > 1:
            with self.transaction:
                self.update(table=objects.Message, pairs=pairs, where_key='id')
        if modseq is not None:
            message.modseq = modseq
        if 'seen' in pairs:
            message.seen = flags['seen']
            message.flagged = flags['flagged']
            message.answered = flags['answered']
            message.imap_deleted = flags['imap_deleted']
            message.draft = flags['draft']
            message.keywords = keywords
        self.caches[objects.Message].clear()

    def _outbox_execute_flag(self, client, operation):
        message = self._outbox_get_message(operation)
        if message is None:
            raise exceptions.PendingConflict('The message no longer exists locally.')

        folder = self.get_folder(message.folder_id)
        if not folder.is_selectable:
            raise exceptions.PendingConflict('The folder is not selectable.')

        client.select_folder(folder.name, readonly=False)
        (add, remove) = self._outbox_flag_changes(operation, message)
        if not add and not remove:
            self._outbox_mark(operation.id, constants.PENDING_STATE_SENT)
            return

        conditional = (
            operation.base_modseq is not None and
            folder.highestmodseq is not None and
            client.has_capability('CONDSTORE')
        )
        if conditional:
            (accepted, modified) = helpers.conditional_store(
                client,
                folder.name,
                [message.uid],
                add=add,
                remove=remove,
                since=operation.base_modseq,
            )
            if modified:
                if operation.is_private_flag:
                    # Local intent wins for private flags: apply the user's
                    # click against the newer server state.
                    self._outbox_plain_store(client, message, add, remove)
                else:
                    raise exceptions.PendingConflict(
                        'The message changed on the server after this action was queued.'
                    )
        else:
            if (
                    operation.operation == constants.OPERATION_KEYWORDS and
                    not message.account.offline
            ):
                response = client.fetch([int(message.uid)], ['FLAGS'])
                data = response.get(int(message.uid)) or {}
                (_server_flags, server_keywords, _server_modseq) = _response_flag_info(data)
                if set(server_keywords) != operation.base_flag_value():
                    raise exceptions.PendingConflict(
                        'The message keywords changed on the server after this action was queued.'
                    )
            self._outbox_plain_store(client, message, add, remove)

        self._outbox_refresh_message_flags(client, message, operation)
        self._outbox_mark(operation.id, constants.PENDING_STATE_SENT)

    def _outbox_execute_folder(self, client, operation):
        message = self._outbox_get_message(operation)
        if message is None:
            if operation.operation == constants.OPERATION_DELETE:
                # The message is already gone; the delete is satisfied.
                self._outbox_mark(operation.id, constants.PENDING_STATE_SENT)
                return None
            raise exceptions.PendingConflict('The message no longer exists locally.')

        try:
            source = self.get_folder(message.folder_id)
            target = self.get_folder(operation.target_folder_id)
        except exceptions.NoSuchFolder:
            raise exceptions.PendingConflict('The destination folder no longer exists.')

        if not target.is_selectable:
            raise exceptions.PendingConflict('The destination folder is not selectable.')

        if source.id == target.id:
            with self.transaction:
                self.update(
                    table=objects.Message,
                    pairs={'id': message.id, 'local_deleted': False},
                    where_key='id',
                )
            message.local_deleted = False
            self._outbox_mark(operation.id, constants.PENDING_STATE_SENT)
            return None

        selected = client.select_folder(source.name, readonly=False)
        server_uidvalidity = selected.get(b'UIDVALIDITY') if isinstance(selected, dict) else None
        if (
                message.uidvalidity is not None and
                server_uidvalidity is not None and
                int(message.uidvalidity) != int(server_uidvalidity)):
            raise exceptions.PendingConflict(
                'The source folder was renumbered after this action was queued.'
            )

        found = client.search(['UID', str(message.uid)])
        if not found:
            if operation.operation == constants.OPERATION_DELETE:
                with self.transaction:
                    self.delete(table=objects.Message, pairs={'id': message.id})
                self.caches[objects.Message].clear()
                self._outbox_mark(operation.id, constants.PENDING_STATE_SENT)
                return None
            with self.transaction:
                self.delete(table=objects.Message, pairs={'id': message.id})
            self.caches[objects.Message].clear()
            raise exceptions.PendingConflict(
                'The message no longer exists in the source folder.'
            )

        if operation.is_cross_account:
            return self._outbox_execute_cross_account(
                client,
                operation,
                message,
                source,
                target,
            )

        (new_uid, new_uidvalidity, uid_source) = objects.move_message_on_server(
            client,
            source_name=source.name,
            destination_name=target.name,
            uid=message.uid,
            message_id=message.message_id,
        )

        with self.transaction:
            if new_uid is not None:
                self.update(
                    table=objects.Message,
                    pairs={
                        'id': message.id,
                        'folder_id': target.id,
                        'uid': new_uid,
                        'uidvalidity': new_uidvalidity,
                        'local_deleted': False,
                    },
                    where_key='id',
                )
                message.folder_id = target.id
                message.uid = new_uid
                message.uidvalidity = new_uidvalidity
                message.local_deleted = False
                message._folder = target
            else:
                log.warning(
                    'Could not determine the destination UID for message %s; '
                    'removing the local row. The next sync will re-download it.',
                    message.id,
                )
                self.delete(table=objects.Message, pairs={'id': message.id})
        self.caches[objects.Message].clear()
        self._outbox_mark(operation.id, constants.PENDING_STATE_SENT)
        return None

    def _outbox_destination_has_copy(self, client, target, message):
        '''
        Best-effort guard against APPENDing the same message twice on retry.
        A matching size is enough evidence for a copy that already reached the
        destination; the operation can continue with source cleanup.
        '''
        if not message.message_id:
            return False

        try:
            found = client.search(['HEADER', 'Message-ID', message.message_id])
        except Exception:
            log.debug(
                'Could not search "%s" for a previous copy of message %s.',
                target.name,
                message.id,
                exc_info=True,
            )
            return False
        if not found:
            return False
        if message.size is None:
            return True

        try:
            response = client.fetch(found, ['RFC822.SIZE'])
        except Exception:
            log.debug(
                'Could not fetch sizes for duplicate check in "%s".',
                target.name,
                exc_info=True,
            )
            return True

        for data in response.values():
            size = helpers.maybe_int(data.get(b'RFC822.SIZE'))
            if size is not None and int(size) == int(message.size):
                return True
        return False

    def _outbox_execute_cross_account(self, client, operation, message, source, target):
        '''
        Copy one message to a folder in another account and, when requested,
        move the source to Trash afterwards.

        The source is never touched before APPEND succeeds. If cleanup fails
        after a successful APPEND, the operation stays pending and a retry
        first looks for the destination copy so it does not append again.
        '''
        try:
            destination_account = target.account
        except exceptions.NoSuchAccount as exc:
            raise exceptions.PendingConflict(
                'The destination account no longer exists.'
            ) from exc
        if destination_account.id == message.account_id:
            raise exceptions.PendingConflict(
                'The destination folder belongs to the source account.'
            )

        copy_only = operation.operation == constants.OPERATION_COPY_ACCOUNT
        trash = None
        if not copy_only:
            try:
                trash = message.account.get_trash_folder()
            except exceptions.NoSuchAccount as exc:
                raise exceptions.PendingConflict(
                    'The source account no longer exists.'
                ) from exc
            if trash is None or not trash.is_selectable or trash.id == source.id:
                copy_only = True

        raw = self.decompress_raw(message.raw, message.raw_codec)
        if raw is None:
            raise exceptions.PendingConflict(
                'The message body has not been downloaded yet. '
                'Refresh the source folder and retry.'
            )

        flags = _outbox_message_flags(message)
        msg_time = _outbox_message_time(message)

        def destination_operation(destination_client):
            destination_client.select_folder(target.name, readonly=False)
            if self._outbox_destination_has_copy(destination_client, target, message):
                log.info(
                    'Destination "%s" already contains message %s; '
                    'skipping APPEND.',
                    target.name,
                    message.id,
                )
                return
            destination_client.append(
                target.name,
                raw,
                flags=flags,
                msg_time=msg_time,
            )

        try:
            helpers.imap_call(destination_account, destination_operation)
        except (exceptions.MailServerConnectionError,
                exceptions.MailServerLoginError) as exc:
            raise RuntimeError(
                f'Could not connect to the destination account: {exc}'
            ) from exc

        refresh_folder_ids = getattr(self, '_outbox_refresh_folder_ids', None)
        if refresh_folder_ids is not None:
            refresh_folder_ids.add(target.id)

        source_account = message.account
        if source_account.offline:
            # Moving out of an offline account is a true local move: the
            # destination copy has succeeded, so the local source row is
            # removed instead of moving to the offline Trash. This also
            # upgrades any copy-only row left pending by an older version.
            with self.transaction:
                source_uidnext = max(
                    int(source.uidnext or 1),
                    int(message.uid) + 1,
                )
                self.update(
                    table=objects.Folder,
                    pairs={'id': source.id, 'uidnext': source_uidnext},
                    where_key='id',
                )
                source.uidnext = source_uidnext
                other_rows = self.get_pending_operations(
                    message_ids=[message.id],
                    states=(constants.PENDING_STATE_PENDING,),
                )
                for row in other_rows:
                    if row.id == operation.id:
                        continue
                    self.set_pending_state(
                        row.id,
                        constants.PENDING_STATE_SUPERSEDED,
                        last_error='The message was moved out of the offline account.',
                    )
                self.delete(table=objects.Message, pairs={'id': message.id})
                self.set_pending_state(operation.id, constants.PENDING_STATE_SENT)
            self.caches[objects.Message].clear()
            log.info(
                'Moved message %s out of offline account %s; removed the local row.',
                message.id,
                source_account,
            )
            return target.id

        if copy_only or trash is None or trash.id == source.id:
            if operation.operation == constants.OPERATION_MOVE_ACCOUNT:
                log.warning(
                    'Message %s was copied to another account, but the source '
                    'account has no usable Trash folder; the source was kept.',
                    message.id,
                )
            self._outbox_mark(operation.id, constants.PENDING_STATE_SENT)
            return target.id

        try:
            (new_uid, new_uidvalidity, _uid_source) = objects.move_message_on_server(
                client,
                source_name=source.name,
                destination_name=trash.name,
                uid=message.uid,
                message_id=message.message_id,
                allow_copy_fallback=not _client_has_capability(client, 'MOVE'),
            )
        except Exception as exc:
            raise RuntimeError(
                'The destination copy succeeded, but moving the source '
                f'message to Trash failed: {exc}'
            ) from exc

        with self.transaction:
            if new_uid is not None:
                self.update(
                    table=objects.Message,
                    pairs={
                        'id': message.id,
                        'folder_id': trash.id,
                        'uid': new_uid,
                        'uidvalidity': new_uidvalidity,
                        'local_deleted': False,
                    },
                    where_key='id',
                )
                message.folder_id = trash.id
                message.uid = new_uid
                message.uidvalidity = new_uidvalidity
                message.local_deleted = False
                message._folder = trash
            else:
                log.warning(
                    'Could not determine the Trash UID for message %s; '
                    'removing the local row. The next sync will re-download it.',
                    message.id,
                )
                self.delete(table=objects.Message, pairs={'id': message.id})
            self.set_pending_state(operation.id, constants.PENDING_STATE_SENT)

        self.caches[objects.Message].clear()
        return target.id


class BringMailDB(
        BDBAccountMixin,
        BDBFolderMixin,
        BDBMessageMixin,
        BDBSyncMixin,
        BDBPendingMixin,
        BDBOutboxMixin,
        worms.DatabaseWithCaching,
    ):
    def __init__(
            self,
            data_directory=None,
            *,
            create=False,
            skip_version_check=False,
        ):
        super().__init__()
        self._check_sqlite_version()

        if data_directory is None:
            data_directory = pathclass.cwd().with_child(constants.DEFAULT_DATADIR)

        self.data_directory = pathclass.Path(data_directory)
        if self.data_directory.exists and not self.data_directory.is_dir:
            raise exceptions.BadDataDirectory(self.data_directory.absolute_path)

        self._init_sql(create=create, skip_version_check=skip_version_check)
        self.id_type = int
        self._init_column_index()
        self._init_caches()

    def __del__(self):
        self.close()

    def __repr__(self):
        return f'BringMailDB(data_directory={self.data_directory})'

    def _check_sqlite_version(self):
        installed = sqlite3.sqlite_version_info
        required = constants.MINIMUM_SQLITE
        if installed < required:
            raise exceptions.SQLiteTooOld(
                installed=sqlite3.sqlite_version,
                required=constants.MINIMUM_SQLITE_STRING,
            )

    def _check_version(self):
        existing = int(self.pragma_read('user_version') or 0)
        if existing > constants.DATABASE_VERSION:
            raise exceptions.DatabaseTooNew(
                existing=existing,
                new=constants.DATABASE_VERSION,
            )
        if existing != constants.DATABASE_VERSION:
            raise exceptions.DatabaseOutOfDate(
                existing=existing,
                new=constants.DATABASE_VERSION,
            )

    def _first_time_setup(self):
        log.info('Running first-time database setup.')
        with self.transaction:
            for statement in constants.DB_INIT.split(';'):
                statement = statement.strip()
                if statement:
                    self.execute(statement)
            for statement in constants.DB_TRIGGERS:
                self.execute(statement)
            self.pragma_write('user_version', constants.DATABASE_VERSION)

    def _init_caches(self):
        self.caches = {
            objects.Account: cacheclass.Cache(maxlen=1000),
            objects.Folder: cacheclass.Cache(maxlen=2000),
            objects.Message: cacheclass.Cache(maxlen=5000),
        }

    def _init_column_index(self):
        tables = [
            row['name'] for row in self.select(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            if not row['name'].startswith('sqlite_')
        ]
        columns = {}
        for table in tables:
            rows = self.select(f'PRAGMA table_xinfo("{table}")')
            columns[table] = [row['name'] for row in rows]
        self.COLUMNS = columns
        self.COLUMN_INDEX = {
            table: {column: index for (index, column) in enumerate(names)}
            for (table, names) in columns.items()
        }

    def _init_sql(self, create=False, skip_version_check=True):
        self.database_filepath = self.data_directory.with_child(constants.DEFAULT_DBNAME)
        existing_database = self.database_filepath.exists

        if not existing_database and not create:
            message = f'"{self.database_filepath.absolute_path}" does not exist and create is off.'
            raise FileNotFoundError(message)

        self.data_directory.makedirs(exist_ok=True)

        if not existing_database:
            # The read connection below uses mode=ro and cannot create the
            # file, so create an empty one first.
            sqlite3.connect(self.database_filepath.absolute_path).close()

        # Open the writer first so WAL mode is established before the read-only
        # connection is created. Changing journal mode while another connection
        # is active is avoidable.
        self.sql_write = self._make_sqlite_write_connection(self.database_filepath)
        self._load_write_pragmas()
        self.sql_read = self._make_sqlite_read_connection(self.database_filepath)
        self._load_read_pragmas()

        if existing_database:
            if not skip_version_check:
                self._check_version()
        else:
            self._first_time_setup()

    def _load_pragmas(self):
        # Kept for compatibility with callers that reinitialize pragmas after a
        # connection recovery.
        self._load_write_pragmas()
        self._load_read_pragmas()

    def _load_write_pragmas(self):
        for (key, value) in constants.WRITE_PRAGMAS.items():
            self.sql_write.execute(f'PRAGMA {key} = {value}')

    def _load_read_pragmas(self):
        for (key, value) in constants.READ_PRAGMAS.items():
            self.sql_read.execute(f'PRAGMA {key} = {value}')

    @classmethod
    def closest_bringmaildb(cls, path='.', *args, **kwargs):
        starting = pathclass.Path(path)
        current = starting
        while True:
            possible = current.with_child(constants.DEFAULT_DATADIR)
            if possible.is_dir:
                break
            parent = current.parent
            if current == parent:
                raise exceptions.NoClosestBringMailDB(starting.absolute_path)
            current = parent

        log.debug('Found closest BringMailDB at "%s".', possible.absolute_path)
        return cls(data_directory=possible, create=False, *args, **kwargs)

    def clear_object_caches(self):
        for cache in self.caches.values():
            cache.clear()

    def close(self):
        super().close()

    def _make_sqlite_read_connection(self, path):
        # The web request threads and the background sync worker both use these
        # connections. SQLite's default check_same_thread would reject that.
        if isinstance(path, pathclass.Path):
            path = path.absolute_path
        sql_read = sqlite3.connect(
            f'file:{path}?mode=ro',
            uri=True,
            check_same_thread=False,
        )
        sql_read.row_factory = sqlite3.Row
        return sql_read

    def _make_sqlite_write_connection(self, path):
        if isinstance(path, pathclass.Path):
            path = path.absolute_path
        sql_write = sqlite3.connect(path, check_same_thread=False)
        sql_write.row_factory = sqlite3.Row
        return sql_write

    def generate_id(self, thing_class):
        if not issubclass(thing_class, objects.ObjectBase):
            raise TypeError(thing_class)

        table = thing_class.table
        while True:
            thing_id = RNG.getrandbits(32)
            if not self.exists(f'SELECT 1 FROM {table} WHERE id == ?', [thing_id]):
                return thing_id
