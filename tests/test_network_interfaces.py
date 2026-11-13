"""Configured host interfaces and module handles, using local SDK accounts."""

from concurrent.futures import ThreadPoolExecutor
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from heredity.application import Application
from heredity.network_interfaces import NetworkInterfaceError, NetworkInterfaceManager
from nyxion_heredity_sdk import BackendError, thaw_json
from nyxion_heredity_sdk.example import OfflineExamplePlugin
from nyxion_heredity_sdk.networks import NetworkBindingClosed


PASSWORD_ENV = "NYXION_INTERFACE_TEST_PASSWORD"
PASSWORD = "local-interface-password-2026"
ACCOUNT_A, ACCOUNT_B = "12345678", "22334455"
ROUTE = "/api/heredity/network-interfaces"


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        threading.Event().wait(0.005)
    raise AssertionError("local interface state did not converge")


def drain(binding):
    messages = []
    while True:
        message = binding.receive()
        if message is None:
            return messages
        messages.append(message)


def observed(binding, kind, predicate=lambda message: True, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = binding.receive(min(0.1, deadline - time.monotonic()))
        if message is not None and message.kind == kind and predicate(message):
            return message
    raise AssertionError(f"no {kind} observation reached the selected interface")


def http_request(url, *, method="GET", payload=None, token=None, extra_headers=None):
    parsed = urlsplit(url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
    headers = dict(extra_headers or {})
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if payload is not None:
        headers["Content-Type"] = "application/json"
    try:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request(method, path, json.dumps(payload) if payload is not None else None, headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


class EmptyAccounts:
    def account_descriptors(self):
        return []

    def account_runtimes(self):
        return []


class LocalReceiver:
    """One localhost destination recording actual module HTTP requests."""

    def __init__(self):
        self.requests = queue.Queue()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                value = json.loads(raw)
                owner.requests.put((self.path, dict(self.headers), value, raw))
                body = json.dumps({
                    "status": "ok", "retcode": 0,
                    "data": value.get("params", {"event_received": True}),
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/onebot"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)
        if self.worker.is_alive():
            raise AssertionError("local receiver did not stop")


class InterfaceRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="NyXion interface registry ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manager = NetworkInterfaceManager(self.root, EmptyAccounts())
        self.addCleanup(self.manager.close)

    def create(self, **changes):
        return self.manager.create({
            "name": "Local interface", "type": "http_server", "enabled": False,
            "account": None, "settings": {"host": "127.0.0.1", "port": 0},
        } | changes)

    def test_creation_is_metadata_only_and_allocates_a_unique_sixteen_hex_id(self):
        with patch("socket.socket", side_effect=AssertionError("metadata creation opened a socket")):
            first, second = self.create(), self.create()
        self.assertRegex(first["interface_id"], r"^[0-9a-f]{16}$")
        self.assertNotEqual(first["interface_id"], second["interface_id"])
        self.assertEqual(first["state"], "stopped")
        self.assertIsNone(first["endpoint"])
        self.assertIsNone(first["actual_port"])
        self.assertEqual(self.manager.catalog()["module_bus"]["id_format"], "hex16")

    def test_deleted_ids_are_tombstoned_and_collision_is_retried(self):
        first = self.create()
        deleted = first["interface_id"]
        self.manager.delete(deleted)
        replacement = "b" * 16 if deleted != "b" * 16 else "c" * 16
        values = iter([deleted, replacement])
        def token_hex(size):
            self.assertEqual(size, 8)
            return next(values, "d" * 16)
        with patch("heredity.network_interfaces.secrets.token_hex", side_effect=token_hex) as random:
            second = self.create()
        self.assertEqual(second["interface_id"], replacement)
        self.assertGreaterEqual(random.call_count, 3)
        persisted = json.loads((self.root / "network-interfaces.json").read_text())
        self.assertIn(deleted, persisted["used_ids"])
        self.assertFalse(self.manager.exists(deleted))

    def test_restart_keeps_ids_and_settings_without_starting_disabled_resources(self):
        first = self.create(settings={
            "host": "127.0.0.1", "port": 0, "path": "/configured",
            "sse_enabled": True,
        })
        token = self.manager.credentials(first["interface_id"])["access_token"]
        self.manager.close()
        with patch("socket.socket", side_effect=AssertionError("restoring metadata opened a socket")):
            restored = NetworkInterfaceManager(self.root, EmptyAccounts())
        self.addCleanup(restored.close)
        value = restored.get(first["interface_id"])
        self.assertEqual(value["interface_id"], first["interface_id"])
        self.assertEqual(value["settings"]["path"], "/configured")
        self.assertTrue(value["settings"]["authentication"]["configured"])
        self.assertTrue(value["settings"]["sse_enabled"])
        self.assertEqual(value["state"], "stopped")
        self.assertEqual(restored.credentials(first["interface_id"])["access_token"], token)
        self.assertNotIn(token, json.dumps(restored.catalog()))

    def test_running_edit_rebuilds_same_id_and_revokes_old_bindings(self):
        first = self.create()
        identifier = first["interface_id"]
        running = self.manager.start(identifier)
        self.assertEqual(running["state"], "running", running)
        self.assertGreater(running["actual_port"], 0)
        self.assertEqual(running["settings"]["port"], 0)
        binding = self.manager.bind_module("module.before-edit", identifier)
        with self.assertRaises(NetworkInterfaceError) as failure:
            self.manager.delete(identifier)
        self.assertEqual(failure.exception.status, 409)
        updated = self.manager.update(identifier, {"name": "Changed", "settings": {"path": "/next"}})
        self.assertEqual(updated["interface_id"], identifier)
        self.assertEqual(updated["name"], "Changed")
        self.assertEqual(updated["settings"]["path"], "/next")
        self.assertEqual(updated["state"], "running")
        self.assertGreater(updated["revision"], running["revision"])
        self.assertTrue(binding.closed)
        with self.assertRaises(NetworkBindingClosed):
            binding.receive()
        token = self.manager.credentials(identifier)["access_token"]
        self.assertEqual(http_request(updated["endpoint"] + "/get_status", token=token)[0], 200)
        stopped = self.manager.stop(identifier)
        self.assertEqual(stopped["state"], "stopped")
        self.assertTrue(self.manager.delete(identifier)["deleted"])

    def test_schema_one_migration_removes_implicit_primary_and_retains_user_interfaces(self):
        primary, extra = self.create(name="Legacy implicit HTTP"), self.create(name="User configured")
        token = self.manager.credentials(extra["interface_id"])["access_token"]
        self.manager.close()
        path = self.root / "network-interfaces.json"
        legacy = json.loads(path.read_text(encoding="utf-8"))
        legacy.update(schema_version=1, primary_id=primary["interface_id"])
        for config in legacy["interfaces"]:
            config.pop("revision", None)
            config["settings"]["event_secret"] = "legacy-separate-signing-secret"
            if config["interface_id"] == primary["interface_id"]:
                config["enabled"] = True
        path.write_text(json.dumps(legacy), encoding="utf-8")
        with patch("socket.socket", side_effect=AssertionError("legacy migration opened a listener")):
            migrated = NetworkInterfaceManager(self.root, EmptyAccounts())
            migrated.start_all()
        self.addCleanup(migrated.close)
        self.assertEqual([item["interface_id"] for item in migrated.catalog()["interfaces"]],
                         [extra["interface_id"]])
        self.assertFalse(migrated.exists(primary["interface_id"]))
        self.assertEqual(migrated.credentials(extra["interface_id"])["access_token"], token)
        persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["schema_version"], 2)
        self.assertNotIn("primary_id", persisted)
        self.assertIn(primary["interface_id"], persisted["used_ids"])
        self.assertNotIn("event_secret", persisted["interfaces"][0]["settings"])

    def test_deleted_interface_revokes_existing_module_handles_without_stopping_other_interface(self):
        first, second = self.create(), self.create()
        handle = self.manager.bind_module("module.first", first["interface_id"])
        other = self.manager.bind_module("module.second", second["interface_id"])
        self.manager.delete(first["interface_id"])
        self.assertTrue(handle.closed)
        with self.assertRaises(NetworkBindingClosed):
            handle.receive()
        self.assertFalse(other.closed)
        self.assertEqual(self.manager.get(second["interface_id"])["state"], "stopped")

    def test_stopped_handle_observes_state_but_cannot_dispatch(self):
        first = self.create()
        handle = self.manager.bind_module("module.observer", first["interface_id"])
        with self.assertRaises(BackendError):
            handle.call("get_status", {})
        self.manager.start(first["interface_id"])
        self.assertEqual(observed(handle, "state").payload["state"], "running")
        self.manager.stop(first["interface_id"])
        self.assertEqual(observed(handle, "state").payload["state"], "stopped")
        with self.assertRaises(BackendError):
            handle.call("get_status", {})

    def test_http_client_module_call_and_event_use_exact_destination_once(self):
        receiver = LocalReceiver()
        self.addCleanup(receiver.close)
        value = self.create(type="http_client", enabled=True, settings={
            "url": receiver.url,
        })
        token = self.manager.credentials(value["interface_id"])["access_token"]
        handle = self.manager.bind_module("module.client", value["interface_id"])
        result = thaw_json(handle.call("echo", {"value": "local-call"}, timeout=2))
        self.assertEqual(result["data"], {"value": "local-call"})
        path, headers, payload, _ = receiver.requests.get(timeout=2)
        self.assertEqual(path, "/onebot")
        self.assertEqual(payload, {"action": "echo", "params": {"value": "local-call"}})
        self.assertEqual(headers["Authorization"], "Bearer " + token)
        handle.publish_event({
            "post_type": "meta_event", "self_id": 123, "time": 1,
            "meta_event_type": "lifecycle", "sub_type": "enable"})
        _, headers, event, encoded = receiver.requests.get(timeout=2)
        lowered = {key.lower(): value for key, value in headers.items()}
        self.assertEqual(event["self_id"], 123)
        self.assertEqual(lowered["x-signature"],
                         "sha1=" + hmac.new(token.encode("utf-8"), encoded, "sha1").hexdigest())
        self.assertEqual(lowered["authorization"], "Bearer " + token)
        self.assertTrue(value["settings"]["signing_uses_access_token"])
        kinds = [item.kind for item in drain(handle)]
        self.assertEqual(kinds, ["action", "response", "event"])
        self.assertTrue(receiver.requests.empty())


class InterfaceApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="NyXion interface application ")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        args = SimpleNamespace(
            state_dir=self.directory, password_env=PASSWORD_ENV, market_refresh_on_start=False,
            plugin_registry=None, account=None, ui_port=0, static_dir=None)
        with patch.dict(os.environ, {PASSWORD_ENV: PASSWORD}):
            self.app = Application(args)
        self.addCleanup(self.app.close)
        self.app.host.register(OfflineExamplePlugin.manifest, factory=OfflineExamplePlugin)
        self.app.host.create_instance("example.offline", "second", "Second local account")
        self.app.start()
        _, login = self.ui_request("POST", "/api/login", {"password": PASSWORD}, authorized=False)
        self.ui_token = login["token"]
        self.manager = self.app.network_interfaces

    def ui_request(self, method, path, payload=None, *, authorized=True, headers=None):
        address, port = self.app.ui.server_address[:2]
        token = getattr(self, "ui_token", None) if authorized else None
        return http_request(f"http://{address}:{port}{path}", method=method, payload=payload,
                            token=token, extra_headers=headers)

    def online_accounts(self):
        self.app.host.start("example.offline", {"self_id": ACCOUNT_A})
        self.app.host.start("example.offline", {"self_id": ACCOUNT_B}, instance_key="second")
        wait_for(lambda: len(self.app.host.account_runtimes()) == 2)

    def create_interface(self, account=None, *, enabled=True, name="Configured local HTTP", path="/"):
        return self.manager.create({
            "name": name, "type": "http_server", "enabled": enabled, "account": account,
            "settings": {"host": "127.0.0.1", "port": 0, "path": path,
                         "sse_enabled": True, "heartbeat_seconds": 3600},
        })

    def token(self, interface):
        return self.manager.credentials(interface["interface_id"])["access_token"]

    def test_fresh_application_has_no_default_network_listener(self):
        self.assertEqual(self.manager.catalog()["interfaces"], [])
        self.assertIsNone(self.app.gateway)
        self.assertIsNone(self.app._gateway_worker)

    def test_two_http_interfaces_dispatch_only_their_selected_account_and_module(self):
        self.online_accounts()
        first = self.create_interface(f"example:{ACCOUNT_A}", path="/onebot-a")
        second = self.create_interface(f"example:{ACCOUNT_B}", path="/onebot-b")
        a = self.manager.bind_module("module.a", first["interface_id"])
        b = self.manager.bind_module("module.b", second["interface_id"])
        status, result = http_request(first["endpoint"], method="POST", payload={
            "action": "echo", "params": {"marker": "first-interface"}, "echo": "request-1"},
            token=self.token(first))
        self.assertEqual(status, 200)
        self.assertEqual(result["data"], {"marker": "first-interface"})
        self.assertEqual(result["echo"], "request-1")
        self.assertEqual(observed(a, "action").payload["action"], "echo")
        self.assertEqual(observed(a, "response").payload["data"]["marker"], "first-interface")
        self.assertFalse(any(message.kind in {"action", "response"} for message in drain(b)))
        status, identity = http_request(second["endpoint"] + "/get_login_info", token=self.token(second))
        self.assertEqual(status, 200)
        self.assertEqual(identity["data"]["user_id"], int(ACCOUNT_B))
        self.assertEqual(observed(b, "response").payload["data"]["user_id"], int(ACCOUNT_B))
        self.assertFalse(any(message.kind == "action" for message in drain(a)))

    def test_module_call_returns_selected_interface_identity_without_global_selection(self):
        self.online_accounts()
        first = self.create_interface(f"example:{ACCOUNT_A}")
        second = self.create_interface(f"example:{ACCOUNT_B}")
        a = self.manager.bind_module("module.a", first["interface_id"])
        b = self.manager.bind_module("module.b", second["interface_id"])
        self.assertEqual(a.call("get_login_info", {})["data"]["user_id"], int(ACCOUNT_A))
        self.assertEqual(b.call("get_login_info", {})["data"]["user_id"], int(ACCOUNT_B))
        self.assertEqual(self.app.gateway_backend.status()["state"], "account_selection_required")

    def test_module_publication_is_observed_only_on_its_interface_and_sse_stream(self):
        self.online_accounts()
        first = self.create_interface(f"example:{ACCOUNT_A}")
        second = self.create_interface(f"example:{ACCOUNT_B}")
        a = self.manager.bind_module("module.a", first["interface_id"])
        b = self.manager.bind_module("module.b", second["interface_id"])
        event = {"time": 11, "self_id": int(ACCOUNT_A), "post_type": "meta_event",
                 "meta_event_type": "lifecycle", "sub_type": "enable"}
        self.assertTrue(a.publish_event(event)["published"])
        self.assertEqual(observed(a, "event", lambda item: item.payload.get("time") == 11).payload["self_id"],
                         int(ACCOUNT_A))
        self.assertFalse(any(item.kind == "event" and item.payload.get("time") == 11
                             for item in drain(b)))
        parsed = urlsplit(first["endpoint"])
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        try:
            connection.request("GET", "/_heredity/events?after=0",
                               headers={"Authorization": "Bearer " + self.token(first)})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("X-Self-ID"), ACCOUNT_A)
            found = False
            for _ in range(20):
                line = response.readline().decode()
                if line.startswith("data: "):
                    message = json.loads(line[6:])
                    self.assertNotEqual(message.get("self_id"), int(ACCOUNT_B))
                    if message.get("time") == 11:
                        found = True
                        break
            self.assertTrue(found)
        finally:
            connection.close()

    def test_platform_events_are_fanned_out_only_to_matching_account_interfaces(self):
        self.online_accounts()
        first = self.create_interface(f"example:{ACCOUNT_A}")
        second = self.create_interface(f"example:{ACCOUNT_B}")
        a = self.manager.bind_module("module.a", first["interface_id"])
        b = self.manager.bind_module("module.b", second["interface_id"])
        identity, runtime = next((identity, runtime)
                                 for identity, runtime in self.app.host.account_runtimes()
                                 if identity.self_id == ACCOUNT_A)
        event = {"time": 12, "self_id": int(ACCOUNT_A), "post_type": "meta_event",
                 "meta_event_type": "lifecycle", "sub_type": "enable"}
        runtime.ingest(event, event_key="interface-account-event",
                       session_epoch=identity.session_epoch)
        self.assertEqual(observed(a, "event", lambda item: item.payload.get("time") == 12).payload["self_id"],
                         int(ACCOUNT_A))
        self.assertFalse(any(item.kind == "event" and item.payload.get("time") == 12
                             for item in drain(b)))

    def test_wrong_account_publication_and_consumer_auth_are_enforced(self):
        self.online_accounts()
        first = self.create_interface(f"example:{ACCOUNT_A}")
        access_token = self.token(first)
        handle = self.manager.bind_module("module.a", first["interface_id"])
        with self.assertRaises(ValueError):
            handle.publish_event({"self_id": int(ACCOUNT_B), "post_type": "meta_event", "time": 1})
        for token, expected in ((None, 401), ("wrong", 403), (access_token, 200)):
            status, _ = http_request(first["endpoint"] + "/get_status", token=token)
            self.assertEqual(status, expected)
        self.assertNotIn(access_token, json.dumps(self.manager.get(first["interface_id"])))

    def test_ui_crud_is_authenticated_and_cross_site_changes_are_rejected(self):
        for method, path, body in (
                ("GET", ROUTE, None),
                ("POST", ROUTE, {"name": "not-created", "type": "http_server"}),
                ("PUT", ROUTE + "/" + "a" * 16, {"name": "not-changed"}),
                ("DELETE", ROUTE + "/" + "a" * 16, None)):
            with self.subTest(method=method):
                status, _ = self.ui_request(method, path, body, authorized=False)
                self.assertEqual(status, 401)
        status, created = self.ui_request("POST", ROUTE, {
            "name": "Managed interface", "type": "http_server", "enabled": False,
            "settings": {"port": 0, "path": "/managed"}})
        self.assertEqual(status, 200, created)
        identifier = created["interface_id"]
        self.assertRegex(identifier, r"^[0-9a-f]{16}$")
        for method, path, body in (
                ("POST", ROUTE, {"name": "not-created", "type": "http_server"}),
                ("POST", ROUTE + f"/{identifier}/start", {}),
                ("PUT", ROUTE + f"/{identifier}", {"name": "not-changed"}),
                ("DELETE", ROUTE + f"/{identifier}", None)):
            with self.subTest(cross_site_method=method):
                status, _ = self.ui_request(
                    method, path, body, headers={"Origin": "https://cross-site.invalid",
                                                "Sec-Fetch-Site": "cross-site"})
                self.assertEqual(status, 403)
        status, updated = self.ui_request("PUT", ROUTE + f"/{identifier}", {"name": "Updated"})
        self.assertEqual(status, 200)
        self.assertEqual(updated["name"], "Updated")
        status, result = self.ui_request("DELETE", ROUTE + f"/{identifier}")
        self.assertEqual(status, 200)
        self.assertTrue(result["deleted"])
        status, _ = self.ui_request("GET", ROUTE + f"/{identifier}")
        self.assertEqual(status, 404)

    def test_ui_edits_running_interfaces_and_rejects_invalid_ids_or_running_deletion(self):
        first = self.create_interface(enabled=True)
        binding = self.manager.bind_module("module.ui-before-edit", first["interface_id"])
        status, updated = self.ui_request("PUT", ROUTE + f"/{first['interface_id']}", {"name": "Updated"})
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["interface_id"], first["interface_id"])
        self.assertEqual(updated["state"], "running")
        self.assertTrue(binding.closed)
        for method, path, body, expected in (
                ("DELETE", ROUTE + f"/{first['interface_id']}", None, 409),
                ("PUT", ROUTE + "/NOT-AN-ID", {"name": "bad"}, 404),
                ("GET", ROUTE + "/" + "f" * 16, None, 404)):
            with self.subTest(method=method, path=path):
                status, _ = self.ui_request(method, path, body)
                self.assertEqual(status, expected)

    def test_invalid_transport_types_return_validation_errors_without_creating_resources(self):
        before = [value["interface_id"] for value in self.manager.catalog()["interfaces"]]
        for kind in ([], {}, 1, True, None, "unknown"):
            with self.subTest(kind=kind):
                status, response = self.ui_request(
                    "POST", ROUTE, {"name": "Invalid transport", "type": kind})
                self.assertEqual(status, 400, response)
                self.assertNotEqual(response.get("code"), "INTERNAL_ERROR")
        self.assertEqual(
            [value["interface_id"] for value in self.manager.catalog()["interfaces"]], before)

    def test_interface_operations_do_not_restart_either_platform_account(self):
        self.online_accounts()
        before = {identity.self_id: (identity, runtime)
                  for identity, runtime in self.app.host.account_runtimes()}
        first = self.create_interface(f"example:{ACCOUNT_A}")
        self.manager.update(first["interface_id"], {"name": "Edited local interface"})
        self.assertEqual(self.manager.get(first["interface_id"])["state"], "running")
        self.manager.stop(first["interface_id"])
        self.manager.delete(first["interface_id"])
        after = {identity.self_id: (identity, runtime)
                 for identity, runtime in self.app.host.account_runtimes()}
        self.assertEqual(set(before), set(after))
        for account in before:
            self.assertEqual(before[account][0], after[account][0])
            self.assertIs(before[account][1], after[account][1])
            self.assertTrue(after[account][1].online)

    def test_inflight_module_call_keeps_the_original_account_when_interface_is_reconfigured(self):
        self.online_accounts()
        first = self.create_interface(f"example:{ACCOUNT_A}")
        identifier = first["interface_id"]
        binding = self.manager.bind_module("module.captured-account", identifier)
        entered, release = threading.Event(), threading.Event()
        original = self.manager._local_action
        def gated(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("local account capture fixture was not released")
            return original(*args, **kwargs)
        with patch.object(self.manager, "_local_action", side_effect=gated):
            with ThreadPoolExecutor(max_workers=1) as pool:
                action = pool.submit(binding.call, "get_login_info", {})
                self.assertTrue(entered.wait(1))
                try:
                    self.manager.update(identifier, {"account": f"example:{ACCOUNT_B}"})
                finally:
                    release.set()
                result = thaw_json(action.result(timeout=3))
        self.assertEqual(result["data"]["user_id"], int(ACCOUNT_A))
        self.assertTrue(binding.closed)
        replacement = self.manager.bind_module("module.captured-account", identifier)
        self.assertEqual(replacement.call("get_login_info", {})["data"]["user_id"], int(ACCOUNT_B))


if __name__ == "__main__":
    unittest.main()
