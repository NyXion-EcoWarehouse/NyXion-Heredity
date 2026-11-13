"""Dynamic OneBot routing and event transports with offline SDK plugins only."""

from dataclasses import replace
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from heredity.onebot import create_server
from heredity.plugins.gateway import PluginGatewayBackend
from heredity.plugins.host import PluginHost
from nyxion_heredity_sdk import EventEnvelope
from nyxion_heredity_sdk.example import OfflineExamplePlugin


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("local gateway state did not converge")
        threading.Event().wait(0.005)


class RecordingPlugin(OfflineExamplePlugin):
    def start(self, context, config):
        super().start(context, config)
        self.context = context
        self.session = self._session

    def publish(self, identifier, *, subtype="enable"):
        return self.context.publish_event(EventEnvelope(self.session, identifier, {
            "time": 1, "self_id": int(self.session.self_id), "post_type": "meta_event",
            "meta_event_type": "lifecycle", "sub_type": subtype,
        }))


class SecondPlugin(RecordingPlugin):
    manifest = replace(RecordingPlugin.manifest, plugin_id="example.second", platform="second")


def request(server, path):
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


class PluginGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = PluginHost(Path(self.temp.name), heartbeat_interval=0.05,
                               lease_seconds=1, control_timeout=0.5)
        self.addCleanup(self.host.close)
        self.created = {}
        self.host.register(RecordingPlugin.manifest, factory=self.factory(RecordingPlugin))
        self.host.register(SecondPlugin.manifest, factory=self.factory(SecondPlugin))
        self.gateway = PluginGatewayBackend(self.host)
        self.server = create_server("127.0.0.1", 0, "", self.gateway, enable_sse=True,
                                    heartbeat_interval=0.1)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.close_server)

    def factory(self, cls):
        def create():
            plugin = cls()
            self.created[cls.manifest.plugin_id] = plugin
            return plugin
        return create

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)

    def start(self, plugin_id="example.offline", account="123"):
        self.host.start(plugin_id, {"self_id": account})
        wait_for(lambda: any(identity.platform == self.created[plugin_id].manifest.platform
                             and identity.self_id == account
                             for identity, _ in self.host.account_runtimes()))
        return self.created[plugin_id]

    def stream(self, *, after=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        path = "/_heredity/events" + ("" if after is None else "?after=" + str(after))
        connection.request("GET", path)
        response = connection.getresponse()
        self.addCleanup(connection.close)
        return connection, response

    def test_ambiguous_accounts_never_dispatch_actions_until_selected(self):
        self.start()
        self.start("example.second", "456")
        _, state = request(self.server, "/get_status")
        self.assertEqual(state["data"]["state"], "account_selection_required")
        _, blocked = request(self.server, "/get_login_info")
        self.assertEqual((blocked["status"], blocked["retcode"]), ("failed", 1503))
        self.gateway.account = "second:456"
        _, selected = request(self.server, "/get_login_info")
        self.assertEqual(selected["data"]["user_id"], 456)
        self.gateway.account = "example:missing"
        _, missing = request(self.server, "/get_login_info")
        self.assertEqual((missing["status"], missing["retcode"]), ("failed", 1503))

    def test_listener_survives_stop_and_next_epoch_has_a_new_hub(self):
        self.start()
        old_runtime = self.gateway.event_runtime()
        old_hub = old_runtime.event_hub
        self.host.stop("example.offline")
        wait_for(lambda: self.host.catalog()["plugins"][0]["state"] == "stopped")
        self.assertTrue(old_hub.closed)
        _, state = request(self.server, "/get_status")
        self.assertFalse(state["data"]["online"])
        self.assertTrue(self.worker.is_alive())
        self.start(account="789")
        self.assertIsNot(self.gateway.event_runtime().event_hub, old_hub)
        _, identity = request(self.server, "/get_login_info")
        self.assertEqual(identity["data"]["user_id"], 789)

    def test_dynamic_event_worker_starts_before_a_plugin_is_online(self):
        self.server.start_events()
        self.assertIsNotNone(self.server._event_thread)
        self.assertTrue(self.server._event_thread.is_alive())
        self.start(account="321")
        runtime = self.gateway.event_runtime()
        wait_for(lambda: runtime.event_hub.latest_sequence > 0)
        self.assertTrue(self.server._event_thread.is_alive())

    def test_event_worker_survives_runtime_closed_between_selection_and_metadata(self):
        from nyxion_heredity_sdk import BackendError
        self.start(account="432")
        runtime = self.gateway.event_runtime()
        original = runtime.emit_meta
        raised = threading.Event()

        def racing_emit(fields):
            if threading.current_thread().name == "heredity-events" and not raised.is_set():
                raised.set()
                raise BackendError("fixture store closed during selection", 1503)
            return original(fields)

        with patch.object(runtime, "emit_meta", side_effect=racing_emit):
            self.server.start_events()
            wait_for(raised.is_set)
            wait_for(lambda: runtime.event_hub.latest_sequence > 0)
            self.assertTrue(self.server._event_thread.is_alive())

    def test_sse_header_binds_dynamic_account_not_listener_startup_identity(self):
        self.assertEqual(self.server.self_id, 0)
        plugin = self.start(account="234")
        self.assertTrue(plugin.publish("initial-event").persisted)
        _, response = self.stream(after=0)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("X-Self-ID"), "234")
        lines = []
        while len(lines) < 3:
            lines.append(response.readline().decode())
        self.assertIn('"self_id": 234', "".join(lines))

    def test_existing_sse_ends_on_epoch_close_and_new_stream_has_new_account(self):
        self.start(account="345")
        old_runtime = self.gateway.event_runtime()
        _, response = self.stream()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.readline(), b": keep-alive\n")
        self.assertEqual(response.readline(), b"\n")
        self.host.stop("example.offline")
        wait_for(lambda: old_runtime.event_hub.closed)
        self.assertEqual(response.readline(), b"")
        wait_for(lambda: self.host.catalog()["plugins"][0]["state"] == "stopped")
        self.start(account="678")
        _, fresh = self.stream()
        self.assertEqual(fresh.status, 200)
        self.assertEqual(fresh.getheader("X-Self-ID"), "678")

    def test_forward_delivery_header_uses_each_event_account_after_reselection(self):
        received = queue.Queue()

        class Receiver(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.put((self.headers.get("X-Self-ID"), payload))
                self.send_response(204)
                self.end_headers()

        receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        worker = threading.Thread(target=receiver.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(receiver.server_close)
        self.addCleanup(receiver.shutdown)
        self.server.event_urls = (f"http://127.0.0.1:{receiver.server_address[1]}/",)
        self.server.start_events()
        first = self.start(account="111")
        first.publish("first")
        seen = []

        def collect_account(account):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                header, event = received.get(timeout=max(0.01, deadline - time.monotonic()))
                seen.append((header, event))
                if event["self_id"] == account:
                    return header
            raise AssertionError("forward event did not arrive")

        self.assertEqual(collect_account(111), "111")
        self.start("example.second", "222")
        self.gateway.account = "second:222"
        self.created["example.second"].publish("second")
        self.assertEqual(collect_account(222), "222")
        self.assertTrue(all(header == str(event["self_id"]) for header, event in seen))
        self.assertTrue(self.server._event_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
