import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from bringmail import constants
from bringmail import outbox


def make_row(
        id,
        *,
        operation=constants.OPERATION_SEEN,
        message_id=1,
        folder_id=10,
        message_uid=1,
        enabled=None,
        base_folder_id=10,
        target_folder_id=None,
        target_keywords=None,
        observed=None,
        state=constants.PENDING_STATE_PENDING,
        attempts=0,
        next_attempt=0,
        created=0,
):
    if observed is None:
        observed = {
            'seen': False,
            'flagged': False,
            'answered': False,
            'keywords': [],
        }
    return {
        'id': id,
        'account_id': 1,
        'folder_id': folder_id,
        'message_id': message_id,
        'message_uid': message_uid,
        'message_uidvalidity': 5,
        'operation': operation,
        'enabled': None if enabled is None else int(bool(enabled)),
        'base_folder_id': base_folder_id,
        'target_folder_id': target_folder_id,
        'target_keywords': (
            json.dumps(target_keywords) if target_keywords is not None else None
        ),
        'base_modseq': 10,
        'observed': json.dumps(observed),
        'state': state,
        'attempts': attempts,
        'next_attempt': next_attempt,
        'last_error': None,
        'created': created,
        'updated': created,
    }


class ConsolidationTest(unittest.TestCase):
    def execute_ids(self, rows):
        plan = outbox.consolidate(rows)
        return ([operation.id for operation in plan.execute], set(plan.superseded))

    def test_single_flag_operation_executes(self):
        rows = [make_row(1, operation=constants.OPERATION_SEEN, enabled=True)]
        (execute, superseded) = self.execute_ids(rows)
        self.assertEqual(execute, [1])
        self.assertEqual(superseded, set())

    def test_flag_toggle_back_to_original_cancels_both(self):
        rows = [
            make_row(1, operation=constants.OPERATION_SEEN, enabled=True),
            make_row(
                2,
                operation=constants.OPERATION_SEEN,
                enabled=False,
                observed={'seen': True, 'flagged': False, 'answered': False, 'keywords': []},
            ),
        ]
        (execute, superseded) = self.execute_ids(rows)
        self.assertEqual(execute, [])
        self.assertEqual(superseded, {1, 2})

    def test_latest_flag_value_wins_and_earlier_rows_supersede(self):
        rows = [
            make_row(
                1,
                operation=constants.OPERATION_KEYWORDS,
                target_keywords=['$one'],
                observed={'seen': False, 'flagged': False, 'answered': False, 'keywords': []},
            ),
            make_row(
                2,
                operation=constants.OPERATION_KEYWORDS,
                target_keywords=['$two'],
                observed={'seen': False, 'flagged': False, 'answered': False, 'keywords': ['$one']},
            ),
        ]
        (execute, superseded) = self.execute_ids(rows)
        self.assertEqual(execute, [2])
        self.assertEqual(superseded, {1})

    def test_net_zero_keywords_cancel(self):
        rows = [
            make_row(
                1,
                operation=constants.OPERATION_KEYWORDS,
                target_keywords=['$one'],
                observed={'seen': False, 'flagged': False, 'answered': False, 'keywords': []},
            ),
            make_row(
                2,
                operation=constants.OPERATION_KEYWORDS,
                target_keywords=[],
                observed={'seen': False, 'flagged': False, 'answered': False, 'keywords': ['$one']},
            ),
        ]
        (execute, superseded) = self.execute_ids(rows)
        self.assertEqual(execute, [])
        self.assertEqual(superseded, {1, 2})

    def test_delete_then_restore_cancels(self):
        rows = [
            make_row(
                1,
                operation=constants.OPERATION_DELETE,
                base_folder_id=10,
                target_folder_id=20,
            ),
            make_row(
                2,
                operation=constants.OPERATION_RESTORE,
                base_folder_id=20,
                target_folder_id=10,
            ),
        ]
        (execute, superseded) = self.execute_ids(rows)
        self.assertEqual(execute, [])
        self.assertEqual(superseded, {1, 2})

    def test_delete_restore_then_delete_again_executes_one(self):
        rows = [
            make_row(1, operation=constants.OPERATION_DELETE, base_folder_id=10, target_folder_id=20),
            make_row(2, operation=constants.OPERATION_RESTORE, base_folder_id=20, target_folder_id=10),
            make_row(3, operation=constants.OPERATION_DELETE, base_folder_id=10, target_folder_id=20),
        ]
        (execute, superseded) = self.execute_ids(rows)
        self.assertEqual(execute, [3])
        self.assertEqual(superseded, {1, 2})

    def test_move_there_and_back_cancels(self):
        rows = [
            make_row(1, operation=constants.OPERATION_MOVE, base_folder_id=10, target_folder_id=30),
            make_row(2, operation=constants.OPERATION_MOVE, base_folder_id=30, target_folder_id=10),
        ]
        (execute, superseded) = self.execute_ids(rows)
        self.assertEqual(execute, [])
        self.assertEqual(superseded, {1, 2})

    def test_move_chain_is_kept_in_order(self):
        rows = [
            make_row(1, operation=constants.OPERATION_MOVE, base_folder_id=10, target_folder_id=30),
            make_row(2, operation=constants.OPERATION_MOVE, base_folder_id=30, target_folder_id=40),
        ]
        (execute, superseded) = self.execute_ids(rows)
        self.assertEqual(execute, [1, 2])
        self.assertEqual(superseded, set())

    def test_move_to_current_folder_is_superseded(self):
        rows = [
            make_row(1, operation=constants.OPERATION_MOVE, base_folder_id=10, target_folder_id=10),
        ]
        (execute, superseded) = self.execute_ids(rows)
        self.assertEqual(execute, [])
        self.assertEqual(superseded, {1})

    def test_messages_are_consolidated_independently(self):
        rows = [
            make_row(1, message_id=1, operation=constants.OPERATION_SEEN, enabled=True),
            make_row(2, message_id=2, operation=constants.OPERATION_SEEN, enabled=True),
        ]
        (execute, superseded) = self.execute_ids(rows)
        self.assertEqual(execute, [1, 2])
        self.assertEqual(superseded, set())

    def test_folder_operations_run_before_flags(self):
        rows = [
            make_row(1, message_id=1, operation=constants.OPERATION_SEEN, enabled=True),
            make_row(2, message_id=1, operation=constants.OPERATION_MOVE, base_folder_id=10, target_folder_id=30),
        ]
        (execute, superseded) = self.execute_ids(rows)
        self.assertEqual(execute, [2, 1])
        self.assertEqual(superseded, set())

    def test_virtual_folder_id_tracks_chained_folder_operations(self):
        operations = [
            outbox.Operation.from_row(make_row(
                1,
                operation=constants.OPERATION_DELETE,
                base_folder_id=10,
                target_folder_id=20,
            )),
            outbox.Operation.from_row(make_row(
                2,
                operation=constants.OPERATION_RESTORE,
                base_folder_id=20,
                target_folder_id=10,
            )),
        ]
        self.assertEqual(outbox.virtual_folder_id(10, operations), 10)

        operations = [
            outbox.Operation.from_row(make_row(
                1,
                operation=constants.OPERATION_MOVE,
                base_folder_id=10,
                target_folder_id=30,
            )),
        ]
        self.assertEqual(outbox.virtual_folder_id(10, operations), 30)


class BackoffTest(unittest.TestCase):
    def test_backoff_grows_and_caps(self):
        self.assertEqual(outbox.backoff_seconds(0), constants.OUTBOX_RETRY_BASE_SECONDS)
        self.assertGreater(outbox.backoff_seconds(2), outbox.backoff_seconds(1))
        self.assertEqual(
            outbox.backoff_seconds(100),
            constants.OUTBOX_RETRY_MAX_SECONDS,
        )

    def test_is_expired(self):
        row = outbox.Operation.from_row(make_row(1, created=0))
        self.assertTrue(outbox.is_expired(row, constants.OUTBOX_RETRY_EXPIRE_SECONDS))
        self.assertFalse(outbox.is_expired(row, 10))


class SummaryTest(unittest.TestCase):
    def test_pending_message_states_uses_latest_row(self):
        rows = [
            outbox.Operation.from_row(make_row(1, operation=constants.OPERATION_SEEN, enabled=True)),
            outbox.Operation.from_row(make_row(2, operation=constants.OPERATION_DELETE, target_folder_id=20)),
        ]
        states = outbox.pending_message_states(rows)
        self.assertEqual(states[1]['operation'], constants.OPERATION_DELETE)
        self.assertEqual(states[1]['state'], constants.PENDING_STATE_PENDING)

    def test_summarize_counts_states(self):
        rows = [
            outbox.Operation.from_row(make_row(1)),
            outbox.Operation.from_row(make_row(2, state=constants.PENDING_STATE_CONFLICTED)),
        ]
        counts = outbox.summarize(rows)
        self.assertEqual(counts[constants.PENDING_STATE_PENDING], 1)
        self.assertEqual(counts[constants.PENDING_STATE_CONFLICTED], 1)


if __name__ == '__main__':
    unittest.main()
