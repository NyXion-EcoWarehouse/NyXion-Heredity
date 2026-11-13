"""Management integration against an explicit, local OneBot HTTP fixture."""

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from heredity.backends.mock import MockBackend
from heredity.onebot import create_server
from heredity.runtime import AccountRuntime
from heredity.storage import MessageStore
from heredity.webui import create_webui_server


PASSWORD = "heredity-integration-password"
UPSTREAM_TOKEN = "fixture-upstream-secret"
ACCOUNT = 87654321


class UpstreamHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.server.calls.append(self.path)
        size = int(self.headers.get("Content-Length", "0"))
        params = json.loads(self.rfile.read(size) or b"{}")
        if self.headers.get("Authorization") != "Bearer " + UPSTREAM_TOKEN:
            self.send_response(401)
            self.end_headers()
            return
        status = "ok"
        if self.path == "/get_login_info":
            value = {"user_id": ACCOUNT, "nickname": "Fixture Account"}
        elif self.path == "/get_status":
            value = {"online": self.server.online, "good": True}
        elif self.path == "/get_friend_list":
            value = [{"user_id": 87654322, "nickname": "Fixture Friend", "remark": ""}]
        elif self.path == "/send_private_msg":
            self.server.sent.append(params)
            value = {"message_id": -701}
        elif self.path == "/can_send_image":
            value = {"yes": True}
        else:
            value, status = None, "failed"
        raw = json.dumps({"status": status, "retcode": 0 if status == "ok" else 1404,
                          "data": value}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class WebUIIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = AccountRuntime(MockBackend(), MessageStore(":memory:"), mode="demo")
        self.addCleanup(self.runtime.close)
        self.runtime.initialize()
        self.default = create_server("127.0.0.1", 0, "", self.runtime, enable_sse=True)
        self.start(self.default)
        self.default_origin = f"http://127.0.0.1:{self.default.server_port}"
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        self.upstream.calls, self.upstream.sent, self.upstream.online = [], [], True
        self.start(self.upstream)
        self.upstream_origin = f"http://127.0.0.1:{self.upstream.server_port}"
        self.ui = create_webui_server("127.0.0.1", 0, PASSWORD,
                                      gateway=self.default_origin,
                                      state_dir=self.root / "management")
        self.start(self.ui)
        status, result = self.request("POST", "/api/login", {"password": PASSWORD}, auth=False)
        self.assertEqual(status, 200)
        self.token = result["token"]

    def start(self, server):
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(self.stop, server, worker)

    @staticmethod
    def stop(server, worker):
        server.shutdown()
        server.server_close()
        worker.join(timeout=4)

    def request(self, method, path, value=None, *, auth=True, headers=None):
        connection = http.client.HTTPConnection(*self.ui.server_address, timeout=15)
        outgoing = dict(headers or {})
        if auth:
            outgoing.setdefault("X-Heredity-Token", self.token)
        raw = None
        if value is not None:
            outgoing["Content-Type"] = "application/json"
            raw = json.dumps(value)
        try:
            connection.request(method, path, body=raw, headers=outgoing)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def probe(self):
        status, result = self.request("POST", "/api/heredity/connection/probe", {
            "origin": self.upstream_origin, "access_token": UPSTREAM_TOKEN,
            "execution_level": "L2"})
        self.assertEqual(status, 200, result)
        return result

    def connect(self):
        probe = self.probe()
        status, result = self.request("POST", "/api/heredity/connection/connect",
                                     {"probe_id": probe["probe_id"]})
        self.assertEqual(status, 200, result)
        return result

    def test_new_endpoints_require_session_and_header_precedence(self):
        paths = ["/api/accounts", "/api/heredity/connection",
                 "/api/heredity/connection/gateway",
                 "/api/heredity/connection/events/config"]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path, auth=False)[0], 401)
        status, _ = self.request("GET", "/api/status", headers={
            "X-Heredity-Token": "invalid",
            "Authorization": "Bearer " + self.token})
        self.assertEqual(status, 401)
        self.assertEqual(self.upstream.calls, [])

    def test_probe_is_read_only_and_connect_routes_verified_account_through_gateway(self):
        probe = self.probe()
        self.assertEqual(probe["self_id"], ACCOUNT)
        self.assertNotIn("access_token", probe)
        self.assertTrue(set(self.upstream.calls) <= {"/get_login_info", "/get_status"})
        status, accounts = self.request("GET", "/api/accounts")
        self.assertEqual(status, 200)
        self.assertEqual(accounts["list"][0]["account"], "onebot:10000")
        status, result = self.request("POST", "/api/heredity/connection/connect",
                                     {"probe_id": probe["probe_id"]})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["configured"])
        active = result["active"]
        self.assertEqual(active["kind"], "external")
        self.assertEqual(active["origin"], self.upstream_origin)
        self.assertEqual(active["self_id"], ACCOUNT)
        self.assertTrue(active["verified"])
        self.assertNotEqual(active["gateway_origin"], self.upstream_origin)
        status, accounts = self.request("GET", "/api/accounts")
        self.assertEqual(status, 200)
        self.assertEqual(accounts["list"], [{
            "account": f"onebot:{ACCOUNT}", "platform": "onebot",
            "self_id": str(ACCOUNT), "nickname": "Fixture Account"}])
        status, execution = self.request("GET", "/api/heredity/execution")
        self.assertEqual(status, 200)
        descriptor = execution["state"]["descriptor"]
        self.assertEqual(descriptor["source_kind"], "profile")
        self.assertEqual(descriptor["execution_level"], "L2")
        self.assertEqual(descriptor["level_provenance"], "declared")
        status, friends = self.request("POST", "/api/debug/invoke", {
            "account": f"onebot:{ACCOUNT}", "action": "get_friend_list", "params": {}})
        self.assertEqual(status, 200)
        self.assertEqual(friends["data"][0]["nickname"], "Fixture Friend")
        self.assertEqual(self.upstream.sent, [])

    def test_old_account_is_rejected_and_only_explicit_fixture_send_has_effect(self):
        self.connect()
        for account in ["onebot:10000", "onebot:999999"]:
            status, result = self.request("POST", "/api/debug/invoke", {
                "account": account, "action": "send_private_msg",
                "params": {"user_id": 87654322, "message": "must not send"}})
            self.assertEqual(status, 403)
            self.assertEqual(result["code"], "ACCOUNT_MISMATCH")
        self.assertEqual(self.upstream.sent, [])
        status, result = self.request("POST", "/api/debug/invoke", {
            "account": f"onebot:{ACCOUNT}", "action": "send_private_msg",
            "params": {"user_id": 87654322, "message": "local fixture only"}})
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["message_id"], -701)
        self.assertEqual(len(self.upstream.sent), 1)
        status, stored = self.request("POST", "/api/debug/invoke", {
            "account": f"onebot:{ACCOUNT}", "action": "get_msg", "params": {"message_id": -701}})
        self.assertEqual(status, 200)
        self.assertEqual(stored["data"]["raw_message"], "local fixture only")
        self.assertEqual(len(self.upstream.sent), 1)

    def test_gateway_config_is_explicit_and_disconnect_restores_default(self):
        self.connect()
        status, config = self.request("GET", "/api/heredity/connection/gateway")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(config["access_token"]), 24)
        self.assertNotEqual(config["access_token"], UPSTREAM_TOKEN)
        parsed = urlsplit(config["origin"])
        client = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        try:
            client.request("GET", "/get_login_info",
                           headers={"Authorization": "Bearer " + config["access_token"]})
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["data"]["user_id"], ACCOUNT)
        finally:
            client.close()
        status, logs = self.request("GET", "/api/logs")
        self.assertEqual(status, 200)
        self.assertNotIn(UPSTREAM_TOKEN, json.dumps(logs))
        status, result = self.request("POST", "/api/heredity/connection/disconnect", {})
        self.assertEqual(status, 200)
        self.assertFalse(result["configured"])
        self.assertEqual(result["active"]["origin"], self.default_origin)
        status, accounts = self.request("GET", "/api/accounts")
        self.assertEqual(accounts["list"][0]["account"], "onebot:10000")

    def test_failed_probe_or_forged_confirmation_does_not_replace_gateway(self):
        for payload in [
            {"origin": self.default_origin},
            {"origin": f"http://127.0.0.1:{self.ui.server_port}"},
            {"origin": "http://example.invalid:5701"},
            {"origin": self.upstream_origin, "access_token": "incorrect"},
        ]:
            status, result = self.request("POST", "/api/heredity/connection/probe", payload)
            self.assertGreaterEqual(status, 400, result)
        self.upstream.online = False
        status, result = self.request("POST", "/api/heredity/connection/probe", {
            "origin": self.upstream_origin, "access_token": UPSTREAM_TOKEN})
        self.assertGreaterEqual(status, 400, result)
        status, _ = self.request("POST", "/api/heredity/connection/connect",
                                 {"probe_id": "invented"})
        self.assertGreaterEqual(status, 400)
        self.assertEqual(self.ui.state.gateway.origin, self.default_origin)
        self.assertEqual(self.upstream.sent, [])

    def test_signed_external_events_persist_once_and_can_be_read_through_management(self):
        connected = self.connect()
        self.assertIsNone(connected["active"]["event_ingress"]["healthy"])
        status, config = self.request("GET", "/api/heredity/connection/events/config")
        self.assertEqual(status, 200)
        parsed = urlsplit(config["url"])
        event = {"time": int(time.time()), "self_id": ACCOUNT, "post_type": "message",
                 "message_type": "private", "sub_type": "friend", "message_id": -901,
                 "user_id": 87654322, "message": "signed fixture event",
                 "raw_message": "signed fixture event", "font": 0,
                 "sender": {"user_id": 87654322, "nickname": "Fixture Friend"}}
        raw = json.dumps(event).encode()

        def deliver(signed):
            client = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
            headers = {"Content-Type": "application/json"}
            if signed:
                headers["X-Signature"] = "sha1=" + hmac.new(
                    config["secret"].encode(), raw, hashlib.sha1).hexdigest()
            try:
                client.request("POST", parsed.path, body=raw, headers=headers)
                response = client.getresponse()
                return response.status, json.loads(response.read())
            finally:
                client.close()

        self.assertEqual(deliver(False)[0], 401)
        self.assertEqual(deliver(True), (200, {}))
        self.assertEqual(deliver(True), (200, {}))
        status, snapshot = self.request("GET", "/api/heredity/connection")
        self.assertEqual(status, 200)
        received = snapshot["active"]["event_ingress"]
        self.assertTrue(received["healthy"])
        self.assertEqual(received["accepted"], 1)
        self.assertEqual(received["duplicates"], 1)
        self.assertNotIn(config["secret"], json.dumps(snapshot))
        status, stored = self.request("POST", "/api/debug/invoke", {
            "account": f"onebot:{ACCOUNT}", "action": "get_msg", "params": {"message_id": -901}})
        self.assertEqual(status, 200)
        self.assertEqual(stored["data"]["raw_message"], "signed fixture event")
        self.assertEqual(stored["data"]["_heredity"]["provider_id"],
                         connected["active"]["provider_id"])
        self.assertEqual(self.upstream.sent, [])

    def test_removed_process_routes_do_not_create_an_account_or_connect_upstream(self):
        for method, path, body in [
            ("GET", "/api/heredity/program", None),
            ("POST", "/api/heredity/program/bind",
             {"pid": 1234, "start_token": "created-1"}),
            ("POST", "/api/heredity/program/inspect",
             {"pid": 1234, "start_token": "created-1"}),
            ("POST", "/api/heredity/program/unbind", {}),
        ]:
            with self.subTest(path=path):
                status, result = self.request(method, path, body)
                self.assertEqual(status, 501)
                self.assertEqual(result["code"], "UNSUPPORTED")
        self.assertFalse(hasattr(self.ui.state, "program"))
        self.assertEqual(self.upstream.calls, [])
        status, accounts = self.request("GET", "/api/accounts")
        self.assertEqual(status, 200)
        self.assertEqual(accounts["list"][0]["account"], "onebot:10000")


if __name__ == "__main__":
    unittest.main()
