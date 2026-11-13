"""Host interface IDs route actual localhost WebSocket traffic and module calls."""

import http.client
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

from websockets.sync.client import connect
from websockets.sync.server import serve
from websockets.exceptions import ConnectionClosed

from heredity.application import Application
from nyxion_heredity_sdk import EventEnvelope, thaw_json
from nyxion_heredity_sdk.example import OfflineExamplePlugin


ACCOUNT = "12345678"


class LocalAccount(OfflineExamplePlugin):
    def start(self, context, config):
        super().start(context, config)
        self.context, self.session = context, self._session


def wait_for(check, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.005)
    raise AssertionError("local interface state did not converge")


def receive_kind(binding, kind, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = binding.receive(timeout=min(0.1, max(0, deadline - time.monotonic())))
        if value is not None and value.kind == kind:
            return value
    raise AssertionError("the selected interface observation did not arrive")


def receive_wire(connection, check, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = json.loads(connection.recv(timeout=max(0.01, deadline - time.monotonic())))
        if check(value):
            return value
    raise AssertionError("the selected WebSocket message did not arrive")


def drain(binding):
    values = []
    while True:
        value = binding.receive()
        if value is None:
            return values
        values.append(value)


class NetworkInterfaceWebSocketTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="NyXion network WS integration ")
        self.addCleanup(self.temporary.cleanup)
        options = SimpleNamespace(
            state_dir=Path(self.temporary.name), password_env="NYXION_WS_INTEGRATION_PASSWORD",
            market_refresh_on_start=False,
            plugin_registry=None, account=None, ui_port=0, static_dir=None)
        with patch.dict(os.environ, {
                options.password_env: "local-websocket-integration-password"}):
            self.app = Application(options)
        self.addCleanup(self.app.close)
        self.created = []
        def factory():
            plugin = LocalAccount()
            self.created.append(plugin)
            return plugin
        self.app.host.register(LocalAccount.manifest, factory=factory)
        self.app.start()
        self.app.host.start(LocalAccount.manifest.plugin_id, {"self_id": ACCOUNT})
        wait_for(lambda: self.app.gateway_backend.online)
        self.manager = self.app.network_interfaces

    def websocket(self, name="local-ws", **overrides):
        config = dict(name=name, type="websocket_server", account="example:" + ACCOUNT,
                      settings={"host": "127.0.0.1", "port": 0, "path": "/onebot",
                                "heartbeat_seconds": 86400}, enabled=True)
        snapshot = self.manager.create(config | overrides)
        self.assertEqual(snapshot["state"], "running", snapshot)
        self.assertRegex(snapshot["interface_id"], r"^[0-9a-f]{16}$")
        return snapshot

    def bind(self, identifier, name="local.receiver"):
        binding = self.manager.bind_module(name, identifier)
        self.addCleanup(binding.close)
        return binding

    def connect_interface(self, endpoint):
        token = self.manager.credentials(endpoint["interface_id"])["access_token"]
        return connect(endpoint["endpoint"], proxy=None, legacy=True,
                       additional_headers={"Authorization": "Bearer " + token})

    def test_server_interface_id_observes_actions_and_response_once(self):
        endpoint = self.websocket()
        binding = self.bind(endpoint["interface_id"])
        with self.connect_interface(endpoint) as client:
            client.send(json.dumps({"action": "get_login_info", "params": {},
                                    "echo": "interface-action"}))
            response = receive_wire(client, lambda item: item.get("echo") == "interface-action")
            self.assertEqual(response["data"]["user_id"], int(ACCOUNT))
            observed = drain(binding)
            self.assertEqual([value.kind for value in observed].count("action"), 1)
            self.assertEqual([value.kind for value in observed].count("response"), 1)
            self.assertTrue(all(value.interface_id == endpoint["interface_id"] for value in observed))
            action = next(value for value in observed if value.kind == "action")
            self.assertEqual(action.payload["action"], "get_login_info")
            current = self.manager.get(endpoint["interface_id"])
            self.assertEqual(current["modules"], [{"module_id": "local.receiver", "subscriptions": 1}])

    def test_publication_and_module_call_stay_on_the_selected_interface(self):
        first, second = self.websocket("first"), self.websocket("second")
        one, two = self.bind(first["interface_id"]), self.bind(second["interface_id"], "other.receiver")
        with self.connect_interface(first) as peer:
            wait_for(lambda: self.manager._resources[first["interface_id"]].transport.snapshot()[
                "connected_peers"] == 1)
            drain(one)
            drain(two)
            response = thaw_json(one.call("get_login_info", {}, timeout=2))
            self.assertEqual(response["data"]["user_id"], int(ACCOUNT))
            self.assertEqual(receive_kind(one, "action").payload["action"], "get_login_info")
            self.assertEqual(receive_kind(one, "response").payload["status"], "ok")
            event = {"post_type": "meta_event", "meta_event_type": "synthetic",
                     "self_id": int(ACCOUNT), "time": 77}
            one.publish_event(event)
            self.assertEqual(receive_wire(peer, lambda item: item.get("time") == 77), event)
            self.assertEqual(thaw_json(receive_kind(one, "event").payload), event)
            self.assertEqual(drain(two), [])

    def test_interface_account_binding_drops_unrelated_incoming_events(self):
        endpoint = self.websocket()
        binding = self.bind(endpoint["interface_id"])
        with self.connect_interface(endpoint) as client:
            drain(binding)
            client.send(json.dumps({"post_type": "message", "self_id": int(ACCOUNT) + 1,
                                    "message": "not-the-bound-account", "time": 1}))
            state = receive_kind(binding, "state")
            self.assertNotEqual(state.payload.get("state"), "running")
            self.assertFalse(any(value.kind == "event" for value in drain(binding)))
            client.send(json.dumps({"action": "get_login_info", "echo": "still-bound"}))
            reply = receive_wire(client, lambda item: item.get("echo") == "still-bound")
            self.assertEqual(reply["data"]["user_id"], int(ACCOUNT))

    def test_platform_event_reaches_each_own_id_and_explicit_http_remains_independent(self):
        first, second = self.websocket("first"), self.websocket("second")
        http_interface = self.manager.create({
            "name": "Independent HTTP", "type": "http_server", "enabled": True,
            "account": "example:" + ACCOUNT,
            "settings": {"host": "127.0.0.1", "port": 0, "heartbeat_seconds": 86400}})
        first_module, second_module = self.bind(first["interface_id"]), self.bind(
            second["interface_id"], "other.receiver")
        first_wire = self.connect_interface(first)
        second_wire = self.connect_interface(second)
        self.addCleanup(first_wire.close)
        self.addCleanup(second_wire.close)
        event = {"post_type": "meta_event", "meta_event_type": "lifecycle",
                 "sub_type": "enable", "time": 99, "self_id": int(ACCOUNT)}
        acknowledgment = self.created[0].context.publish_event(
            EventEnvelope(self.created[0].session, "ws-interface-event", event))
        self.assertTrue(acknowledgment.persisted)
        self.assertEqual(receive_wire(first_wire, lambda item: item.get("time") == 99), event | {
            "_heredity": {"provider_id": LocalAccount.manifest.plugin_id,
                          "session_epoch": self.created[0].session.session_epoch}})
        self.assertEqual(receive_wire(second_wire, lambda item: item.get("time") == 99)["self_id"],
                         int(ACCOUNT))
        self.assertEqual(receive_kind(first_module, "event").interface_id, first["interface_id"])
        self.assertEqual(receive_kind(second_module, "event").interface_id, second["interface_id"])
        resource = self.manager._resources[http_interface["interface_id"]]
        connection = http.client.HTTPConnection(*resource.server.server_address, timeout=2)
        try:
            connection.request("GET", "/get_login_info", headers={
                "Authorization": "Bearer " + self.manager.credentials(
                    http_interface["interface_id"])["access_token"]})
            result = json.loads(connection.getresponse().read())
        finally:
            connection.close()
        self.assertEqual(result["data"]["user_id"], int(ACCOUNT))
        self.assertNotEqual(http_interface["interface_id"], first["interface_id"])
        self.assertIsNone(self.app.gateway)

    def test_reverse_websocket_interface_routes_remote_actions_and_module_calls(self):
        incoming_action_result, role_headers = queue.Queue(), queue.Queue()
        release = threading.Event()
        def remote(connection):
            role_headers.put({key.lower(): value for key, value in connection.request.headers.items()})
            connection.send(json.dumps({"action": "get_login_info", "echo": "remote-read"}))
            incoming_action_result.put(receive_wire(
                connection, lambda item: item.get("echo") == "remote-read"))
            while not release.is_set():
                try:
                    item = json.loads(connection.recv(timeout=0.1))
                except TimeoutError:
                    continue
                except ConnectionClosed:
                    return
                if "action" in item:
                    connection.send(json.dumps({
                        "status": "ok", "retcode": 0, "data": {"remote": True},
                        "echo": item.get("echo")}))
        server = serve(remote, "127.0.0.1", 0, close_timeout=1)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def close_remote():
            release.set()
            server.shutdown()
            worker.join(timeout=2)
        self.addCleanup(close_remote)
        endpoint = self.manager.create({
            "name": "reverse-local", "type": "websocket_client", "enabled": True,
            "account": "example:" + ACCOUNT,
            "settings": {"url": f"ws://127.0.0.1:{server.socket.getsockname()[1]}/onebot",
                         "heartbeat_seconds": 86400}})
        self.assertEqual(endpoint["state"], "running", endpoint)
        binding = self.bind(endpoint["interface_id"])
        remote_result = incoming_action_result.get(timeout=2)
        self.assertEqual(remote_result["data"]["user_id"], int(ACCOUNT))
        headers = role_headers.get(timeout=2)
        self.assertEqual(headers["x-client-role"], "Universal")
        self.assertEqual(headers["x-self-id"], ACCOUNT)
        self.assertEqual(headers["authorization"],
                         "Bearer " + self.manager.credentials(endpoint["interface_id"])["access_token"])
        response = thaw_json(binding.call("get_status", {}, timeout=2))
        self.assertEqual(response["data"], {"remote": True})
        self.assertEqual(receive_kind(binding, "action").payload["action"], "get_status")
        self.assertEqual(receive_kind(binding, "response").payload["status"], "ok")
        release.set()
        self.manager.stop(endpoint["interface_id"])
        server.shutdown()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())

    def test_start_failure_and_stop_are_interface_local(self):
        good = self.websocket()
        bad = self.manager.create({
            "name": "bad-remote", "type": "websocket_client", "enabled": True,
            "settings": {"url": "ws://127.0.0.1:1/unavailable"}})
        self.assertEqual(bad["state"], "error")
        self.assertFalse(bad["enabled"])
        self.assertEqual(self.manager.get(good["interface_id"])["state"], "running")
        binding = self.bind(good["interface_id"])
        self.manager.stop(good["interface_id"])
        self.assertEqual(receive_kind(binding, "state").payload["state"], "stopped")
        self.assertEqual(self.manager.get(good["interface_id"])["state"], "stopped")
        self.assertTrue(self.app.gateway_backend.online)
        self.assertTrue(self.app._ui_worker.is_alive())


if __name__ == "__main__":
    unittest.main()
