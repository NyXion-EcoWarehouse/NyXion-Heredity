import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from heredity.backends.mock import MockBackend
from heredity.onebot import create_server
from heredity.runtime import AccountRuntime
from heredity.storage import MessageStore
from heredity.webui import MAX_BODY, create_webui_server


PASSWORD = "fixture-password-2026"


class WebUIHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state_dir = self.root / "ui-state"
        self.runtime = AccountRuntime(
            MockBackend(), MessageStore(":memory:"), mode="demo"
        )
        self.addCleanup(self.runtime.close)
        self.assertTrue(self.runtime.initialize())
        self.gateway = create_server(
            "127.0.0.1", 0, "gateway-consumer", self.runtime, enable_sse=True
        )
        self.gateway_worker = self.start_server(self.gateway)
        self.gateway_origin = f"http://127.0.0.1:{self.gateway.server_port}"
        self.ui = self.make_ui(state_dir=self.state_dir)
        self.token = self.login(self.ui)

    def start_server(self, server):
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(self.close_server, server, worker)
        return worker

    def close_server(self, server, worker):
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())

    def make_ui(self, *, state_dir=None, static_dir=None):
        server = create_webui_server(
            "127.0.0.1",
            0,
            PASSWORD,
            gateway=self.gateway_origin,
            gateway_token="gateway-consumer",
            state_dir=state_dir or self.root / "additional-ui",
            static_dir=static_dir,
        )
        self.start_server(server)
        return server

    def raw_request(self, method, path, body=None, *, headers=None, token=None, server=None):
        server = server or self.ui
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        request_headers = dict(headers or {})
        if token is not None:
            request_headers.setdefault("X-Heredity-Token", token)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, response.read(), response.getheader("Content-Type")
        finally:
            connection.close()

    def request(self, method, path, value=None, *, token=None, headers=None, server=None):
        request_headers = dict(headers or {})
        body = None
        if value is not None:
            body = json.dumps(value, ensure_ascii=True)
            request_headers.setdefault("Content-Type", "application/json")
        status, raw, _ = self.raw_request(
            method, path, body, headers=request_headers, token=token, server=server
        )
        return status, json.loads(raw)

    def login(self, server=None):
        status, payload = self.request(
            "POST", "/api/login", {"password": PASSWORD}, server=server
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["success"])
        self.assertIsInstance(payload["token"], str)
        self.assertGreaterEqual(len(payload["token"]), 32)
        return payload["token"]

    def invoke(self, action, params=None, *, account="onebot:10000"):
        return self.request(
            "POST",
            "/api/debug/invoke",
            {"account": account, "action": action, "params": params or {}},
            token=self.token,
        )

    def open_stream(self, path):
        connection = http.client.HTTPConnection(*self.ui.server_address, timeout=3)
        connection.request("GET", path, headers={"X-Heredity-Token": self.token})
        response = connection.getresponse()
        self.addCleanup(connection.close)
        self.addCleanup(response.close)
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("Content-Type").startswith("text/event-stream"))
        return connection, response

    def next_stream_frame(self, response):
        payload = None
        for _ in range(30):
            line = response.readline()
            if not line:
                self.fail("Management stream closed before the expected frame arrived.")
            if line.startswith(b"data: "):
                payload = json.loads(line[6:])
            if line in {b"\n", b"\r\n"} and payload is not None:
                return payload
        self.fail("No management frame arrived within the bounded frame read.")

    def test_login_tokens_authentication_and_logout_revocation(self):
        status, payload = self.request(
            "POST", "/api/login", {"password": "incorrect-password"}
        )
        self.assertEqual(status, 401)
        self.assertFalse(payload["success"])
        second = self.login()
        self.assertNotEqual(self.token, second)
        for token in [None, "wrong-token"]:
            with self.subTest(token=token):
                status, payload = self.request("GET", "/api/status", token=token)
                self.assertEqual(status, 401)
                self.assertFalse(payload["success"])
        status, payload = self.request("GET", "/api/status", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["app"], "Heredity")
        status, _ = self.request(
            "GET", "/api/status", headers={"Authorization": "Bearer " + self.token}
        )
        self.assertEqual(status, 200)
        status, _ = self.request(
            "GET", "/api/status",
            headers={"Authorization": "Bearer " + self.token,
                     "X-Heredity-Token": "wrong-token"},
        )
        self.assertEqual(status, 401)
        status, payload = self.request("POST", "/api/logout", {}, token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(payload["success"])
        status, _ = self.request("GET", "/api/status", token=self.token)
        self.assertEqual(status, 401)
        status, _ = self.request("GET", "/api/status", token=second)
        self.assertEqual(status, 200)

    def test_public_appearance_strips_private_css_and_preferences_survive_reload(self):
        status, public = self.request("GET", "/api/ui/public")
        self.assertEqual(status, 200)
        self.assertEqual(public["appearance"]["customCss"], "")
        private_css = "body { --fixture-private-color: #123456; }"
        status, saved = self.request(
            "POST",
            "/api/ui",
            {"appearance": {"mode": "dark", "uiScale": 1.1, "customCss": private_css}},
            token=self.token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved["config"]["appearance"]["customCss"], private_css)
        status, public = self.request("GET", "/api/ui/public")
        self.assertEqual(status, 200)
        self.assertEqual(public["appearance"]["mode"], "dark")
        self.assertEqual(public["appearance"]["customCss"], "")
        self.assertNotIn("fixture-private-color", json.dumps(public))
        status, _ = self.request("GET", "/api/ui")
        self.assertEqual(status, 401)
        reloaded = self.make_ui(state_dir=self.state_dir)
        token = self.login(reloaded)
        status, restored = self.request(
            "GET", "/api/ui", token=token, server=reloaded
        )
        self.assertEqual(status, 200)
        appearance = restored["config"]["appearance"]
        self.assertEqual(appearance["mode"], "dark")
        self.assertEqual(appearance["uiScale"], 1.1)
        self.assertEqual(appearance["customCss"], private_css)

    def test_resource_shapes_describe_actual_demo_gateway_and_own_system(self):
        status, accounts = self.request("GET", "/api/accounts", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(accounts["list"], [
            {"account": "onebot:10000", "platform": "onebot", "self_id": "10000",
             "nickname": "Heredity Demo"}
        ])
        status, connections = self.request(
            "GET", "/api/connections", token=self.token
        )
        self.assertEqual(status, 200)
        account = connections["list"][0]
        self.assertEqual(account["account"], "onebot:10000")
        adapter = account["adapters"][0]
        self.assertEqual(adapter["kind"], "httpServer")
        self.assertEqual(adapter["status"], "ok")
        self.assertIn(self.gateway_origin, adapter["detail"])
        self.assertIn("DEMO", adapter["detail"])
        status, system = self.request("GET", "/api/system", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(system["runtime"]["pid"], os.getpid())
        self.assertTrue(system["nodeVersion"].startswith("Python "))
        self.assertIsInstance(system["cpu"], dict)
        self.assertIsInstance(system["memory"], dict)
        self.assertIsInstance(system["metricsUnavailable"], list)
        status, catalog = self.request(
            "GET", "/api/debug/actions", token=self.token
        )
        self.assertEqual(status, 200)
        actions = {item["name"]: item for item in catalog["actions"]}
        self.assertIn("get_login_info", actions)
        self.assertIn("get_local_history", actions)
        self.assertFalse(actions["send_private_msg"]["readOnly"])
        self.assertTrue(actions["get_msg"]["readOnly"])
        self.assertIsInstance(actions["send_private_msg"]["params"], list)
        self.assertIsInstance(catalog["categories"], list)

    def test_debug_rejects_another_account_without_sending(self):
        original = self.runtime.backend.call
        with patch.object(self.runtime.backend, "call", wraps=original) as provider:
            status, body = self.invoke(
                "send_private_msg", {"user_id": 10001, "message": "must not send"},
                account="onebot:99999",
            )
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "ACCOUNT_MISMATCH")
        self.assertFalse(body["success"])
        self.assertNotIn(
            "send_private_msg", [call.args[0] for call in provider.call_args_list]
        )
        self.assertEqual(self.runtime.store.history(10000, "private", 10001), [])

    def test_debug_login_send_and_get_message_use_real_gateway(self):
        status, login = self.invoke("get_login_info")
        self.assertEqual(status, 200)
        self.assertEqual(login["status"], "ok")
        self.assertEqual(login["data"]["user_id"], 10000)
        status, sent = self.invoke(
            "send_private_msg",
            {"user_id": 10001, "message": "fixture[CQ:at,target=10002]"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(sent["status"], "ok")
        identifier = sent["data"]["message_id"]
        self.assertIsInstance(identifier, int)
        status, retrieved = self.invoke("get_msg", {"message_id": identifier})
        self.assertEqual(status, 200)
        self.assertEqual(retrieved["status"], "ok")
        self.assertEqual(retrieved["data"]["message_id"], identifier)
        self.assertEqual(retrieved["data"]["raw_message"], "fixture[CQ:at,target=10002]")
        self.assertEqual(
            self.runtime.store.get_message(10000, identifier), retrieved["data"]
        )

    def test_unsupported_mutations_return_explicit_501(self):
        paths = [
            "/api/processes/123/hook",
            "/api/config/10000",
            "/api/extensions/unprovided",
            "/api/agreements/record-consent",
        ]
        for path in paths:
            with self.subTest(path=path):
                status, payload = self.request(
                    "POST", path, {}, token=self.token
                )
                self.assertEqual(status, 501)
                self.assertFalse(payload["success"])
                self.assertEqual(payload["code"], "UNSUPPORTED")
                self.assertTrue(payload["error"])
        status, payload = self.request(
            "DELETE", "/api/config/10000", token=self.token
        )
        self.assertEqual(status, 501)
        self.assertFalse(payload["success"])

    def test_state_stream_bootstraps_generic_account_and_connection_resources(self):
        _, response = self.open_stream("/api/state/stream")
        self.assertEqual(self.next_stream_frame(response), {"kind": "ready"})
        resources = {}
        for _ in range(2):
            frame = self.next_stream_frame(response)
            resources[frame["resource"]] = frame["data"]
        self.assertEqual(set(resources), {"accounts", "connections"})
        self.assertEqual(resources["accounts"][0]["account"], "onebot:10000")
        self.assertEqual(resources["connections"][0]["account"], "onebot:10000")
        response.close()

    def test_debug_stream_receives_action_from_management_invocation(self):
        _, response = self.open_stream("/api/debug/stream")
        self.assertEqual(self.next_stream_frame(response), {"kind": "ready"})
        status, invoked = self.invoke("get_login_info")
        self.assertEqual(status, 200)
        frame = self.next_stream_frame(response)
        self.assertEqual(frame["kind"], "action")
        self.assertEqual(frame["account"], "onebot:10000")
        self.assertEqual(frame["action"], "get_login_info")
        self.assertEqual(frame["response"], invoked)
        self.assertIsInstance(frame["ms"], int)
        response.close()

    def test_malformed_or_nonfinite_json_is_rejected(self):
        for raw in [b"{broken", b"[]", b'{"appearance":NaN}', b'{"appearance":Infinity}']:
            with self.subTest(raw=raw):
                status, body, _ = self.raw_request(
                    "POST", "/api/ui", raw, token=self.token,
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 400)
                self.assertFalse(json.loads(body)["success"])
        status, body, _ = self.raw_request(
            "POST", "/api/ui", b"mode=dark", token=self.token,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 415)
        self.assertFalse(json.loads(body)["success"])

    def test_ambiguous_framing_and_oversized_body_are_rejected(self):
        connection = http.client.HTTPConnection(*self.ui.server_address, timeout=3)
        try:
            connection.putrequest("POST", "/api/ui")
            connection.putheader("X-Heredity-Token", self.token)
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", "0")
            connection.putheader("Content-Length", "0")
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            self.assertFalse(json.loads(response.read())["success"])
        finally:
            connection.close()
        for headers in [
            {"Content-Length": str(MAX_BODY + 1)},
            {"Content-Length": "-1"},
            {"Transfer-Encoding": "chunked"},
        ]:
            with self.subTest(headers=headers):
                status, body, _ = self.raw_request(
                    "POST", "/api/ui", b"", token=self.token,
                    headers={"Content-Type": "application/json", **headers},
                )
                self.assertEqual(status, 400)
                self.assertFalse(json.loads(body)["success"])

    def test_static_files_and_encoded_traversal_stay_inside_static_root(self):
        static = self.root / "frontend"
        static.mkdir()
        (static / "index.html").write_text("<html>fixture ui</html>", encoding="utf-8")
        (static / "app.js").write_text("console.log('fixture');", encoding="utf-8")
        (self.root / "outside.txt").write_text("outside-secret", encoding="utf-8")
        server = self.make_ui(state_dir=self.root / "static-ui", static_dir=static)
        status, body, content_type = self.raw_request("GET", "/app.js", server=server)
        self.assertEqual(status, 200)
        self.assertIn(b"fixture", body)
        self.assertIn("javascript", content_type)
        status, body, _ = self.raw_request("GET", "/nested/client-route", server=server)
        self.assertEqual(status, 200)
        self.assertIn(b"fixture ui", body)
        for path in ["/../outside.txt", "/%2e%2e/outside.txt", "/..%5coutside.txt"]:
            with self.subTest(path=path):
                status, body, _ = self.raw_request("GET", path, server=server)
                self.assertIn(status, {403, 404})
                self.assertNotIn(b"outside-secret", body)

    def test_listener_and_gateway_must_remain_loopback(self):
        for host in ["0.0.0.0", "::", "192.0.2.10"]:
            with self.subTest(host=host), self.assertRaises(ValueError):
                create_webui_server(
                    host, 0, PASSWORD, gateway=self.gateway_origin,
                    state_dir=self.root / "invalid-bind",
                )
        with self.assertRaises(ValueError):
            create_webui_server(
                "127.0.0.1", 0, PASSWORD, gateway="http://192.0.2.10:5700",
                state_dir=self.root / "invalid-gateway",
            )
        with self.assertRaises(ValueError):
            create_webui_server(
                "127.0.0.1", 0, "short", gateway=self.gateway_origin,
                state_dir=self.root / "short-password",
            )

    def test_offline_gateway_returns_unavailable_instead_of_fake_empty_accounts(self):
        self.gateway.shutdown()
        self.gateway.server_close()
        self.gateway_worker.join(timeout=3)
        self.assertFalse(self.gateway_worker.is_alive())
        status, body = self.request("GET", "/api/accounts", token=self.token)
        self.assertEqual(status, 503)
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "GATEWAY_UNAVAILABLE")
        self.assertNotIn("list", body)

    def test_logout_closes_an_existing_authenticated_stream(self):
        _, response = self.open_stream("/api/debug/stream")
        self.assertEqual(self.next_stream_frame(response), {"kind": "ready"})
        status, payload = self.request("POST", "/api/logout", {}, token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(payload["success"])
        # Reading to EOF is bounded by the socket timeout. No more private data
        # may be emitted after the session is revoked.
        remaining = response.read()
        self.assertNotIn(b"data:", remaining)

    def test_invalid_debug_actions_fail_before_provider_effect(self):
        invalid = [
            {"account": "onebot:10000", "action": "../send_private_msg", "params": {}},
            {"account": "onebot:10000", "action": "send_private_msg", "params": []},
            {"account": "onebot:10000", "action": None, "params": {}},
        ]
        original = self.runtime.backend.call
        with patch.object(self.runtime.backend, "call", wraps=original) as provider:
            for payload in invalid:
                with self.subTest(payload=payload):
                    status, body = self.request(
                        "POST", "/api/debug/invoke", payload, token=self.token
                    )
                    self.assertEqual(status, 400)
                    self.assertFalse(body["success"])
        self.assertEqual(provider.call_count, 0)


if __name__ == "__main__":
    unittest.main()
