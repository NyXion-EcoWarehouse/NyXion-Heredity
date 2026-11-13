import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch

from heredity.backends.base import BackendError
from heredity.backends.mock import MockBackend
from heredity.onebot import create_server
from heredity.runtime import AccountRuntime
from heredity.storage import MessageStore


def private_event(identifier=-301, **fields):
    return {
        "time": 1700000000,
        "self_id": 10000,
        "post_type": "message",
        "message_type": "private",
        "message_id": identifier,
        "user_id": 10001,
        "message": "received[CQ:at,target=10002]",
        **fields,
    }


class RuntimeHTTPTests(unittest.TestCase):
    def setUp(self):
        self.server, self.runtime, self.worker = self.make_gateway()

    def make_gateway(self, *, event_capacity=256, **options):
        backend = MockBackend()
        store = MessageStore(":memory:")
        runtime = AccountRuntime(
            backend, store, mode="demo", event_capacity=event_capacity
        )
        self.assertTrue(runtime.initialize())
        try:
            server = create_server(
                "127.0.0.1",
                0,
                "consumer",
                runtime,
                provider_token="provider",
                event_secret="event-secret",
                enable_sse=options.pop("enable_sse", True),
                **options,
            )
        except BaseException:
            runtime.close()
            raise
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(self.close_gateway, server, runtime, worker)
        return server, runtime, worker

    def close_gateway(self, server, runtime, worker):
        try:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
        finally:
            runtime.close()

    def request(self, method, path, value=None, *, token="consumer", headers=None, server=None):
        server = server or self.server
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        request_headers = dict(headers or {})
        if token is not None:
            request_headers.setdefault("Authorization", "Bearer " + token)
        body = None
        if value is not None:
            body = json.dumps(value, ensure_ascii=True)
            request_headers.setdefault("Content-Type", "application/json")
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def ingest(self, event, *, server=None, headers=None):
        return self.request(
            "POST",
            "/_heredity/events",
            event,
            token="provider",
            server=server,
            headers=headers,
        )

    def open_stream(self, path="/_heredity/events", *, headers=None, server=None):
        server = server or self.server
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        request_headers = {"Authorization": "Bearer consumer", **(headers or {})}
        connection.request("GET", path, headers=request_headers)
        response = connection.getresponse()
        self.addCleanup(connection.close)
        self.addCleanup(response.close)
        return connection, response

    def next_sse_event(self, response):
        identifier = None
        payload = None
        for _ in range(30):
            line = response.readline()
            if not line:
                self.fail("Event stream closed before an event arrived.")
            if line.startswith(b"id: "):
                identifier = int(line[4:].strip())
            elif line.startswith(b"data: "):
                payload = json.loads(line[6:])
            elif line in {b"\n", b"\r\n"} and payload is not None:
                return identifier, payload
        self.fail("No complete SSE event arrived within the bounded frame read.")

    def test_provider_requires_separate_header_token(self):
        cases = [
            ("consumer", "/_heredity/events", 403),
            (None, "/_heredity/events", 401),
            (None, "/_heredity/events?access_token=provider", 401),
            ("consumer", "/_heredity/events?access_token=provider", 403),
        ]
        for token, path, expected in cases:
            with self.subTest(token=token, path=path):
                status, body = self.request(
                    "POST", path, private_event(), token=token
                )
                self.assertEqual(status, expected)
                self.assertEqual(body["status"], "failed")
        self.assertEqual(self.runtime.store.events_after(10000), [])
        self.assertEqual(self.runtime.event_hub.latest_sequence, 0)
        status, body = self.ingest(private_event())
        self.assertEqual(status, 200)
        self.assertTrue(body["data"]["persisted"])

    def test_provider_events_require_json_content_type(self):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request(
                "POST",
                "/_heredity/events",
                body="message=not-json",
                headers={
                    "Authorization": "Bearer provider",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 406)
            self.assertEqual(json.loads(response.read())["status"], "failed")
        finally:
            connection.close()
        self.assertEqual(self.runtime.store.events_after(10000), [])

    def test_account_mismatch_is_rejected_before_persistence(self):
        for self_id in [10001, True, None]:
            with self.subTest(self_id=self_id):
                status, body = self.ingest(private_event(self_id=self_id))
                self.assertEqual(status, 400)
                self.assertEqual(body["retcode"], 1400)
        self.assertEqual(self.runtime.store.events_after(10000), [])
        self.assertIsNone(self.runtime.store.get_message(10000, -301))
        self.assertEqual(self.runtime.event_hub.latest_sequence, 0)

    def test_normalized_negative_message_is_persisted_deduplicated_and_readable(self):
        original = private_event(-901)
        status, first = self.ingest(original)
        self.assertEqual(status, 200)
        self.assertFalse(first["data"]["duplicate"])
        self.assertTrue(first["data"]["persisted"])
        sequence = first["data"]["sequence"]
        self.assertGreater(sequence, 0)
        status, second = self.ingest(
            {**original, "time": original["time"] + 1, "message": "changed duplicate"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(second["data"]["duplicate"])
        self.assertIsNone(second["data"]["sequence"])
        self.assertEqual(self.runtime.event_hub.latest_sequence, sequence)
        persisted = self.runtime.store.events_after(10000)
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]["sequence"], sequence)
        status, response = self.request(
            "POST", "/get_msg", {"message_id": "-901"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(response["status"], "ok")
        record = response["data"]
        self.assertEqual(record["message_id"], -901)
        self.assertEqual(record["real_id"], -901)
        self.assertEqual(record["raw_message"], original["message"])
        self.assertEqual(record["message"], [
            {"type": "text", "data": {"text": "received"}},
            {"type": "at", "data": {"target": "10002"}},
        ])
        self.assertEqual(record["sender"]["user_id"], 10001)
        self.assertEqual(self.runtime.store.get_message(10000, -901), record)

    def test_optional_provider_event_id_controls_ingress_deduplication(self):
        headers = {"X-Heredity-Event-ID": "provider-fixture-event"}
        status, first = self.ingest(private_event(-910), headers=headers)
        self.assertEqual(status, 200)
        status, second = self.ingest(private_event(-911), headers=headers)
        self.assertEqual(status, 200)
        self.assertTrue(second["data"]["duplicate"])
        self.assertEqual(len(self.runtime.store.events_after(10000)), 1)
        self.assertIsNone(self.runtime.store.get_message(10000, -911))
        self.assertEqual(
            self.runtime.event_hub.latest_sequence, first["data"]["sequence"]
        )

    def test_sse_consumer_auth_and_query_replay(self):
        for token, expected in [(None, 401), ("provider", 403)]:
            with self.subTest(token=token):
                status, body = self.request(
                    "GET", "/_heredity/events", token=token
                )
                self.assertEqual(status, expected)
                self.assertEqual(body["status"], "failed")
        _, accepted = self.ingest(private_event(-920))
        connection, response = self.open_stream("/_heredity/events?after=0")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("X-Self-ID"), "10000")
        self.assertTrue(response.getheader("Content-Type").startswith("text/event-stream"))
        sequence, received = self.next_sse_event(response)
        self.assertEqual(sequence, accepted["data"]["sequence"])
        self.assertEqual(received["message_id"], -920)
        self.assertEqual(received["self_id"], 10000)
        connection.close()

    def test_sse_last_event_id_replays_only_later_records(self):
        _, first = self.ingest(private_event(-930))
        _, second = self.ingest(private_event(-931))
        connection, response = self.open_stream(
            "/_heredity/events?after=999999",
            headers={"Last-Event-ID": str(first["data"]["sequence"])},
        )
        self.assertEqual(response.status, 200)
        sequence, received = self.next_sse_event(response)
        self.assertEqual(sequence, second["data"]["sequence"])
        self.assertEqual(received["message_id"], -931)
        connection.close()

    def test_sse_live_stream_receives_ingested_message(self):
        connection, response = self.open_stream()
        self.assertEqual(response.status, 200)
        # A fresh live subscription emits a transport comment before any event.
        self.assertTrue(response.readline().startswith(b":"))
        self.assertIn(response.readline(), {b"\n", b"\r\n"})
        _, accepted = self.ingest(private_event(-940))
        sequence, received = self.next_sse_event(response)
        self.assertEqual(sequence, accepted["data"]["sequence"])
        self.assertEqual(received["message_id"], -940)
        connection.close()

    def test_sse_gap_future_and_malformed_cursors_are_visible(self):
        server, runtime, _ = self.make_gateway(event_capacity=2)
        sequences = []
        for identifier in [-950, -951, -952]:
            status, accepted = self.ingest(private_event(identifier), server=server)
            self.assertEqual(status, 200)
            sequences.append(accepted["data"]["sequence"])
        for cursor in [0, sequences[0]]:
            with self.subTest(cursor=cursor):
                status, body = self.request(
                    "GET", f"/_heredity/events?after={cursor}", server=server
                )
                self.assertEqual(status, 409)
                self.assertEqual(body["oldest_sequence"], sequences[1])
                self.assertEqual(body["latest_sequence"], sequences[2])
        for cursor in [sequences[2] + 1, -1, "invalid", "1.5"]:
            with self.subTest(cursor=cursor):
                status, body = self.request(
                    "GET", f"/_heredity/events?after={cursor}", server=server
                )
                self.assertEqual(status, 400)
                self.assertEqual(body["status"], "failed")
        self.assertEqual(len(runtime.store.events_after(10000)), 3)

    def test_disabled_sse_is_404(self):
        server, _, _ = self.make_gateway(enable_sse=False)
        status, response = self.request("GET", "/_heredity/events", server=server)
        self.assertEqual(status, 404)
        self.assertEqual(response["retcode"], 1404)

    def test_forwarded_actual_message_signature_uses_original_body_bytes(self):
        received = []
        received_lock = threading.Lock()
        message_arrived = threading.Event()

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                payload = json.loads(raw)
                with received_lock:
                    received.append(
                        (raw, payload, self.headers.get("X-Signature"),
                         self.headers.get("X-Self-ID"))
                    )
                if payload["post_type"] == "message":
                    message_arrived.set()
                self.send_response(204)
                self.end_headers()

            def log_message(self, format, *args):
                pass

        receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        receiver.daemon_threads = True
        receiver_worker = threading.Thread(target=receiver.serve_forever, daemon=True)
        receiver_worker.start()

        def close_receiver():
            receiver.shutdown()
            receiver.server_close()
            receiver_worker.join(timeout=3)
            self.assertFalse(receiver_worker.is_alive())

        self.addCleanup(close_receiver)
        server, runtime, _ = self.make_gateway(
            event_urls=[f"http://127.0.0.1:{receiver.server_port}/events"],
            heartbeat_interval=15,
        )
        server.start_events()
        status, accepted = self.ingest(
            private_event(-960, message="\u4f60\u597d[CQ:at,target=10002]"),
            server=server,
        )
        self.assertEqual(status, 200)
        self.assertTrue(accepted["data"]["persisted"])
        self.assertTrue(message_arrived.wait(3))
        with received_lock:
            messages = [item for item in received if item[1]["post_type"] == "message"]
        self.assertEqual(len(messages), 1)
        raw, payload, signature, self_id = messages[0]
        expected = "sha1=" + hmac.new(
            b"event-secret", raw, hashlib.sha1
        ).hexdigest()
        self.assertTrue(hmac.compare_digest(signature, expected))
        self.assertEqual(self_id, "10000")
        self.assertEqual(payload["message_id"], -960)
        self.assertEqual(payload["message"][0]["data"]["text"], "\u4f60\u597d")
        self.assertEqual(
            runtime.store.get_message(10000, -960)["message"], payload["message"]
        )

    def test_server_close_terminates_active_sse_connection(self):
        connection, response = self.open_stream()
        self.assertEqual(response.status, 200)
        self.assertTrue(response.readline().startswith(b":"))
        self.assertIn(response.readline(), {b"\n", b"\r\n"})
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=3)
        self.assertFalse(self.worker.is_alive())
        # EOF must arrive within the connection timeout, without another event.
        self.assertEqual(response.read(), b"")
        connection.close()

    def test_post_effect_persistence_failure_exposes_no_retry_details(self):
        original_call = self.runtime.backend.call
        with (
            patch.object(
                self.runtime.store,
                "put_message",
                side_effect=BackendError("fixture disk failure", 1500),
            ),
            patch.object(self.runtime.backend, "call", wraps=original_call) as provider,
        ):
            status, response = self.request(
                "POST", "/send_private_msg",
                {"user_id": 10001, "message": "send once"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(response["status"], "failed")
        self.assertEqual(response["retcode"], 1501)
        self.assertEqual(
            response["details"], {"action_completed": True, "message_id": 1}
        )
        self.assertIn("do not resend", response["message"])
        self.assertEqual([call.args[0] for call in provider.call_args_list],
                         ["get_login_info", "send_private_msg"])
        self.assertIsNone(self.runtime.store.get_message(10000, 1))

    def test_failed_ingress_persistence_returns_503_without_publication(self):
        with patch.object(
            self.runtime.store,
            "commit_event",
            side_effect=BackendError("fixture disk failure", 1500),
        ):
            status, response = self.ingest(private_event(-970))
        self.assertEqual(status, 503)
        self.assertEqual(response["status"], "failed")
        self.assertEqual(response["retcode"], 1500)
        self.assertEqual(self.runtime.event_hub.latest_sequence, 0)
        self.assertEqual(self.runtime.store.events_after(10000), [])


if __name__ == "__main__":
    unittest.main()
