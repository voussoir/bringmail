BringMail
=========

It brings you the mail.

I am writing this project with DeepSeek Harness to see what it's like.

BringMail copies most of its foundation from [BringRSS](https://git.voussoir.net/voussoir/bringrss).

## What am I looking at

BringMail is an IMAP email client made with Python, SQLite3, and Flask. Its main features are:

- Unlimited accounts.
- Unified inbox+outbox for all accounts.
- "Offline" accounts, for storing mail in the database not backed by any mail server.
- IMAP IDLE support.
- Plaintext rendering by default.

## Requires

- Python 3.11+.
- SQLite 3.38.0+ with JSON functions.

## Insecurity

Right now, the application stores the email account password directly in the database as plain text. There is no login or authentication.
