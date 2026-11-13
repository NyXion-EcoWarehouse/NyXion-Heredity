from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from heredity.backends.base import BackendError
from heredity.runtime import AccountRuntime
from heredity.storage import MessageStore


class FakeProvider:
    self_id = 123

    def __init__(self):
        self.online = True
        self.login_id = self.self_id
        self.calls = []
        self.failures = {}
        self.closed = False

    def call(self, action, params):
        self.calls.append((action, deepcopy(params)))
        if action in self.failures:
            raise self.failures[action]
        if action == "get_login_info":
            return {"user_id": self.login_id, "nickname": "fixture"}
        if action in {"send_private_msg", "send_group_msg", "send_msg"}:
            return {"message_id": -123}
        if action == "delete_msg":
            return None
        return {"action": action}

    def close(self):
        self.closed = True
        self.online = False


def private_message(identifier=-201):
    return {
        "time": 1700000000,
        "self_id": 123,
        "post_type": "message",
        "message_type": "private",
        "message_id": identifier,
        "user_id": 456,
        "message": "received",
    }


def group_message(identifier=-202):
    return {
        **private_message(identifier),
        "message_type": "group",
        "group_id": 789,
    }


def friend_recall(identifier=-203):
    return {
        "time": 1700000001,
        "self_id": 123,
        "post_type": "notice",
        "notice_type": "friend_recall",
        "message_id": identifier,
        "user_id": 456,
    }


class AccountRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "account.sqlite"
        self.provider = FakeProvider()
        self.store = MessageStore(self.path)
        self.runtime = AccountRuntime(self.provider, self.store)
        self.addCleanup(self.runtime.close)
        self.assertTrue(self.runtime.initialize())
        self.provider.calls.clear()

    def assert_backend_error(self, code, operation):
        with self.assertRaises(BackendError) as caught:
            operation()
        self.assertEqual(caught.exception.retcode, code)
        return caught.exception

    def test_verified_identity_does_not_imply_receive_health(self):
        status = self.runtime.status()
        self.assertTrue(status["online"])
        self.assertTrue(status["identity_verified"])
        self.assertEqual(status["state"], "ready")
        self.assertIsNone(status["receive_healthy"])
        self.assertFalse(status["good"])
        self.runtime.ingest(private_message())
        status = self.runtime.status()
        self.assertTrue(status["online"])
        self.assertTrue(status["receive_healthy"])
        self.assertTrue(status["good"])

    def test_identity_mismatch_revokes_readiness_and_stops_send(self):
        self.provider.login_id = 999
        self.assert_backend_error(
            1503, lambda: self.runtime.call("get_login_info", {})
        )
        self.assertFalse(self.runtime.online)
        self.assertEqual(self.runtime.status()["state"], "identity_mismatch")
        self.provider.calls.clear()
        self.assert_backend_error(
            1503,
            lambda: self.runtime.call(
                "send_private_msg", {"user_id": 456, "message": "must not send"}
            ),
        )
        self.assertEqual(
            [action for action, _ in self.provider.calls], ["get_login_info"]
        )
        self.assertIsNone(self.store.get_message(123, -123))

    def test_malformed_login_identity_revokes_previously_verified_state(self):
        for invalid_id in (None, 0, True, "not-an-account"):
            with self.subTest(user_id=invalid_id):
                self.provider.login_id = 123
                self.assertTrue(self.runtime.initialize())
                self.provider.login_id = invalid_id
                self.assert_backend_error(
                    1503, lambda: self.runtime.call("get_login_info", {})
                )
                status = self.runtime.status()
                self.assertFalse(status["online"])
                self.assertFalse(status["identity_verified"])
                self.assertEqual(status["state"], "identity_mismatch")

    def test_invalid_message_is_rejected_before_provider_effect(self):
        cases = [
            [{"type": "text", "data": {"text": "valid"}}, "invalid segment"],
            [
                {"type": "text", "data": {"text": "valid"}},
                {"type": "text", "data": {"text": None}},
            ],
            "text[CQ:at,target=456",
            "text[CQ:at,target=456,target=789]",
            [{"type": "text", "data": {"text": "valid", "extra": "unrepresentable"}}],
            [{"type": "image", "data": {"file": {"path": "unrepresentable"}}}],
        ]
        for message in cases:
            with self.subTest(message=message):
                self.provider.calls.clear()
                self.assert_backend_error(
                    1400,
                    lambda: self.runtime.call(
                        "send_private_msg", {"user_id": 456, "message": message}
                    ),
                )
                self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.store.history(123, "private", 456), [])

    def test_send_persists_negative_identity_and_get_delete_use_it(self):
        original = {"user_id": "456", "message": "hello[CQ:at,target=789]"}
        result = self.runtime.call("send_private_msg", original)
        self.assertEqual(result, {"message_id": -123})
        self.assertEqual(original["message"], "hello[CQ:at,target=789]")
        self.assertEqual([action for action, _ in self.provider.calls],
                         ["get_login_info", "send_private_msg"])
        sent = self.provider.calls[1][1]
        self.assertEqual(sent["user_id"], 456)
        self.assertEqual(
            sent["message"],
            [
                {"type": "text", "data": {"text": "hello"}},
                {"type": "at", "data": {"target": "789"}},
            ],
        )
        self.provider.calls.clear()
        stored = self.runtime.call("get_msg", {"message_id": "-123"})
        self.assertEqual(stored["message_id"], -123)
        self.assertEqual(stored["raw_message"], "hello[CQ:at,target=789]")
        self.assertEqual(stored["sender"]["user_id"], 123)
        self.assertEqual(self.provider.calls, [])
        self.assertIsNone(self.runtime.call("delete_msg", {"message_id": "-123"}))
        self.assertEqual(self.provider.calls, [
            ("get_login_info", {}), ("delete_msg", {"message_id": -123})])
        self.assertTrue(self.store.is_deleted(123, -123))
        self.assert_backend_error(
            1404, lambda: self.runtime.call("get_msg", {"message_id": -123})
        )
        self.assertEqual(self.store.history(123, "private", 456), [])

    def test_reopened_store_serves_local_reads_without_provider(self):
        self.runtime.call("send_private_msg", {"user_id": 456, "message": "durable"})
        self.runtime.close()
        provider = FakeProvider()
        provider.failures["get_login_info"] = BackendError("provider unavailable", 1502)
        store = MessageStore(self.path)
        runtime = AccountRuntime(provider, store)
        self.addCleanup(runtime.close)
        self.assertFalse(runtime.initialize())
        provider.calls.clear()
        stored = runtime.call("get_msg", {"message_id": -123})
        history = runtime.call(
            "get_local_history", {"message_type": "private", "user_id": 456}
        )
        self.assertEqual(stored["raw_message"], "durable")
        self.assertEqual(history["messages"], [stored])
        self.assertEqual(provider.calls, [])
        self.assertFalse(runtime.online)

    def test_standard_message_notice_and_request_ingress(self):
        events = [
            private_message(),
            group_message(),
            friend_recall(),
            {
                **friend_recall(-204),
                "notice_type": "group_recall",
                "group_id": 789,
                "operator_id": 123,
            },
            {
                "time": 1700000002,
                "self_id": 123,
                "post_type": "request",
                "request_type": "friend",
                "user_id": 456,
                "flag": "friend-request",
                "comment": "hello",
            },
            {
                "time": 1700000003,
                "self_id": 123,
                "post_type": "request",
                "request_type": "group",
                "sub_type": "add",
                "user_id": 456,
                "group_id": 789,
                "flag": "group-request",
                "comment": "",
            },
        ]
        sequences = []
        for event in events:
            result = self.runtime.ingest(event)
            self.assertFalse(result["duplicate"])
            self.assertTrue(result["persisted"])
            sequences.append(result["sequence"])
        self.assertEqual(sequences, sorted(set(sequences)))
        self.assertEqual(len(self.store.events_after(123)), len(events))
        self.assertEqual(len(self.runtime.event_hub.read(0)), len(events))
        self.assertEqual(self.store.get_message(123, -201)["user_id"], 456)
        self.assertEqual(self.store.get_message(123, -202)["group_id"], 789)
        self.assertTrue(self.store.is_deleted(123, -203))
        self.assertTrue(self.store.is_deleted(123, -204))
        self.assertEqual(self.provider.calls, [])
        for event in events:
            with self.subTest(kind=event["post_type"]):
                wrong_account = {**event, "self_id": 999}
                self.assert_backend_error(
                    1400, lambda: self.runtime.ingest(wrong_account)
                )
        self.assertEqual(len(self.store.events_after(123)), len(events))

    def test_message_ingress_requires_conversation_identifiers(self):
        private = private_message()
        del private["user_id"]
        group = group_message()
        del group["group_id"]
        for event in (private, group):
            with self.subTest(message_type=event["message_type"]):
                self.assert_backend_error(1400, lambda: self.runtime.ingest(event))
        self.assertEqual(self.store.events_after(123), [])
        self.assertEqual(self.runtime.event_hub.read(0), [])

    def test_repeated_message_identity_is_deduplicated(self):
        first = self.runtime.ingest(private_message())
        repeated = private_message()
        repeated["time"] += 1
        repeated["message"] = "changed duplicate payload"
        second = self.runtime.ingest(repeated)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertIsNone(second["sequence"])
        self.assertEqual(len(self.store.events_after(123)), 1)
        self.assertEqual(len(self.runtime.event_hub.read(0)), 1)
        self.assertEqual(self.store.get_message(123, -201)["raw_message"], "received")

    def test_message_is_durable_before_event_publication(self):
        published = []
        original_publish = self.runtime.event_hub.publish

        def verify_persistence(event, sequence):
            persisted = self.store.events_after(123)
            self.assertEqual(persisted[-1]["sequence"], sequence)
            self.assertEqual(persisted[-1]["event"], event)
            self.assertEqual(
                self.store.get_message(123, event["message_id"])["raw_message"],
                event["raw_message"],
            )
            published.append(sequence)
            return original_publish(event, sequence)

        with patch.object(
            self.runtime.event_hub, "publish", side_effect=verify_persistence
        ):
            result = self.runtime.ingest(private_message())
        self.assertEqual(published, [result["sequence"]])

    def test_persistence_failure_never_publishes_ingress(self):
        with (
            patch.object(
                self.store, "commit_event", side_effect=BackendError("disk failed", 1500)
            ),
            patch.object(self.runtime.event_hub, "publish") as publish,
        ):
            self.assert_backend_error(
                1500, lambda: self.runtime.ingest(private_message())
            )
            publish.assert_not_called()
        self.assertEqual(self.runtime.event_hub.read(0), [])
        self.assertIsNone(self.runtime.status()["receive_healthy"])

    def test_recall_before_late_message_prevents_publication_and_local_get(self):
        recall = self.runtime.ingest(friend_recall(-301))
        late = self.runtime.ingest(private_message(-301))
        self.assertFalse(recall["suppressed"])
        self.assertTrue(late["suppressed"])
        self.assertEqual(len(self.store.events_after(123)), 2)
        published = self.runtime.event_hub.read(0)
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["event"]["notice_type"], "friend_recall")
        self.assert_backend_error(
            1404, lambda: self.runtime.call("get_msg", {"message_id": -301})
        )
        self.assertEqual(self.store.history(123, "private", 456), [])

    def test_event_ingress_does_not_fabricate_verified_identity(self):
        provider = FakeProvider()
        store = MessageStore(Path(self.temporary.name) / "unverified.sqlite")
        runtime = AccountRuntime(provider, store)
        self.addCleanup(runtime.close)
        event = {
            "time": 1700000000,
            "self_id": 123,
            "post_type": "meta_event",
            "meta_event_type": "heartbeat",
            "status": {"online": True},
        }
        runtime.ingest(event)
        status = runtime.status()
        self.assertFalse(status["online"])
        self.assertFalse(status["identity_verified"])
        self.assertEqual(status["state"], "disconnected")
        self.assertTrue(status["receive_healthy"])
        self.assertEqual(provider.calls, [])

    def test_provider_failure_revokes_identity_and_receive_health(self):
        self.runtime.ingest(private_message())
        self.provider.failures["get_friend_list"] = BackendError("offline", 1502)
        self.assert_backend_error(
            1502, lambda: self.runtime.call("get_friend_list", {})
        )
        status = self.runtime.status()
        self.assertFalse(status["online"])
        self.assertFalse(status["identity_verified"])
        self.assertIsNone(status["receive_healthy"])
        self.assertEqual(status["state"], "disconnected")

    def test_completed_send_storage_failure_reports_details_without_retry(self):
        with patch.object(
            self.store, "put_message", side_effect=BackendError("disk failed", 1500)
        ):
            failure = self.assert_backend_error(
                1501,
                lambda: self.runtime.call(
                    "send_private_msg", {"user_id": 456, "message": "send exactly once"}
                ),
            )
        self.assertEqual(
            failure.details, {"action_completed": True, "message_id": -123}
        )
        self.assertEqual(
            [action for action, _ in self.provider.calls], ["get_login_info", "send_private_msg"]
        )
        self.assertIsNone(self.store.get_message(123, -123))

    def test_account_switch_is_detected_before_next_write(self):
        self.provider.login_id = 999
        self.assert_backend_error(
            1503, lambda: self.runtime.call(
                "send_private_msg", {"user_id": 456, "message": "must not send"}))
        self.assertEqual([action for action, _ in self.provider.calls], ["get_login_info"])
        self.assertFalse(self.runtime.online)

    def test_durable_record_limits_are_checked_before_send(self):
        self.assert_backend_error(
            1400, lambda: self.runtime.call(
                "send_private_msg", {"user_id": 456, "message": "x" * 600000}))
        self.assertEqual(self.provider.calls, [])

    def test_profile_custom_action_checks_account_switch(self):
        self.provider.login_id = 999
        self.assert_backend_error(
            1503, lambda: self.runtime.call(
                "set_group_ban", {"group_id": 789, "user_id": 456, "duration": 30}))
        self.assertEqual([action for action, _ in self.provider.calls], ["get_login_info"])

    def test_provider_bad_status_is_not_promoted_to_good(self):
        heartbeat = {"time": 1700000000, "self_id": 123, "post_type": "meta_event",
                     "meta_event_type": "heartbeat", "status": {"online": True, "good": False}}
        self.runtime.ingest(heartbeat)
        self.assertTrue(self.runtime.online)
        self.assertFalse(self.runtime.status()["good"])
        self.assertIs(self.runtime.status()["provider_good"], False)
        heartbeat["time"] += 1
        heartbeat["status"]["good"] = "false"
        self.assert_backend_error(1400, lambda: self.runtime.ingest(heartbeat))

    def test_invalid_send_extras_keep_confirmed_identity_and_no_retry_details(self):
        original = self.provider.call
        def provider(action, params):
            if action == "send_private_msg":
                return {"message_id": -901, "extra": float("nan")}
            return original(action, params)
        with patch.object(self.provider, "call", side_effect=provider):
            error = self.assert_backend_error(
                1502, lambda: self.runtime.call(
                    "send_private_msg", {"user_id": 456, "message": "confirmed"}))
        self.assertEqual(error.details, {"action_completed": True, "message_id": -901,
                                         "retry_safe": False})
        self.assertEqual(self.store.get_message(123, -901)["raw_message"], "confirmed")
        self.assertFalse(self.runtime.online)

    def test_private_self_event_uses_explicit_peer_for_history(self):
        event = {**private_message(-301), "post_type": "message_sent",
                 "user_id": 123, "target_id": 456}
        self.runtime.ingest(event)
        self.assertEqual(self.store.history(123, "private", 123), [])
        record = self.store.history(123, "private", 456)[0]
        self.assertEqual(record["sender"]["user_id"], 123)
        self.assertEqual(record["user_id"], 456)
        del event["target_id"]
        event["message_id"] = -302
        self.assert_backend_error(1400, lambda: self.runtime.ingest(event))

    def test_malformed_send_response_is_unknown_not_caller_error(self):
        original = self.provider.call
        for invalid in (None, {}, {"message_id": 0}, {"message_id": True}):
            with self.subTest(result=invalid):
                def provider(action, params):
                    if action in {"send_private_msg", "send_msg"}:
                        return invalid
                    return original(action, params)
                with patch.object(self.provider, "call", side_effect=provider):
                    error = self.assert_backend_error(
                        1502, lambda: self.runtime.call(
                            "send_private_msg", {"user_id": 456, "message": "once"}))
                self.assertEqual(error.details, {"action_outcome": "unknown", "retry_safe": False})
                self.assertFalse(self.runtime.online)

    def test_close_rejects_actions_and_ingress(self):
        self.runtime.close()
        self.assertTrue(self.provider.closed)
        self.assertTrue(self.runtime.event_hub.closed)
        self.assertEqual(self.runtime.status()["state"], "closed")
        self.assert_backend_error(1503, lambda: self.runtime.call("get_login_info", {}))
        self.assert_backend_error(1503, lambda: self.runtime.ingest(private_message()))
        self.assertEqual(self.provider.calls, [])
        self.runtime.close()


if __name__ == "__main__":
    unittest.main()
