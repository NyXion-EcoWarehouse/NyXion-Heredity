from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from heredity.backends.base import BackendError
from heredity.storage import MessageStore


def record(identifier=1, text="hello", kind="private", conversation=200):
    return {
        "message_id": identifier,
        "message_type": kind,
        "group_id" if kind == "group" else "user_id": conversation,
        "message": [{"type": "text", "data": {"text": text}}],
    }


class MessageStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "records.sqlite"
        self.store = MessageStore(self.path)
        self.addCleanup(self.store.close)

    def test_messages_and_events_survive_reopen(self):
        original = record(-12)
        event = {"self_id": 101, "post_type": "message", "message_id": -12}
        self.store.put_message(101, -12, original)
        sequence = self.store.append_event(101, event, "event-1")
        self.store.close()
        with self.assertRaises(BackendError):
            self.store.get_message(101, -12)
        reopened = MessageStore(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get_message(101, -12), original)
        self.assertEqual(reopened.events_after(101), [{"sequence": sequence, "event": event}])

    def test_accounts_are_isolated_even_for_identical_message_and_event_keys(self):
        self.store.put_message(101, -7, record(-7, "account-one"))
        self.store.put_message(102, -7, record(-7, "account-two"))
        self.assertEqual(self.store.get_message(101, -7)["message"][0]["data"]["text"],
                         "account-one")
        self.assertEqual(self.store.get_message(102, -7)["message"][0]["data"]["text"],
                         "account-two")
        self.assertIsNone(self.store.get_message(103, -7))
        self.assertIsNotNone(self.store.append_event(101, {"value": 1}, "same"))
        self.assertIsNotNone(self.store.append_event(102, {"value": 2}, "same"))
        self.assertEqual(self.store.events_after(101)[0]["event"], {"value": 1})

    def test_duplicate_events_are_atomic_and_account_local(self):
        event = {"post_type": "message", "message_id": 9}
        first = self.store.commit_event(101, event, "unique", message_record=record(9))
        duplicate = self.store.commit_event(
            101, event, "unique", message_record=record(9, "must-not-overwrite"),
            deleted_message_id=9,
        )
        self.assertIsNotNone(first)
        self.assertIsNone(duplicate)
        self.assertEqual(len(self.store.events_after(101)), 1)
        self.assertFalse(self.store.is_deleted(101, 9))
        self.assertEqual(self.store.get_message(101, 9), record(9))

    def test_inputs_and_returned_values_do_not_mutate_storage(self):
        original, event = record(), {"values": [1]}
        before_record, before_event = deepcopy(original), deepcopy(event)
        self.store.put_message(101, 1, original)
        self.store.append_event(101, event)
        self.assertEqual(original, before_record)
        self.assertEqual(event, before_event)
        original["message"][0]["data"]["text"] = "outside"
        event["values"].append(2)
        returned = self.store.get_message(101, 1)
        returned["message"][0]["data"]["text"] = "return mutation"
        self.assertEqual(self.store.get_message(101, 1), before_record)
        self.assertEqual(self.store.events_after(101)[0]["event"], before_event)

    def test_deleted_records_are_retained_and_not_revived_by_late_put(self):
        self.store.put_message(101, -3, record(-3))
        self.assertTrue(self.store.mark_deleted(101, -3))
        self.assertIsNone(self.store.get_message(101, -3))
        self.assertTrue(self.store.is_deleted(101, -3))
        self.assertEqual(self.store.get_message(101, -3, include_deleted=True), record(-3))
        self.store.put_message(101, -3, record(-3, "late"))
        self.assertIsNone(self.store.get_message(101, -3))
        self.assertEqual(
            self.store.get_message(101, -3, include_deleted=True), record(-3, "late")
        )
        self.assertEqual(self.store.history(101, "private", 200), [])

    def test_recall_before_message_persists_a_separate_tombstone(self):
        self.assertFalse(self.store.mark_deleted(101, -4))
        self.assertTrue(self.store.is_deleted(101, -4))
        self.assertFalse(self.store.is_deleted(102, -4))
        self.store.commit_event(101, {"kind": "late-message"}, "late", message_record=record(-4))
        self.assertIsNone(self.store.get_message(101, -4))
        self.store.close()
        reopened = MessageStore(self.path)
        self.addCleanup(reopened.close)
        self.assertTrue(reopened.is_deleted(101, -4))
        self.assertIsNotNone(reopened.get_message(101, -4, include_deleted=True))

    def test_event_and_message_or_recall_changes_roll_back_together(self):
        with patch.object(self.store, "_put", side_effect=sqlite3.OperationalError("injected")):
            with self.assertRaises(BackendError):
                self.store.commit_event(
                    101, {"kind": "message"}, "retryable", message_record=record(12)
                )
        self.assertEqual(self.store.events_after(101), [])
        self.assertIsNone(self.store.get_message(101, 12))
        with patch.object(self.store, "_tombstone", side_effect=sqlite3.OperationalError("injected")):
            with self.assertRaises(BackendError):
                self.store.commit_event(101, {"kind": "recall"}, "recall", deleted_message_id=12)
        self.assertEqual(self.store.events_after(101), [])
        self.assertFalse(self.store.is_deleted(101, 12))
        self.assertIsNotNone(self.store.commit_event(
            101, {"kind": "message"}, "retryable", message_record=record(12)
        ))

    def test_history_uses_insertion_order_and_account_local_anchor(self):
        for identifier in (20, -5, 3, 99):
            self.store.put_message(101, identifier, record(identifier))
        self.store.put_message(101, 7, record(7, kind="group"))
        self.store.put_message(102, 8, record(8))
        self.store.put_message(101, 20, record(20, "updated"))
        self.assertEqual(
            [item["message_id"] for item in self.store.history(101, "private", 200, limit=3)],
            [-5, 3, 99],
        )
        self.assertEqual(
            [item["message_id"] for item in self.store.history(
                101, "private", 200, before_message_id=3
            )],
            [20, -5],
        )
        self.assertEqual(self.store.history(101, "private", 200, before_message_id=8), [])

    def test_event_cursor_is_monotonic_and_filtered_by_account(self):
        first = self.store.append_event(101, {"n": 1})
        self.store.append_event(102, {"n": 2})
        last = self.store.append_event(101, {"n": 3})
        self.assertGreater(last, first)
        self.assertEqual(self.store.events_after(101, after=first, limit=1), [
            {"sequence": last, "event": {"n": 3}}
        ])

    def test_invalid_inputs_and_account_mismatches_are_rejected(self):
        invalid = [
            lambda: self.store.put_message(True, 1, record()),
            lambda: self.store.put_message(101, 0, record()),
            lambda: self.store.put_message(101, 1, {"message_type": "private"}),
            lambda: self.store.put_message(101, 1, record(2)),
            lambda: self.store.put_message(101, 1, {**record(), "self_id": 102}),
            lambda: self.store.append_event(101, {"self_id": 102}),
            lambda: self.store.append_event(101, {"n": float("nan")}),
            lambda: self.store.append_event(101, {}, "x" * 513),
            lambda: self.store.append_event(101, {}, "\ud800"),
            lambda: self.store.history(101, "private", 200, limit=1001),
            lambda: self.store.history(101, "private", 200, limit=True),
            lambda: self.store.events_after(101, after=-1),
            lambda: self.store.events_after(101, after=True),
        ]
        for operation in invalid:
            with self.subTest(operation=operation), self.assertRaises(BackendError) as caught:
                operation()
            self.assertEqual(caught.exception.retcode, 1400)
        self.assertEqual(self.store.events_after(101), [])

    def test_concurrent_events_deduplicate_atomically(self):
        def write(_):
            return self.store.append_event(101, {"value": "same"}, "same-event")

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(write, range(40)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.store.events_after(101)), 1)

    def test_demo_identifier_allocation_is_concurrent_durable_and_account_scoped(self):
        self.store.put_message(101, 500, record(500))
        self.store.mark_deleted(101, 600)
        with ThreadPoolExecutor(max_workers=8) as executor:
            allocated = list(executor.map(lambda _: self.store.allocate_message_id(101), range(40)))
        self.assertEqual(sorted(allocated), list(range(601, 641)))
        self.assertEqual(self.store.allocate_message_id(102), 1)
        self.store.close()
        reopened = MessageStore(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.allocate_message_id(101), 641)

    def test_allocation_is_safe_across_separate_connections_and_exhaustion(self):
        second = MessageStore(self.path)
        self.addCleanup(second.close)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(store.allocate_message_id, 101)
                for store in (self.store, second) * 10
            ]
            allocated = [future.result() for future in futures]
        self.assertEqual(len(set(allocated)), 20)
        self.store.put_message(102, 2147483647, record(2147483647))
        with self.assertRaises(BackendError):
            self.store.allocate_message_id(102)

    def test_close_is_idempotent(self):
        self.store.close()
        self.store.close()
        with self.assertRaises(BackendError):
            self.store.append_event(101, {})

    def test_schema_version_and_corrupt_schema_are_rejected(self):
        self.store.close()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA user_version = 2")
        with self.assertRaises(BackendError):
            MessageStore(self.path)
        missing = Path(self.temporary.name) / "missing.sqlite"
        with closing(sqlite3.connect(missing)) as db:
            db.execute("PRAGMA user_version = 1")
        with self.assertRaises(BackendError):
            MessageStore(missing)
        unversioned = Path(self.temporary.name) / "unversioned.sqlite"
        with closing(sqlite3.connect(unversioned)) as db:
            db.execute("CREATE TABLE foreign_records(value TEXT)")
        with self.assertRaises(BackendError):
            MessageStore(unversioned)
        constraints = Path(self.temporary.name) / "constraints.sqlite"
        with closing(sqlite3.connect(constraints)) as db:
            db.execute("PRAGMA user_version = 1")
            db.execute(
                "CREATE TABLE messages (record_order INTEGER, self_id INTEGER, "
                "message_id INTEGER, message_type TEXT, conversation_id TEXT, record TEXT)"
            )
            db.commit()
        with self.assertRaises(BackendError):
            MessageStore(constraints)


if __name__ == "__main__":
    unittest.main()
