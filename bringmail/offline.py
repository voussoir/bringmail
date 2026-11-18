'''
Local IMAP emulation for offline accounts.

An offline account has no server. The rest of BringMail is written against
IMAPClient, so this module provides the small part of that interface the sync
engine, the outbox, and message actions use. Every method reads or writes the
local database, which keeps the callers unaware that no network is involved.

The client is deliberately not a general IMAP implementation. It supports the
searches, fetches, flag changes, moves, copies, and appends BringMail performs
for its own folders. Unsupported requests raise NotSupported instead of
quietly returning an incorrect answer.
'''
import datetime
import json

from . import constants
from . import exceptions
from . import helpers
from . import objects


class _FakeImapState:
    '''
    The tiny part of imaplib's state that helpers.copyuid_from_response reads.

    A real IMAPClient exposes ``_imap.untagged_responses``. The offline client
    returns COPYUID in the command response, so this map stays empty.
    '''
    def __init__(self):
        self.untagged_responses = {}


def _decode_flag(flag):
    return helpers.decode_flag(flag)


def _message_flags(message):
    flags = list(helpers.booleans_to_flags(
        seen=message.seen,
        flagged=message.flagged,
        answered=message.answered,
        imap_deleted=message.imap_deleted,
        draft=message.draft,
    ))
    for keyword in message.keywords or []:
        flags.append(str(keyword).encode('utf-8', 'replace'))
    return flags


def _flags_from_imap(flags):
    decoded = [_decode_flag(flag) for flag in (flags or [])]
    keywords = sorted({
        flag for flag in decoded
        if flag and not flag.startswith('\\') and flag.lower() != '$recent'
    })
    standard = helpers.flags_to_booleans(decoded)
    return (standard, keywords)


def _parse_uid_set(text, maximum):
    uids = set()
    for part in str(text).split(','):
        part = part.strip()
        if not part:
            continue
        if ':' in part:
            (start, end) = part.split(':', 1)
            start = maximum if start.strip() == '*' else int(start)
            end = maximum if end.strip() == '*' else int(end)
            if end < start:
                (start, end) = (end, start)
            if end - start > 1000:
                raise ValueError(text)
            uids.update(range(start, end + 1))
        else:
            uids.add(int(part))
    return uids


class OfflineClient:
    '''
    A database-backed stand-in for imapclient.IMAPClient.

    One instance represents one selected folder at a time, like an IMAP
    connection. Callers should not hold it across threads; the helpers create
    a fresh instance per operation for offline accounts.
    '''
    def __init__(self, account):
        if not getattr(account, 'offline', False):
            raise ValueError('OfflineClient requires an offline account.')
        self.account = account
        self.bringdb = account.bringmaildb
        self._selected = None
        self._imap = _FakeImapState()

    def __repr__(self):
        return f'OfflineClient(account={self.account.id})'

    # Folder selection ############################################################################

    def _folder(self, name=None):
        if name is None:
            name = self._selected
        if name is None:
            raise exceptions.InvalidFolder('No folder is selected.')
        folder = self.bringdb.get_folder_by_name(self.account, name)
        if folder is None:
            raise exceptions.NoSuchFolder(name)
        return folder

    def _all_uids(self, folder):
        rows = self.bringdb.select(
            'SELECT uid FROM messages WHERE folder_id == ? ORDER BY uid ASC',
            [folder.id],
        )
        return [int(row['uid']) for row in rows]

    def list_folders(self):
        '''
        The account's complete folder list. Offline accounts have a fixed set,
        so this creates any missing record instead of contacting a server.
        '''
        with self.bringdb.transaction:
            folders = self.bringdb.ensure_offline_folders(self.account)
        return [
            ([], constants.OFFLINE_FOLDER_DELIMITER, folder.name)
            for folder in folders
        ]

    def select_folder(self, name, readonly=False):
        folder = self._folder(name)
        self._selected = folder.name
        return {
            b'UIDVALIDITY': folder.uidvalidity,
            b'UIDNEXT': folder.uidnext,
            b'HIGHESTMODSEQ': folder.highestmodseq,
        }

    # Capabilities and connection lifecycle #####################################################

    def has_capability(self, capability):
        return False

    def capabilities(self):
        return ()

    def enable(self, capability):
        return None

    def noop(self):
        return None

    def logout(self):
        return None

    def shutdown(self):
        return None

    def close(self):
        return None

    # Search and fetch ############################################################################

    def _resolve_uids(self, uids, folder):
        maximum = max(self._all_uids(folder), default=0)
        if isinstance(uids, (str, bytes)):
            return sorted(_parse_uid_set(helpers.decode_text(uids), maximum))
        resolved = set()
        for uid in uids:
            if isinstance(uid, str) and ':' in uid:
                resolved.update(_parse_uid_set(uid, maximum))
            else:
                resolved.add(int(uid))
        return sorted(resolved)

    def search(self, criteria):
        folder = self._folder()
        criteria = list(criteria or [])
        if not criteria:
            return []
        key = helpers.decode_text(criteria[0]).upper()
        if key == 'ALL':
            return self._all_uids(folder)
        if key == 'UID' and len(criteria) >= 2:
            uid = int(criteria[1])
            return [uid] if uid in set(self._all_uids(folder)) else []
        if (
                key == 'HEADER' and
                len(criteria) >= 3 and
                helpers.decode_text(criteria[1]).upper() == 'MESSAGE-ID'):
            message_id = criteria[2]
            message_id = helpers.decode_text(message_id)
            rows = self.bringdb.select(
                '''
                SELECT uid FROM messages
                WHERE folder_id == ? AND message_id == ?
                ORDER BY uid ASC
                ''',
                [folder.id, message_id],
            )
            return [int(row['uid']) for row in rows]
        raise exceptions.NotSupported(
            f'Unsupported offline search: {criteria!r}'
        )

    def fetch(self, messages, data, modifiers=None):
        folder = self._folder()
        uids = self._resolve_uids(messages, folder)
        if isinstance(data, (str, bytes)):
            data = [data]
        wanted = [helpers.decode_text(item).upper() for item in data]
        response = {}
        for uid in uids:
            row = self.bringdb.select_one(
                'SELECT * FROM messages WHERE folder_id == ? AND uid == ?',
                [folder.id, uid],
            )
            if row is None:
                continue
            message = self.bringdb.get_cached_instance(objects.Message, row)
            raw = None
            if message.body_fetched and message.raw is not None:
                raw = self.bringdb.decompress_raw(message.raw, message.raw_codec)

            data_items = {}
            if 'FLAGS' in wanted:
                data_items[b'FLAGS'] = tuple(_message_flags(message))
            if 'INTERNALDATE' in wanted:
                timestamp = message.meta.get('internaldate_unix')
                if timestamp is None:
                    timestamp = message.date_unix or 0
                data_items[b'INTERNALDATE'] = datetime.datetime.fromtimestamp(
                    int(timestamp),
                    datetime.timezone.utc,
                )
            if 'RFC822.SIZE' in wanted:
                data_items[b'RFC822.SIZE'] = (
                    message.size
                    if message.size is not None
                    else len(raw or b'')
                )
            if any(item in wanted for item in ('BODY.PEEK[]', 'BODY[]', 'RFC822')):
                if raw is not None:
                    data_items[b'BODY[]'] = raw
            if any(item in wanted for item in ('BODY.PEEK[HEADER]', 'BODY[HEADER]')):
                if raw is not None:
                    data_items[b'BODY[HEADER]'] = (
                        raw.split(b'\r\n\r\n', 1)[0] + b'\r\n\r\n'
                    )
            if data_items:
                response[uid] = data_items
        return response

    # Flags #######################################################################################

    def _apply_flags(self, messages, flags, *, add):
        folder = self._folder()
        uids = self._resolve_uids(messages, folder)
        changed = [item for item in (_decode_flag(flag) for flag in (flags or [])) if item]
        with self.bringdb.transaction:
            for uid in uids:
                row = self.bringdb.select_one(
                    'SELECT * FROM messages WHERE folder_id == ? AND uid == ?',
                    [folder.id, uid],
                )
                if row is None:
                    continue
                message = self.bringdb.get_cached_instance(objects.Message, row)
                current = {_decode_flag(flag) for flag in _message_flags(message)}
                if add:
                    current.update(changed)
                else:
                    current.difference_update(changed)
                (standard, keywords) = _flags_from_imap(sorted(current))
                self.bringdb.update(
                    table=objects.Message,
                    pairs={
                        'id': message.id,
                        'seen': standard['seen'],
                        'flagged': standard['flagged'],
                        'answered': standard['answered'],
                        'imap_deleted': standard['imap_deleted'],
                        'draft': standard['draft'],
                        'keywords': json.dumps(keywords) if keywords else None,
                    },
                    where_key='id',
                )
        self.bringdb.caches[objects.Message].clear()

    def add_flags(self, messages, flags):
        self._apply_flags(messages, flags, add=True)

    def remove_flags(self, messages, flags):
        self._apply_flags(messages, flags, add=False)

    # Appending and moving ########################################################################

    def _uidvalidity(self, folder):
        if folder.uidvalidity is None:
            return constants.OFFLINE_UIDVALIDITY
        return int(folder.uidvalidity)

    def _next_uid(self, folder):
        row = self.bringdb.select_one(
            'SELECT MAX(uid) AS max_uid FROM messages WHERE folder_id == ?',
            [folder.id],
        )
        max_uid = int(row['max_uid'] or 0) if row is not None else 0
        next_uid = max(max_uid + 1, int(folder.uidnext or 1))
        return next_uid

    def _set_uidnext(self, folder, uid):
        next_uid = max(int(folder.uidnext or 1), int(uid) + 1)
        self.bringdb.update(
            table=objects.Folder,
            pairs={'id': folder.id, 'uidnext': next_uid},
            where_key='id',
        )
        folder.uidnext = next_uid

    def append(self, folder, message, flags=(), msg_time=None):
        target = self._folder(folder)
        if not isinstance(message, (bytes, bytearray)):
            raise TypeError(message)
        raw = bytes(message)
        (_message, meta) = helpers.parse_full_message(
            raw,
            internaldate=msg_time,
            size=len(raw),
        )
        (standard, keywords) = _flags_from_imap(flags)
        with self.bringdb.transaction:
            uid = self._next_uid(target)
            self.bringdb.add_message(
                account=self.account,
                folder=target,
                uid=uid,
                uidvalidity=self._uidvalidity(target),
                raw=raw,
                meta=meta,
                size=len(raw),
                flags=standard,
                keywords=keywords,
            )
            self._set_uidnext(target, uid)
        self.bringdb.caches[objects.Message].clear()
        return [f'[APPENDUID {self._uidvalidity(target)} {uid}]'.encode('ascii')]

    def _copy_rows(self, uids, destination_name):
        source = self._folder()
        target = self._folder(destination_name)
        source_uids = []
        destination_uids = []
        with self.bringdb.transaction:
            for uid in uids:
                row = self.bringdb.select_one(
                    'SELECT * FROM messages WHERE folder_id == ? AND uid == ?',
                    [source.id, uid],
                )
                if row is None:
                    continue
                message = self.bringdb.get_cached_instance(objects.Message, row)
                new_uid = self._next_uid(target)
                raw = None
                if message.raw is not None:
                    raw = self.bringdb.decompress_raw(message.raw, message.raw_codec)
                self.bringdb.add_message(
                    account=self.account,
                    folder=target,
                    uid=new_uid,
                    uidvalidity=self._uidvalidity(target),
                    raw=raw,
                    meta=message.meta,
                    size=message.size,
                    flags={
                        'seen': message.seen,
                        'flagged': message.flagged,
                        'answered': message.answered,
                        'imap_deleted': message.imap_deleted,
                        'draft': message.draft,
                    },
                    keywords=message.keywords,
                    modseq=None,
                )
                self._set_uidnext(target, new_uid)
                source_uids.append(int(uid))
                destination_uids.append(int(new_uid))
        self.bringdb.caches[objects.Message].clear()
        return (source, target, source_uids, destination_uids)

    def _copyuid_response(self, target, source_uids, destination_uids):
        if not source_uids:
            return []
        source_text = ','.join(str(uid) for uid in source_uids)
        destination_text = ','.join(str(uid) for uid in destination_uids)
        return [
            f'[COPYUID {self._uidvalidity(target)} {source_text} {destination_text}]'.encode('ascii')
        ]

    def copy(self, messages, folder):
        source = self._folder()
        uids = self._resolve_uids(messages, source)
        (source, target, source_uids, destination_uids) = self._copy_rows(uids, folder)
        return self._copyuid_response(target, source_uids, destination_uids)

    def move(self, messages, folder):
        source = self._folder()
        uids = self._resolve_uids(messages, source)
        target = self._folder(folder)
        source_uids = []
        destination_uids = []
        with self.bringdb.transaction:
            for uid in uids:
                row = self.bringdb.select_one(
                    'SELECT * FROM messages WHERE folder_id == ? AND uid == ?',
                    [source.id, uid],
                )
                if row is None:
                    continue
                message = self.bringdb.get_cached_instance(objects.Message, row)
                new_uid = self._next_uid(target)
                self.bringdb.update(
                    table=objects.Message,
                    pairs={
                        'id': message.id,
                        'folder_id': target.id,
                        'uid': new_uid,
                        'uidvalidity': self._uidvalidity(target),
                    },
                    where_key='id',
                )
                self._set_uidnext(target, new_uid)
                self._set_uidnext(source, uid)
                source_uids.append(int(uid))
                destination_uids.append(int(new_uid))
        self.bringdb.caches[objects.Message].clear()
        return self._copyuid_response(target, source_uids, destination_uids)

    def expunge(self, messages):
        folder = self._folder()
        uids = self._resolve_uids(messages, folder)
        with self.bringdb.transaction:
            for uid in uids:
                row = self.bringdb.select_one(
                    'SELECT id FROM messages WHERE folder_id == ? AND uid == ?',
                    [folder.id, uid],
                )
                if row is None:
                    continue
                self.bringdb.delete(
                    table=objects.Message,
                    pairs={'id': row['id']},
                )
                self._set_uidnext(folder, uid)
        self.bringdb.caches[objects.Message].clear()
