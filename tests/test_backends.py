from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import unittest
from unittest.mock import patch

from heredity.backends.base import BackendError, UnavailableBackend
from heredity.backends.http_json import HTTPJSONBackend
from heredity.backends.mock import MockBackend


class MockBackendTests(unittest.TestCase):
    def test_default_is_not_demo(self):
        backend = UnavailableBackend()
        self.assertFalse(backend.online)
        with self.assertRaises(BackendError) as caught:
            backend.call("send_private_msg", {"user_id": 1, "message": "test"})
        self.assertEqual(caught.exception.retcode, 1503)

    def test_demo_message_roundtrip_delete_and_copy(self):
        backend = MockBackend()
        result = backend.call("send_private_msg", {"user_id": "123", "message": "hello"})
        message = backend.call("get_msg", result)
        self.assertEqual(message["message"], "hello")
        self.assertEqual(message["user_id"], 123)
        message["message"] = "modified copy"
        self.assertEqual(backend.call("get_msg", result)["message"], "hello")
        self.assertIsNone(backend.call("delete_msg", result))
        with self.assertRaises(BackendError):
            backend.call("get_msg", result)

    def test_demo_input_validation_and_no_fake_image_support(self):
        backend = MockBackend()
        for bad_id in (None, 0, -1, True, 1.5, "not-an-id"):
            with self.assertRaises(BackendError):
                backend.call("send_private_msg", {"user_id": bad_id, "message": "x"})
        with self.assertRaises(BackendError):
            backend.call("send_group_msg", {"group_id": 2, "message": [{"type": "text"}]})
        self.assertEqual(backend.call("can_send_image", {}), {"yes": False})


class ProfileTests(unittest.TestCase):
    def setUp(self):
        class Handler(BaseHTTPRequestHandler):
            calls = []

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.calls.append((self.path, body, self.headers.get("Authorization")))
                if self.path == "/malformed":
                    self.connection.sendall(b"NOT-HTTP\r\n\r\n")
                    self.close_connection = True
                    return
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/login")
                    self.end_headers()
                    return
                if self.path == "/failure":
                    result = {"status": "failed", "retcode": 1400, "data": None}
                else:
                    result = {"data": {"user_id": 123, "nickname": "fixture"}}
                data = b'{"data":NaN}' if self.path == "/nan" else json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.handler = Handler
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.profile = {
            "self_id": 123, "base_url": f"http://127.0.0.1:{self.server.server_port}",
            "actions": {"get_login_info": {"path": "/login", "response_path": ["data"]}},
        }

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_explicit_profile_calls_only_declared_route(self):
        backend = HTTPJSONBackend(self.profile)
        self.assertFalse(backend.online)
        self.assertEqual(backend.call("get_login_info", {}), {"user_id": 123, "nickname": "fixture"})
        self.assertTrue(backend.online)
        with self.assertRaises(BackendError) as caught:
            backend.call("send_private_msg", {})
        self.assertEqual(caught.exception.retcode, 1404)

    def test_token_only_from_explicit_environment(self):
        self.profile["access_token_env"] = "HEREDITY_TEST_BACKEND_TOKEN"
        with patch.dict(os.environ, {"HEREDITY_TEST_BACKEND_TOKEN": "fixture-token"}):
            backend = HTTPJSONBackend(self.profile)
        backend.call("get_login_info", {})
        self.assertEqual(self.handler.calls[-1][2], "Bearer fixture-token")

    def test_redirect_not_followed(self):
        self.profile["actions"]["get_login_info"]["path"] = "/redirect"
        with self.assertRaises(BackendError):
            HTTPJSONBackend(self.profile).call("get_login_info", {})
        self.assertEqual(len(self.handler.calls), 1)

    def test_upstream_failure_cannot_be_unwrapped_to_success(self):
        self.profile["actions"]["get_login_info"]["path"] = "/failure"
        with self.assertRaises(BackendError):
            HTTPJSONBackend(self.profile).call("get_login_info", {})

    def test_nonfinite_response_is_failure_and_stays_offline(self):
        self.profile["actions"]["get_login_info"]["path"] = "/nan"
        backend = HTTPJSONBackend(self.profile)
        with self.assertRaises(BackendError) as caught:
            backend.call("get_login_info", {})
        self.assertEqual(caught.exception.retcode, 1502)
        self.assertFalse(backend.online)

    def test_malformed_http_marks_previously_online_backend_offline(self):
        backend = HTTPJSONBackend(self.profile)
        backend.call("get_login_info", {})
        self.assertTrue(backend.online)
        backend.actions["get_login_info"]["path"] = "/malformed"
        with self.assertRaises(BackendError) as caught:
            backend.call("get_login_info", {})
        self.assertEqual(caught.exception.retcode, 1502)
        self.assertFalse(backend.online)

    def test_remote_ambiguous_and_credential_urls_rejected(self):
        for base in ("http://example.com:80", "http://0.0.0.0:80", "http://127.0.0.1",
                     "http://user:secret@127.0.0.1:80", "http://127.0.0.1:80/path",
                     "http://127.0.0.1:80?token=x"):
            profile = {**self.profile, "base_url": base}
            with self.assertRaises(ValueError, msg=base):
                HTTPJSONBackend(profile)

    def test_route_cannot_escape_configured_origin(self):
        for route in ("//example.com/action", "https://example.com", "/x\\y", "/x\nInjected",
                      "/x?token=y", "/x#fragment"):
            self.profile["actions"]["get_login_info"]["path"] = route
            with self.assertRaises(ValueError, msg=route):
                HTTPJSONBackend(self.profile)


if __name__ == "__main__":
    unittest.main()
