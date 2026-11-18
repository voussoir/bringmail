'''
Constants and database schema for BringMail.

BringMail requires SQLite 3.38.0 or newer. Generated columns were added in
3.31.0 and the JSON functions became part of the SQLite core in 3.38.0, so
3.38.0 is the lowest version that can run this schema without relying on a
separately compiled JSON1 extension. See bringmail.exceptions.SQLiteTooOld.
'''

# Increment this when DB_INIT changes, and add a matching upgrade function in
# bringmail/upgrades.py so existing databases can be brought forward.
DATABASE_VERSION = 2

MINIMUM_SQLITE = (3, 38, 0)
MINIMUM_SQLITE_STRING = '.'.join(str(part) for part in MINIMUM_SQLITE)

DEFAULT_DATADIR = '_bringmail'
DEFAULT_DBNAME = 'bringmail.db'

DEFAULT_AUTOREFRESH_INTERVAL = 900
DEFAULT_PAGE_SIZE = 500
MAX_PAGE_SIZE = 2500

# IMAP timeouts, in seconds.
IMAP_CONNECT_TIMEOUT = 15
IMAP_READ_TIMEOUT = 120

# Network batch sizes.
FETCH_BATCH_HEADERS = 50
FETCH_BATCH_FULL = 5
FETCH_BATCH_FLAGS = 500
FLAG_BATCH = 500

# IMAP IDLE. RFC 2177 allows a client to remain idle for up to 29 minutes,
# but some servers use shorter inactivity timeouts, so renew sooner.
IDLE_RENEW_SECONDS = 9 * 60
# The poll timeout is a local wake-up for shutdown and renewal checks. It does
# not send traffic to the server.
IDLE_POLL_SECONDS = 5
IDLE_DEBOUNCE_SECONDS = 2
IDLE_RECONNECT_MIN_SECONDS = 5
IDLE_RECONNECT_MAX_SECONDS = 5 * 60
IDLE_UNSUPPORTED_RETRY_SECONDS = 60 * 60
IDLE_STARTUP_STAGGER_SECONDS = 1

# When importing a folder for the first time and more than this many messages
# are new, fetch headers for everything first and then fill in the bodies
# newest-first. This keeps the UI usable while a large archive downloads.
HEADER_FIRST_THRESHOLD = 50

# A single message larger than this is fetched by itself rather than in a
# batch with its neighbors.
LARGE_MESSAGE_BYTES = 10 * 1024 * 1024

# Raw message compression codecs. 1 = zlib.
RAW_CODEC_ZLIB = 1

META_VERSION = 1

# Pending (offline) operations. A queued row records the operation the user
# asked for plus the pre-state they were looking at, so a flush can tell
# whether the server state still matches what the user acted on.
OPERATION_SEEN = 'seen'
OPERATION_FLAGGED = 'flagged'
OPERATION_ANSWERED = 'answered'
OPERATION_KEYWORDS = 'keywords'
OPERATION_MOVE = 'move'
OPERATION_DELETE = 'delete'
OPERATION_RESTORE = 'restore'
# Cross-account operations. The destination lives in another account and is
# reached with APPEND rather than a same-server MOVE.
OPERATION_COPY_ACCOUNT = 'copy_account'
OPERATION_MOVE_ACCOUNT = 'move_account'

OPERATION_PRIVATE_FLAGS = (
    OPERATION_SEEN,
    OPERATION_FLAGGED,
    OPERATION_ANSWERED,
)
OPERATION_FLAG_KINDS = OPERATION_PRIVATE_FLAGS + (OPERATION_KEYWORDS,)
OPERATION_FOLDER_KINDS = (
    OPERATION_MOVE,
    OPERATION_DELETE,
    OPERATION_RESTORE,
    OPERATION_COPY_ACCOUNT,
    OPERATION_MOVE_ACCOUNT,
)
OPERATION_CROSS_ACCOUNT_KINDS = (
    OPERATION_COPY_ACCOUNT,
    OPERATION_MOVE_ACCOUNT,
)
OPERATION_KINDS = OPERATION_FLAG_KINDS + OPERATION_FOLDER_KINDS

PENDING_STATE_PENDING = 'pending'
PENDING_STATE_SENT = 'sent'
PENDING_STATE_FAILED = 'failed'
PENDING_STATE_CONFLICTED = 'conflicted'
PENDING_STATE_SUPERSEDED = 'superseded'

# Connection failures leave the row pending and retry later. Permanent
# refusals move the row to failed with no further attempts.
OUTBOX_RETRY_BASE_SECONDS = 5
OUTBOX_RETRY_MAX_SECONDS = 15 * 60
OUTBOX_RETRY_EXPIRE_SECONDS = 24 * 60 * 60
# A local-intent flag write that loses a conditional STORE race is retried
# once against the newer mod-sequence before it is given up on.
OUTBOX_MAX_CONDITIONAL_RETRIES = 1
# How many pending rows to load per account in one flush.
OUTBOX_BATCH_SIZE = 500

SPECIAL_USE_INBOX = '\\Inbox'
SPECIAL_USE_TRASH = '\\Trash'
SPECIAL_USE_SENT = '\\Sent'
SPECIAL_USE_DRAFTS = '\\Drafts'
SPECIAL_USE_JUNK = '\\Junk'
SPECIAL_USE_ARCHIVE = '\\Archive'

SPECIAL_USES = (
    SPECIAL_USE_INBOX,
    SPECIAL_USE_TRASH,
    SPECIAL_USE_SENT,
    SPECIAL_USE_DRAFTS,
    SPECIAL_USE_JUNK,
    SPECIAL_USE_ARCHIVE,
)

VERSION = '0.0.1'

DB_INIT = f'''
CREATE TABLE IF NOT EXISTS accounts(
    id INTEGER PRIMARY KEY,
    label TEXT,
    host TEXT NOT NULL,
    port INT NOT NULL,
    ssl BOOLEAN NOT NULL,
    starttls BOOLEAN NOT NULL,
    username TEXT NOT NULL,
    password TEXT NOT NULL,
    autorefresh_interval INT NOT NULL,
    ui_order_rank REAL NOT NULL,
    created INT NOT NULL,
    last_refresh INT NOT NULL,
    last_refresh_attempt INT NOT NULL,
    last_refresh_error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS index_accounts_endpoint
    ON accounts(host, port, username);
CREATE INDEX IF NOT EXISTS index_accounts_ui_order_rank
    ON accounts(ui_order_rank);

CREATE TABLE IF NOT EXISTS folders(
    id INTEGER PRIMARY KEY,
    account_id INT NOT NULL,
    name TEXT NOT NULL,
    display_name TEXT,
    delimiter TEXT,
    attributes TEXT,
    special_use TEXT,
    uidvalidity INT,
    uidnext INT,
    highestmodseq INT,
    last_sync INT NOT NULL,
    last_sync_error TEXT,
    sync_enabled BOOLEAN NOT NULL,
    ui_order_rank REAL NOT NULL,
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS index_folders_account_name
    ON folders(account_id, name);
CREATE INDEX IF NOT EXISTS index_folders_account_rank
    ON folders(account_id, ui_order_rank);

CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY,
    account_id INT NOT NULL,
    folder_id INT NOT NULL,
    uid INT NOT NULL,
    uidvalidity INT NOT NULL,
    raw BLOB,
    raw_codec INT NOT NULL,
    body_fetched BOOLEAN NOT NULL,
    meta TEXT NOT NULL,
    meta_version INT NOT NULL,
    seen BOOLEAN NOT NULL,
    flagged BOOLEAN NOT NULL,
    answered BOOLEAN NOT NULL,
    imap_deleted BOOLEAN NOT NULL,
    draft BOOLEAN NOT NULL,
    keywords TEXT,
    modseq INT,
    size INT,
    local_deleted BOOLEAN NOT NULL DEFAULT 0,
    subject TEXT GENERATED ALWAYS AS (json_extract(meta, '$.subject')) VIRTUAL,
    from_name TEXT GENERATED ALWAYS AS (json_extract(meta, '$.from[0].name')) VIRTUAL,
    from_addr TEXT GENERATED ALWAYS AS (json_extract(meta, '$.from[0].addr')) VIRTUAL,
    date_unix INT GENERATED ALWAYS AS (json_extract(meta, '$.date_unix')) VIRTUAL,
    message_id TEXT GENERATED ALWAYS AS (json_extract(meta, '$.message_id')) VIRTUAL,
    has_attachments INT GENERATED ALWAYS AS (json_extract(meta, '$.has_attachments')) VIRTUAL,
    FOREIGN KEY(folder_id) REFERENCES folders(id) ON DELETE CASCADE,
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS index_messages_folder_uid
    ON messages(folder_id, uid);
CREATE INDEX IF NOT EXISTS index_messages_folder_date
    ON messages(folder_id, date_unix DESC);
CREATE INDEX IF NOT EXISTS index_messages_account_date
    ON messages(account_id, date_unix DESC);
CREATE INDEX IF NOT EXISTS index_messages_folder_seen
    ON messages(folder_id, seen);
CREATE INDEX IF NOT EXISTS index_messages_folder_flagged
    ON messages(folder_id, flagged);
CREATE INDEX IF NOT EXISTS index_messages_account_message_id
    ON messages(account_id, message_id);

CREATE TABLE IF NOT EXISTS pending_operations(
    id INTEGER PRIMARY KEY,
    account_id INT NOT NULL,
    folder_id INT NOT NULL,
    message_id INT,
    message_uid INT NOT NULL,
    message_uidvalidity INT,
    operation TEXT NOT NULL,
    enabled INT,
    base_folder_id INT,
    target_folder_id INT,
    target_keywords TEXT,
    base_modseq INT,
    observed TEXT,
    state TEXT NOT NULL,
    attempts INT NOT NULL,
    next_attempt INT NOT NULL,
    last_error TEXT,
    created INT NOT NULL,
    updated INT NOT NULL,
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE,
    FOREIGN KEY(folder_id) REFERENCES folders(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS index_pending_ops_account_state
    ON pending_operations(account_id, state);
CREATE INDEX IF NOT EXISTS index_pending_ops_message
    ON pending_operations(message_id);
CREATE INDEX IF NOT EXISTS index_pending_ops_folder_state
    ON pending_operations(folder_id, state);
'''


# Settings used by the network connection. These are applied to both sqlite
# connections in bringmaildb._load_pragmas.
READ_PRAGMAS = {
    'busy_timeout': 5000,
    'cache_size': -50000,
    'foreign_keys': 'on',
    'query_only': 'on',
}

WRITE_PRAGMAS = {
    'busy_timeout': 5000,
    'cache_size': -50000,
    'foreign_keys': 'on',
    'journal_mode': 'wal',
    'synchronous': 'normal',
    'temp_store': 'memory',
}
