from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import math
import threading
import unittest
from unittest.mock import patch
from uuid import UUID

from heredity.backends.base import BackendError
from heredity.providers.contracts import ExecutionLevel, ProviderDescriptor
from heredity.providers.manager import ProviderManager, ProviderRegistration
from heredity.providers.registry import placeholder_registrations
from heredity.runtime import AccountRuntime
from heredity.storage import MessageStore


PRIVATE_VALUE = "fixture-provider-private-value"
DEFAULT_ACTIONS = ("get_login_info", "get_friend_list", "send_private_msg", "get_msg", "delete_msg")


def descriptor(identifier, *, source="profile", actions=DEFAULT_ACTIONS, level=None):
    known_l2 = level == ExecutionLevel.L2
    return ProviderDescriptor(
        provider_id=identifier,
        label="Fixture " + identifier,
        execution_level=level,
        source_kind=source,
        host_kind="full_client" if known_l2 else "isolated_fixture",
        session_owner="heredity" if known_l2 else "fixture",
        target_memory_access="none",
        in_process_code=False,
        business_request_builder="heredity" if known_l2 else "fixture",
        primary_transport_owner="heredity" if known_l2 else "fixture",
        supported_actions=tuple(actions),
        availability="ready",
        level_provenance="declared" if level is not None else "none",
    )


class FixtureProvider:
    self_id = 123

    def __init__(self, name, identifier=-51):
        self.name = name
        self.online = True
        self.login_id = self.self_id
        self.calls = []
        self.close_calls = 0
        self.send_result = {"message_id": identifier}
        self.block_action = None
        self.action_started = threading.Event()
        self.action_release = threading.Event()

    def call(self, action, params):
        self.calls.append((action, deepcopy(params)))
        if action == self.block_action:
            self.action_started.set()
            if not self.action_release.wait(3):
                raise RuntimeError("fixture was not released")
        if action == "get_login_info":
            return {"user_id": self.login_id, "nickname": self.name}
        if action == "send_private_msg":
            return deepcopy(self.send_result)
        if action == "delete_msg":
            return None
        return {"provider": self.name}

    def close(self):
        self.close_calls += 1
        self.online = False


def private_event(identifier=-71, text="fixture event"):
    return {
        "time": 1700000000,
        "self_id": 123,
        "post_type": "message",
        "message_type": "private",
        "message_id": identifier,
        "user_id": 456,
        "message": text,
    }


class ExecutionRuntimeTests(unittest.TestCase):
    def make_runtime(self, *, source="profile", second_source=None,
                     first_actions=DEFAULT_ACTIONS, second_level=None, initialize=True):
        first, second = FixtureProvider("first", -51), FixtureProvider("second", -52)
        factories = []

        def factory(provider):
            def create():
                factories.append(provider.name)
                return provider
            return create

        registrations = [
            ProviderRegistration(
                descriptor("first", source=source, actions=first_actions), factory(first)
            ),
            ProviderRegistration(
                descriptor("second", source=second_source or source, level=second_level),
                factory(second),
            ),
            *placeholder_registrations(123),
        ]
        manager = ProviderManager(123, registrations, "first")
        store = MessageStore()
        runtime = AccountRuntime(manager, store)
        self.addCleanup(runtime.close)
        if initialize:
            self.assertTrue(runtime.initialize())
        return runtime, store, manager, first, second, factories

    def assert_error(self, code, operation):
        with self.assertRaises(BackendError) as caught:
            operation()
        self.assertEqual(caught.exception.retcode, code)
        self.assertNotIn(PRIVATE_VALUE, str(caught.exception))
        self.assertNotIn(PRIVATE_VALUE, repr(caught.exception.details))
        return caught.exception

    def test_initialization_keeps_source_level_epoch_and_identity_separate(self):
        runtime, _, manager, first, _, factories = self.make_runtime(initialize=False)
        epoch = manager.session_epoch
        UUID(epoch)
        self.assertEqual(factories, ["first"])
        self.assertEqual(first.calls, [])
        self.assertFalse(runtime.online)
        self.assertTrue(runtime.initialize())
        info = runtime.call("get_execution_info", {})
        self.assertTrue(info["switch_supported"])
        self.assertEqual(info["state"]["active_provider_id"], "first")
        self.assertEqual(info["state"]["session_epoch"], epoch)
        self.assertIsNone(info["state"]["descriptor"]["execution_level"])
        status = runtime.status()
        self.assertEqual(status["provider_id"], "first")
        self.assertEqual(status["session_epoch"], epoch)
        self.assertTrue(status["identity_verified"])
        self.assertIsNone(status["receive_healthy"])
        self.assertEqual(status["mode"], "profile")

    def test_capabilities_reject_business_action_before_provider_effect(self):
        runtime, store, _, first, _, _ = self.make_runtime(
            first_actions=("get_login_info", "get_friend_list")
        )
        first.calls.clear()
        self.assert_error(
            1404,
            lambda: runtime.call("send_private_msg", {"user_id": 456, "message": "not supported"}),
        )
        self.assertFalse(any(action == "send_private_msg" for action, _ in first.calls))
        self.assertIsNone(store.get_message(123, -51))

    def test_managed_ingress_requires_epoch_before_storage(self):
        runtime, store, manager, _, _, _ = self.make_runtime()
        with patch.object(store, "commit_event") as commit:
            for epoch in (None, "stale-epoch"):
                with self.subTest(epoch=epoch):
                    self.assert_error(
                        1409, lambda: runtime.ingest(private_event(), session_epoch=epoch)
                    )
            commit.assert_not_called()
        self.assertEqual(store.events_after(123), [])
        result = runtime.ingest(private_event(), session_epoch=manager.session_epoch)
        self.assertTrue(result["persisted"])

    def test_switch_drains_runtime_send_before_old_provider_close(self):
        runtime, store, manager, first, second, factories = self.make_runtime()
        old_epoch = manager.session_epoch
        first.block_action = "send_private_msg"
        with ThreadPoolExecutor(max_workers=2) as executor:
            sending = executor.submit(
                runtime.call, "send_private_msg", {"user_id": 456, "message": "in flight"}
            )
            self.assertTrue(first.action_started.wait(1))
            switching = executor.submit(runtime.switch_provider, "second", old_epoch, 2)
            try:
                self.assertTrue(runtime._switching.wait(1))
                self.assertEqual(first.close_calls, 0)
                self.assertEqual(second.calls, [])
                self.assertEqual(factories, ["first"])
                self.assert_error(1503, lambda: runtime.call("get_friend_list", {}))
            finally:
                first.action_release.set()
            self.assertEqual(sending.result(timeout=2), {"message_id": -51})
            self.assertTrue(switching.result(timeout=2)["changed"])
        self.assertEqual(first.close_calls, 1)
        record = store.get_message(123, -51)
        self.assertEqual(record["_heredity"], {"provider_id": "first", "session_epoch": old_epoch})
        self.assertEqual(manager.active_descriptor.provider_id, "second")

    def test_epoch_conflict_does_not_mutate_runtime_or_close_active_resources(self):
        runtime, _, manager, first, second, factories = self.make_runtime()
        epoch = manager.session_epoch
        runtime.ingest(private_event(), session_epoch=epoch)
        hub, status = runtime.event_hub, runtime.status()
        snapshot = manager.snapshot()
        self.assert_error(1409, lambda: runtime.switch_provider("second", "stale"))
        self.assertEqual(manager.snapshot(), snapshot)
        self.assertEqual(runtime.status(), status)
        self.assertIs(runtime.event_hub, hub)
        self.assertFalse(hub.closed)
        self.assertEqual(first.close_calls, 0)
        self.assertEqual(second.calls, [])
        self.assertEqual(factories, ["first"])

    def test_unavailable_native_selection_leaves_demo_unchanged(self):
        runtime, _, manager, first, _, factories = self.make_runtime(source="demo")
        epoch, hub = manager.session_epoch, runtime.event_hub
        self.assert_error(1503, lambda: runtime.switch_provider("l1-attached", epoch))
        self.assertEqual(manager.session_epoch, epoch)
        self.assertEqual(manager.active_descriptor.provider_id, "first")
        self.assertEqual(runtime.mode, "demo")
        self.assertTrue(runtime.online)
        self.assertIs(runtime.event_hub, hub)
        self.assertFalse(hub.closed)
        self.assertEqual(first.close_calls, 0)
        self.assertEqual(factories, ["first"])

    def test_successful_switch_rotates_epoch_hub_and_unknown_receive_health(self):
        runtime, _, manager, first, second, _ = self.make_runtime(
            second_level=ExecutionLevel.L2
        )
        old_epoch = manager.session_epoch
        runtime.ingest(private_event(), session_epoch=old_epoch)
        old_hub = runtime.event_hub
        changed = runtime.switch_provider("second", old_epoch)
        self.assertTrue(changed["changed"])
        self.assertNotEqual(manager.session_epoch, old_epoch)
        self.assertTrue(old_hub.closed)
        self.assertIsNot(runtime.event_hub, old_hub)
        self.assertFalse(runtime.event_hub.closed)
        self.assertEqual(first.close_calls, 1)
        self.assertEqual(second.calls[0][0], "get_login_info")
        self.assertTrue(runtime.online)
        status = runtime.status()
        self.assertIsNone(status["receive_healthy"])
        self.assertFalse(status["good"])
        self.assertEqual(status["execution_level"], "L2")
        self.assertEqual(runtime.mode, "profile")
        self.assertEqual(manager.active_descriptor.level_provenance, "declared")

    def test_stale_event_epoch_is_rejected_and_current_event_is_persisted_with_origin(self):
        runtime, store, manager, _, _, _ = self.make_runtime()
        old_epoch = manager.session_epoch
        runtime.switch_provider("second", old_epoch)
        with patch.object(store, "commit_event") as commit:
            self.assert_error(
                1409, lambda: runtime.ingest(private_event(), session_epoch=old_epoch)
            )
            commit.assert_not_called()
        current = manager.session_epoch
        result = runtime.ingest(private_event(), session_epoch=current)
        expected = {"provider_id": "second", "session_epoch": current}
        self.assertEqual(store.get_message(123, -71)["_heredity"], expected)
        durable = [item for item in store.events_after(123)
                   if item["sequence"] == result["sequence"]][0]
        self.assertEqual(durable["event"]["_heredity"], expected)
        published = [item for item in runtime.event_hub.read(0)
                     if item["sequence"] == result["sequence"]][0]
        self.assertEqual(published["event"]["_heredity"], expected)

    def test_legacy_runtime_still_accepts_unversioned_events_without_inferred_level(self):
        for mode in ("demo", "profile"):
            with self.subTest(mode=mode):
                store = MessageStore()
                runtime = AccountRuntime(FixtureProvider(mode), store, mode=mode)
                self.addCleanup(runtime.close)
                self.assertTrue(runtime.initialize())
                result = runtime.ingest(private_event())
                self.assertTrue(result["persisted"])
                self.assertNotIn("_heredity", store.get_message(123, -71))
                info = runtime.execution_info()
                self.assertFalse(info["switch_supported"])
                self.assertIsNone(info["state"]["descriptor"]["execution_level"])
                self.assertEqual(info["state"]["session_epoch"], "legacy-unversioned")

    def test_failed_new_identity_leaves_no_active_provider_or_fallback(self):
        runtime, _, manager, first, second, factories = self.make_runtime()
        second.login_id = 999
        epoch, old_hub = manager.session_epoch, runtime.event_hub
        self.assert_error(1503, lambda: runtime.switch_provider("second", epoch))
        self.assertNotEqual(manager.session_epoch, epoch)
        self.assertIsNone(manager.active_descriptor)
        self.assertEqual(manager.snapshot()["state"], "failed")
        self.assertEqual(runtime.mode, "unconfigured")
        self.assertFalse(runtime.online)
        self.assertTrue(old_hub.closed)
        self.assertEqual(first.close_calls, 1)
        self.assertEqual(second.close_calls, 1)
        self.assertEqual(factories, ["first", "second"])
        self.assert_error(1503, lambda: runtime.call("get_friend_list", {}))

    def test_demo_and_profile_sources_cannot_switch_into_each_other(self):
        for source, target in (("demo", "profile"), ("profile", "demo")):
            with self.subTest(source=source, target=target):
                runtime, _, manager, first, second, factories = self.make_runtime(
                    source=source, second_source=target
                )
                epoch, hub = manager.session_epoch, runtime.event_hub
                self.assert_error(1400, lambda: runtime.switch_provider("second", epoch))
                self.assertEqual(manager.session_epoch, epoch)
                self.assertEqual(runtime.mode, source)
                self.assertIs(runtime.event_hub, hub)
                self.assertFalse(hub.closed)
                self.assertEqual(first.close_calls, 0)
                self.assertEqual(second.calls, [])
                self.assertEqual(factories, ["first"])

    def test_old_provider_local_record_is_readable_but_not_recalled_through_new_provider(self):
        runtime, store, manager, _, second, _ = self.make_runtime()
        runtime.call("send_private_msg", {"user_id": 456, "message": "first provider"})
        runtime.switch_provider("second", manager.session_epoch)
        second.calls.clear()
        record = runtime.call("get_msg", {"message_id": -51})
        history = runtime.call("get_local_history", {"message_type": "private", "user_id": 456})
        self.assertEqual(record["raw_message"], "first provider")
        self.assertEqual(record["_heredity"]["provider_id"], "first")
        self.assertEqual(history["messages"], [record])
        self.assertEqual(second.calls, [])
        failure = self.assert_error(1409, lambda: runtime.call("delete_msg", {"message_id": -51}))
        self.assertEqual(failure.details, {"action_completed": False})
        self.assertEqual(second.calls, [])
        self.assertFalse(store.is_deleted(123, -51))

    def test_cross_provider_send_id_collision_preserves_original_record(self):
        runtime, store, manager, _, second, _ = self.make_runtime()
        runtime.call("send_private_msg", {"user_id": 456, "message": "original"})
        original = store.get_message(123, -51)
        runtime.switch_provider("second", manager.session_epoch)
        second.send_result = {"message_id": -51}
        failure = self.assert_error(
            1501,
            lambda: runtime.call("send_private_msg", {"user_id": 456, "message": "colliding"}),
        )
        self.assertEqual(failure.details, {
            "action_completed": True, "message_id": -51,
            "persistence_error": "provider_identity_conflict",
        })
        self.assertEqual(store.get_message(123, -51), original)
        self.assertEqual(sum(action == "send_private_msg" for action, _ in second.calls), 1)

    def test_legacy_or_malformed_message_provenance_is_not_used_for_writes(self):
        for provenance in (None, "legacy metadata", {"unexpected": "value"}):
            with self.subTest(provenance=provenance):
                runtime, store, manager, first, _, _ = self.make_runtime()
                record = {"message_id": -71, "message_type": "private", "user_id": 456,
                          "message": "retained legacy", "_heredity": provenance}
                store.put_message(123, -71, record)
                first.calls.clear()
                self.assertEqual(runtime.call("get_msg", {"message_id": -71}), record)
                self.assert_error(1409, lambda: runtime.call("delete_msg", {"message_id": -71}))
                self.assert_error(1409, lambda: runtime.ingest(
                    private_event(), session_epoch=manager.session_epoch))
                self.assertEqual(first.calls, [])
                self.assertEqual(store.get_message(123, -71), record)
                self.assertEqual(store.events_after(123), [])

    def test_completed_send_collision_with_unowned_legacy_data_keeps_no_retry_evidence(self):
        runtime, store, _, first, _, _ = self.make_runtime()
        record = {"message_id": -51, "message_type": "private", "user_id": 456,
                  "message": "retained legacy"}
        store.put_message(123, -51, record)
        failure = self.assert_error(
            1501, lambda: runtime.call("send_private_msg", {"user_id": 456, "message": "new"}))
        self.assertTrue(failure.details["action_completed"])
        self.assertEqual(failure.details["persistence_error"], "provider_identity_conflict")
        self.assertEqual(store.get_message(123, -51), record)
        self.assertEqual(sum(action == "send_private_msg" for action, _ in first.calls), 1)

    def test_cross_provider_event_id_collision_is_rejected_before_commit(self):
        runtime, store, manager, _, _, _ = self.make_runtime()
        runtime.ingest(private_event(), session_epoch=manager.session_epoch)
        original = store.get_message(123, -71)
        runtime.switch_provider("second", manager.session_epoch)
        with patch.object(store, "commit_event") as commit:
            self.assert_error(
                1409,
                lambda: runtime.ingest(
                    private_event(text="colliding"), session_epoch=manager.session_epoch
                ),
            )
            commit.assert_not_called()
        self.assertEqual(store.get_message(123, -71), original)

    def test_invalid_json_send_result_preserves_confirmed_minimal_record_without_success(self):
        runtime, store, manager, first, _, _ = self.make_runtime()
        first.send_result = {"message_id": -81, "invalid": math.nan, "private": PRIVATE_VALUE}
        failure = self.assert_error(
            1502,
            lambda: runtime.call("send_private_msg", {"user_id": 456, "message": "confirmed"}),
        )
        self.assertEqual(failure.details, {
            "action_completed": True, "message_id": -81, "retry_safe": False,
        })
        record = store.get_message(123, -81)
        self.assertEqual(record["raw_message"], "confirmed")
        self.assertEqual(record["message_id"], -81)
        self.assertEqual(record["user_id"], 456)
        self.assertEqual(record["_heredity"], {
            "provider_id": "first", "session_epoch": manager.session_epoch,
        })
        self.assertNotIn("invalid", record)
        self.assertNotIn("private", record)
        self.assertEqual(sum(action == "send_private_msg" for action, _ in first.calls), 1)
        self.assertFalse(runtime.online)
        self.assertFalse(manager.online)


if __name__ == "__main__":
    unittest.main()
