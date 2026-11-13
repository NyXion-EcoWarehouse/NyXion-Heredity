import hashlib
import hmac
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib import error, parse, request

from heredity.backends.base import BackendError
from heredity.connections import ConnectionManagementError, ConnectionManager


UPSTREAM_TOKEN = "fixture-upstream-token-never-in-public-state"


def ok(data):
    return {"status": "ok", "retcode": 0, "data": data}


class ExternalFixture:
    def __init__(self):
        self.identity = {"user_id": "123", "nickname": "External Fixture"}
        self.status = {"online": True, "good": True}
        self.token = UPSTREAM_TOKEN
        self.calls = []
        self.responses = {}
        self.send_count = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                action = parse.urlsplit(self.path).path.strip("/")
                size = int(self.headers.get("Content-Length", 0))
                params = json.loads(self.rfile.read(size) or b"{}")
                owner.calls.append({
                    "method": "POST", "action": action, "params": params,
                    "authorization": self.headers.get("Authorization"),
                })
                if self.headers.get("Authorization") != "Bearer " + owner.token:
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if action in owner.responses:
                    configured = owner.responses[action]
                    if callable(configured):
                        configured = configured()
                    if isinstance(configured, tuple):
                        code, raw, headers = configured
                        self.send_response(code)
                        for key, value in headers.items():
                            self.send_header(key, value)
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                        return
                    result = configured
                elif action == "get_login_info":
                    result = ok(owner.identity)
                elif action == "get_status":
                    result = ok(owner.status)
                elif action in {"send_private_msg", "send_group_msg", "send_msg"}:
                    owner.send_count += 1
                    result = ok({"message_id": -800 - owner.send_count})
                elif action == "get_friend_list":
                    result = ok([{"user_id": 456, "nickname": "Fixture Friend"}])
                elif action == "get_group_list":
                    result = ok([{"group_id": 789, "group_name": "Fixture Group"}])
                elif action == "delete_msg":
                    result = ok(None)
                elif action in {"can_send_image", "can_send_record"}:
                    result = ok({"yes": True})
                elif action == "get_msg":
                    result = ok({"message_id": params["message_id"], "message": "fixture history"})
                else:
                    result = {"status": "failed", "retcode": 1404, "data": None}
                raw = json.dumps(result, allow_nan=True).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


def gateway_action(handle, action, params=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token is None:
        token = handle.gateway_token
    if token:
        headers["Authorization"] = "Bearer " + token
    outgoing = request.Request(
        handle.gateway_origin + "/" + action,
        data=json.dumps(params or {}).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with request.urlopen(outgoing, timeout=4) as response:
            return json.load(response)
    except error.HTTPError as exc:
        if exc.code != 404:
            raise
        with exc:
            return json.load(exc)


class ConnectionManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = ExternalFixture()
        self.addCleanup(self.fixture.close)
        self.manager = ConnectionManager(
            self.temporary.name, "http://127.0.0.1:5700", status_ttl=0
        )
        self.addCleanup(self.manager.close)

    def probe(self, **changes):
        return self.manager.probe({
            "origin": self.fixture.origin, "access_token": self.fixture.token, **changes,
        })

    def connect(self, **changes):
        probe = self.probe(**changes)
        return self.manager.connect(probe["probe_id"])

    def assert_error(self, operation, code=None):
        with self.assertRaises(ConnectionManagementError) as caught:
            operation()
        if code is not None:
            self.assertEqual(caught.exception.code, code)
        self.assertNotIn(UPSTREAM_TOKEN, str(caught.exception))
        return caught.exception

    def test_unconfigured_snapshot_never_contacts_default_gateway(self):
        self.assertEqual(self.manager.snapshot(), {"active": None, "configured": False})
        self.assertEqual(self.fixture.calls, [])
        self.assert_error(self.manager.gateway_config, "CONNECTION_UNAVAILABLE")
        self.assert_error(self.manager.event_config, "CONNECTION_UNAVAILABLE")

    def test_probe_only_reads_explicit_identity_and_status_and_normalizes_account(self):
        result = self.probe(execution_level="L2")
        self.assertEqual(result["self_id"], 123)
        self.assertEqual(result["nickname"], "External Fixture")
        self.assertTrue(result["online"])
        self.assertEqual(result["execution_level"], "L2")
        self.assertGreaterEqual(result["expires_at"], time.time())
        self.assertEqual(
            [call["action"] for call in self.fixture.calls], ["get_login_info", "get_status"]
        )
        self.assertTrue(all(call["method"] == "POST" for call in self.fixture.calls))
        self.assertTrue(all(call["params"] == {} for call in self.fixture.calls))
        self.assertNotIn(UPSTREAM_TOKEN, json.dumps(result))
        self.assertEqual(self.manager.snapshot(), {"active": None, "configured": False})
        self.assertEqual(self.fixture.send_count, 0)

    def test_invalid_origins_and_credential_fields_are_rejected_without_network(self):
        origins = [
            "http://example.invalid:3000",
            "http://0.0.0.0:3000",
            "http://127.0.0.1",
            "http://127.0.0.1:0",
            "http://user:pass@127.0.0.1:3000",
            "http://127.0.0.1:3000/api",
            "http://127.0.0.1:3000?access_token=secret",
            "http://127.0.0.1:3000?",
            "http://127.0.0.1:3000/#",
            "file:///tmp/upstream",
            " http://127.0.0.1:3000",
            "http://127.0.0.1:5700",
            "http://localhost:5099",
            "https://[::1]:5178",
        ]
        for origin in origins:
            with self.subTest(origin=origin):
                self.assert_error(lambda: self.probe(origin=origin))
        self.assert_error(lambda: self.probe(access_token="bad\r\nheader"))
        self.assert_error(lambda: self.probe(token="duplicate"))
        self.assert_error(lambda: self.probe(execution_level="L9"))
        self.assertEqual(self.fixture.calls, [])

    def test_forbidden_ports_cover_loopback_aliases_and_newly_bound_services(self):
        self.manager.add_forbidden_origin("http://127.0.0.1:19090")
        for host in ("127.0.0.1", "localhost", "[::1]", "127.0.0.2"):
            with self.subTest(host=host):
                self.assert_error(lambda: self.probe(origin=f"https://{host}:19090"), "CONNECTION_LOOP")
        result = self.probe()
        self.manager.add_forbidden_origin(self.fixture.origin)
        self.assert_error(lambda: self.manager.connect(result["probe_id"]), "CONNECT_FAILED")
        self.assertIsNone(self.manager.snapshot()["active"])

    def test_probe_rejects_wrong_token_redirects_and_oversized_responses(self):
        self.assert_error(lambda: self.probe(access_token="wrong"), "PROBE_FAILED")
        self.fixture.responses["get_login_info"] = (
            302, b"", {"Location": self.fixture.origin + "/redirect-target"}
        )
        self.assert_error(self.probe, "PROBE_FAILED")
        self.assertFalse(any(call["action"] == "redirect-target" for call in self.fixture.calls))
        self.fixture.responses["get_login_info"] = (
            200, b"x" * (1024 * 1024 + 1), {"Content-Type": "application/json"}
        )
        self.assert_error(self.probe, "PROBE_FAILED")

    def test_probe_requires_strict_success_envelopes(self):
        malformed = [
            {"status": "ok", "retcode": False, "data": self.fixture.identity},
            {"status": "async", "retcode": 0, "data": self.fixture.identity},
            {"status": "ok", "retcode": 1, "data": self.fixture.identity},
            {"status": "failed", "retcode": 1400, "data": None},
            {"status": "ok", "retcode": 0},
            ok({"user_id": 123, "nickname": "test", "nonfinite": math.nan}),
        ]
        for envelope in malformed:
            with self.subTest(envelope=envelope):
                self.fixture.responses["get_login_info"] = envelope
                self.assert_error(self.probe, "PROBE_FAILED")

    def test_probe_rejects_invalid_accounts_offline_status_and_known_demo(self):
        for identifier in (True, 0, -1, 1.0, "1.0", "+123", " 123", str(2**63)):
            with self.subTest(identifier=identifier):
                self.fixture.identity["user_id"] = identifier
                self.assert_error(self.probe, "PROBE_FAILED")
        self.fixture.identity["user_id"] = 123
        for status in (
            {"online": False, "good": False},
            {"online": 1, "good": True},
            {"online": True, "good": "true"},
            {"online": True, "self_id": 999},
            {"online": True, "mode": "demo"},
            {"online": True, "execution": {"state": {"descriptor": {"source_kind": "demo"}}}},
        ):
            with self.subTest(status=status):
                self.fixture.status = status
                self.assert_error(self.probe, "PROBE_FAILED")
        self.fixture.status = {"online": True}
        self.assertEqual(self.probe()["self_id"], 123)

    def test_expired_probe_is_single_use_and_credentials_are_not_persisted(self):
        probe = self.probe()
        self.manager._probes[probe["probe_id"]].deadline = time.monotonic() - 1
        self.assert_error(lambda: self.manager.connect(probe["probe_id"]), "PROBE_EXPIRED")
        self.assertEqual(list(Path(self.temporary.name).rglob("*")), [])
        handle = self.connect()
        self.assert_error(lambda: self.manager.connect(probe["probe_id"]), "PROBE_EXPIRED")
        self.assertTrue(handle.gateway_token)
        for path in Path(self.temporary.name).rglob("*"):
            if path.is_file():
                self.assertNotIn(UPSTREAM_TOKEN.encode(), path.read_bytes())

    def test_connect_rechecks_identity_and_preserves_existing_handle_on_failure(self):
        active = self.connect()
        pending = self.probe()
        self.fixture.identity["user_id"] = 999
        self.assert_error(lambda: self.manager.connect(pending["probe_id"]), "CONNECT_FAILED")
        self.assertEqual(self.manager.gateway_config()["origin"], active.gateway_origin)
        self.assertFalse(active._closed)
        self.assertEqual(self.fixture.send_count, 0)

    def test_connect_builds_authenticated_local_gateway_and_persists_confirmed_send(self):
        handle = self.connect(execution_level="L2")
        self.assertNotEqual(handle.gateway_origin, self.fixture.origin)
        self.assertNotEqual(handle.gateway_token, self.fixture.token)
        self.assertNotEqual(handle.execution_token, handle.gateway_token)
        with self.assertRaises(error.HTTPError) as caught:
            gateway_action(handle, "get_status", token="")
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()
        status = gateway_action(handle, "get_status")["data"]
        self.assertTrue(status["online"])
        self.assertIsNone(status["receive_healthy"])
        self.assertEqual(status["mode"], "profile")
        self.assertEqual(status["execution_level"], "L2")
        execution = gateway_action(handle, "get_execution_info")["data"]
        item = execution["state"]["descriptor"]
        self.assertEqual(item["level_provenance"], "declared")
        self.assertEqual(item["source_kind"], "profile")
        result = gateway_action(
            handle, "send_private_msg", {"user_id": 456, "message": "fixture send"}
        )
        self.assertEqual(result, ok({"message_id": -801}))
        self.assertEqual(self.fixture.send_count, 1)
        record = gateway_action(handle, "get_msg", {"message_id": -801})["data"]
        self.assertEqual(record["raw_message"], "fixture send")
        self.assertEqual(record["_heredity"]["provider_id"], handle.provider_id)
        database = Path(self.temporary.name) / "connections" / "123.sqlite3"
        self.assertTrue(database.is_file())
        with closing(sqlite3.connect(database)) as connection:
            stored = json.loads(connection.execute(
                "SELECT record FROM messages WHERE self_id=123 AND message_id=-801"
            ).fetchone()[0])
        self.assertEqual(stored["raw_message"], "fixture send")
        self.assertNotIn(UPSTREAM_TOKEN, json.dumps(self.manager.snapshot()))
        self.assertEqual(
            self.manager.gateway_config(),
            {"origin": handle.gateway_origin, "access_token": handle.gateway_token},
        )
        snapshot = self.manager.snapshot()["active"]
        self.assertEqual(snapshot["origin"], self.fixture.origin)
        self.assertEqual(snapshot["gateway_origin"], handle.gateway_origin)
        self.assertTrue(snapshot["verified"])
        self.assertEqual(snapshot["event_ingress"]["healthy"], None)

    def test_standard_actions_only_run_when_explicitly_requested(self):
        handle = self.connect()
        self.assertEqual(self.fixture.send_count, 0)
        for action in ("get_friend_list", "get_group_list", "can_send_image", "can_send_record"):
            self.assertEqual(gateway_action(handle, action)["status"], "ok")
        self.assertEqual(gateway_action(handle, "get_msg", {"message_id": -900})["status"], "ok")
        for action, target in (
            ("send_private_msg", {"user_id": 456}),
            ("send_group_msg", {"group_id": 789}),
            ("send_msg", {"message_type": "private", "user_id": 456}),
        ):
            with self.subTest(action=action):
                self.assertEqual(
                    gateway_action(handle, action, {**target, "message": "fixture"})["status"], "ok"
                )
        self.assertEqual(self.fixture.send_count, 3)
        self.assertEqual(gateway_action(handle, "delete_msg", {"message_id": -801}), ok(None))
        self.assertEqual(gateway_action(handle, "unsupported_action")["status"], "failed")

    def test_write_failure_is_reported_without_automatic_retry(self):
        handle = self.connect()
        self.fixture.responses["send_private_msg"] = {
            "status": "failed", "retcode": 1502, "data": None, "message": UPSTREAM_TOKEN,
        }
        response = gateway_action(handle, "send_private_msg", {"user_id": 456, "message": "fixture"})
        self.assertEqual(response["status"], "failed")
        self.assertEqual(response["retcode"], 1502)
        self.assertNotIn(UPSTREAM_TOKEN, json.dumps(response))
        self.assertEqual(
            sum(call["action"] == "send_private_msg" for call in self.fixture.calls), 1
        )

    def test_write_rechecks_online_status_before_any_external_effect(self):
        handle = self.connect()
        self.fixture.status = {"online": False, "good": False}
        result = gateway_action(
            handle, "send_private_msg", {"user_id": 456, "message": "must not send"}
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["details"], {"action_completed": False})
        self.assertEqual(self.fixture.send_count, 0)
        self.assertFalse(any(call["action"] == "send_private_msg" for call in self.fixture.calls))

    def test_known_demo_or_changed_account_cannot_recover_old_handle_write_access(self):
        for changed in ("demo", "account"):
            with self.subTest(changed=changed):
                handle = self.connect()
                if changed == "demo":
                    self.fixture.status = {"online": True, "mode": "demo"}
                else:
                    self.fixture.identity["user_id"] = 999
                self.assertFalse(self.manager.snapshot()["active"]["verified"])
                self.fixture.status = {"online": True, "good": True}
                self.fixture.identity["user_id"] = "123"
                result = gateway_action(
                    handle, "send_private_msg", {"user_id": 456, "message": "must reconnect"}
                )
                self.assertEqual(result["status"], "failed")
                self.assertEqual(self.fixture.send_count, 0)
                self.assertFalse(any(call["action"] == "send_private_msg" for call in self.fixture.calls))

    def test_snapshot_refresh_does_not_remain_online_after_external_offline(self):
        self.connect()
        self.assertTrue(self.manager.snapshot()["active"]["online"])
        self.fixture.status = {"online": False, "good": False}
        snapshot = self.manager.snapshot()
        self.assertFalse(snapshot["active"]["online"])
        self.assertFalse(snapshot["active"]["verified"])
        self.assertIn("error", snapshot)
        self.assertTrue(snapshot["configured"])
        self.assertEqual(self.fixture.send_count, 0)

    def test_successful_replacement_closes_old_owned_resources_only(self):
        first = self.connect()
        second = self.connect()
        self.assertTrue(first._closed)
        self.assertFalse(first._thread.is_alive())
        self.assertFalse(second._closed)
        self.assertEqual(self.manager.gateway_config()["origin"], second.gateway_origin)
        self.assertEqual(self.probe()["self_id"], 123)
        self.assertEqual(self.fixture.send_count, 0)
        self.manager.disconnect()
        self.assertTrue(second._closed)
        self.assertFalse(second._thread.is_alive())
        self.assertEqual(self.manager.snapshot(), {"active": None, "configured": False})

    def test_failed_gateway_setup_cleans_new_store_and_retains_old_active(self):
        active = self.connect()
        pending = self.probe()
        with patch("heredity.connections.create_server", side_effect=OSError("fixture setup failure")):
            self.assert_error(lambda: self.manager.connect(pending["probe_id"]), "CONNECT_FAILED")
        self.assertEqual(self.manager.gateway_config()["origin"], active.gateway_origin)
        self.assertFalse(active._closed)
        self.assertEqual(gateway_action(active, "get_friend_list")["status"], "ok")
        self.manager.disconnect()
        database = Path(self.temporary.name) / "connections" / "123.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("BEGIN EXCLUSIVE")
            connection.rollback()

    def test_signed_reverse_event_is_persisted_and_event_secret_is_explicit_only(self):
        handle = self.connect()
        configuration = self.manager.event_config()
        snapshot = self.manager.snapshot()["active"]
        self.assertTrue(snapshot["event_ingress"]["enabled"])
        self.assertIsNone(snapshot["event_ingress"]["healthy"])
        self.assertNotIn(configuration["secret"], json.dumps(snapshot))
        self.assert_error(
            lambda: self.probe(origin="http://localhost:" + str(parse.urlsplit(configuration["url"]).port)),
            "CONNECTION_LOOP",
        )
        event = {
            "time": 1700000000, "self_id": 123, "post_type": "message",
            "message_type": "private", "message_id": -991, "user_id": 456,
            "message": [{"type": "text", "data": {"text": "signed fixture"}}],
        }
        raw = json.dumps(event).encode()
        outgoing = request.Request(
            configuration["url"], data=raw,
            headers={
                "Content-Type": "application/json",
                "X-Signature": "sha1=" + hmac.new(
                    configuration["secret"].encode(), raw, hashlib.sha1
                ).hexdigest(),
            },
            method="POST",
        )
        with request.urlopen(outgoing, timeout=4) as response:
            self.assertEqual(response.status, 200)
        stored = gateway_action(handle, "get_msg", {"message_id": -991})["data"]
        self.assertEqual(stored["raw_message"], "signed fixture")
        self.assertEqual(stored["_heredity"]["provider_id"], handle.provider_id)
        status = gateway_action(handle, "get_status")["data"]
        self.assertTrue(status["receive_healthy"])
        self.assertTrue(self.manager.snapshot()["active"]["event_ingress"]["healthy"])

    def test_expired_event_session_never_reveals_obsolete_configuration(self):
        handle = self.connect()
        with patch.object(
            handle._receiver, "config", side_effect=BackendError("old epoch", 1409)
        ):
            failure = self.assert_error(self.manager.event_config, "EVENT_SESSION_STALE")
        self.assertEqual(failure.status, 409)
        self.assertNotIn("secret", str(failure))

    def test_close_is_idempotent_and_refuses_later_configuration(self):
        handle = self.connect()
        self.manager.close()
        self.manager.close()
        self.assertTrue(handle._closed)
        self.assertEqual(handle.gateway_token, "")
        self.assertEqual(handle.execution_token, "")
        self.assertFalse(handle._thread.is_alive())
        self.assert_error(self.probe, "CONNECTION_CLOSED")
        self.assert_error(self.manager.gateway_config, "CONNECTION_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
