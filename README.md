BringMail
=========

BringMail is a local IMAP mail reader with an offline SQLite cache and a
three-panel web interface modeled on
[BringRSS](https://github.com/voussoir/bringrss).

The default LTB layout has a left panel for accounts and folders, an
upper-right message list, and a lower-right reader. The LMR layout puts the
account/folder selector, message list, and reader in three columns, with the
reader occupying the largest column. Layout is selected on the About page.
All mail is downloaded into a local database, so cached folders remain
readable when the mail server is unreachable.

Features
--------

- Unlimited accounts, each with its own IMAP settings and refresh interval.
- Folder discovery, including special-use folders (Inbox, Sent, Drafts, Junk,
  Archive, Trash).
- Selecting an account shows INBOX and SENT plus their subfolders. Junk, Trash,
  Archive, and unrelated folders remain available as individual folders.
- An All accounts entry combines those INBOX/SENT views across every account.
- Incremental synchronization using UIDVALIDITY, UIDNEXT, and CONDSTORE where
  the server supports them.
- IMAP IDLE push for each account's INBOX when the server advertises it. Each
  watched INBOX holds one IMAP connection, and there is no per-account toggle.
  IDLE keeps the account tree and unread counts current; it does not reload the
  message list or reader. Re-select the account or folder to load messages.
  Servers without IDLE continue to use the scheduled refresh.
- Every message body is downloaded during refresh and stored locally, so every
  message you have synced is available offline.
- Optional header-first import of large folders: the message list becomes
  usable while bodies fill in newest-first.
- Read/unread, move to folder, and delete to Trash. Flag state is synchronized
  and retained in the backend, but the web UI does not expose flag controls.
- Drag messages onto a folder in another account to transfer them. BringMail
  copies the cached raw message with IMAP APPEND and moves the source to Trash
  only after the copy succeeds. If the source account has no selectable Trash,
  the transfer becomes a copy and the source message remains.
- Offline queueing of read/unread, move, delete, and undo. Actions are recorded
  in the local database and replayed when the connection returns; see
  "Offline actions" below.
- Clicking a message's read/unread dot toggles that message without opening it.
- The browser tab title shows the total unread count across all accounts, for
  example `BringMail (5)`. It reverts to `BringMail` when nothing is unread.
- Right-clicking a message offers Mark as read, Mark as unread, Delete, Move to
  folder, and Undo delete for a pending or flushed delete. A single-account
  selection can move to any account's folder; selections spanning source
  accounts remain blocked.
- The reader opens in plaintext mode by default; HTML remains one click away.
  HTML mail is sanitized server-side and rendered inside a sandboxed iframe.
  Remote images are blocked until you click "Load images"; inline `cid:` images
  come from the local cache.
- A bottom status bar reports active synchronization progress through SSE.
- The account list shows `↑` for queued actions and `!` for actions that could
  not be synced. Right-click an account for Retry pending and Discard pending.
- The reader panel uses theme-aware dark colors; HTML email content keeps its
  own canvas for sender styling.
- Message lists load 500 at a time and automatically fetch older messages when
  you scroll near the bottom.
- Multiple themes.
- User-selectable LTB and LMR layouts.

Requirements
------------

- Python 3.11 or newer.
- SQLite 3.38.0 or newer, including the JSON functions. BringMail checks this at
  startup and refuses to run on older versions. CPython 3.11+ from python.org
  bundles a new enough SQLite on Windows; Linux users may need a newer
  `libsqlite3`.
- Install dependencies:

      pip install -r requirements.txt

- The `voussoirkit` package provides the database and web helpers.

Setting up
----------

BringMail stores everything in a `_bringmail` directory next to the directory
from which you run the launcher. The directory contains `bringmail.db`.

1. Create a directory for your BringMail data, or use an existing one:

       mkdir D:\bringmail_data
       cd D:\bringmail_data

2. Create the database:

       python D:\Git\BringMail\frontends\bringmail_flask\bringmail_flask_dev.py --init

3. Start the server:

       python D:\Git\BringMail\frontends\bringmail_flask\bringmail_flask_dev.py

   The default address is `http://127.0.0.1:27465`. The default bind address is
   `127.0.0.1`; use `--host 0.0.0.0` only if you understand that this exposes
   the cache to your network. There is no login.

4. Open the web interface and click **+Account**, or use the CLI:

       python D:\Git\BringMail\frontends\bringmail_cli.py add_account --host imap.example.com --username user@example.com

   You will be prompted for the password unless you pass `--password`.

The account settings page accepts the IMAP host, port, SSL/STARTTLS choice,
username, password, and autorefresh interval. The password field is write-only:
leaving it blank keeps the existing password.

Security
--------

**Passwords are stored in plaintext in `_bringmail/bringmail.db`.** This is a
deliberate choice for a local-only tool. Anyone who can read that file, or a
backup of it, can read the passwords and the cached mail.

- Keep the `_bringmail` directory out of version control. The included
  `.gitignore` already ignores it.
- Do not bind the server to a public interface without an authenticating
  reverse proxy. The application has no login.
- Mutating requests require the `X-BringMail-Request: 1` header, which the
  web UI sends automatically. This blocks cross-site form submissions from
  other websites. API clients must send the same header.
- Remote images in HTML mail are blocked by default to avoid tracking pixels.

Command line
------------

Run the CLI from the directory containing `_bringmail`, or from a subdirectory
of it:

    python D:\Git\BringMail\frontends\bringmail_cli.py init
    python D:\Git\BringMail\frontends\bringmail_cli.py accounts
    python D:\Git\BringMail\frontends\bringmail_cli.py add_account --host imap.example.com --username user@example.com
    python D:\Git\BringMail\frontends\bringmail_cli.py refresh
    python D:\Git\BringMail\frontends\bringmail_cli.py refresh_all

`refresh` syncs accounts whose `last_refresh_attempt + autorefresh_interval`
has elapsed. `refresh_all` syncs every account.

There is also a REPL:

    python D:\Git\BringMail\frontends\bringmail_repl.py

It loads the database into the variable `B`.

Running in production
---------------------

`frontends/bringmail_flask/bringmail_flask_prod.py` is a WSGI entrypoint:

    gunicorn bringmail_flask_prod:site --bind "127.0.0.1:27465" --worker-class gevent --workers 1

Run it from the directory containing `_bringmail`. Use exactly one worker: each
worker process would start its own background scheduler, refresh worker, and
IMAP IDLE watchers, causing duplicate connections and synchronization. If you
run it under a different user account, that user needs access to the database
and any cached credentials in it.

Database
--------

The `_bringmail/bringmail.db` schema has four tables:

- `accounts`: IMAP endpoint, username, password, refresh interval, and last
  refresh state.
- `folders`: IMAP folder names, hierarchy metadata, special-use flags, and the
  UIDVALIDITY/UIDNEXT/HIGHESTMODSEQ synchronization checkpoint.
- `messages`: one self-contained row per message, with the compressed RFC822
  bytes, a decoded `meta` JSON document, mutable flags, and generated columns
  for `subject`, `from_name`, `from_addr`, `date_unix`, `message_id`, and
  `has_attachments`. `local_deleted` marks a delete that is still queued.
- `pending_operations`: the offline action queue. Each row records the
  operation, the message it targets, the state the user was looking at when
  they acted (`observed`, `base_modseq`), and its delivery state.

The `messages` generated columns are VIRTUAL columns computed with
`json_extract` from `meta` and indexed for the message list. The database is
unbounded: BringMail never prunes or expires cached mail. Delete an account to
remove its cache. Sent and superseded `pending_operations` rows are retained as
history; account badges and queue actions only consider rows that still need
attention.

If you delete or move a message on the server, the next refresh removes or
relocates the local row, except when a queued operation still refers to that
message. A UIDVALIDITY change causes that folder's local cache to be discarded
and re-downloaded.

Offline actions
---------------

Reading the cache works offline. Write actions are queued and replayed by the
same background worker that performs refreshes.

- Mark read/unread, same-account move, and delete are applied to the local
  database immediately and recorded in `pending_operations`.
- When a connection is available, the queue is flushed one account at a time.
  The affected folders are synchronized first, so the flush knows the current
  server state before writing.
- **Flags**: on servers that advertise CONDSTORE, private flags (`\Seen`,
  `\Flagged`, `\Answered`) are sent with `UNCHANGEDSINCE`. If another client
  changed the message in the meantime, BringMail reapplies the user's value
  once. Keywords and other shared metadata become a conflict instead of
  overwriting the other client.
- **Moves and deletes** cannot be made conditional in IMAP. They are validated
  after the reconcile (message still present, UIDVALIDITY unchanged) and then
  sent. If the message moved or vanished while BringMail was offline, the
  queued action is marked conflicted and the local row is left to the next
  refresh.
- **Cross-account transfers** are queued as `move_account` or `copy_account`.
  The flush opens a second authenticated connection, appends the cached raw
  message to the destination folder, and only then moves the source to the
  source account's Trash. If the source account has no usable Trash folder,
  the operation is copy-only and the source remains. A retry after a lost
  APPEND response searches the destination by Message-ID and size so it does
  not append a second copy. The destination folder is synchronized after a
  successful copy. Discarding a cross-account cleanup after the APPEND has
  already succeeded leaves the destination copy and the source message in
  place.
- **Inverse actions cancel**: queuing a read and then an unread returns to the
  original state and sends nothing. Delete followed by Undo, or a move followed
  by a move back, cancels the pair as long as neither has reached the server.
- **Failures are retained**: a connection failure leaves the rows pending with
  exponential backoff; a server refusal or a conflict moves the row to a
  visible failed/conflicted state. The account list shows `↑` for waiting
  actions and `!` for failed ones. Right-click an account for Retry pending and
  Discard pending. Discarding restores the local cached state recorded before
  the action and marks the rows superseded without sending them.
- Servers without CONDSTORE use a read-compare-write fallback. Shared
  operations may overwrite a change that lands between the read and the write;
  private flags are applied unconditionally.


Upgrading an existing database
------------------------------

BringMail stores its schema version in SQLite's `user_version`. A newer version
of BringMail may change the schema and refuse to open an older database. Stop
the server before upgrading, then run the upgrader from the directory containing
`_bringmail`:

    python D:\Git\BringMail\utilities\database_upgrader.py

The upgrader finds the closest `_bringmail` directory. You can also pass a data
directory explicitly:

    python D:\Git\BringMail\utilities\database_upgrader.py D:\bringmail_data\_bringmail

It prints the pending versions first. `--dry-run` stops after that preview,
`--yes` skips the confirmation, and `--no-backup` disables the copy of
`bringmail.db` that is made before any changes. The backup contains the same
plaintext passwords and cached mail as the live database, so keep it protected
and delete it when it is no longer needed. Each version is applied in its own
transaction, indices are recreated, and foreign keys are checked before the
version is committed. If an upgrade fails, that version is rolled back.

Schema migrations live in `bringmail/upgrades.py`. When changing the schema,
update `constants.DB_INIT`, increment `constants.DATABASE_VERSION`, and add an
`upgrade_X_to_Y` function decorated with `@upgrade_function(X, Y)`.

Tests
-----

    python -m unittest discover -s tests -v

The tests cover message parsing, generated-column queries, local flags,
pagination, incremental and targeted fake-IMAP sync, UIDVALIDITY resets,
reusable IMAP sessions, IMAP IDLE sessions and watchers, the offline pending
queue (consolidation, conditional flag writes, conflicts, backoff), cross-account
APPEND transfers and copy-only fallbacks, database schema upgrades, and the Flask
routes. They do not contact a real mail server.

Architecture
------------

- `bringmail/`: core package. `objects.py` contains Account, Folder, and
  Message; `bringmaildb.py` contains the database, IMAP sync engine, and the
  outbox flush; `outbox.py` contains the pending-operation consolidation rules;
  `helpers.py` contains MIME parsing, sanitization, IMAP connection/session
  handling, and conditional STORE; `idle.py` contains the IDLE protocol
  session; `idle_supervisor.py` contains the reusable watcher threads and
  reconciliation. Frontends start the supervisor explicitly; importing the
  package does not start any threads. `upgrades.py` contains the schema
  migration framework.
- `frontends/bringmail_flask/`: Flask backend and three-panel frontend.
  `backend/common.py` sets up the app, background scheduler, refresh worker, and
  server-sent events. `backend/endpoints/` contains the HTTP API.
- `frontends/bringmail_cli.py` and `frontends/bringmail_repl.py`: command-line
  management and an interactive database shell.
- `utilities/database_upgrader.py`: applies pending schema migrations.
- `tests/`: unit tests and a small `.eml` fixture.

Not implemented
---------------

- Compose, reply, forward, and SMTP sending.
- Creating, renaming, or deleting folders on the server.
- OAuth2. BringMail currently uses IMAP password authentication.
- Server-side full-text search. The message list has the same client-side
  filter box as BringRSS.
- Gmail label semantics. A message that exists in several Gmail labels may be
  downloaded once per folder.
- IDLE watches only INBOX. Mail that lands in other folders is picked up by the
  scheduled refresh; watching more folders would require one connection each.
