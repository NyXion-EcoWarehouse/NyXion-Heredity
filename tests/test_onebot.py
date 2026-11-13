import http.client
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import parse

from heredity.backends.base import BackendError
from heredity.backends.mock import MockBackend
from heredity.onebot import MAX_BODY_BYTES, create_server


class FakeBackend:
    self_id = 10000
    online = True

    def call(self, action, params):
        if action == "get_login_info":
            return {"user_id": self.self_id, "nickname": "test"}
        if action == "inspect":
            return params
        if action == "broken":
            raise RuntimeError("secret exception details")
        if action == "bad_json":
            return float("nan")
        raise BackendError("Unsupported action.", retcode=1404)

    def close(self):
        pass


class HTTPGatewayTests(unittest.TestCase):
    def setUp(self):
        self.server = create_server("127.0.0.1", 0, "test-token", FakeBackend())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None, authenticated=True):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        all_headers = dict(headers or {})
        if authenticated:
            all_headers.setdefault("Authorization", "Bearer test-token")
        connection.request(method, path, body=body, headers=all_headers)
        response = connection.getresponse()
        status = response.status
        payload = json.loads(response.read())
        connection.close()
        return status, payload

    def post(self, path, value):
        return self.request(
            "POST", path, json.dumps(value), {"Content-Type": "application/json"}
        )

    def test_metadata(self):
        status, response = self.request("GET", "/get_status/")
        self.assertEqual(status, 200)
        self.assertEqual(response["data"], {"online": True, "good": True})
        self.assertNotIn("echo", response)
        _, version = self.request("GET", "/get_version_info")
        self.assertEqual(version["data"]["app_name"], "Heredity")
        self.assertEqual(version["data"]["protocol_version"], "v11")

    def test_action_envelope_echo(self):
        status, response = self.post(
            "/", {"action": "get_login_info", "params": {}, "echo": {"id": 7}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(response["retcode"], 0)
        self.assertEqual(response["data"]["user_id"], 10000)
        self.assertEqual(response["echo"], {"id": 7})

    def test_demo_send_get_delete_over_http(self):
        self.server.backend = MockBackend()
        status, sent = self.post(
            "/", {"action": "send_private_msg",
                  "params": {"user_id": 10001, "message": "local integration fixture"},
                  "echo": "demo-roundtrip"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(sent["echo"], "demo-roundtrip")
        message_id = sent["data"]["message_id"]
        _, received = self.request("GET", f"/get_msg?message_id={message_id}")
        self.assertEqual(received["data"]["message"], "local integration fixture")
        _, deleted = self.post("/delete_msg", {"message_id": message_id})
        self.assertEqual(deleted["retcode"], 0)
        status, missing = self.request("GET", f"/get_msg?message_id={message_id}")
        self.assertEqual(status, 404)
        self.assertEqual(missing["status"], "failed")

    def test_query_and_body_parameters(self):
        status, response = self.post("/inspect?user_id=5&value=old", {"value": "new"})
        self.assertEqual(status, 200)
        self.assertEqual(response["data"], {"user_id": "5", "value": "new"})
        message = parse.quote('[{"type":"text","data":{"text":"hello"}}]')
        _, response = self.request("GET", "/inspect?message=" + message + "&auto_escape=false")
        self.assertIsInstance(response["data"]["message"], list)
        self.assertIs(response["data"]["auto_escape"], False)
        _, response = self.request(
            "POST",
            "/inspect",
            "user_id=5&message=hello",
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(response["data"], {"user_id": "5", "message": "hello"})

    def test_token_enforced_everywhere(self):
        for method, path in [("GET", "/get_status"), ("POST", "/")]:
            with self.subTest(method=method):
                status, response = self.request(method, path, authenticated=False)
                self.assertEqual(status, 401)
                self.assertEqual(response["retcode"], 1401)
        status, _ = self.request(
            "GET", "/get_status", headers={"Authorization": "Bearer wrong"}
        )
        self.assertEqual(status, 403)
        status, response = self.request(
            "GET", "/get_status?access_token=test-token", authenticated=False
        )
        self.assertEqual(status, 200)
        self.assertTrue(response["data"]["online"])
        status, response = self.request(
            "GET", "/inspect?access_token=test-token", authenticated=False
        )
        self.assertNotIn("access_token", response["data"])

    def test_unknown_and_async_actions(self):
        for action in ["unknown", "send_private_msg_async"]:
            status, response = self.request("GET", "/" + action)
            self.assertEqual(status, 404)
            self.assertEqual(response["retcode"], 1404)
            self.assertEqual(response["status"], "failed")
            self.assertIsNone(response["data"])

    def test_invalid_request_objects(self):
        for value in [[], None, {"action": []}, {"action": "inspect", "params": []}]:
            with self.subTest(value=value):
                status, _ = self.post("/", value)
                self.assertEqual(status, 400)
        for raw in ['{"message": NaN}', '{"message": Infinity}', '{"message": 1e10000}', "{broken"]:
            status, _ = self.request(
                "POST", "/inspect", raw, {"Content-Type": "application/json"}
            )
            self.assertEqual(status, 400)

    def test_json_depth_and_escaped_unicode(self):
        status, _ = self.request(
            "POST", "/inspect", '{"message":' + "[" * 2000 + "0" + "]" * 2000 + "}",
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        status, response = self.post(
            "/", {"action": "inspect", "params": {}, "echo": "\ud800"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(response["echo"], "\ud800")

    def test_bad_body_framing_and_content_type(self):
        status, _ = self.request(
            "POST", "/inspect", "x", {"Content-Type": "text/plain"}
        )
        self.assertEqual(status, 406)
        status, _ = self.request(
            "POST",
            "/inspect",
            "",
            {"Content-Type": "application/json", "Content-Length": str(MAX_BODY_BYTES + 1)},
        )
        self.assertEqual(status, 413)
        status, _ = self.request(
            "POST", "/inspect", "", {"Transfer-Encoding": "chunked"}
        )
        self.assertEqual(status, 400)
        status, _ = self.request("POST", "/inspect", "", {"Content-Length": "-1"})
        self.assertEqual(status, 400)

    def test_unexpected_backend_failures_are_sanitized(self):
        for action in ["broken", "bad_json"]:
            status, response = self.request("GET", "/" + action)
            self.assertEqual(status, 200)
            self.assertEqual(response["retcode"], 1500)
            self.assertNotIn("secret", json.dumps(response))

    def test_listener_validation(self):
        with self.assertRaises(ValueError):
            create_server("0.0.0.0", 0, "", FakeBackend())
        for interval in [0, -1, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                create_server(
                    "127.0.0.1", 0, "", FakeBackend(), heartbeat_interval=interval
                )
        for url in ["file:///tmp/events", "http://user:pass@localhost/", "http://x:bad/"]:
            with self.assertRaises(ValueError):
                create_server("127.0.0.1", 0, "", FakeBackend(), event_urls=[url])

    def test_dispatch_after_server_close_does_not_call_backend(self):
        class TrackingBackend(FakeBackend):
            calls = 0

            def call(self, action, params):
                self.calls += 1
                return super().call(action, params)

        backend = TrackingBackend()
        server = create_server("127.0.0.1", 0, "", backend)
        try:
            server.server_close()
            status, response = server.dispatch("inspect", {"value": 1})
            self.assertEqual(status, 200)
            self.assertEqual(response["retcode"], 1503)
            self.assertEqual(backend.calls, 0)
        finally:
            server.server_close()

    def test_periodic_events_and_shutdown(self):
        received = []
        got_heartbeat = threading.Event()

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                received.append((json.loads(raw), self.headers["X-Self-ID"]))
                if received[-1][0]["meta_event_type"] == "heartbeat":
                    got_heartbeat.set()
                self.send_response(204)
                self.end_headers()

            def log_message(self, format, *args):
                pass

        receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        worker = threading.Thread(target=receiver.serve_forever, daemon=True)
        worker.start()
        event_server = create_server(
            "127.0.0.1",
            0,
            "",
            FakeBackend(),
            event_urls=[f"http://127.0.0.1:{receiver.server_port}/events"],
            heartbeat_interval=0.05,
        )
        try:
            event_server.start_events()
            self.assertTrue(got_heartbeat.wait(3))
            event_server.stop_events()
            self.assertFalse(event_server._event_thread.is_alive())
            self.assertEqual(received[0][0]["sub_type"], "enable")
            self.assertEqual(received[0][1], "10000")
            self.assertEqual(received[1][0]["interval"], 50)
        finally:
            event_server.server_close()
            receiver.shutdown()
            receiver.server_close()
            worker.join(timeout=2)

    def test_malformed_receiver_does_not_stop_other_event_delivery(self):
        got_heartbeat = threading.Event()
        healthy_events = []

        class MalformedReceiver(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.wfile.write(b"not-an-http-status-line\r\n\r\n")
                self.wfile.flush()
                self.close_connection = True

            def log_message(self, format, *args):
                pass

        class HealthyReceiver(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                event = json.loads(raw)
                healthy_events.append(event)
                if event["meta_event_type"] == "heartbeat":
                    got_heartbeat.set()
                self.send_response(204)
                self.end_headers()

            def log_message(self, format, *args):
                pass

        receivers = [
            ThreadingHTTPServer(("127.0.0.1", 0), handler)
            for handler in (MalformedReceiver, HealthyReceiver)
        ]
        workers = [
            threading.Thread(target=receiver.serve_forever, daemon=True)
            for receiver in receivers
        ]
        for worker in workers:
            worker.start()
        event_server = create_server(
            "127.0.0.1",
            0,
            "",
            FakeBackend(),
            event_urls=[
                f"http://127.0.0.1:{receiver.server_port}/events"
                for receiver in receivers
            ],
            heartbeat_interval=0.05,
        )
        try:
            with self.assertLogs("heredity.onebot", level="WARNING"):
                event_server.start_events()
                self.assertTrue(got_heartbeat.wait(3))
                event_server.stop_events()
            self.assertFalse(event_server._event_thread.is_alive())
            self.assertEqual(healthy_events[0]["meta_event_type"], "lifecycle")
            self.assertTrue(
                any(event["meta_event_type"] == "heartbeat" for event in healthy_events)
            )
        finally:
            event_server.server_close()
            for receiver in receivers:
                receiver.shutdown()
                receiver.server_close()
            for worker in workers:
                worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
