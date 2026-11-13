from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
import math
import threading
import time
import unittest
from uuid import UUID

from heredity.backends.base import BackendError
from heredity.providers.contracts import ProviderDescriptor
from heredity.providers.manager import ProviderManager, ProviderRegistration


SECRET = "fixture-credential-never-in-status"


def descriptor(identifier, *, available=True, actions=None):
    return ProviderDescriptor(
        provider_id=identifier,
        label="Fixture " + identifier,
        execution_level=None,
        source_kind="demo",
        host_kind="isolated_fixture",
        session_owner="fixture",
        target_memory_access="none",
        in_process_code=False,
        business_request_builder="fixture",
        primary_transport_owner="fixture",
        supported_actions=tuple(actions or ("get_login_info", "send_private_msg", "get_friend_list")),
        availability="ready" if available else "unimplemented",
    )


class Provider:
    self_id = 123

    def __init__(self, name="fixture"):
        self.name = name
        self.online = True
        self.login_id = self.self_id
        self.calls = []
        self.close_calls = 0
        self.close_error = False
        self.login_error = False
        self.fail_action = None
        self.block_action = None
        self.action_started = threading.Event()
        self.action_release = threading.Event()
        self.close_started = threading.Event()
        self.close_release = None

    def call(self, action, params):
        self.calls.append((action, deepcopy(params)))
        if action == self.block_action:
            self.action_started.set()
            if not self.action_release.wait(3):
                raise RuntimeError("fixture action wait exceeded")
        if action == "get_login_info":
            if self.login_error:
                raise RuntimeError(SECRET)
            return {"user_id": self.login_id, "nickname": self.name, "private": SECRET}
        if action == self.fail_action:
            raise BackendError(SECRET, 1502, details={"private": SECRET})
        params["mutated"] = True
        return {"provider": self.name, "message_id": -17}

    def close(self):
        self.close_calls += 1
        self.close_started.set()
        if self.close_release is not None and not self.close_release.wait(3):
            raise RuntimeError("fixture close wait exceeded")
        if self.close_error:
            raise RuntimeError(SECRET)
        self.online = False


class ProviderManagerTests(unittest.TestCase):
    def make_manager(self, *, initial="first", second=None):
        first = Provider("first")
        second = second or Provider("second")
        factories = []

        def factory(provider):
            def create():
                factories.append(provider.name)
                return provider
            return create

        registrations = [
            ProviderRegistration(descriptor("first"), factory(first)),
            ProviderRegistration(descriptor("second"), factory(second)),
            ProviderRegistration(descriptor("unavailable", available=False), factory(Provider())),
        ]
        manager = ProviderManager(123, registrations, initial)
        self.addCleanup(manager.close)
        return manager, first, second, factories

    def assert_error(self, code, operation):
        with self.assertRaises(BackendError) as caught:
            operation()
        self.assertEqual(caught.exception.retcode, code)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn(SECRET, repr(caught.exception.details))
        return caught.exception

    def wait_for(self, predicate, seconds=1):
        deadline = time.monotonic() + seconds
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Fixture state did not settle")
            time.sleep(0.002)

    def test_initial_factory_does_not_call_provider_and_business_requires_login(self):
        manager, first, _, factories = self.make_manager()
        self.assertEqual(factories, ["first"])
        self.assertEqual(first.calls, [])
        self.assertEqual(manager.snapshot()["state"], "disconnected")
        self.assertFalse(manager.online)
        self.assert_error(1503, lambda: manager.call("send_private_msg", {}))
        self.assertEqual(first.calls, [])
        result = manager.call("get_login_info", {})
        self.assertEqual(result["user_id"], 123)
        self.assertTrue(manager.online)
        self.assertEqual(manager.snapshot()["state"], "ready")

    def test_unconfigured_manager_does_not_construct_any_provider(self):
        manager, _, _, factories = self.make_manager(initial=None)
        self.assertEqual(factories, [])
        self.assertEqual(manager.snapshot()["state"], "unconfigured")
        self.assertIsNone(manager.active_descriptor)
        self.assertEqual(manager.source_kind, "unconfigured")
        UUID(manager.session_epoch)
        self.assert_error(1503, lambda: manager.call("get_login_info", {}))
        changed = manager.switch("second", manager.session_epoch)
        self.assertTrue(changed["changed"])
        self.assertEqual(changed["active_provider_id"], "second")
        self.assertTrue(manager.online)

    def test_no_account_manager_only_exposes_catalog(self):
        registration = ProviderRegistration(descriptor("first"), Provider)
        manager = ProviderManager(0, [registration])
        self.addCleanup(manager.close)
        self.assert_error(1503, lambda: manager.call("get_login_info", {}))
        self.assert_error(1503, lambda: manager.switch("first", manager.session_epoch))
        with self.assertRaises(ValueError):
            ProviderManager(0, [registration], "first")

    def test_unsupported_actions_do_not_reach_provider(self):
        manager, first, _, _ = self.make_manager()
        self.assert_error(1404, lambda: manager.call("unsupported", {}))
        self.assertEqual(first.calls, [])
        bad = ProviderRegistration(descriptor("no-login", actions=("send_private_msg",)), Provider)
        with self.assertRaises(BackendError):
            ProviderManager(123, [bad], "no-login")

    def test_initial_factory_exception_is_sanitized(self):
        def broken():
            raise RuntimeError(SECRET)

        self.assert_error(
            1503,
            lambda: ProviderManager(123, [ProviderRegistration(descriptor("broken"), broken)], "broken"),
        )

    def test_initial_wrong_login_revokes_readiness_without_replacing_owner(self):
        manager, first, _, _ = self.make_manager()
        first.login_id = 999
        self.assert_error(1503, lambda: manager.call("get_login_info", {}))
        self.assertFalse(manager.online)
        self.assertEqual(manager.active_descriptor.provider_id, "first")
        self.assert_error(1503, lambda: manager.call("send_private_msg", {}))

    def test_switch_closes_old_before_factory_and_verifies_new_account(self):
        events = []
        first, second = Provider("first"), Provider("second")
        original_close, original_login = first.close, second.call

        def close_old():
            events.append("close-first")
            original_close()

        def create_new():
            events.append("create-second")
            return second

        def call_new(action, params):
            events.append(action)
            return original_login(action, params)

        first.close, second.call = close_old, call_new
        manager = ProviderManager(123, [
            ProviderRegistration(descriptor("first"), lambda: first),
            ProviderRegistration(descriptor("second"), create_new),
        ], "first")
        self.addCleanup(manager.close)
        manager.call("get_login_info", {})
        old_epoch = manager.session_epoch
        result = manager.switch("second", old_epoch)
        self.assertEqual(events, ["close-first", "create-second", "get_login_info"])
        self.assertTrue(result["changed"])
        self.assertNotEqual(old_epoch, result["session_epoch"])
        self.assertTrue(manager.online)
        self.assertEqual(manager.source_kind, "demo")

    def test_same_ready_provider_is_noop(self):
        manager, first, _, factories = self.make_manager()
        manager.call("get_login_info", {})
        epoch = manager.session_epoch
        result = manager.switch("first", epoch)
        self.assertFalse(result["changed"])
        self.assertEqual(result["session_epoch"], epoch)
        self.assertEqual(first.close_calls, 0)
        self.assertEqual(factories, ["first"])

    def test_stale_epoch_and_unavailable_or_missing_target_preserve_owner(self):
        manager, first, _, factories = self.make_manager()
        manager.call("get_login_info", {})
        epoch = manager.session_epoch
        conflict = self.assert_error(1409, lambda: manager.switch("second", "old"))
        self.assertEqual(conflict.details, {"session_epoch": epoch})
        self.assert_error(1503, lambda: manager.switch("unavailable", epoch))
        self.assert_error(1404, lambda: manager.switch("missing", epoch))
        self.assertEqual(manager.active_descriptor.provider_id, "first")
        self.assertEqual(manager.session_epoch, epoch)
        self.assertEqual(first.close_calls, 0)
        self.assertEqual(factories, ["first"])

    def test_drain_timeout_does_not_detach_or_close_inflight_provider(self):
        manager, first, _, factories = self.make_manager()
        manager.call("get_login_info", {})
        first.block_action = "send_private_msg"
        epoch = manager.session_epoch
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(manager.call, "send_private_msg", {"message": "once"})
            self.assertTrue(first.action_started.wait(1))
            try:
                self.assert_error(1504, lambda: manager.switch("second", epoch, timeout=0.02))
                self.assertEqual(manager.session_epoch, epoch)
                self.assertEqual(manager.active_descriptor.provider_id, "first")
                self.assertEqual(manager.snapshot()["in_flight"], 1)
                self.assertEqual(first.close_calls, 0)
                self.assertEqual(factories, ["first"])
                self.assert_error(1504, lambda: manager.call("get_login_info", {}))
            finally:
                first.action_release.set()
            self.assertEqual(future.result(timeout=1)["message_id"], -17)
        self.assertTrue(manager.online)
        manager.call("get_friend_list", {})

    def test_switch_serializes_and_second_old_epoch_never_builds_provider(self):
        manager, first, second, factories = self.make_manager()
        manager.call("get_login_info", {})
        second.block_action = "get_login_info"
        epoch = manager.session_epoch
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_switch = executor.submit(manager.switch, "second", epoch, 1)
            self.assertTrue(second.action_started.wait(1))
            second_switch = executor.submit(manager.switch, "first", epoch, 1)
            try:
                self.assert_error(1409, lambda: second_switch.result(timeout=1))
                self.assert_error(1503, lambda: manager.call("get_friend_list", {}))
            finally:
                second.action_release.set()
            self.assertTrue(first_switch.result(timeout=1)["changed"])
        self.assertEqual(factories, ["first", "second"])
        self.assertEqual(first.close_calls, 1)
        self.assertEqual(manager.active_descriptor.provider_id, "second")

    def test_switch_drain_stops_new_admission_then_completes(self):
        manager, first, _, _ = self.make_manager()
        manager.call("get_login_info", {})
        first.block_action = "send_private_msg"
        with ThreadPoolExecutor(max_workers=2) as executor:
            call = executor.submit(manager.call, "send_private_msg", {})
            self.assertTrue(first.action_started.wait(1))
            switch = executor.submit(manager.switch, "second", manager.session_epoch, 1)
            self.wait_for(lambda: manager.snapshot()["state"] == "draining")
            try:
                self.assert_error(1503, lambda: manager.call("get_friend_list", {}))
                self.assertEqual(first.close_calls, 0)
            finally:
                first.action_release.set()
            call.result(timeout=1)
            self.assertTrue(switch.result(timeout=1)["changed"])
        self.assertEqual(first.close_calls, 1)

    def test_wrong_account_candidate_is_closed_and_old_owner_is_not_restored(self):
        wrong = Provider("wrong")
        wrong.login_id = 999
        manager, first, _, _ = self.make_manager(second=wrong)
        epoch = manager.session_epoch
        self.assert_error(1503, lambda: manager.switch("second", epoch))
        self.assertEqual(first.close_calls, 1)
        self.assertEqual(wrong.close_calls, 1)
        self.assertIsNone(manager.active_descriptor)
        self.assertEqual(manager.snapshot()["state"], "failed")
        self.assertNotEqual(epoch, manager.session_epoch)
        self.assert_error(1503, lambda: manager.call("get_login_info", {}))

    def test_candidate_binding_must_match_before_login_can_confirm_anything(self):
        for binding in (999, "123", True, None):
            with self.subTest(binding=binding):
                candidate = Provider("different-binding")
                candidate.self_id = binding
                candidate.login_id = 123
                manager, first, _, _ = self.make_manager(second=candidate)
                epoch = manager.session_epoch
                self.assert_error(1503, lambda: manager.switch("second", epoch))
                self.assertEqual(candidate.calls, [])
                self.assertEqual(candidate.close_calls, 1)
                self.assertEqual(first.close_calls, 1)
                self.assertIsNone(manager.active_descriptor)
                self.assertNotEqual(epoch, manager.session_epoch)

    def test_initial_candidate_binding_is_checked_without_network_calls(self):
        candidate = Provider()
        candidate.self_id = 999
        candidate.login_id = 123
        self.assert_error(
            1503,
            lambda: ProviderManager(
                123, [ProviderRegistration(descriptor("wrong"), lambda: candidate)], "wrong"
            ),
        )
        self.assertEqual(candidate.calls, [])
        self.assertEqual(candidate.close_calls, 1)

    def test_failed_factory_and_login_leave_no_fallback_owner(self):
        for failure in ("factory", "login"):
            with self.subTest(failure=failure):
                first, second = Provider("first"), Provider("second")
                second.login_error = True

                def factory():
                    if failure == "factory":
                        raise RuntimeError(SECRET)
                    return second

                manager = ProviderManager(123, [
                    ProviderRegistration(descriptor("first"), lambda: first),
                    ProviderRegistration(descriptor("second"), factory),
                ], "first")
                self.addCleanup(manager.close)
                epoch = manager.session_epoch
                self.assert_error(1503, lambda: manager.switch("second", epoch))
                self.assertEqual(manager.snapshot()["state"], "failed")
                self.assertIsNone(manager.active_descriptor)
                self.assertNotEqual(manager.session_epoch, epoch)
                self.assertEqual(first.close_calls, 1)
                self.assertEqual(second.close_calls, 1 if failure == "login" else 0)

    def test_failed_old_close_is_retained_and_no_candidate_is_created_before_retry(self):
        manager, first, _, factories = self.make_manager()
        first.close_error = True
        epoch = manager.session_epoch
        self.assert_error(1503, lambda: manager.switch("second", epoch))
        self.assertNotEqual(manager.session_epoch, epoch)
        self.assertEqual(manager.snapshot()["state"], "close_failed")
        self.assertEqual(factories, ["first"])
        first.close_error = False
        result = manager.switch("second", manager.session_epoch)
        self.assertTrue(result["changed"])
        self.assertEqual(first.close_calls, 2)
        self.assertEqual(factories, ["first", "second"])

    def test_blocked_close_has_bounded_timeout_and_retains_owner_until_resolved(self):
        manager, first, _, factories = self.make_manager()
        first.close_release = threading.Event()
        epoch = manager.session_epoch
        try:
            self.assert_error(1504, lambda: manager.switch("second", epoch, timeout=0.02))
            self.assertTrue(first.close_started.wait(1))
            self.assertNotEqual(manager.session_epoch, epoch)
            self.assertEqual(factories, ["first"])
            self.assert_error(
                1504, lambda: manager.switch("second", manager.session_epoch, timeout=0.02)
            )
            self.assertEqual(first.close_calls, 1)
        finally:
            first.close_release.set()
        self.wait_for(lambda: manager.snapshot()["state"] == "failed")
        self.assertTrue(manager.switch("second", manager.session_epoch)["changed"])

    def test_timed_out_login_cannot_activate_late_candidate(self):
        second = Provider("second")
        second.block_action = "get_login_info"
        manager, _, _, _ = self.make_manager(second=second)
        try:
            self.assert_error(
                1504, lambda: manager.switch("second", manager.session_epoch, timeout=0.02)
            )
            epoch = manager.session_epoch
            self.assertIsNone(manager.active_descriptor)
        finally:
            second.action_release.set()
        self.wait_for(lambda: second.close_calls == 1)
        self.assertIsNone(manager.active_descriptor)
        self.assertEqual(manager.session_epoch, epoch)
        self.assertFalse(manager.online)
        self.assert_error(1503, lambda: manager.call("get_login_info", {}))

    def test_close_cancels_candidate_verification_and_waits_for_its_cleanup(self):
        second = Provider("second")
        second.block_action = "get_login_info"
        manager, first, _, _ = self.make_manager(second=second)
        with ThreadPoolExecutor(max_workers=2) as executor:
            switching = executor.submit(manager.switch, "second", manager.session_epoch, 1)
            self.assertTrue(second.action_started.wait(1))
            closing = executor.submit(manager.close, 1)
            try:
                self.wait_for(lambda: manager.snapshot()["state"] == "closing")
                self.assertIsNone(manager.active_descriptor)
                self.assertEqual(second.close_calls, 0)
                self.assert_error(1503, lambda: manager.call("get_friend_list", {}))
            finally:
                second.action_release.set()
            self.assert_error(1503, lambda: switching.result(timeout=1))
            closing.result(timeout=1)
        self.assertEqual(first.close_calls, 1)
        self.assertEqual(second.close_calls, 1)
        self.assertEqual(manager.snapshot()["state"], "closed")
        self.assertIsNone(manager.active_descriptor)

    def test_failed_candidate_cleanup_is_retained_and_blocks_new_init_until_closed(self):
        second = Provider("second")
        second.login_id = 999
        second.close_error = True
        manager, _, _, factories = self.make_manager(second=second)
        self.assert_error(1503, lambda: manager.switch("second", manager.session_epoch))
        self.assertEqual(manager.snapshot()["state"], "close_failed")
        self.assertEqual(second.close_calls, 1)
        self.assertEqual(factories, ["first", "second"])
        second.close_error = False
        manager.close()
        self.assertEqual(second.close_calls, 2)
        self.assertEqual(manager.snapshot()["state"], "closed")

    def test_timed_out_factory_is_cleaned_without_login_or_automatic_retry(self):
        candidate = Provider("late")
        started, release = threading.Event(), threading.Event()

        def factory():
            started.set()
            release.wait(3)
            return candidate

        manager = ProviderManager(123, [ProviderRegistration(descriptor("late"), factory)])
        self.addCleanup(manager.close)
        try:
            self.assert_error(
                1504, lambda: manager.switch("late", manager.session_epoch, timeout=0.02)
            )
            self.assertTrue(started.is_set())
        finally:
            release.set()
        self.wait_for(lambda: candidate.close_calls == 1)
        self.assertEqual(candidate.calls, [])
        self.assertIsNone(manager.active_descriptor)

    def test_close_drains_actions_without_closing_beneath_them_and_can_retry(self):
        manager, first, _, _ = self.make_manager()
        manager.call("get_login_info", {})
        first.block_action = "send_private_msg"
        epoch = manager.session_epoch
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(manager.call, "send_private_msg", {})
            self.assertTrue(first.action_started.wait(1))
            try:
                self.assert_error(1504, lambda: manager.close(timeout=0.02))
                self.assertEqual(first.close_calls, 0)
                self.assertEqual(manager.session_epoch, epoch)
                self.assert_error(1503, lambda: manager.call("get_login_info", {}))
            finally:
                first.action_release.set()
            future.result(timeout=1)
        manager.close()
        manager.close()
        self.assertEqual(first.close_calls, 1)
        self.assertEqual(manager.snapshot()["state"], "closed")
        self.assertIsNone(manager.active_descriptor)

    def test_close_failure_retains_handle_for_explicit_retry(self):
        manager, first, _, factories = self.make_manager()
        first.close_error = True
        self.assert_error(1503, manager.close)
        self.assertEqual(manager.snapshot()["state"], "close_failed")
        self.assert_error(1503, lambda: manager.switch("second", manager.session_epoch))
        self.assertEqual(factories, ["first"])
        first.close_error = False
        manager.close()
        self.assertEqual(first.close_calls, 2)
        self.assertEqual(manager.snapshot()["state"], "closed")

    def test_call_payload_status_and_registration_are_isolated(self):
        manager, first, _, _ = self.make_manager()
        manager.call("get_login_info", {})
        params = {"nested": {"value": 1}}
        before = deepcopy(params)
        result = manager.call("send_private_msg", params)
        self.assertEqual(result["message_id"], -17)
        self.assertEqual(params, before)
        snapshot = manager.snapshot()
        self.assertEqual(set(snapshot), {
            "state", "active_provider_id", "session_epoch", "descriptor", "providers", "in_flight"
        })
        self.assertNotIn(SECRET, json.dumps(snapshot))
        snapshot["descriptor"]["supported_actions"].clear()
        self.assertTrue(manager.active_descriptor.supported_actions)
        registration = ProviderRegistration(descriptor("immutable"), Provider)
        with self.assertRaises(FrozenInstanceError):
            registration.factory = lambda: None
        first.fail_action = "send_private_msg"
        self.assert_error(1502, lambda: manager.call("send_private_msg", {}))
        self.assertFalse(manager.online)
        self.assertNotIn(SECRET, json.dumps(manager.snapshot()))

    def test_invalid_send_result_preserves_confirmed_message_id_without_private_data(self):
        manager, first, _, _ = self.make_manager()
        manager.call("get_login_info", {})
        original = first.call

        def call(action, params):
            if action == "send_private_msg":
                return {"message_id": "-61", "invalid": math.nan, "private": SECRET}
            return original(action, params)

        first.call = call
        failure = self.assert_error(1502, lambda: manager.call("send_private_msg", {}))
        self.assertEqual(failure.details, {
            "action_completed": True, "message_id": -61, "retry_safe": False
        })
        self.assertFalse(manager.online)

    def test_invalid_send_identity_reports_unknown_and_is_never_retry_safe(self):
        for identifier in (None, 0, True, 1.0, 2**31):
            with self.subTest(identifier=identifier):
                manager, first, _, _ = self.make_manager()
                manager.call("get_login_info", {})
                first.call = lambda action, params: {
                    "message_id": identifier, "invalid": math.nan, "private": SECRET
                }
                failure = self.assert_error(1502, lambda: manager.call("send_private_msg", {}))
                self.assertEqual(failure.details, {
                    "action_outcome": "unknown", "retry_safe": False
                })

    def test_invalid_non_send_result_does_not_claim_a_business_effect(self):
        manager, first, _, _ = self.make_manager()
        manager.call("get_login_info", {})
        first.call = lambda action, params: {"message_id": -1, "invalid": math.nan}
        failure = self.assert_error(1502, lambda: manager.call("get_friend_list", {}))
        self.assertIsNone(failure.details)

    def test_backend_errors_preserve_only_typed_bounded_confirmation_fields(self):
        manager, first, _, _ = self.make_manager()
        manager.call("get_login_info", {})
        epoch = manager.session_epoch
        details = {
            "action_completed": True,
            "message_id": "-71",
            "retry_safe": False,
            "action_outcome": "unknown",
            "session_epoch": epoch,
            "private": SECRET,
            "payload": {"private": SECRET},
        }

        def call(action, params):
            raise BackendError(SECRET, 1307, details=details)

        first.call = call
        failure = self.assert_error(1307, lambda: manager.call("send_private_msg", {}))
        self.assertEqual(failure.details, {
            "action_completed": True, "message_id": -71, "retry_safe": False,
            "action_outcome": "unknown", "session_epoch": epoch,
        })
        self.assertEqual(details["message_id"], "-71")
        failure.details["retry_safe"] = True
        self.assertFalse(details["retry_safe"])
        self.assertTrue(manager.online)

    def test_bad_confirmation_field_types_are_removed(self):
        manager, first, _, _ = self.make_manager()
        manager.call("get_login_info", {})

        def call(action, params):
            raise BackendError(SECRET, 1400, details={
                "action_completed": 1, "message_id": True, "retry_safe": "false",
                "action_outcome": SECRET, "session_epoch": "x" * 129, "secret": SECRET,
            })

        first.call = call
        failure = self.assert_error(1400, lambda: manager.call("send_private_msg", {}))
        self.assertIsNone(failure.details)

    def test_unknown_business_exception_has_explicit_no_retry_details(self):
        manager, first, _, _ = self.make_manager()
        manager.call("get_login_info", {})

        def call(action, params):
            raise RuntimeError(SECRET)

        first.call = call
        failure = self.assert_error(1502, lambda: manager.call("send_private_msg", {}))
        self.assertEqual(failure.details, {"action_outcome": "unknown", "retry_safe": False})
        self.assertFalse(manager.online)

    def test_timeouts_and_registration_configuration_are_validated(self):
        manager, _, _, _ = self.make_manager()
        for timeout in (0, -1, True, math.inf, math.nan, 61, "5"):
            with self.subTest(timeout=timeout):
                self.assert_error(
                    1400, lambda: manager.switch("second", manager.session_epoch, timeout)
                )
        registration = ProviderRegistration(descriptor("same"), Provider)
        with self.assertRaises(ValueError):
            ProviderManager(123, [registration, registration])
        for identifier in (True, -1, 2**63):
            with self.assertRaises(ValueError):
                ProviderManager(identifier, [])


if __name__ == "__main__":
    unittest.main()
