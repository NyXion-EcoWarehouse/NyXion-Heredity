import hashlib
import hmac
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from heredity.backends.base import BackendError
from heredity.connection_events import EventReceiver, MAX_EVENT_BYTES
from heredity.providers.manager import ProviderManager
from heredity.providers.registry import demo_registration
from heredity.runtime import AccountRuntime
from heredity.storage import MessageStore


ACCOUNT = 10000
PRIVATE = "fixture-private-value-never-in-output"


class EventReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = MessageStore(Path(self.temporary.name) / "account.sqlite")
        self.manager = ProviderManager(ACCOUNT, [
            demo_registration(self.store, ACCOUNT, "demo-one"),
            demo_registration(self.store, ACCOUNT, "demo-two"),
        ], "demo-one")
        self.runtime = AccountRuntime(self.manager, self.store, mode="demo")
        self.addCleanup(self.runtime.close)
        self.assertTrue(self.runtime.initialize())
        self.epoch = self.manager.session_epoch
        self.receiver = EventReceiver(self.runtime, self.epoch)
        self.addCleanup(self.receiver.close)
        self.receiver.start()

    def event(self, identifier=-501, account=ACCOUNT):
        return {
            "self_id": account, "time": 1700000000,
            "post_type": "message", "message_type": "private",
            "message_id": identifier, "user_id": 10001,
            "message": "received \u4f60\u597d",
        }

    def encoded(self, event=None):
        return json.dumps(
            self.event() if event is None else event, ensure_ascii=False
        ).encode("utf-8")

    def signature(self, raw, secret=None):
        return "sha1=" + hmac.new(
            (secret or self.receiver.secret).encode("ascii"), raw, hashlib.sha1
        ).hexdigest()

    def request(self, raw=None, *, method="POST", path=None, headers=None, sign=True):
        origin = urlsplit(self.receiver.url)
        raw = self.encoded() if raw is None else raw
        outgoing = {"Content-Type": "application/json", **(headers or {})}
        if sign:
            outgoing.setdefault("X-Signature", self.signature(raw))
        connection = http.client.HTTPConnection(origin.hostname, origin.port, timeout=3)
        try:
            connection.request(method, path or origin.path, body=raw, headers=outgoing)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def raw_headers_request(self, lines, body=b"{}"):
        origin = urlsplit(self.receiver.url)
        request = (
            f"POST {origin.path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            + "\r\n".join(lines)
            + "\r\nConnection: close\r\n\r\n"
        ).encode("ascii") + body
        with socket.create_connection((origin.hostname, origin.port), timeout=3) as connection:
            connection.sendall(request)
            response = http.client.HTTPResponse(connection)
            response.begin()
            return response.status, json.loads(response.read())

    def test_start_is_idempotent_and_never_claims_reception_before_any_event(self):
        self.receiver.start()
        state = self.receiver.snapshot()
        self.assertEqual(set(state), {
            "enabled", "healthy", "last_event_at", "accepted", "duplicates", "reason"
        })
        self.assertTrue(state["enabled"])
        self.assertIsNone(state["healthy"])
        self.assertIsNone(state["last_event_at"])
        self.assertEqual(state["accepted"], 0)
        self.assertEqual(state["duplicates"], 0)
        config = self.receiver.config()
        self.assertEqual(set(config), {"url", "secret"})
        self.assertGreaterEqual(len(config["secret"]), 32)
        origin = urlsplit(config["url"])
        self.assertEqual(origin.hostname, "127.0.0.1")
        self.assertGreater(origin.port, 0)
        self.assertTrue(origin.path.startswith("/events/"))
        self.assertNotIn(config["secret"], json.dumps(state))
        self.assertNotIn(config["url"], json.dumps(state))

    def test_distinct_receivers_generate_independent_routes_and_secrets(self):
        other = EventReceiver(self.runtime, self.epoch)
        self.addCleanup(other.close)
        other.start()
        self.assertNotEqual(other.secret, self.receiver.secret)
        self.assertNotEqual(urlsplit(other.url).path, urlsplit(self.receiver.url).path)
        self.assertNotEqual(urlsplit(other.url).port, urlsplit(self.receiver.url).port)

    def test_signed_raw_bytes_commit_before_success_and_return_no_quick_operation(self):
        raw = self.encoded()
        with patch.object(self.runtime, "ingest", wraps=self.runtime.ingest) as ingest:
            status, body = self.request(raw)
        self.assertEqual(status, 200)
        self.assertEqual(body, {})
        self.assertEqual(ingest.call_args.kwargs, {"session_epoch": self.epoch})
        self.assertIsNotNone(self.store.get_message(ACCOUNT, -501))
        persisted = self.store.events_after(ACCOUNT)
        self.assertEqual(len(persisted), 1)
        self.assertEqual(self.runtime.event_hub.latest_sequence, persisted[0]["sequence"])
        state = self.receiver.snapshot()
        self.assertTrue(state["healthy"])
        self.assertEqual(state["accepted"], 1)
        self.assertEqual(state["duplicates"], 0)
        self.assertLessEqual(abs(state["last_event_at"] - time.time()), 2)

    def test_repeated_events_use_durable_runtime_deduplication(self):
        raw = self.encoded()
        self.assertEqual(self.request(raw), (200, {}))
        sequence = self.runtime.event_hub.latest_sequence
        self.assertEqual(self.request(raw), (200, {}))
        self.assertEqual(len(self.store.events_after(ACCOUNT)), 1)
        self.assertEqual(self.runtime.event_hub.latest_sequence, sequence)
        state = self.receiver.snapshot()
        self.assertEqual((state["accepted"], state["duplicates"]), (1, 1))
        self.assertTrue(state["healthy"])

    def test_signature_is_required_and_checked_before_json_decoding(self):
        raw = b"not JSON " + PRIVATE.encode()
        invalid = [
            ({}, 401),
            ({"X-Signature": "sha1=invalid"}, 403),
            ({"X-Signature": self.signature(raw, "wrong-secret")}, 403),
            ({"Authorization": "Bearer " + self.receiver.secret}, 401),
        ]
        with (
            patch("heredity.connection_events._decode_json") as decode,
            patch.object(self.runtime, "ingest") as ingest,
        ):
            for headers, expected in invalid:
                with self.subTest(headers=headers):
                    status, body = self.request(raw, headers=headers, sign=False)
                    self.assertEqual(status, expected)
                    self.assertNotIn(PRIVATE, json.dumps(body))
                    self.assertNotIn(self.receiver.secret, json.dumps(body))
            decode.assert_not_called()
            ingest.assert_not_called()
        self.assertIsNone(self.receiver.snapshot()["healthy"])

    def test_signature_must_cover_the_exact_received_body_bytes(self):
        compact = json.dumps(self.event(), separators=(",", ":")).encode()
        pretty = json.dumps(self.event(), indent=2).encode()
        self.assertNotEqual(compact, pretty)
        status, _ = self.request(pretty, headers={"X-Signature": self.signature(compact)})
        self.assertEqual(status, 403)
        self.assertEqual(self.store.events_after(ACCOUNT), [])
        self.assertEqual(self.request(pretty), (200, {}))

    def test_only_exact_random_post_route_accepts_events_and_query_cannot_replace_signature(self):
        path = urlsplit(self.receiver.url).path
        for path_value, method, sign, expected in (
            ("/events/incorrect", "POST", True, 404),
            (path + "?access_token=" + self.receiver.secret, "POST", False, 404),
            (path + "/", "POST", True, 404),
            (path, "GET", True, 405),
            (path, "PUT", True, 405),
        ):
            with self.subTest(path=path_value, method=method):
                status, _ = self.request(method=method, path=path_value, sign=sign)
                self.assertEqual(status, expected)
        self.assertEqual(self.store.events_after(ACCOUNT), [])

    def test_ambiguous_framing_missing_length_and_oversized_bodies_are_rejected(self):
        cases = (
            (["Content-Type: application/json", "Content-Length: 2", "Content-Length: 2"], 400),
            (["Content-Type: application/json", "Content-Length: 2", "Transfer-Encoding: chunked"], 400),
            (["Content-Type: application/json"], 400),
            (["Content-Type: application/json", "Content-Length: -1"], 400),
            (["Content-Type: application/json", "Content-Length: +2"], 400),
            (["Content-Type: application/json", f"Content-Length: {MAX_EVENT_BYTES + 1}"], 413),
        )
        for headers, expected in cases:
            with self.subTest(headers=headers):
                status, _ = self.raw_headers_request(headers)
                self.assertEqual(status, expected)
        self.assertEqual(self.store.events_after(ACCOUNT), [])

    def test_non_json_and_duplicate_signature_headers_are_rejected(self):
        status, _ = self.request(headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        raw = b"{}"
        signature = self.signature(raw)
        status, _ = self.raw_headers_request([
            "Content-Type: application/json", "Content-Length: 2",
            "X-Signature: " + signature, "X-Signature: " + signature,
        ], raw)
        self.assertEqual(status, 403)
        self.assertEqual(self.store.events_after(ACCOUNT), [])

    def test_signed_invalid_nonobject_nonfinite_and_deep_json_fail_safely(self):
        samples = [
            b"not-json", b"[]", b"null", b'{"value":NaN}', b"\xff",
            (b'{"child":' * 70) + b"0" + (b"}" * 70),
        ]
        for raw in samples:
            with self.subTest(raw=raw[:32]):
                status, result = self.request(raw)
                self.assertEqual(status, 400)
                self.assertEqual(result["retcode"], 1400)
        self.assertFalse(self.receiver.snapshot()["healthy"])
        self.assertEqual(self.store.events_after(ACCOUNT), [])
        self.assertEqual(self.runtime.event_hub.latest_sequence, 0)

    def test_cross_account_and_stale_epoch_have_precise_failures_without_writes(self):
        status, result = self.request(self.encoded(self.event(account=99999)))
        self.assertEqual(status, 400)
        self.assertEqual(result["retcode"], 1400)
        self.assertEqual(self.store.events_after(ACCOUNT), [])
        self.runtime.switch_provider("demo-two", self.epoch)
        state = self.receiver.snapshot()
        self.assertFalse(state["enabled"])
        self.assertFalse(state["healthy"])
        self.assertIn("reconnect", state["reason"])
        with self.assertRaises(BackendError) as caught:
            self.receiver.config()
        self.assertEqual(caught.exception.retcode, 1409)
        persisted = self.store.events_after(ACCOUNT)
        latest = self.runtime.event_hub.latest_sequence
        status, result = self.request()
        self.assertEqual(status, 409)
        self.assertEqual(result["retcode"], 1409)
        self.assertEqual(self.store.events_after(ACCOUNT), persisted)
        self.assertEqual(self.runtime.event_hub.latest_sequence, latest)
        self.assertFalse(self.receiver.snapshot()["healthy"])

    def test_epoch_change_invalidates_previously_healthy_receiver_before_another_event(self):
        self.assertEqual(self.request(), (200, {}))
        before = self.receiver.snapshot()
        self.assertTrue(before["healthy"])
        self.runtime.switch_provider("demo-two", self.epoch)
        state = self.receiver.snapshot()
        self.assertFalse(state["enabled"])
        self.assertFalse(state["healthy"])
        self.assertEqual(state["accepted"], before["accepted"])
        self.assertEqual(state["last_event_at"], before["last_event_at"])
        self.assertIn("new event callback", state["reason"])
        with self.assertRaises(BackendError) as caught:
            self.receiver.config()
        self.assertEqual(caught.exception.retcode, 1409)
        self.assertNotIn(self.receiver.secret, str(caught.exception))

    def test_simple_ingest_runtime_without_managed_backend_keeps_receiver_compatibility(self):
        class Runtime:
            self_id = ACCOUNT

            def ingest(self, event, session_epoch=None):
                return {"persisted": True, "duplicate": False}

        receiver = EventReceiver(Runtime(), "fixed-fixture-epoch")
        self.addCleanup(receiver.close)
        receiver.start()
        self.assertTrue(receiver.snapshot()["enabled"])
        self.assertIsNone(receiver.snapshot()["healthy"])
        self.assertEqual(set(receiver.config()), {"url", "secret"})
        receiver._accept(self.event())
        self.assertTrue(receiver.snapshot()["healthy"])

    def test_storage_failure_is_503_without_success_or_sensitive_error_echo(self):
        with patch.object(
            self.store, "commit_event", side_effect=BackendError(PRIVATE, 1500)
        ):
            status, result = self.request()
        self.assertEqual(status, 503)
        self.assertEqual(result["retcode"], 1500)
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertFalse(self.receiver.snapshot()["healthy"])
        self.assertEqual(self.receiver.snapshot()["accepted"], 0)
        self.assertEqual(self.store.events_after(ACCOUNT), [])
        self.assertEqual(self.runtime.event_hub.latest_sequence, 0)

    def test_close_is_bounded_idempotent_and_never_closes_runtime(self):
        started = time.monotonic()
        self.receiver.close()
        self.receiver.close()
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertFalse(self.receiver.snapshot()["enabled"])
        self.assertFalse(self.receiver.snapshot()["healthy"])
        with self.assertRaises(BackendError):
            self.receiver.start()
        self.assertEqual(self.runtime.call("get_login_info", {})["user_id"], ACCOUNT)
        unopened = EventReceiver(self.runtime, self.manager.session_epoch)
        self.addCleanup(unopened.close)
        started = time.monotonic()
        unopened.close()
        self.assertLess(time.monotonic() - started, 2.5)

    def test_fixed_epoch_remains_attached_to_original_receiver_and_start_failure_is_bounded(self):
        failed = EventReceiver(self.runtime, self.epoch)
        self.addCleanup(failed.close)
        with patch.object(failed._server, "serve_forever", side_effect=RuntimeError(PRIVATE)):
            started = time.monotonic()
            with self.assertRaises(BackendError) as caught:
                failed.start()
            self.assertLess(time.monotonic() - started, 2.5)
        self.assertNotIn(PRIVATE, str(caught.exception))
        self.assertFalse(failed.snapshot()["enabled"])
        for epoch in ("", None, True, "x" * 257, "line\nbreak"):
            with self.subTest(epoch=epoch), self.assertRaises(ValueError):
                EventReceiver(self.runtime, epoch)


if __name__ == "__main__":
    unittest.main()
