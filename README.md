BringMail
=========

BringMail is a local IMAP mail reader with an offline SQLite cache and a
three-panel web interface modeled on
[BringRSS](https://github.com/voussoir/bringrss).

The left panel lists accounts and their folders, the upper-right panel lists
messages in the selected folder, and the lower-right panel shows the selected
message. All mail is downloaded into a local database, so cached folders remain
readable when the mail server is unreachable.

Features
--------

- Unlimited accounts, each with its own IMAP settings and refresh interval.
- Folder discovery, including special-use folders (Inbox, Sent, Drafts, Junk,
  Archive, Trash).
- Incremental synchronization using UIDVALIDITY, UIDNEXT, and CONDSTORE where
  the server supports them.
- Every message body is downloaded during refresh and stored locally, so every
  message you have synced is available offline.
- Optional header-first import of large folders: the message list becomes
  usable while bodies fill in newest-first.
- Read/unread, flag/unflag, move to folder, and delete to Trash.
- HTML mail is sanitized server-side and rendered inside a sandboxed iframe.
  Remote images are blocked until you click "Load images"; inline `cid:` images
  come from the local cache.
- Multiple themes.

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
worker process would start its own background scheduler and refresh worker,
causing duplicate IMAP synchronization. If you run it under a different user
account, that user needs access to the database and any cached credentials in it.

Database
--------

The `_bringmail/bringmail.db` schema has three tables:

- `accounts`: IMAP endpoint, username, password, refresh interval, and last
  refresh state.
- `folders`: IMAP folder names, hierarchy metadata, special-use flags, and the
  UIDVALIDITY/UIDNEXT/HIGHESTMODSEQ synchronization checkpoint.
- `messages`: one self-contained row per message, with the compressed RFC822
  bytes, a decoded `meta` JSON document, mutable flags, and generated columns
  for `subject`, `from_name`, `from_addr`, `date_unix`, `message_id`, and
  `has_attachments`.

The `messages` generated columns are VIRTUAL columns computed with
`json_extract` from `meta` and indexed for the message list. The database is
unbounded: BringMail never prunes or expires cached mail. Delete an account to
remove its cache.

If you delete or move a message on the server, the next refresh removes or
relocates the local row. A UIDVALIDITY change causes that folder's local cache
to be discarded and re-downloaded.

Tests
-----

    python -m unittest discover -s tests -v

The tests cover message parsing, generated-column queries, local flags,
pagination, incremental fake-IMAP sync, UIDVALIDITY resets, reusable IMAP
sessions, and the Flask routes. They do not contact a real mail server.

Architecture
------------

- `bringmail/`: core package. `objects.py` contains Account, Folder, and
  Message; `bringmaildb.py` contains the database and IMAP sync engine;
  `helpers.py` contains MIME parsing, sanitization, and IMAP connection/session
  handling.
- `frontends/bringmail_flask/`: Flask backend and three-panel frontend.
  `backend/common.py` sets up the app, background scheduler, refresh worker, and
  server-sent events. `backend/endpoints/` contains the HTTP API.
- `frontends/bringmail_cli.py` and `frontends/bringmail_repl.py`: command-line
  management and an interactive database shell.
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
- Offline queueing of write actions. Reading the cache works offline; marking
  read, moving, and deleting require a working mail server connection.
