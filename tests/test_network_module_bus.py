"""Host module handles route by interface identity, without a platform plugin."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from heredity.network_bus import NetworkModuleBus
from nyxion_heredity_sdk import ContractError, DeadlineExceeded, thaw_json
from nyxion_heredity_sdk.networks import (
    ModuleBinding, NetworkBindingClosed, NetworkInterfaces, NetworkMessage,
    NetworkMessageGap,
)


FIRST = "1234567890abcdef"
SECOND = "fedcba0987654321"


class NetworkModuleBusTests(unittest.TestCase):
    def setUp(self):
        self.interfaces = {FIRST, SECOND}
        self.dispatched = []
        self.published = []
        self.bus = NetworkModuleBus(
            exists=lambda identifier: identifier in self.interfaces,
            dispatch=self.dispatch, publish=self.publish)
        self.addCleanup(self.bus.close)

    def dispatch(self, interface_id, action, params, timeout):
        self.dispatched.append((interface_id, action, params, timeout))
        return {"status": "ok", "data": {"interface": interface_id}}

    def publish(self, interface_id, event):
        self.published.append((interface_id, event))
        return {"accepted": True}

    def test_binding_calls_and_events_use_one_explicit_interface(self):
        binding = self.bus.bind("local.module", FIRST)
        result = binding.call("get_status", {"values": [1, 2]})
        self.assertEqual(result["data"]["interface"], FIRST)
        self.assertEqual(len(self.dispatched), 1)
        identifier, action, params, timeout = self.dispatched[0]
        self.assertEqual((identifier, action), (FIRST, "get_status"))
        self.assertEqual(params["values"], (1, 2))
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 30)
        with self.assertRaises(TypeError):
            result["data"]["interface"] = SECOND
        event = {"post_type": "message", "self_id": 123, "message": [{"type": "text"}]}
        self.assertTrue(binding.publish_event(event)["accepted"])
        self.assertEqual(self.published[0][0], FIRST)
        event["message"].clear()
        self.assertEqual(len(self.published[0][1]["message"]), 1)

    def test_each_subscription_receives_only_its_interface_and_no_action_replay(self):
        first = self.bus.bind("local.first", FIRST)
        second = self.bus.bind("local.second", SECOND)
        observed = self.bus.emit(FIRST, "action", {"action": "get_status", "params": {}})
        self.assertIs(first.receive(), observed)
        self.assertIsNone(second.receive())
        self.assertIsNone(first.receive())
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.published, [])

    def test_multi_subscriber_message_is_frozen_once_and_shared(self):
        first = self.bus.bind("local.module", FIRST)
        again = self.bus.bind("local.module", FIRST)
        other = self.bus.bind("local.observer", FIRST)
        payload = {"nested": [{"value": "snapshot"}]}
        message = self.bus.emit(FIRST, "event", payload)
        payload["nested"][0]["value"] = "mutated"
        self.assertIs(first.receive(), message)
        self.assertIs(again.receive(), message)
        self.assertIs(other.receive(), message)
        self.assertEqual(message.payload["nested"][0]["value"], "snapshot")
        self.assertEqual(self.bus.bindings(FIRST), [
            {"module_id": "local.module", "subscriptions": 2},
            {"module_id": "local.observer", "subscriptions": 1},
        ])
        again.close()
        self.assertEqual(self.bus.bindings(FIRST)[0]["subscriptions"], 1)
        self.assertEqual(self.bus.bindings(SECOND), [])

    def test_message_schema_is_bounded_and_payload_hidden_from_repr(self):
        value = NetworkMessage("message-1", FIRST, "event",
                               {"secret": "not-a-public-repr"}, 1.5)
        self.assertNotIn("not-a-public-repr", repr(value))
        with self.assertRaises(FrozenInstanceError):
            value.interface_id = SECOND
        for change in (
            {"interface_id": "../path"}, {"kind": "unknown"}, {"kind": []},
            {"timestamp": -1}, {"timestamp": True}, {"timestamp": float("nan")},
            {"payload": []}, {"payload": {"unsafe": object()}},
            {"payload": {"oversized": "x" * (1024 * 1024 + 1)}},
        ):
            fields = dict(id="message", interface_id=FIRST, kind="event", payload={}, timestamp=1)
            with self.subTest(change=list(change)), self.assertRaises(ContractError):
                NetworkMessage(**(fields | change))

    def test_invalid_identifiers_never_reach_host_callbacks(self):
        for identifier in ("", "123", "1234567890ABCDEF", "x" * 16, "a" * 17,
                           "1234567890abcde/", None):
            with self.subTest(identifier=identifier), self.assertRaises(ContractError):
                self.bus.bind("local.module", identifier)
        for module in ("", "module with spaces", "A.Module", "../module", "x" * 129):
            with self.subTest(module=module), self.assertRaises(ContractError):
                self.bus.bind(module, FIRST)
        for action in ("", "send/message", "x" * 129):
            binding = self.bus.bind("local.module", FIRST)
            with self.subTest(action=action), self.assertRaises(ContractError):
                binding.call(action, {})
            binding.close()
        self.assertEqual(self.dispatched, [])

    def test_nonexistent_interface_is_not_bound(self):
        self.interfaces.remove(FIRST)
        with self.assertRaises(NetworkBindingClosed):
            self.bus.bind("local.module", FIRST)
        self.assertEqual(self.bus.bindings(FIRST), [])

    def test_call_timeout_validation_and_expiry_do_not_dispatch(self):
        binding = self.bus.bind("local.module", FIRST)
        for timeout in (0, -1, True, 121, float("nan"), float("inf"), "30"):
            with self.subTest(timeout=timeout), self.assertRaises(ContractError):
                binding.call("get_status", {}, timeout=timeout)
        with patch("nyxion_heredity_sdk.networks.time.monotonic", side_effect=[100, 102]):
            with self.assertRaises(DeadlineExceeded):
                binding.call("get_status", {}, timeout=1)
        self.assertEqual(self.dispatched, [])

    def test_host_callback_failure_is_not_retried_or_wrapped_as_success(self):
        calls = []
        def fail(*args):
            calls.append(args)
            raise OSError("local synthetic callback failure")
        bus = NetworkModuleBus(exists=lambda identifier: True, dispatch=fail, publish=fail)
        self.addCleanup(bus.close)
        binding = bus.bind("local.module", FIRST)
        with self.assertRaises(OSError):
            binding.call("send_msg", {"message": "in-memory fixture"})
        with self.assertRaises(OSError):
            binding.publish_event({"post_type": "message"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(binding.interface_id, FIRST)

    def test_receive_waits_for_matching_message_and_times_out_without_side_effects(self):
        binding = self.bus.bind("local.module", FIRST)
        with ThreadPoolExecutor(max_workers=1) as pool:
            receiving = pool.submit(binding.receive, 1)
            message = self.bus.emit(FIRST, "response", {"status": "ok", "retcode": 0})
            self.assertIs(receiving.result(timeout=2), message)
        started = time.monotonic()
        self.assertIsNone(binding.receive(0.01))
        self.assertLess(time.monotonic() - started, 0.5)
        for timeout in (-1, True, 121, float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ContractError):
                binding.receive(timeout)
        self.assertEqual(self.dispatched, [])

    def test_overflow_reports_a_gap_and_rebind_is_explicit(self):
        bus = NetworkModuleBus(exists=lambda identifier: True, dispatch=self.dispatch,
                               publish=self.publish, capacity=2)
        self.addCleanup(bus.close)
        slow = bus.bind("local.slow", FIRST)
        fast = bus.bind("local.fast", FIRST)
        for number in range(3):
            bus.emit(FIRST, "event", {"number": number})
            self.assertEqual(fast.receive().payload["number"], number)
        self.assertTrue(slow.closed)
        with self.assertRaises(NetworkMessageGap) as failure:
            slow.receive()
        self.assertEqual(failure.exception.interface_id, FIRST)
        self.assertEqual(failure.exception.dropped_messages, 3)
        with self.assertRaises(NetworkMessageGap):
            slow.call("get_status", {})
        self.assertEqual(bus.bindings(FIRST), [{"module_id": "local.fast", "subscriptions": 1}])
        replacement = bus.bind("local.slow", FIRST)
        self.assertIsNone(replacement.receive())
        bus.emit(FIRST, "state", {"state": "running"})
        self.assertEqual(replacement.receive().payload["state"], "running")

    def test_invalidation_revokes_all_old_handles_and_increments_generation(self):
        first = self.bus.bind("local.module", FIRST)
        second = self.bus.bind("local.second", SECOND)
        self.bus.emit(FIRST, "event", {"previous": True})
        self.bus.invalidate(FIRST)
        self.assertTrue(first.closed)
        for operation in (lambda: first.call("get_status", {}), first.receive,
                          lambda: first.publish_event({"old": True})):
            with self.assertRaises(NetworkBindingClosed):
                operation()
        new = self.bus.bind("local.module", FIRST)
        self.assertGreater(new.generation, first.generation)
        self.assertIsNone(new.receive())
        self.assertFalse(second.closed)
        self.assertEqual(self.dispatched, [])

    def test_close_and_invalidation_wake_a_waiting_receiver(self):
        for close in (False, True):
            with self.subTest(close=close):
                binding = self.bus.bind("local.waiter", FIRST)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    receiving = pool.submit(binding.receive, 10)
                    if close:
                        binding.close()
                    else:
                        self.bus.invalidate(FIRST)
                    with self.assertRaises(NetworkBindingClosed):
                        receiving.result(timeout=1)
                binding.close()

    def test_stopped_interface_can_emit_state_without_changing_identity(self):
        binding = self.bus.bind("local.module", FIRST)
        generation = binding.generation
        self.bus.emit(FIRST, "state", {"state": "stopped"})
        self.assertEqual(binding.receive().payload["state"], "stopped")
        self.bus.emit(FIRST, "state", {"state": "running"})
        self.assertEqual(binding.receive().payload["state"], "running")
        self.assertEqual(binding.generation, generation)
        self.assertEqual(binding.interface_id, FIRST)

    def test_disappeared_interface_rejects_use_without_another_route(self):
        binding = self.bus.bind("local.module", FIRST)
        self.interfaces.remove(FIRST)
        with self.assertRaises(NetworkBindingClosed):
            binding.call("get_status", {})
        self.assertTrue(binding.closed)
        self.assertEqual(self.dispatched, [])

    def test_operations_do_not_hold_subscription_registry_lock_during_io(self):
        entered, release = threading.Event(), threading.Event()
        def delayed(*args):
            entered.set()
            if not release.wait(2):
                raise AssertionError("fixture operation was not released")
            return {"status": "ok"}
        bus = NetworkModuleBus(exists=lambda identifier: True, dispatch=delayed,
                               publish=self.publish)
        self.addCleanup(bus.close)
        binding = bus.bind("local.module", FIRST)
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(binding.call, "get_status", {})
            self.assertTrue(entered.wait(1))
            observed = bus.emit(FIRST, "state", {"state": "running"})
            self.assertIs(binding.receive(), observed)
            bus.invalidate(FIRST)
            release.set()
            self.assertEqual(running.result(timeout=1)["status"], "ok")
        self.assertTrue(binding.closed)
        self.assertEqual(bus.bindings(FIRST), [])

    def test_subscription_limit_is_bounded_and_released_after_close(self):
        bus = NetworkModuleBus(exists=lambda identifier: True, dispatch=self.dispatch,
                               publish=self.publish, max_bindings=1)
        self.addCleanup(bus.close)
        binding = bus.bind("local.module", FIRST)
        with self.assertRaises(ContractError):
            bus.bind("local.module", SECOND)
        binding.close()
        self.assertFalse(bus.bind("local.module", SECOND).closed)

    def test_bus_close_is_idempotent_and_blocks_new_bindings_and_emissions(self):
        binding = self.bus.bind("local.module", FIRST)
        self.bus.close()
        self.bus.close()
        self.assertTrue(binding.closed)
        self.assertEqual(self.bus.bindings(FIRST), [])
        with self.assertRaises(NetworkBindingClosed):
            binding.receive()
        with self.assertRaises(NetworkBindingClosed):
            self.bus.bind("local.other", FIRST)
        with self.assertRaises(NetworkBindingClosed):
            self.bus.emit(FIRST, "event", {})

    def test_public_handle_is_interface_service_not_a_plugin_contract(self):
        class Provider:
            def bind_module(self, module_id, interface_id):
                return self.bus.bind(module_id, interface_id)
        provider = Provider()
        provider.bus = self.bus
        self.assertIsInstance(provider, NetworkInterfaces)
        binding = provider.bind_module("local.module", FIRST)
        self.assertIsInstance(binding, ModuleBinding)
        self.assertFalse(hasattr(binding, "manifest"))
        self.assertFalse(hasattr(binding, "plugin_id"))
        self.assertEqual(thaw_json(binding.call("get_status", {}))["status"], "ok")

    def test_sdk_networks_imports_without_host_or_platform_modules(self):
        source = Path(__file__).resolve().parents[1] / "src"
        script = (
            "import sys;sys.path.insert(0,sys.argv[1]);"
            "from nyxion_heredity_sdk.networks import ModuleBinding,NetworkMessage;"
            "assert not any(n=='heredity' or n.startswith(('heredity.',"
            "'cryptography')) for n in sys.modules);"
            "assert not any(n.startswith('nyxion_heredity_') and "
            "not n.startswith('nyxion_heredity_sdk') for n in sys.modules);"
            "print('network-sdk-only')"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", script, str(source)], capture_output=True,
            text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("network-sdk-only", result.stdout)


if __name__ == "__main__":
    unittest.main()
