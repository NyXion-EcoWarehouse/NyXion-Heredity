"""Real local endpoints obey revocation without replaying a paused account."""

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from heredity.event_hub import EventHub
from heredity.network_interfaces import NetworkInterfaceManager
from nyxion_heredity_sdk import NetworkBindingClosed


ACCOUNT = 12345678


class LocalRuntime:
    self_id = ACCOUNT
    online = True

    def __init__(self):
        self.event_hub = EventHub(self.self_id, capacity=64)
        self._sequence = 0
        self._lock = threading.Lock()

    def status(self):
        return {"online": True, "good": True, "self_id": self.self_id}

    def call(self, action, params):
        return {"user_id": self.self_id}

    def emit_meta(self, fields):
        self.publish({"time": 1, "self_id": self.self_id, "post_type": "meta_event", **fields})

    def publish(self, event):
        with self._lock:
            self._sequence += 1
            self.event_hub.publish(event, self._sequence)


class LocalAccounts:
    def __init__(self, runtime):
        self.runtime = runtime

    def account_runtimes(self):
        return [(SimpleNamespace(platform="example", self_id=str(ACCOUNT)), self.runtime)]

    def account_descriptors(self):
        return [{"platform": "example", "self_id": str(ACCOUNT)}]


class LocalReceiver:
    def __init__(self):
        self.received = queue.Queue()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
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


class RoutingGateEventTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="NyXion routing gate ")
        self.addCleanup(temporary.cleanup)
        self.runtime = LocalRuntime()
        self.addCleanup(self.runtime.event_hub.close)
        self.manager = NetworkInterfaceManager(Path(temporary.name), LocalAccounts(self.runtime))
        self.addCleanup(self.manager.close)
        self.receiver = LocalReceiver()
        self.addCleanup(self.receiver.close)
        self.addCleanup(self.manager.close)

    def create(self, kind="http_server", *, account=f"example:{ACCOUNT}"):
        value = self.manager.create({
            "name": "Local gated endpoint", "type": kind, "enabled": True, "account": account,
            "settings": {"port": 0, "sse_enabled": True, "heartbeat_seconds": 3600,
                         "event_urls": [self.receiver.url], "url": self.receiver.url},
        })
        self.assertEqual(value["state"], "running", value)
        identifier = value["interface_id"]
        self.add_owner(identifier, "owner-a")
        return identifier, self.manager._resources[identifier]

    def add_owner(self, identifier, owner):
        state = self.manager.allocation_state(identifier)
        self.manager.add_allocation_owner(
            identifier, owner, state["revision"], state["generation"])

    @staticmethod
    def event(marker):
        return {"time": marker, "self_id": ACCOUNT, "post_type": "meta_event",
                "meta_event_type": "lifecycle", "sub_type": "enable"}

    def wait_for_marker(self, marker):
        seen = []
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                event = self.receiver.received.get(timeout=0.1)
            except queue.Empty:
                continue
            seen.append(event)
            if event.get("time") == marker:
                return seen
        self.fail("The active local account event did not reach its endpoint")

    def request(self, resource, path):
        connection = http.client.HTTPConnection(*resource.server.server_address, timeout=2)
        token = self.manager.credentials(resource.config["interface_id"])["access_token"]
        connection.request("GET", path, headers={"Authorization": "Bearer " + token})
        return connection, connection.getresponse()

    def test_last_revocation_closes_open_sse_and_new_sse_has_no_old_cache_or_auto_account(self):
        identifier, resource = self.create(account=None)
        hub = self.manager._selected_hub(resource)
        connection, response = self.request(resource, "/_heredity/events")
        self.addCleanup(connection.close)
        self.assertEqual(response.status, 200)
        self.assertTrue(response.readline())
        server = resource.server
        self.manager.remove_allocation_owner(identifier, "owner-a")
        self.assertTrue(hub.closed)
        self.assertIsNone(resource.hub)
        self.assertIsNone(self.manager._selected_hub(resource))
        response.read()
        self.assertTrue(response.isclosed())
        self.assertIs(resource.server, server)
        self.assertEqual(self.manager.get(identifier)["state"], "running")
        for path, expected in (("/_heredity/events?after=0", 404), ("/get_status", 200)):
            connection, response = self.request(resource, path)
            try:
                body = json.loads(response.read())
                self.assertEqual(response.status, expected)
                if path == "/get_status":
                    self.assertEqual(body["data"]["state"], "network_interface_unassigned")
                    self.assertFalse(body["data"]["online"])
            finally:
                connection.close()
        self.assertTrue(self.runtime.online)

    def check_paused_events_are_not_replayed(self, kind):
        identifier, resource = self.create(kind)
        self.runtime.publish(self.event(11))
        self.wait_for_marker(11)
        entered, release = threading.Event(), threading.Event()
        original = self.runtime.event_hub.read
        gate_once = True

        def blocked_read(*args, **kwargs):
            nonlocal gate_once
            if gate_once:
                gate_once = False
                entered.set()
                if not release.wait(3):
                    raise AssertionError("Local event read barrier was not released")
            return original(*args, **kwargs)

        with patch.object(self.runtime.event_hub, "read", side_effect=blocked_read):
            try:
                self.assertTrue(entered.wait(1))
                self.manager.remove_allocation_owner(identifier, "owner-a")
                self.runtime.publish(self.event(12))
                self.add_owner(identifier, "owner-b")
                self.runtime.publish(self.event(13))
            finally:
                release.set()
            seen = self.wait_for_marker(13)
        self.assertFalse(any(event.get("time") in (11, 12) for event in seen))
        remaining = []
        while True:
            try:
                remaining.append(self.receiver.received.get(timeout=0.15))
            except queue.Empty:
                break
        self.assertFalse(any(event.get("time") in (11, 12, 13) for event in remaining))
        self.assertEqual(self.manager.get(identifier)["allocation_owner_count"], 1)
        if kind == "http_server":
            hub = self.manager._selected_hub(resource)
            self.assertEqual([record["event"]["time"] for record in hub.read(0)
                              if record["event"]["time"] >= 11], [13])

    def test_http_server_does_not_replay_events_when_regranted_between_source_polls(self):
        self.check_paused_events_are_not_replayed("http_server")

    def test_http_client_does_not_replay_events_when_regranted_between_source_polls(self):
        self.check_paused_events_are_not_replayed("http_client")

    def test_busy_lifecycle_fails_fast_without_altering_existing_routes_or_persistence(self):
        identifier, resource = self.create()
        state = self.manager.allocation_state(identifier)
        registry = self.manager._path.read_bytes()
        entered, release = threading.Event(), threading.Event()

        def busy_lifecycle():
            with resource.lifecycle:
                entered.set()
                release.wait(3)

        worker = threading.Thread(target=busy_lifecycle, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            before = time.monotonic()
            with self.assertRaisesRegex(NetworkBindingClosed, "lifecycle is busy"):
                self.manager.add_allocation_owner(
                    identifier, "blocked-owner", state["revision"], state["generation"])
            self.assertLess(time.monotonic() - before, 0.2)
            self.assertEqual(self.manager._path.read_bytes(), registry)
            self.assertEqual(self.manager.get(identifier)["allocation_owner_count"], 1)
            self.assertTrue(resource.backend.status()["online"])
            self.runtime.publish(self.event(23))
            self.wait_for_marker(23)
        finally:
            release.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())

    def test_busy_uncontrolled_interface_does_not_enter_allocated_mode(self):
        value = self.manager.create({
            "name": "Manual endpoint", "type": "http_server", "enabled": False,
            "account": "example:" + str(ACCOUNT), "settings": {"port": 0},
        })
        identifier = value["interface_id"]
        resource = self.manager._resources[identifier]
        state = self.manager.allocation_state(identifier)
        registry = self.manager._path.read_bytes()
        entered, release = threading.Event(), threading.Event()

        def busy_lifecycle():
            with resource.lifecycle:
                entered.set()
                release.wait(3)

        worker = threading.Thread(target=busy_lifecycle, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaises(NetworkBindingClosed):
                self.manager.add_allocation_owner(
                    identifier, "blocked-owner", state["revision"], state["generation"])
            self.assertEqual(self.manager._path.read_bytes(), registry)
            current = self.manager.get(identifier)
            self.assertFalse(current["allocation_controlled"])
            self.assertEqual(current["allocation_owner_count"], 0)
            self.assertTrue(resource.backend.status()["online"])
        finally:
            release.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
