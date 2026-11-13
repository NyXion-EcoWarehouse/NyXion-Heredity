"""Real localhost WebSocket traffic; no platform plugins or external services."""

from concurrent.futures import ThreadPoolExecutor
import json
import queue
import socket
import threading
import time
import unittest
from unittest.mock import patch

from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect
from websockets.sync.server import serve

from heredity.network_websocket import (
    WebSocketClientTransport, WebSocketServerTransport, WebSocketTransportError,
)
from nyxion_heredity_sdk import BackendError


def wait_for(check, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.005)
    raise AssertionError("local WebSocket state did not converge")


def result(message):
    return {"status": "ok", "retcode": 0, "data": message.get("params", {})}


class WebSocketTransportTests(unittest.TestCase):
    def listener(self, **options):
        messages = []
        defaults = dict(host="127.0.0.1", port=0, path="/onebot", on_action=result,
                        on_message=lambda kind, message: messages.append((kind, message)))
        transport = WebSocketServerTransport(**(defaults | options))
        transport.start()
        self.addCleanup(transport.stop)
        return transport, messages

    def connector(self, url, **options):
        messages = []
        defaults = dict(url=url, reconnect=False,
                        on_message=lambda kind, message: messages.append((kind, message)))
        transport = WebSocketClientTransport(**(defaults | options))
        transport.start()
        self.addCleanup(transport.stop)
        return transport, messages

    def test_server_binds_configured_path_and_correlates_action_echo(self):
        server, observed = self.listener()
        with connect(server.endpoint, proxy=None) as client:
            client.send(json.dumps({"action": "get_login_info", "params": {"x": 7},
                                    "echo": {"client": 1}}))
            response = json.loads(client.recv(timeout=2))
            self.assertEqual(response, {"status": "ok", "retcode": 0,
                                        "data": {"x": 7}, "echo": {"client": 1}})
        wait_for(lambda: len([kind for kind, _ in observed if kind == "response"]) == 1)
        self.assertEqual([kind for kind, _ in observed].count("action"), 1)
        self.assertEqual(server.snapshot()["state"], "running")
        self.assertGreater(server.actual_port, 0)

    def test_server_accepts_header_or_query_token_and_rejects_wrong_credentials(self):
        server, _ = self.listener(token="local-auth-token")
        with self.assertRaises(InvalidStatus) as denied:
            connect(server.endpoint, proxy=None)
        self.assertEqual(denied.exception.response.status_code, 401)
        with self.assertRaises(InvalidStatus):
            connect(server.endpoint, additional_headers={"Authorization": "Bearer wrong"}, proxy=None)
        with connect(server.endpoint, additional_headers={
                "Authorization": "Bearer local-auth-token"}, proxy=None) as client:
            client.send(json.dumps({"action": "echo", "echo": "header"}))
            self.assertEqual(json.loads(client.recv(timeout=2))["echo"], "header")
        with connect(server.endpoint + "?access_token=local-auth-token", proxy=None) as client:
            client.send(json.dumps({"action": "echo", "echo": "query"}))
            self.assertEqual(json.loads(client.recv(timeout=2))["echo"], "query")
        with self.assertRaises(InvalidStatus) as wrong_path:
            connect(server.endpoint.replace("/onebot", "/wrong"), proxy=None,
                    additional_headers={"Authorization": "Bearer local-auth-token"})
        self.assertEqual(wrong_path.exception.response.status_code, 404)

    def test_server_broadcasts_only_events_not_action_responses(self):
        server, observed = self.listener()
        with connect(server.endpoint, proxy=None) as first, connect(server.endpoint, proxy=None) as second:
            wait_for(lambda: server.snapshot()["connected_peers"] == 2)
            first.send(json.dumps({"action": "echo", "params": {"only": "first"}, "echo": 3}))
            self.assertEqual(json.loads(first.recv(timeout=2))["data"], {"only": "first"})
            with self.assertRaises(TimeoutError):
                second.recv(timeout=0.05)
            event = {"post_type": "message", "message": "local synthetic event", "self_id": 123}
            server.publish(event)
            self.assertEqual(json.loads(first.recv(timeout=2)), event)
            self.assertEqual(json.loads(second.recv(timeout=2)), event)
        self.assertEqual([kind for kind, _ in observed].count("event"), 1)

    def test_server_reports_inbound_event_and_recovers_from_bad_json(self):
        server, observed = self.listener()
        with connect(server.endpoint, proxy=None) as client:
            for invalid in ("[]", "{", '{"action":"echo","action":"second"}'):
                client.send(invalid)
                self.assertEqual(json.loads(client.recv(timeout=2))["status"], "failed")
            client.send(json.dumps({"post_type": "meta_event", "time": 1, "self_id": 123}))
            wait_for(lambda: any(kind == "event" for kind, _ in observed))
            client.send(json.dumps({"action": "echo", "echo": "after-invalid"}))
            self.assertEqual(json.loads(client.recv(timeout=2))["echo"], "after-invalid")

    def test_client_calls_remote_with_unique_echo_and_receives_events_once(self):
        server, _ = self.listener(token="local-auth-token")
        client, observed = self.connector(server.endpoint, token="local-auth-token")
        self.assertEqual(client.snapshot()["state"], "running")
        response = client.call_remote("get_status", {"request": "local"}, timeout=2)
        self.assertEqual(response["data"], {"request": "local"})
        self.assertTrue(response["echo"].startswith("nyxion-"))
        self.assertEqual([kind for kind, _ in observed].count("action"), 1)
        self.assertEqual([kind for kind, _ in observed].count("response"), 1)
        event = {"post_type": "meta_event", "self_id": 123, "time": 1}
        server.publish(event)
        wait_for(lambda: any(kind == "event" for kind, _ in observed))
        self.assertEqual([value for kind, value in observed if kind == "event"], [event])

    def test_client_publish_owns_queued_payload_and_does_not_duplicate_bus_event(self):
        server, inbound = self.listener()
        client, outgoing = self.connector(server.endpoint)
        event = {"post_type": "message", "message": [{"type": "text", "data": {"text": "original"}}]}
        client.publish(event)
        event["message"][0]["data"]["text"] = "modified"
        wait_for(lambda: any(kind == "event" for kind, _ in inbound))
        delivered = next(value for kind, value in inbound if kind == "event")
        self.assertEqual(delivered["message"][0]["data"]["text"], "original")
        wait_for(lambda: any(kind == "event" for kind, _ in outgoing))
        self.assertEqual([kind for kind, _ in outgoing].count("event"), 1)

    def test_client_reverse_connection_handles_remote_actions_and_sets_role_headers(self):
        observed_headers = queue.Queue()
        received_response = queue.Queue()
        def handler(connection):
            observed_headers.put(dict(connection.request.headers))
            connection.send(json.dumps({"action": "get_status", "params": {"reverse": True},
                                        "echo": "reverse-local"}))
            received_response.put(json.loads(connection.recv(timeout=2)))
        server = serve(handler, "127.0.0.1", 0, open_timeout=2, close_timeout=1, compression=None)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        url = f"ws://127.0.0.1:{server.socket.getsockname()[1]}"
        client, observed = self.connector(url, on_action=result, self_id=lambda: "12345678")
        response = received_response.get(timeout=2)
        headers = {key.lower(): value for key, value in observed_headers.get(timeout=2).items()}
        self.assertEqual(headers["x-client-role"], "Universal")
        self.assertEqual(headers["x-self-id"], "12345678")
        self.assertEqual(response["echo"], "reverse-local")
        self.assertTrue(response["data"]["reverse"])
        wait_for(lambda: [kind for kind, _ in observed].count("response") == 1)
        client.stop()
        server.shutdown()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def test_client_disconnect_marks_pending_action_unknown_without_replay(self):
        calls = []
        def handler(connection):
            calls.append(json.loads(connection.recv(timeout=2)))
            connection.close()
        server = serve(handler, "127.0.0.1", 0, open_timeout=2, close_timeout=1)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        client, _ = self.connector(f"ws://127.0.0.1:{server.socket.getsockname()[1]}")
        with self.assertRaises(BackendError) as error:
            client.call_remote("send_group_msg", {"message": "local-only"}, timeout=2)
        self.assertEqual(error.exception.details["action_outcome"], "unknown")
        self.assertFalse(error.exception.details["retry_safe"])
        self.assertEqual(len(calls), 1)
        client.stop()
        server.shutdown()
        thread.join(timeout=2)

    def test_client_response_deadline_does_not_resend_action(self):
        seen = []
        release = threading.Event()
        def handler(connection):
            seen.append(json.loads(connection.recv(timeout=2)))
            release.wait(2)
        server = serve(handler, "127.0.0.1", 0, close_timeout=1)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(release.set)
        self.addCleanup(server.shutdown)
        client, _ = self.connector(f"ws://127.0.0.1:{server.socket.getsockname()[1]}")
        with self.assertRaises(BackendError) as error:
            client.call_remote("send_private_msg", {"message": "local-only"}, timeout=0.05)
        self.assertEqual(error.exception.retcode, 1504)
        self.assertEqual(error.exception.details["action_outcome"], "unknown")
        self.assertEqual(len(seen), 1)
        release.set()
        client.stop()
        server.shutdown()
        thread.join(timeout=2)

    def test_client_failure_is_sanitized_and_disconnected_call_was_not_sent(self):
        server, _ = self.listener(token="never-log-this-token")
        client, _ = self.connector(server.endpoint + "?secret=private-url", token="wrong")
        self.assertEqual(client.snapshot()["state"], "error")
        rendered = json.dumps(client.snapshot())
        self.assertNotIn("private-url", rendered)
        self.assertNotIn("never-log-this-token", rendered)
        self.assertNotIn("wrong", rendered)
        with self.assertRaises(BackendError) as error:
            client.call_remote("echo", {}, timeout=1)
        self.assertFalse(error.exception.details["action_completed"])

    def test_stop_releases_listener_and_connector_threads(self):
        server, _ = self.listener()
        client, _ = self.connector(server.endpoint)
        port = server.actual_port
        client.stop()
        server.stop()
        self.assertFalse(client._thread.is_alive())
        self.assertFalse(server._thread.is_alive())
        self.assertEqual(server.snapshot()["connected_peers"], 0)
        self.assertEqual(server.snapshot()["state"], "stopped")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))

    def test_client_reconnect_restores_only_connection_not_an_old_action(self):
        seen = []
        connection_count = []
        release = threading.Event()
        def handler(connection):
            connection_count.append(connection)
            if len(connection_count) == 1:
                seen.append(json.loads(connection.recv(timeout=2)))
                connection.close()
                return
            connection.send(json.dumps({"post_type": "meta_event", "time": 2, "self_id": 123}))
            release.wait(2)
        server = serve(handler, "127.0.0.1", 0, close_timeout=1)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(release.set)
        self.addCleanup(server.shutdown)
        client, observed = self.connector(
            f"ws://127.0.0.1:{server.socket.getsockname()[1]}", reconnect=True)
        with self.assertRaises(BackendError):
            client.call_remote("send_private_msg", {"message": "local only"}, timeout=2)
        wait_for(lambda: len(connection_count) >= 2)
        wait_for(lambda: any(kind == "event" for kind, _ in observed))
        self.assertEqual(len(seen), 1)
        self.assertEqual(client.snapshot()["state"], "running")
        release.set()
        client.stop()
        server.shutdown()
        thread.join(timeout=2)

    def test_server_slow_peer_queue_closes_only_that_peer(self):
        server, observed = self.listener(queue_size=1)
        client = connect(server.endpoint, proxy=None, legacy=True)
        self.addCleanup(client.close)
        wait_for(lambda: len(server._peers) == 1)
        peer = next(iter(server._peers))
        write_entered, release = threading.Event(), threading.Event()
        original_send = peer.connection.send
        def delayed_send(value):
            write_entered.set()
            release.wait(2)
            return original_send(value)
        self.addCleanup(release.set)
        with patch.object(peer.connection, "send", side_effect=delayed_send):
            server.publish({"post_type": "meta_event", "time": 1})
            self.assertTrue(write_entered.wait(1))
            server.publish({"post_type": "meta_event", "time": 2})
            server.publish({"post_type": "meta_event", "time": 3})
            self.assertTrue(peer.stop.is_set())
            self.assertEqual(server.snapshot()["state"], "running")
            release.set()
        wait_for(lambda: server.snapshot()["connected_peers"] == 0)
        self.assertEqual([kind for kind, _ in observed].count("event"), 3)

    def test_client_deadline_is_bounded_even_if_socket_writer_blocks(self):
        server, _ = self.listener()
        client, _ = self.connector(server.endpoint)
        connection = client._connection
        original_send = connection.send
        entered, release = threading.Event(), threading.Event()
        def delayed_send(message):
            entered.set()
            release.wait(2)
            return original_send(message)
        self.addCleanup(release.set)
        with patch.object(connection, "send", side_effect=delayed_send):
            started = time.monotonic()
            with self.assertRaises(BackendError) as error:
                client.call_remote("send_private_msg", {"message": "local only"}, timeout=0.05)
            self.assertTrue(entered.is_set())
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(error.exception.details["action_outcome"], "unknown")
            release.set()
        client.stop()
        self.assertFalse(client._thread.is_alive())

    def test_client_requires_well_formed_onebot_response(self):
        def handler(connection):
            request = json.loads(connection.recv(timeout=2))
            connection.send(json.dumps({"status": "ok", "echo": request["echo"]}))
        server = serve(handler, "127.0.0.1", 0, close_timeout=1)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        client, _ = self.connector(f"ws://127.0.0.1:{server.socket.getsockname()[1]}")
        with self.assertRaises(BackendError) as error:
            client.call_remote("echo", {}, timeout=2)
        self.assertEqual(error.exception.details["action_outcome"], "unknown")
        client.stop()
        server.shutdown()
        thread.join(timeout=2)

    def test_invalid_configuration_never_opens_a_listener_or_connector(self):
        for options in ({"host": "", "port": 1}, {"host": "127.0.0.1", "port": True},
                        {"host": "127.0.0.1", "port": 0, "path": "missing-slash"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                WebSocketServerTransport(**options)
        for url in ("http://127.0.0.1", "ws://user:secret@localhost", "ws://localhost/#bad",
                    "ws://127.0.0.1:99999"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                WebSocketClientTransport(url=url)


if __name__ == "__main__":
    unittest.main()
