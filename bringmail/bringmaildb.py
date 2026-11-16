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


class BDBAccountMixin:
    def __init__(self):
        super().__init__()

    @worms.atomic
    def add_account(
            self,
            *,
            label=None,
            host,
            password,
            port=993,
            autorefresh_interval=constants.DEFAULT_AUTOREFRESH_INTERVAL,
            ssl=True,
            starttls=False,
            username,
        ):
        label = objects.Account.normalize_label(label)
        host = objects.Account.normalize_host(host)
        port = objects.Account.normalize_port(port)
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
        return account

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

    def update_message_body(self, message_id, *, raw, meta, size=None, flags=None, keywords=None, modseq=None):
        flags = flags or {}
        data = {
            'id': message_id,
            'raw': zlib.compress(raw, 6),
            'raw_codec': constants.RAW_CODEC_ZLIB,
            'body_fetched': True,
            'meta': json.dumps(meta, ensure_ascii=False, separators=(',', ':')),
            'meta_version': constants.META_VERSION,
            'size': size,
            'seen': bool(flags.get('seen', False)),
            'flagged': bool(flags.get('flagged', False)),
            'answered': bool(flags.get('answered', False)),
            'imap_deleted': bool(flags.get('imap_deleted', False)),
            'draft': bool(flags.get('draft', False)),
            'keywords': json.dumps(keywords) if keywords else None,
            'modseq': modseq,
        }
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
        removed_uids = local_uids - server_uids
        new_uids = sorted(server_uids - local_uids)

        if removed_uids:
            with self.transaction:
                self.delete_messages_by_uid(folder, removed_uids)
            self.caches[objects.Message].clear()

        self._sync_folder_flags(client, folder, highestmodseq)

        if new_uids:
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


class BringMailDB(
        BDBAccountMixin,
        BDBFolderMixin,
        BDBMessageMixin,
        BDBSyncMixin,
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
        existing = self.pragma_read('user_version')
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
