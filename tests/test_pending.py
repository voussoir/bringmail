import os
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from bringmail import bringmaildb
from bringmail import constants
from bringmail import exceptions
from bringmail import objects
from bringmail import outbox


class FakeFlushClient:
    def __init__(self, uidvalidity=1):
        self.uidvalidity = uidvalidity
        self.selected = None
        self.flag_calls = []
        self.move_calls = []
        self.copy_calls = []
        self.expunge_calls = []
        self.fetch_flags = {}
        self.fetch_calls = []
        self.logged_out = False
        self.condstore = True

    def select_folder(self, name, readonly=False):
        self.selected = name
        return {b'UIDVALIDITY': self.uidvalidity, b'UIDNEXT': 2}

    def has_capability(self, capability):
        return self.condstore and capability == 'CONDSTORE'

    def capabilities(self):
        return (b'IMAP4rev1', b'CONDSTORE')

    def fetch(self, messages, data, modifiers=None):
        self.fetch_calls.append((list(messages), list(data)))
        result = {}
        for uid in messages:
            uid = int(uid)
            result[uid] = {
                b'FLAGS': tuple(self.fetch_flags.get(uid, ())),
                b'MODSEQ': 11,
                b'RFC822.SIZE': 10,
            }
        return result

    def search(self, criteria):
        return [1]

    def add_flags(self, messages, flags):
        self.flag_calls.append(('add', list(messages), list(flags)))

    def remove_flags(self, messages, flags):
        self.flag_calls.append(('remove', list(messages), list(flags)))

    def move(self, messages, folder):
        self.move_calls.append((list(messages), folder))
        return b'[COPYUID 1 1 42]'

    def copy(self, messages, folder):
        self.copy_calls.append((list(messages), folder))
        return b'[COPYUID 1 1 42]'

    def expunge(self, messages):
        self.expunge_calls.append(list(messages))

    def logout(self):
        self.logged_out = True


class PendingQueueTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = bringmaildb.BringMailDB(data_directory=self.tempdir.name, create=True)
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
            self.inbox = self.db.add_folder(
                account=self.account,
                name='INBOX',
                display_name='Inbox',
                delimiter='/',
                special_use=constants.SPECIAL_USE_INBOX,
            )
            self.archive = self.db.add_folder(
                account=self.account,
                name='Archive',
                display_name='Archive',
                delimiter='/',
            )
            self.trash = self.db.add_folder(
                account=self.account,
                name='Trash',
                display_name='Trash',
                delimiter='/',
                special_use=constants.SPECIAL_USE_TRASH,
            )
            self.message = self.db.add_message(
                account=self.account,
                folder=self.inbox,
                uid=1,
                uidvalidity=1,
                raw=None,
                meta={
                    'subject': 'test',
                    'from': [],
                    'to': [],
                    'cc': [],
                    'date_unix': 1,
                    'message_id': '<test@example.com>',
                    'has_attachments': False,
                    'snippet': '',
                },
                size=10,
                flags={'seen': False},
                modseq=10,
            )
        self._set_folder_state(self.inbox, uidvalidity=1, highestmodseq=5)
        self._set_folder_state(self.archive, uidvalidity=1, highestmodseq=5)
        self._set_folder_state(self.trash, uidvalidity=1, highestmodseq=5)

    def tearDown(self):
        self.db.close()
        self.tempdir.cleanup()

    def _set_folder_state(self, folder, *, uidvalidity, highestmodseq):
        with self.db.transaction:
            self.db.execute(
                'UPDATE folders SET uidvalidity = ?, highestmodseq = ? WHERE id == ?',
                [uidvalidity, highestmodseq, folder.id],
            )
        self.db.caches[objects.Folder].clear()
        return self.db.get_folder(folder.id)

    def _patch_move(self, *results):
        return mock.patch(
            'bringmail.objects.move_message_on_server',
            side_effect=list(results),
        )

    def _flush(self, client=None, conditional=None):
        client = client or FakeFlushClient()
        patches = [
            mock.patch('bringmail.helpers.connect_imap', return_value=client),
            mock.patch.object(self.db, '_sync_folder'),
        ]
        if conditional is not None:
            patches.append(mock.patch(
                'bringmail.helpers.conditional_store',
                return_value=conditional,
            ))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return (self.db.flush_pending(self.account), client)

    def test_queue_seen_applies_locally_and_records_prestate(self):
        changed = self.message.queue_seen(True)
        self.assertTrue(changed)
        self.assertTrue(self.message.seen)

        rows = self.db.get_pending_for_message(self.message.id)
        self.assertEqual(len(rows), 1)
        operation = rows[0]
        self.assertEqual(operation.operation, constants.OPERATION_SEEN)
        self.assertTrue(operation.enabled)
        self.assertFalse(operation.observed['seen'])
        self.assertEqual(operation.base_modseq, 10)
        self.assertEqual(self.db.get_pending_count(self.account), 1)

    def test_queue_seen_noop_does_not_queue(self):
        self.assertFalse(self.message.queue_seen(False))
        self.assertEqual(self.db.get_pending_for_message(self.message.id), [])

    def test_queue_delete_and_restore_toggle_local_deleted(self):
        self.assertTrue(self.message.queue_delete())
        self.assertTrue(self.message.local_deleted)
        self.assertTrue(self.message.queue_restore())
        self.assertFalse(self.message.local_deleted)

    def test_restore_without_delete_raises(self):
        with self.assertRaises(exceptions.NothingToRestore):
            self.message.queue_restore()

    def test_queue_move_uses_virtual_folder_for_chains(self):
        self.message.queue_move(self.archive)
        self.message.queue_delete()

        rows = self.db.get_pending_for_message(self.message.id)
        self.assertEqual([row.operation for row in rows], [
            constants.OPERATION_MOVE,
            constants.OPERATION_DELETE,
        ])
        self.assertEqual(rows[0].base_folder_id, self.inbox.id)
        self.assertEqual(rows[0].target_folder_id, self.archive.id)
        self.assertEqual(rows[1].base_folder_id, self.archive.id)
        self.assertEqual(rows[1].target_folder_id, self.trash.id)

    def test_flush_net_zero_flag_change_supersedes_both(self):
        self.message.queue_seen(True)
        self.message.queue_seen(False)

        (summary, client) = self._flush()
        self.assertEqual(summary['sent'], 0)
        self.assertEqual(summary['superseded'], 2)
        self.assertEqual(client.flag_calls, [])

        rows = self.db.get_pending_for_message(self.message.id)
        self.assertEqual(
            {row.state for row in rows},
            {constants.PENDING_STATE_SUPERSEDED},
        )
        self.assertFalse(self.message.seen)

    def test_flush_private_flag_uses_conditional_store(self):
        self.message.queue_seen(True)
        client = FakeFlushClient()
        client.fetch_flags = {1: (b'\\Seen',)}

        (summary, client) = self._flush(client=client, conditional=({1}, set()))
        self.assertEqual(summary['sent'], 1)
        self.assertTrue(client.logged_out)

        row = self.db.select_one(
            'SELECT state, attempts FROM pending_operations WHERE message_id == ?',
            [self.message.id],
        )
        self.assertEqual(row['state'], constants.PENDING_STATE_SENT)
        message = self.db.get_message(self.message.id)
        self.assertTrue(message.seen)
        self.assertEqual(message.modseq, 11)

    def test_flush_flag_fetch_omits_modseq_without_base_modseq(self):
        with self.db.transaction:
            self.db.execute(
                'UPDATE messages SET modseq = NULL WHERE id == ?',
                [self.message.id],
            )
        self.db.caches[objects.Message].clear()
        self.message = self.db.get_message(self.message.id)
        self.assertTrue(self.message.queue_seen(True))

        client = FakeFlushClient()
        (summary, client) = self._flush(client=client)
        self.assertEqual(summary['sent'], 1)
        self.assertFalse(any('MODSEQ' in data for (_, data) in client.fetch_calls))
        self.assertTrue(any(data == ['FLAGS'] for (_, data) in client.fetch_calls))

    def test_flush_shared_conflict_reverts_local_state(self):
        self.message.queue_keywords(['$one'])
        with mock.patch(
                'bringmail.helpers.conditional_store',
                return_value=(set(), {1})):
            (summary, client) = self._flush(client=FakeFlushClient())
        self.assertEqual(summary['conflicted'], 1)
        message = self.db.get_message(self.message.id)
        self.assertEqual(message.keywords, [])

        row = self.db.select_one(
            'SELECT state, last_error FROM pending_operations WHERE message_id == ?',
            [self.message.id],
        )
        self.assertEqual(row['state'], constants.PENDING_STATE_CONFLICTED)
        self.assertIn('changed on the server', row['last_error'])

    def test_connection_failure_backs_off(self):
        self.message.queue_seen(True)
        with mock.patch(
                'bringmail.helpers.connect_imap',
                side_effect=exceptions.MailServerConnectionError('down')):
            summary = self.db.flush_pending(self.account)

        self.assertIsNotNone(summary['error'])
        row = self.db.select_one(
            'SELECT state, attempts, next_attempt FROM pending_operations WHERE message_id == ?',
            [self.message.id],
        )
        self.assertEqual(row['state'], constants.PENDING_STATE_PENDING)
        self.assertEqual(row['attempts'], 1)
        self.assertGreater(row['next_attempt'], 0)

    def test_uidvalidity_change_is_a_conflict(self):
        self.message.queue_seen(True)
        self.inbox = self._set_folder_state(
            self.inbox,
            uidvalidity=99,
            highestmodseq=5,
        )

        (summary, client) = self._flush()
        self.assertEqual(summary['conflicted'], 1)
        message = self.db.get_message(self.message.id)
        self.assertFalse(message.seen)

        row = self.db.select_one(
            'SELECT state, last_error FROM pending_operations WHERE message_id == ?',
            [self.message.id],
        )
        self.assertEqual(row['state'], constants.PENDING_STATE_CONFLICTED)
        self.assertIn('renumbered', row['last_error'])

    def test_flush_move_updates_uid_and_folder(self):
        self.message.queue_move(self.archive)
        client = FakeFlushClient()

        with self._patch_move((42, 1, 'copyuid')):
            (summary, client) = self._flush(client=client)

        self.assertEqual(summary['sent'], 1)
        message = self.db.get_message(self.message.id)
        self.assertEqual(message.folder_id, self.archive.id)
        self.assertEqual(message.uid, 42)
        self.assertEqual(
            self.db.select_one_value(
                'SELECT state FROM pending_operations WHERE message_id == ?',
                [self.message.id],
            ),
            constants.PENDING_STATE_SENT,
        )

    def test_delete_then_restore_cancels_without_moving(self):
        self.message.queue_delete()
        self.message.queue_restore()

        (summary, client) = self._flush()
        self.assertEqual(summary['superseded'], 2)
        self.assertEqual(client.move_calls, [])
        self.assertFalse(self.message.local_deleted)

    def test_restore_after_flushed_delete_moves_back(self):
        self.message.queue_delete()
        with self._patch_move((42, 1, 'copyuid')):
            (summary, client) = self._flush(client=FakeFlushClient())
        self.assertEqual(summary['sent'], 1)
        self.message = self.db.get_message(self.message.id)
        self.assertEqual(self.message.folder_id, self.trash.id)

        self.message.queue_restore()
        with self._patch_move((84, 1, 'copyuid')):
            (summary, client) = self._flush(client=FakeFlushClient())
        self.assertEqual(summary['sent'], 1)
        self.message = self.db.get_message(self.message.id)
        self.assertEqual(self.message.folder_id, self.inbox.id)
        self.assertEqual(self.message.uid, 84)

    def test_pending_summary_counts(self):
        self.message.queue_seen(True)
        summary = self.db.get_pending_summary(self.account)
        self.assertEqual(summary, {'pending': 1, 'conflicted': 0, 'failed': 0})

    def test_expired_row_fails_and_reverts_local_state(self):
        self.message.queue_seen(True)
        with self.db.transaction:
            self.db.execute(
                'UPDATE pending_operations SET created = ? WHERE message_id == ?',
                [0, self.message.id],
            )

        with mock.patch('bringmail.helpers.connect_imap') as connect:
            summary = self.db.flush_pending(self.account)

        self.assertEqual(summary['failed'], 1)
        self.assertFalse(connect.called)
        message = self.db.get_message(self.message.id)
        self.assertFalse(message.seen)
        self.assertEqual(
            self.db.get_pending_operation(
                self.db.get_pending_for_message(self.message.id)[0].id
            ).state,
            constants.PENDING_STATE_FAILED,
        )

    def test_discard_pending_reverts_flags_and_delete_marker(self):
        self.message.queue_seen(True)
        self.message.queue_seen(False)
        self.message.queue_delete()
        self.assertEqual(self.db.get_pending_count(self.account), 3)

        summary = self.db.discard_pending(self.account)
        self.assertEqual(summary['discarded'], 3)

        message = self.db.get_message(self.message.id)
        self.assertFalse(message.seen)
        self.assertFalse(message.local_deleted)
        self.assertEqual(self.db.get_pending_count(self.account), 0)
        rows = self.db.get_pending_for_message(self.message.id)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(
            row.state == constants.PENDING_STATE_SUPERSEDED
            for row in rows
        ))

    def test_discard_pending_leaves_local_folder_unchanged(self):
        self.message.queue_move(self.archive)
        self.assertEqual(self.db.get_pending_count(self.account), 1)

        summary = self.db.discard_pending(self.account)
        self.assertEqual(summary['discarded'], 1)

        message = self.db.get_message(self.message.id)
        self.assertEqual(message.folder_id, self.inbox.id)
        self.assertEqual(self.db.get_pending_count(self.account), 0)

    def test_discard_pending_with_empty_queue(self):
        summary = self.db.discard_pending(self.account)
        self.assertEqual(summary['discarded'], 0)

    def test_dismiss_moves_row_to_superseded(self):
        self.message.queue_seen(True)
        operation = self.db.get_pending_for_message(self.message.id)[0]
        with self.db.transaction:
            self.db.set_pending_state(
                operation.id,
                constants.PENDING_STATE_CONFLICTED,
                last_error='test',
            )
        with self.db.transaction:
            self.db.set_pending_state(
                operation.id,
                constants.PENDING_STATE_SUPERSEDED,
                last_error='Dismissed',
            )
        self.assertEqual(
            self.db.get_pending_operation(operation.id).state,
            constants.PENDING_STATE_SUPERSEDED,
        )


if __name__ == '__main__':
    unittest.main()
