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
from bringmail_flask.backend import common

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

    def test_root_panel_ids(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        for element_id in (
                b'accounts',
                b'messages',
                b'reader',
                b'account_list',
                b'message_list',
                b'message_contextmenu',
                b'message_context_title',
                b'message_context_read_button',
                b'message_context_unread_button',
                b'message_context_delete_button',
                b'message_context_restore_button',
                b'context_retry_pending_button',
                b'context_discard_pending_button',
                b'message_context_move_list'):
            self.assertIn(b'id="' + element_id + b'"', response.data)
        self.assertNotIn(b'id="move_select"', response.data)
        self.assertNotIn(b'id="pending_panel"', response.data)

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
        self.assertIn(b'id="status_bar" class="panel"', response.data)
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
        self.assertNotIn(b'id="idle_status_input"', response.data)

        response = self.client.get(f'/account/{self.account.id}/settings')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Inbox', response.data)
        self.assertNotIn(b'Themes:', response.data)
        self.assertIn(b'id="idle_status_input"', response.data)
        self.assertIn(b'id="idle_status_detail"', response.data)

        response = self.client.get('/about')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Themes:', response.data)

    def test_account_add_page_has_offline_checkbox_first(self):
        response = self.client.get('/account/add')
        self.assertEqual(response.status_code, 200)
        data = response.data
        connection_index = data.index(b'<h2>Connection</h2>')
        offline_index = data.index(b'id="offline_input"')
        host_index = data.index(b'id="host_input"')
        self.assertLess(connection_index, offline_index)
        self.assertLess(offline_index, host_index)
        self.assertIn(b'id="connection_fields"', data)
        self.assertIn(b'id="refresh_panel"', data)

    def test_online_account_settings_hide_offline_checkbox(self):
        response = self.client.get(f'/account/{self.account.id}/settings')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'id="offline_input"', response.data)
        self.assertIn(b'id="host_input"', response.data)
        self.assertIn(b'id="refresh_panel"', response.data)
        self.assertIn(b'id="test_button"', response.data)

    def test_create_offline_account_and_edit_settings(self):
        response = self.client.post(
            '/accounts/add',
            data={'offline': 'true', 'label': 'Local archive'},
            headers={'X-BringMail-Request': '1'},
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['offline'])
        self.assertEqual(data['label'], 'Local archive')
        self.assertIsNone(data['host'])
        self.assertIsNone(data['port'])
        self.assertIsNone(data['username'])
        self.assertEqual(data['autorefresh_interval'], 0)
        self.assertEqual(
            {folder['name'] for folder in data['folders']},
            {'INBOX', 'Trash'},
        )
        account_id = data['id']

        response = self.client.get(f'/account/{account_id}/settings')
        self.assertEqual(response.status_code, 200)
        page = response.data
        self.assertIn(b'id="offline_input" checked disabled', page)
        self.assertNotIn(b'id="host_input"', page)
        self.assertNotIn(b'id="password_input"', page)
        self.assertNotIn(b'id="username_input"', page)
        self.assertNotIn(b'id="interval_hours_input"', page)
        self.assertNotIn(b'id="refresh_panel"', page)
        self.assertNotIn(b'id="test_button"', page)
        self.assertIn(b'id="delete_button"', page)
        self.assertIn(b'Inbox', page)
        self.assertIn(b'Trash', page)

        response = self.client.post(
            f'/account/{account_id}/update',
            data={'label': 'Renamed archive'},
            headers={'X-BringMail-Request': '1'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['label'], 'Renamed archive')

        response = self.client.post(
            f'/account/{account_id}/update',
            data={'offline': 'true'},
            headers={'X-BringMail-Request': '1'},
        )
        self.assertEqual(response.status_code, 400)

        response = self.client.post(
            f'/account/{account_id}/update',
            data={'host': 'imap.example.com'},
            headers={'X-BringMail-Request': '1'},
        )
        self.assertEqual(response.status_code, 400)

    def test_offline_account_add_validation(self):
        response = self.client.post(
            '/accounts/add',
            data={'offline': 'true'},
            headers={'X-BringMail-Request': '1'},
        )
        self.assertEqual(response.status_code, 400)

        response = self.client.post(
            '/accounts/add',
            data={
                'offline': 'true',
                'label': 'Offline',
                'host': 'imap.example.com',
            },
            headers={'X-BringMail-Request': '1'},
        )
        self.assertEqual(response.status_code, 400)

        response = self.client.post(
            f'/account/{self.account.id}/update',
            data={'offline': 'true'},
            headers={'X-BringMail-Request': '1'},
        )
        self.assertEqual(response.status_code, 400)

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

    def test_accounts_reorder(self):
        with self.db.transaction:
            second = self.db.add_account(
                label='Second',
                host='imap.example.com',
                port=993,
                ssl=True,
                starttls=False,
                username='second@example.com',
                password='hunter2',
                autorefresh_interval=900,
            )

        response = self.client.post(
            '/accounts/reorder',
            data={'account_ids': f'{second.id},{self.account.id}'},
            headers={'X-BringMail-Request': '1'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [account['id'] for account in response.get_json()],
            [second.id, self.account.id],
        )
        self.db.clear_object_caches()
        self.assertEqual(
            [account.id for account in self.db.get_accounts()],
            [second.id, self.account.id],
        )
        self.assertEqual(
            [account.ui_order_rank for account in self.db.get_accounts()],
            [1.0, 2.0],
        )

        response = self.client.post(
            '/accounts/reorder',
            data={'account_ids': str(self.account.id)},
            headers={'X-BringMail-Request': '1'},
        )
        self.assertEqual(response.status_code, 400)

        response = self.client.post(
            '/accounts/reorder',
            data={'account_ids': 'not-an-id'},
            headers={'X-BringMail-Request': '1'},
        )
        self.assertEqual(response.status_code, 400)

    def test_accounts_reorder_requires_csrf_header(self):
        response = self.client.post(
            '/accounts/reorder',
            data={'account_ids': str(self.account.id)},
        )
        self.assertEqual(response.status_code, 403)

    def test_idle_status_endpoint(self):
        response = self.client.get('/idle/status.json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {})

    def test_folder_refresh_enqueues_folder_job(self):
        common.clear_refresh_queue()
        response = self.client.post(
            f'/folder/{self.folder.id}/refresh',
            headers={'X-BringMail-Request': '1'},
        )
        self.assertEqual(response.status_code, 200)

        (priority, _, job) = common.REFRESH_QUEUE.get_nowait()
        self.assertEqual(priority, 0)
        self.assertEqual(job.account_id, self.account.id)
        self.assertEqual(job.folder_id, self.folder.id)
        self.assertTrue(job.manual)

    def test_manual_folder_job_precedes_automatic_account_job(self):
        common.clear_refresh_queue()
        common.add_account_to_refresh_queue(self.account)
        common.add_folder_to_refresh_queue(self.folder, manual=True)

        (first_priority, _, first_job) = common.REFRESH_QUEUE.get_nowait()
        (second_priority, _, second_job) = common.REFRESH_QUEUE.get_nowait()

        self.assertEqual(first_priority, 0)
        self.assertEqual(first_job.folder_id, self.folder.id)
        self.assertEqual(second_priority, 1)
        self.assertIsNone(second_job.folder_id)

    def test_refresh_queue_deduplicates_same_job(self):
        common.clear_refresh_queue()
        common.add_folder_to_refresh_queue(self.folder)
        common.add_folder_to_refresh_queue(self.folder)
        self.assertEqual(common.REFRESH_QUEUE.qsize(), 1)

    def test_set_seen_queues_without_network(self):
        response = self.client.post(
            f'/message/{self.message.id}/set_seen',
            data={'seen': 'true'},
            headers={'X-BringMail-Request': '1'},
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['pending']['operation'], constants.OPERATION_SEEN)
        self.assertTrue(data['seen'])
        self.assertEqual(self.db.get_pending_count(self.account), 1)

        jobs = []
        while not common.REFRESH_QUEUE.empty():
            jobs.append(common.REFRESH_QUEUE.get_nowait())
        self.assertTrue(any(isinstance(job, common.FlushJob) for (_, _, job) in jobs))

    def test_delete_and_restore_endpoints(self):
        with self.db.transaction:
            self.db.add_folder(
                account=self.account,
                name='Trash',
                display_name='Trash',
                delimiter='/',
                special_use=constants.SPECIAL_USE_TRASH,
            )

        response = self.client.post(
            f'/message/{self.message.id}/delete',
            headers={'X-BringMail-Request': '1'},
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data['local_deleted'])
        self.assertEqual(data['pending']['operation'], constants.OPERATION_DELETE)
        self.assertTrue(self.db.get_message(self.message.id).local_deleted)

        response = self.client.post(
            f'/message/{self.message.id}/restore',
            headers={'X-BringMail-Request': '1'},
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data['local_deleted'])
        self.assertFalse(self.db.get_message(self.message.id).local_deleted)

    def test_pending_json_lists_and_dismisses(self):
        self.message.queue_seen(True)
        operation = self.db.get_pending_for_message(self.message.id)[0]
        with self.db.transaction:
            self.db.set_pending_state(
                operation.id,
                constants.PENDING_STATE_CONFLICTED,
                last_error='changed on the server',
            )

        response = self.client.get(f'/account/{self.account.id}/pending.json')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['counts']['conflicted'], 1)
        self.assertEqual(len(data['operations']), 1)
        self.assertEqual(data['operations'][0]['state'], constants.PENDING_STATE_CONFLICTED)

        response = self.client.post(
            f'/pending/{operation.id}/dismiss',
            headers={'X-BringMail-Request': '1'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.db.get_pending_operation(operation.id).state,
            constants.PENDING_STATE_SUPERSEDED,
        )

    def test_pending_discard_endpoint_queues_discard(self):
        self.message.queue_seen(True)
        common.clear_refresh_queue()
        response = self.client.post(
            f'/account/{self.account.id}/pending/discard',
            headers={'X-BringMail-Request': '1'},
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['type'], 'discard_queued')
        jobs = []
        while not common.REFRESH_QUEUE.empty():
            jobs.append(common.REFRESH_QUEUE.get_nowait()[2])
        self.assertTrue(any(isinstance(job, common.DiscardJob) for job in jobs))

    def test_accounts_json_includes_pending_count(self):
        self.message.queue_seen(True)
        response = self.client.get('/accounts.json')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data[0]['pending_count'], 1)

    def _add_source_trash(self):
        with self.db.transaction:
            return self.db.add_folder(
                account=self.account,
                name='Trash',
                display_name='Trash',
                delimiter='/',
                special_use=constants.SPECIAL_USE_TRASH,
            )

    def _add_destination_account(self, *, create_message=False):
        with self.db.transaction:
            account = self.db.add_account(
                label='Destination',
                host='imap.example.com',
                port=993,
                ssl=True,
                starttls=False,
                username='destination@example.com',
                password='hunter2',
                autorefresh_interval=900,
            )
            folder = self.db.add_folder(
                account=account,
                name='DestInbox',
                display_name='Dest Inbox',
                delimiter='/',
                special_use=constants.SPECIAL_USE_INBOX,
            )
            message = None
            if create_message:
                (parsed, meta) = helpers.parse_full_message(
                    self.raw,
                    size=len(self.raw),
                )
                message = self.db.add_message(
                    account=account,
                    folder=folder,
                    uid=1,
                    uidvalidity=1,
                    raw=self.raw,
                    meta=meta,
                    size=len(self.raw),
                )
        return (account, folder, message)

    def test_cross_account_move_without_source_trash_is_copy_only(self):
        common.clear_refresh_queue()
        (destination_account, destination_folder, _) = self._add_destination_account()

        response = self.client.post(
            '/batch/messages/move_to_account',
            data={
                'message_ids': str(self.message.id),
                'folder_id': str(destination_folder.id),
            },
            headers={'X-BringMail-Request': '1'},
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['type'], 'batch_move_to_account')
        self.assertEqual(len(data['messages']), 1)
        entry = data['messages'][0]
        self.assertTrue(entry['copy_only'])
        self.assertEqual(entry['pending']['operation'], constants.OPERATION_COPY_ACCOUNT)

        rows = self.db.get_pending_for_message(self.message.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].operation, constants.OPERATION_COPY_ACCOUNT)
        self.assertEqual(rows[0].target_folder_id, destination_folder.id)

        jobs = []
        while not common.REFRESH_QUEUE.empty():
            jobs.append(common.REFRESH_QUEUE.get_nowait()[2])
        self.assertTrue(any(
            isinstance(job, common.FlushJob) and job.account_id == self.account.id
            for job in jobs
        ))

    def test_cross_account_move_with_source_trash_is_move(self):
        self._add_source_trash()
        (destination_account, destination_folder, _) = self._add_destination_account()

        response = self.client.post(
            '/batch/messages/move_to_account',
            data={
                'message_ids': str(self.message.id),
                'folder_id': str(destination_folder.id),
            },
            headers={'X-BringMail-Request': '1'},
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        entry = data['messages'][0]
        self.assertFalse(entry['copy_only'])
        self.assertEqual(entry['pending']['operation'], constants.OPERATION_MOVE_ACCOUNT)

        row = self.db.get_pending_for_message(self.message.id)[0]
        self.assertEqual(row.operation, constants.OPERATION_MOVE_ACCOUNT)

    def test_cross_account_move_rejects_mixed_source_accounts(self):
        (destination_account, destination_folder, other_message) = (
            self._add_destination_account(create_message=True)
        )

        response = self.client.post(
            '/batch/messages/move_to_account',
            data={
                'message_ids': f'{self.message.id},{other_message.id}',
                'folder_id': str(destination_folder.id),
            },
            headers={'X-BringMail-Request': '1'},
        )
        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertEqual(data['error_type'], 'INVALID_FOLDER')
        self.assertIn('one account', data['error_message'])

    def test_cross_account_move_rejects_same_account_destination(self):
        response = self.client.post(
            '/batch/messages/move_to_account',
            data={
                'message_ids': str(self.message.id),
                'folder_id': str(self.folder.id),
            },
            headers={'X-BringMail-Request': '1'},
        )
        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertEqual(data['error_type'], 'INVALID_FOLDER')

if __name__ == '__main__':
    unittest.main()
