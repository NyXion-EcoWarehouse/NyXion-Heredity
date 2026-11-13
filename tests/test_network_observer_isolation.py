"""An observation failure cannot change a completed OneBot operation."""

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from heredity.event_hub import EventHub
from heredity.network_bus import NetworkModuleBus
from heredity.network_interfaces import NetworkInterfaceManager
from nyxion_heredity_sdk import BackendError, ContractError, thaw_json
from nyxion_heredity_sdk.networks import NetworkMessageGap


def nested(depth=35):
    value = {"text": "ordinary-valid-json"}
    for _ in range(depth):
        value = {"nested": value}
    return value


class EmptyAccounts:
    def account_descriptors(self):
        return []

    def account_runtimes(self):
        return []


class LocalBackend:
    self_id = 12345678
    online = True

    def __init__(self):
        self.calls = []
        self.result = {"message_id": 7}
        self.failure = None
        self.runtime = None

    def event_runtime(self):
        return self.runtime

    def status(self):
        return {"online": True, "self_id": self.self_id}

    def call(self, action, params):
        self.calls.append((action, params))
        if self.failure is not None:
            raise self.failure
        return self.result


class LocalRuntime:
    self_id = LocalBackend.self_id

    def __init__(self):
        self.event_hub = EventHub(self.self_id, capacity=32)
        self._sequence = 0
        self._lock = threading.Lock()

    def emit_meta(self, fields):
        self.publish({"time": 1, "self_id": self.self_id,
                      "post_type": "meta_event", **fields})

    def status(self):
        return {"online": True, "self_id": self.self_id}

    def publish(self, event):
        with self._lock:
            self._sequence += 1
            self.event_hub.publish(event, self._sequence)


class EventReceiver:
    def __init__(self):
        self.received = queue.Queue()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                owner.received.put(json.loads(raw))
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/events"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)


class NetworkObserverIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="NyXion observer isolation ")
        self.addCleanup(self.temporary.cleanup)
        self.manager = NetworkInterfaceManager(Path(self.temporary.name), EmptyAccounts())
        self.addCleanup(self.manager.close)
        created = self.manager.create({
            "name": "Local observer interface", "type": "http_server", "enabled": True,
            "account": None,
            "settings": {"host": "127.0.0.1", "port": 0, "sse_enabled": True},
        })
        self.identifier = created["interface_id"]
        self.token = self.manager.credentials(self.identifier)["access_token"]
        self.resource = self.manager._resources[self.identifier]
        self.server = self.resource.server
        self.backend = LocalBackend()
        self.resource.backend = self.backend
        self.server.backend = self.backend

    def binding(self, module="test.observer"):
        value = self.manager.bind_module(module, self.identifier)
        self.addCleanup(value.close)
        return value

    def request(self, action, params):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request("POST", "/", json.dumps({"action": action, "params": params}),
                               {"Content-Type": "application/json",
                                "Authorization": "Bearer " + self.token})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def assert_sticky_gap(self, binding):
        self.assertTrue(binding.closed)
        for _ in range(2):
            with self.assertRaises(NetworkMessageGap) as error:
                binding.receive()
            self.assertEqual(error.exception.interface_id, self.identifier)
            self.assertGreaterEqual(error.exception.dropped_messages, 1)

    def test_valid_deep_action_params_execute_once_and_return_the_original_result(self):
        binding = self.binding()
        params = nested()
        status, result = self.request("synthetic_write", params)
        self.assertEqual(status, 200)
        self.assertEqual(result, {"status": "ok", "retcode": 0, "data": {"message_id": 7}})
        self.assertEqual(self.backend.calls, [("synthetic_write", params)])
        self.assert_sticky_gap(binding)
        # A new subscription can start a fresh complete observation window.
        next_binding = self.binding("test.next")
        status, result = self.request("synthetic_read", {"value": 1})
        self.assertEqual((status, result["status"]), (200, "ok"))
        self.assertEqual(next_binding.receive().kind, "action")
        self.assertEqual(next_binding.receive().kind, "response")
        self.assertFalse(next_binding.closed)

    def test_completed_large_response_is_not_replaced_by_an_observer_error(self):
        binding = self.binding()
        # Valid ordinary JSON, well below the HTTP byte limit but above the
        # SDK observation node budget. The actual action has already finished.
        self.backend.result = {"values": list(range(33000)), "message_id": 21}
        status, result = self.request("synthetic_write", {})
        self.assertEqual((status, result["status"], result["retcode"]), (200, "ok", 0))
        self.assertEqual(result["data"], self.backend.result)
        self.assertEqual(len(self.backend.calls), 1)
        self.assert_sticky_gap(binding)

    def test_backend_completion_details_survive_an_observer_rejection(self):
        binding = self.binding()
        self.backend.failure = BackendError(
            "x" * (1024 * 1024), 1501,
            details={"action_completed": True, "message_id": 22, "retry_safe": False})
        status, result = self.server.dispatch("synthetic_write", {})
        self.assertEqual((status, result["status"], result["retcode"]), (200, "failed", 1501))
        self.assertEqual(result["details"], {
            "action_completed": True, "message_id": 22, "retry_safe": False})
        self.assertEqual(len(self.backend.calls), 1)
        self.assert_sticky_gap(binding)

    def test_manager_module_call_keeps_completed_raw_result_when_observation_is_too_large(self):
        observer = self.binding()
        self.backend.result = {"values": list(range(33000)), "message_id": 23}
        result = self.manager._module_call(
            self.identifier, "synthetic_write", {}, timeout=2)
        self.assertEqual(result, {"status": "ok", "retcode": 0, "data": self.backend.result})
        self.assertEqual(len(self.backend.calls), 1)
        self.assert_sticky_gap(observer)

    def test_manager_module_binding_small_result_remains_valid_after_an_old_gap(self):
        stale = self.binding()
        self.manager._module_call(self.identifier, "synthetic_read", nested(), timeout=2)
        self.assert_sticky_gap(stale)
        active = self.binding("test.active")
        result = thaw_json(active.call("synthetic_read", {"value": 2}, timeout=2))
        self.assertEqual(result["data"], {"message_id": 7})
        self.assertEqual(active.receive().kind, "action")
        self.assertEqual(active.receive().kind, "response")
        self.assertFalse(active.closed)

    def test_generic_http_observer_failure_cannot_suppress_action_or_response(self):
        binding = self.binding()
        with patch.object(self.server, "network_observer",
                          side_effect=RuntimeError("private-observer-failure")):
            status, result = self.request("synthetic_write", {"value": 3})
        self.assertEqual((status, result["status"]), (200, "ok"))
        self.assertEqual(len(self.backend.calls), 1)
        self.assert_sticky_gap(binding)

    def test_event_observation_failure_does_not_stop_the_pump_or_next_event(self):
        receiver = EventReceiver()
        self.addCleanup(receiver.close)
        with self.server._settings_lock:
            self.server.event_urls = (receiver.url,)
        runtime = LocalRuntime()
        self.addCleanup(runtime.event_hub.close)
        self.addCleanup(self.manager.close)
        observer = self.binding()
        self.backend.runtime = runtime
        deep_event = {
            "time": 11, "self_id": runtime.self_id, "post_type": "meta_event",
            "meta_event_type": "heartbeat", "status": {"online": True, "extra": nested()}}
        runtime.publish(deep_event)
        runtime.publish({
            "time": 12, "self_id": runtime.self_id, "post_type": "meta_event",
            "meta_event_type": "lifecycle", "sub_type": "enable"})
        received = []
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                value = receiver.received.get(timeout=0.1)
            except queue.Empty:
                continue
            if value.get("time") in (11, 12):
                received.append(value)
            if len(received) == 2:
                break
        self.assertEqual([value["time"] for value in received], [11, 12])
        self.assertEqual(received[0], deep_event)
        self.assertTrue(self.server._event_thread.is_alive())
        self.assert_sticky_gap(observer)
        fresh = self.binding("test.after-event-gap")
        runtime.publish({
            "time": 13, "self_id": runtime.self_id, "post_type": "meta_event",
            "meta_event_type": "lifecycle", "sub_type": "enable"})
        deadline = time.monotonic() + 3
        observed = None
        while time.monotonic() < deadline:
            value = fresh.receive(0.1)
            if value is not None and value.kind == "event" and value.payload.get("time") == 13:
                observed = value
                break
        self.assertIsNotNone(observed)
        self.assertTrue(self.server._event_thread.is_alive())
        with self.server._settings_lock:
            self.server.event_urls = ()
        self.backend.runtime = None

    def test_direct_bus_emit_retains_strict_validation(self):
        bus = NetworkModuleBus(exists=lambda identifier: True,
                               dispatch=lambda *args: None, publish=lambda *args: None)
        self.addCleanup(bus.close)
        with self.assertRaises(ContractError):
            bus.emit(self.identifier, "response", nested())


if __name__ == "__main__":
    unittest.main()
