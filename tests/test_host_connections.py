"""Core interface management over local HTTP, using offline SDK accounts."""

import hashlib
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
from nyxion_heredity_sdk import EventEnvelope, NetworkBindingClosed
from nyxion_heredity_sdk.example import OfflineExamplePlugin

PASSWORD_ENV = "NYXION_HOST_CONNECTION_TEST_PASSWORD"
PASSWORD = "local-host-connections-password"
ACCOUNT = "12345678"
ROUTE = "/api/heredity/network-interfaces"


class RecordingExample(OfflineExamplePlugin):
    def start(self, context, config):
        super().start(context, config)
        self.context, self.session = context, self._session


class EventReceiver:
    def __init__(self):
        owner = self
        self.received = queue.Queue()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                owner.received.put(({key.lower(): value for key, value in self.headers.items()},
                                    body, self.path))
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


class HostConnectionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="NyXion core interfaces ")
        self.addCleanup(self.temporary.cleanup)
        self.created = []
        args = SimpleNamespace(
            state_dir=Path(self.temporary.name), password_env=PASSWORD_ENV,
            market_refresh_on_start=False,
            plugin_registry=None, account=None, host="127.0.0.1", port=0, ui_port=0,
            static_dir=None)
        with patch.dict(os.environ, {PASSWORD_ENV: PASSWORD}):
            self.app = Application(args)
        self.addCleanup(self.app.close)
        def factory():
            plugin = RecordingExample()
            self.created.append(plugin)
            return plugin
        self.app.host.register(RecordingExample.manifest, factory=factory)
        self.app.start()
        _, result = self.request("POST", "/api/login", {"password": PASSWORD},
                                 authenticated=False)
        self.ui_token = result["token"]

    def request(self, method, path, payload=None, *, authenticated=True, headers=None,
                endpoint=None, token=None):
        outgoing = dict(headers or {})
        if endpoint is None:
            address = self.app.ui.server_address
            if authenticated:
                outgoing["Authorization"] = "Bearer " + self.ui_token
        else:
            parsed = urlsplit(endpoint)
            address = (parsed.hostname, parsed.port)
            if token is not None:
                outgoing["Authorization"] = "Bearer " + token
        if payload is not None:
            outgoing["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(*address, timeout=3)
        try:
            connection.request(method, path,
                               json.dumps(payload) if payload is not None else None, outgoing)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def online(self):
        self.app.host.start(RecordingExample.manifest.plugin_id, {"self_id": ACCOUNT})
        deadline = time.monotonic() + 3
        while not self.app.host.account_runtimes() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(self.app.host.account_runtimes())
        return self.created[-1]

    def create(self, *, enabled=True, settings=None):
        status, result = self.request("POST", ROUTE, {
            "name": "Explicit local interface", "type": "http_server", "enabled": enabled,
            "settings": {"host": "127.0.0.1", "port": 0, "sse_enabled": True,
                         "heartbeat_seconds": 3600, **(settings or {})}})
        self.assertEqual(status, 200, result)
        return result

    def credentials(self, item):
        status, result = self.request("GET", ROUTE + "/" + item["interface_id"] + "/credentials")
        self.assertEqual(status, 200, result)
        return result["access_token"]

    def test_fresh_host_has_only_ui_and_no_default_http_resource(self):
        status, result = self.request("GET", ROUTE)
        self.assertEqual(status, 200)
        self.assertEqual(result["interfaces"], [])
        self.assertIsNone(self.app.gateway)
        self.assertIsNone(self.app._gateway_worker)
        self.assertEqual(self.request("GET", "/api/heredity/host-connections")[0], 503)
        with patch("socket.create_connection", side_effect=AssertionError("no internal HTTP")):
            self.assertEqual(self.app.ui.state.accounts(), [])
            self.assertEqual(self.app.ui.state.connections(), [])

    def test_token_is_generated_not_customizable_or_exposed_in_catalog(self):
        first, second = self.create(enabled=False), self.create(enabled=False)
        token = self.credentials(first)
        self.assertGreaterEqual(len(token), 32)
        self.assertNotEqual(token, self.credentials(second))
        self.assertNotIn(token, json.dumps(self.request("GET", ROUTE)[1]))
        for field in ("access_token", "event_secret"):
            for value in ("custom-token", "", None):
                with self.subTest(field=field, value=value):
                    status, _ = self.request("PUT", ROUTE + "/" + first["interface_id"],
                                             {"settings": {field: value}})
                    self.assertEqual(status, 400)
        self.assertEqual(self.credentials(first), token)

    def test_credentials_and_mutations_require_auth_and_reject_cross_site_writes(self):
        item = self.create()
        path = ROUTE + "/" + item["interface_id"]
        for method, suffix, payload in (
                ("GET", "/credentials", None), ("POST", "/regenerate-token", {}),
                ("POST", "/retry", {})):
            self.assertEqual(self.request(method, path + suffix, payload,
                                          authenticated=False)[0], 401)
        for suffix in ("/regenerate-token", "/retry"):
            self.assertEqual(self.request("POST", path + suffix, {},
                                          headers={"Origin": "https://untrusted.invalid"})[0], 403)

    def test_rotation_updates_auth_and_shared_signature_without_restarting_account(self):
        receiver = EventReceiver()
        self.addCleanup(receiver.close)
        plugin = self.online()
        identity, runtime = self.app.host.account_runtimes()[0]
        item = self.create(settings={"event_urls": [receiver.url]})
        old_token = self.credentials(item)
        status, rotated = self.request(
            "POST", ROUTE + "/" + item["interface_id"] + "/regenerate-token", {})
        self.assertEqual(status, 200, rotated)
        token, item = rotated["access_token"], rotated["interface"]
        self.assertNotEqual(token, old_token)
        for supplied, expected in ((None, 401), (old_token, 403), (token, 200)):
            self.assertEqual(self.request("GET", "/get_status", endpoint=item["endpoint"],
                                          token=supplied)[0], expected)
        event = {"time": 1, "self_id": int(ACCOUNT), "post_type": "meta_event",
                 "meta_event_type": "lifecycle", "sub_type": "enable"}
        self.assertTrue(plugin.context.publish_event(
            EventEnvelope(plugin.session, "shared-interface-token", event)).persisted)
        deadline, found = time.monotonic() + 3, None
        while time.monotonic() < deadline:
            try:
                headers, body, path = receiver.received.get(timeout=0.1)
            except queue.Empty:
                continue
            if json.loads(body).get("time") == 1:
                found = (headers, body, path)
                break
        self.assertIsNotNone(found)
        headers, body, path = found
        self.assertEqual(path, "/events")
        self.assertEqual(headers["x-signature"],
                         "sha1=" + hmac.new(token.encode(), body, hashlib.sha1).hexdigest())
        self.assertEqual(self.app.host.account_runtimes()[0][0], identity)
        self.assertIs(self.app.host.account_runtimes()[0][1], runtime)
        self.assertIs(self.created[-1], plugin)
        self.app.network_interfaces.stop(item["interface_id"])

    def test_running_edit_keeps_id_and_token_and_management_needs_no_listener(self):
        self.online()
        item = self.create()
        token = self.credentials(item)
        identity, runtime = self.app.host.account_runtimes()[0]
        status, edited = self.request("PUT", ROUTE + "/" + item["interface_id"],
                                     {"name": "Edited live", "settings": {"path": "/changed"}})
        self.assertEqual(status, 200, edited)
        self.assertEqual(edited["interface_id"], item["interface_id"])
        self.assertEqual(edited["state"], "running")
        self.assertEqual(self.credentials(edited), token)
        status, response = self.request("GET", "/changed/get_login_info",
                                        endpoint=edited["endpoint"], token=token)
        self.assertEqual(status, 200)
        self.assertEqual(response["data"]["user_id"], int(ACCOUNT))
        self.app.network_interfaces.stop(item["interface_id"])
        self.app.network_interfaces.delete(item["interface_id"])
        self.assertTrue(self.app._ui_worker.is_alive())
        self.assertEqual(self.request("GET", "/api/accounts")[1]["list"][0]["account"],
                         f"example:{ACCOUNT}")
        self.assertEqual(self.app.host.account_runtimes()[0][0], identity)
        self.assertIs(self.app.host.account_runtimes()[0][1], runtime)

    def test_retry_refreshes_transport_without_restarting_plugin(self):
        plugin = self.online()
        item = self.create()
        binding = self.app.network_interfaces.bind_module("test.retry", item["interface_id"])
        self.assertEqual(binding.call("get_login_info", {})["data"]["user_id"], int(ACCOUNT))
        status, retried = self.request("POST", ROUTE + "/" + item["interface_id"] + "/retry", {})
        self.assertEqual(status, 200, retried)
        self.assertEqual(retried["interface_id"], item["interface_id"])
        self.assertEqual(retried["state"], "running")
        self.assertTrue(binding.closed)
        with self.assertRaises(NetworkBindingClosed):
            binding.receive()
        self.assertIs(self.created[-1], plugin)
        self.assertEqual(len(self.created), 1)

    def test_stopped_configuration_does_not_probe_event_destinations(self):
        receiver = EventReceiver()
        self.addCleanup(receiver.close)
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("no probe")):
            item = self.app.network_interfaces.create({
                "name": "Stopped interface", "type": "http_server",
                "settings": {"event_urls": [receiver.url]}})
        self.assertEqual(item["state"], "stopped")
        self.assertTrue(receiver.received.empty())
        self.assertEqual(self.created, [])


if __name__ == "__main__":
    unittest.main()
