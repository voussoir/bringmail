import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTENDS_DIR = os.path.join(REPO_ROOT, 'frontends')
for path in (REPO_ROOT, FRONTENDS_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from bringmail import bringmaildb
from bringmail import constants
from bringmail import helpers

from bringmail_flask import backend

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'simple.eml')


class FlaskTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(FIXTURE, 'rb') as handle:
            cls.raw = handle.read()

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = bringmaildb.BringMailDB(data_directory=self.tempdir.name, create=True)
        backend.common.bringdb = self.db
        backend.site.config['TESTING'] = True
        self.client = backend.site.test_client()

        with self.db.transaction:
            self.account = self.db.add_account(
                label='Test',
                host='imap.example.com',
                port=993,
                ssl=True,
                starttls=False,
                username='user@example.com',
                password='hunter2',
                autorefresh_interval=900,
            )
            self.folder = self.db.add_folder(
                account=self.account,
                name='INBOX',
                display_name='Inbox',
                delimiter='/',
                special_use=constants.SPECIAL_USE_INBOX,
            )
            (message, meta) = helpers.parse_full_message(self.raw, size=len(self.raw))
            self.message = self.db.add_message(
                account=self.account,
                folder=self.folder,
                uid=1,
                uidvalidity=1,
                raw=self.raw,
                meta=meta,
                size=len(self.raw),
                flags={'seen': False},
            )

    def tearDown(self):
        backend.common.clear_refresh_queue()
        backend.common.bringdb = None
        self.db.close()
        self.tempdir.cleanup()

    def test_root_and_about(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'BringMail', response.data)

        response = self.client.get('/about')
        self.assertEqual(response.status_code, 200)

    def test_root_has_no_flag_controls(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'flag_button', response.data)
        self.assertNotIn(b'unflag_button', response.data)
        self.assertNotIn(b'>Flag<', response.data)
        self.assertNotIn(b'>Unflag<', response.data)

    def test_root_has_status_bar(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="status_bar"', response.data)
        self.assertIn(b'id="status_text"', response.data)
        self.assertIn(b'id="status_detail"', response.data)
        self.assertIn(b'id="status_timestamp"', response.data)
        self.assertIn(b'id="reader_iframe" class="hidden"', response.data)
        self.assertNotIn(b'load_more_button', response.data)

    def test_account_settings_pages(self):
        response = self.client.get('/account/add')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Add account', response.data)
        self.assertNotIn(b'Themes:', response.data)

        response = self.client.get(f'/account/{self.account.id}/settings')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Inbox', response.data)
        self.assertNotIn(b'Themes:', response.data)

        response = self.client.get('/about')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Themes:', response.data)

    def test_static_assets(self):
        for path in (
                '/static/css/common.css',
                '/static/css/bringmail.css',
                '/static/js/root_app.js',
                '/static/js/api.js',
                '/static/favicon.svg'):
            response = self.client.get(path)
            try:
                self.assertEqual(response.status_code, 200, path)
            finally:
                response.close()

    def test_account_settings_copy_error(self):
        error = (
            'Traceback (most recent call last):\n'
            '  File "bringmail/bringmaildb.py", line 1, in sync\n'
            'RuntimeError: sample failure'
        )
        with self.db.transaction:
            self.db.execute(
                'UPDATE accounts SET last_refresh_error = ? WHERE id == ?',
                [error, self.account.id],
            )
        self.account.last_refresh_error = error

        response = self.client.get(f'/account/{self.account.id}/settings')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="copy_error_button"', response.data)
        self.assertIn(b'id="last_refresh_error"', response.data)
        self.assertIn(b'RuntimeError: sample failure', response.data)

    def test_layout_selection(self):
        response = self.client.get('/')
        self.assertIn(b'class="layout_ltb"', response.data)

        response = self.client.get('/about?layout=lmr')
        self.assertEqual(response.status_code, 200)

        response = self.client.get('/')
        self.assertIn(b'class="layout_lmr"', response.data)

        response = self.client.get('/about?layout=ltb')
        self.assertEqual(response.status_code, 200)

        response = self.client.get('/')
        self.assertIn(b'class="layout_ltb"', response.data)

    def test_account_messages_only_inbox_and_sent(self):
        def meta_for(subject):
            return {
                'subject': subject,
                'from': [],
                'to': [],
                'cc': [],
                'date_unix': 1,
                'message_id': f'<{subject}@example.com>',
                'has_attachments': False,
                'snippet': '',
            }

        with self.db.transaction:
            sent = self.db.add_folder(
                account=self.account,
                name='Sent',
                display_name='Sent',
                delimiter='/',
                special_use=constants.SPECIAL_USE_SENT,
            )
            sent_sub = self.db.add_folder(
                account=self.account,
                name='Sent/2026',
                display_name='2026',
                delimiter='/',
            )
            junk = self.db.add_folder(
                account=self.account,
                name='Junk',
                display_name='Junk',
                delimiter='/',
                special_use=constants.SPECIAL_USE_JUNK,
            )
            for (folder, uid, subject) in (
                    (sent, 1, 'sent'),
                    (sent_sub, 1, 'sent-year'),
                    (junk, 1, 'junk')):
                self.db.add_message(
                    account=self.account,
                    folder=folder,
                    uid=uid,
                    uidvalidity=1,
                    raw=None,
                    meta=meta_for(subject),
                    size=0,
                )

        response = self.client.get(f'/account/{self.account.id}/messages.json?limit=100')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(
            {message['subject'] for message in data['messages']},
            {'Hello ✓', 'sent', 'sent-year'},
        )
        self.assertEqual(data['account']['unread_count'], 3)

    def test_all_accounts_messages(self):
        with self.db.transaction:
            sent = self.db.add_folder(
                account=self.account,
                name='Sent',
                display_name='Sent',
                delimiter='/',
                special_use=constants.SPECIAL_USE_SENT,
            )
            junk = self.db.add_folder(
                account=self.account,
                name='Junk',
                display_name='Junk',
                delimiter='/',
                special_use=constants.SPECIAL_USE_JUNK,
            )
            for (folder, uid, subject) in (
                    (sent, 1, 'sent'),
                    (junk, 1, 'junk')):
                self.db.add_message(
                    account=self.account,
                    folder=folder,
                    uid=uid,
                    uidvalidity=1,
                    raw=None,
                    meta={
                        'subject': subject,
                        'from': [],
                        'to': [],
                        'cc': [],
                        'date_unix': 1,
                        'message_id': f'<{subject}@example.com>',
                        'has_attachments': False,
                        'snippet': '',
                    },
                    size=0,
                )

        response = self.client.get('/all/messages.json?limit=100')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(
            {message['subject'] for message in data['messages']},
            {'Hello ✓', 'sent'},
        )

    def test_accounts_json_hides_password(self):
        response = self.client.get('/accounts.json')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['id'], self.account.id)
        self.assertNotIn('password', data[0])
        self.assertEqual(data[0]['folders'][0]['id'], self.folder.id)

    def test_message_list_and_body(self):
        response = self.client.get(f'/folder/{self.folder.id}/messages.json')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(len(data['messages']), 1)
        self.assertEqual(data['messages'][0]['id'], self.message.id)

        response = self.client.get(f'/message/{self.message.id}.json')
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertIn('Hello <b>HTML</b> text.', body['html'])
        self.assertNotIn('<script', body['html'])
        self.assertEqual(len(body['attachments']), 1)

        response = self.client.get(f'/message/{self.message.id}/part/2')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Attachment contents.', response.data)

    def test_account_update(self):
        response = self.client.post(
            f'/account/{self.account.id}/update',
            data={
                'label': 'Renamed',
                'host': 'imap.example.com',
                'port': '993',
                'ssl': 'true',
                'starttls': 'false',
                'username': 'user@example.com',
                'autorefresh_interval': '1800',
            },
            headers={'X-BringMail-Request': '1'},
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['label'], 'Renamed')
        self.assertEqual(data['autorefresh_interval'], 1800)
        self.assertNotIn('password', data)

    def test_csrf_header_required_for_post(self):
        response = self.client.post(f'/account/{self.account.id}/refresh')
        self.assertEqual(response.status_code, 403)

        response = self.client.post(
            f'/account/{self.account.id}/refresh',
            headers={'X-BringMail-Request': '1'},
        )
        self.assertEqual(response.status_code, 200)


if __name__ == '__main__':
    unittest.main()
