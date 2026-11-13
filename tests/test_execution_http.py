import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from heredity.backends.mock import MockBackend
from heredity.onebot import create_server
from heredity.providers.manager import ProviderManager, ProviderRegistration
from heredity.providers.registry import demo_registration, placeholder_registrations
from heredity.runtime import AccountRuntime
from heredity.storage import MessageStore
from heredity.webui import create_webui_server


ACCOUNT = 10000
CONSUMER = "fixture-consumer-token"
PROVIDER = "fixture-provider-token"
ADMIN = "fixture-execution-admin-token"
PASSWORD = "fixture-execution-ui-password"


class TrackedDemo(MockBackend):
    def __init__(self, identifier, store):
        super().__init__(
            self_id=ACCOUNT, id_factory=lambda: store.allocate_message_id(ACCOUNT)
        )
        self.identifier = identifier
        self.calls = []

    def call(self, action, params):
        self.calls.append(action)
        return super().call(action, params)


class ExecutionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = MessageStore(self.root / "account.sqlite")
        self.instances = []
        registrations = placeholder_registrations(ACCOUNT)
        for identifier in ("demo-one", "demo-two"):
            descriptor = demo_registration(
                self.store, ACCOUNT, identifier, identifier
            ).descriptor

            def factory(identifier=identifier):
                instance = TrackedDemo(identifier, self.store)
                self.instances.append(instance)
                return instance

            registrations.append(ProviderRegistration(descriptor, factory))
        self.manager = ProviderManager(ACCOUNT, registrations, "demo-one")
        self.runtime = AccountRuntime(self.manager, self.store, mode="demo")
        self.addCleanup(self.runtime.close)
        self.assertTrue(self.runtime.initialize())
        self.server = self.make_gateway()
        self.uis = []

    def start_server(self, server):
        worker = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        worker.start()
        self.addCleanup(self.close_server, server, worker)
        return server

    def close_server(self, server, worker):
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())

    def make_gateway(self, execution_token=ADMIN):
        return self.start_server(create_server(
            "127.0.0.1", 0, CONSUMER, self.runtime,
            provider_token=PROVIDER, execution_token=execution_token, enable_sse=True,
        ))

    def make_ui(self, execution_token=ADMIN, gateway=None):
        gateway = gateway or self.server
        server = create_webui_server(
            "127.0.0.1", 0, PASSWORD,
            gateway=f"http://127.0.0.1:{gateway.server_port}",
            gateway_token=CONSUMER, execution_token=execution_token,
            state_dir=self.root / f"ui-{len(self.uis)}",
        )
        self.uis.append(server)
        self.start_server(server)
        status, result = self.ui_request(
            server, "POST", "/api/login", {"password": PASSWORD}
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["success"])
        return server, result["token"]

    def request(self, method, path, value=None, *, token=CONSUMER, headers=None, server=None):
        server = server or self.server
        outgoing_headers = dict(headers or {})
        if token is not None:
            outgoing_headers.setdefault("Authorization", "Bearer " + token)
        body = None
        if value is not None:
            body = json.dumps(value, ensure_ascii=True)
            outgoing_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        try:
            connection.request(method, path, body=body, headers=outgoing_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def ui_request(self, server, method, path, value=None, token=None, headers=None):
        outgoing_headers = dict(headers or {})
        if token is not None:
            outgoing_headers["X-Heredity-Token"] = token
        return self.request(
            method, path, value, token=None, headers=outgoing_headers, server=server
        )

    def switch(self, target="demo-two", epoch=None, *, token=ADMIN, server=None):
        return self.request(
            "POST", "/_heredity/providers/switch",
            {"provider_id": target, "expected_epoch": epoch or self.manager.session_epoch},
            token=token, server=server,
        )

    def event(self, identifier=-401):
        return {
            "self_id": ACCOUNT, "time": 1700000000, "post_type": "message",
            "message_type": "private", "message_id": identifier,
            "user_id": 10001, "message": "synthetic message",
        }

    def ingest(self, identifier=-401, epoch=None, *, include_epoch=True):
        headers = {}
        if include_epoch:
            headers["X-Heredity-Session-Epoch"] = epoch or self.manager.session_epoch
        return self.request(
            "POST", "/_heredity/events", self.event(identifier),
            token=PROVIDER, headers=headers,
        )

    def send_count(self):
        return sum(
            action in {"send_private_msg", "send_group_msg", "send_msg"}
            for instance in self.instances for action in instance.calls
        )

    def open_stream(self, path="/_heredity/events"):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        connection.request("GET", path, headers={"Authorization": "Bearer " + CONSUMER})
        response = connection.getresponse()
        self.addCleanup(connection.close)
        self.addCleanup(response.close)
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("Content-Type").startswith("text/event-stream"))
        return connection, response

    def sse_frame(self, response):
        sequence = None
        event = None
        for _ in range(30):
            line = response.readline()
            if not line:
                self.fail("SSE closed before its expected event.")
            if line.startswith(b"id: "):
                sequence = int(line[4:].strip())
            elif line.startswith(b"data: "):
                event = json.loads(line[6:])
            elif line in {b"\n", b"\r\n"} and event is not None:
                return sequence, event
        self.fail("No bounded SSE frame was received.")

    def test_provider_metadata_requires_consumer_auth_and_preserves_demo_null_level(self):
        for token, expected in ((None, 401), (PROVIDER, 403), (ADMIN, 403)):
            with self.subTest(token=token):
                status, _ = self.request("GET", "/_heredity/providers", token=token)
                self.assertEqual(status, expected)
        status, model = self.request("GET", "/_heredity/providers")
        self.assertEqual(status, 200)
        self.assertEqual({item["level"] for item in model["levels"]}, {"L0", "L1", "L2", "L3"})
        self.assertTrue(model["switch_enabled"])
        self.assertTrue(model["switch_supported"])
        state = model["state"]
        self.assertEqual(set(state), {
            "state", "active_provider_id", "session_epoch", "descriptor", "providers", "in_flight"
        })
        self.assertEqual(state["active_provider_id"], "demo-one")
        self.assertEqual(state["descriptor"]["source_kind"], "demo")
        self.assertIsNone(state["descriptor"]["execution_level"])
        self.assertEqual(state["descriptor"]["level_provenance"], "none")
        native = [item for item in state["providers"] if item["source_kind"] == "native"]
        self.assertEqual(len(native), 4)
        self.assertTrue(all(not item["selectable"] for item in native))
        serialized = json.dumps(model)
        for secret in (CONSUMER, PROVIDER, ADMIN, PASSWORD):
            self.assertNotIn(secret, serialized)
        self.assertEqual(len(self.instances), 1)

    def test_switch_requires_admin_bearer_and_never_accepts_consumer_provider_or_query(self):
        epoch = self.manager.session_epoch
        cases = [
            (CONSUMER, "/_heredity/providers/switch", {}, 403),
            (PROVIDER, "/_heredity/providers/switch", {}, 403),
            (None, "/_heredity/providers/switch", {}, 401),
            (None, "/_heredity/providers/switch?access_token=" + ADMIN, {}, 401),
            (None, "/_heredity/providers/switch", {"Authorization": "Basic " + ADMIN}, 403),
        ]
        for token, path, headers, expected in cases:
            with self.subTest(path=path, token=token, headers=headers):
                status, result = self.request(
                    "POST", path, {"provider_id": "demo-two", "expected_epoch": epoch},
                    token=token, headers=headers,
                )
                self.assertEqual(status, expected)
                self.assertEqual(result["status"], "failed")
        self.assertEqual(self.manager.session_epoch, epoch)
        self.assertEqual(self.manager.active_descriptor.provider_id, "demo-one")
        self.assertEqual(len(self.instances), 1)

    def test_gateway_without_admin_channel_reports_disabled_and_returns_404(self):
        gateway = self.make_gateway(execution_token="")
        status, model = self.request("GET", "/_heredity/providers", server=gateway)
        self.assertEqual(status, 200)
        self.assertFalse(model["switch_enabled"])
        status, result = self.switch(server=gateway)
        self.assertEqual(status, 404)
        self.assertEqual(result["retcode"], 1404)
        self.assertEqual(len(self.instances), 1)

    def test_configured_consumer_provider_and_admin_tokens_must_differ(self):
        for consumer, provider, admin in (
            (CONSUMER, PROVIDER, CONSUMER),
            (CONSUMER, PROVIDER, PROVIDER),
            (CONSUMER, CONSUMER, ADMIN),
        ):
            with self.subTest(consumer=consumer, provider=provider, admin=admin):
                created = None
                try:
                    with self.assertRaises(ValueError):
                        created = create_server(
                            "127.0.0.1", 0, consumer, self.runtime,
                            provider_token=provider, execution_token=admin,
                        )
                finally:
                    if created is not None:
                        created.server_close()

    def test_admin_switch_returns_panel_shape_and_does_not_replay_demo_actions(self):
        status, sent = self.request(
            "POST", "/send_private_msg", {"user_id": 10001, "message": "first once"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(sent["status"], "ok")
        first_id = sent["data"]["message_id"]
        epoch = self.manager.session_epoch
        status, result = self.switch(epoch=epoch)
        self.assertEqual(status, 200)
        self.assertEqual(set(result), {"state", "changed"})
        self.assertTrue(result["changed"])
        self.assertEqual(result["state"]["active_provider_id"], "demo-two")
        self.assertNotEqual(result["state"]["session_epoch"], epoch)
        self.assertEqual(self.runtime.mode, "demo")
        self.assertIsNone(self.runtime.status()["execution_level"])
        self.assertEqual(self.send_count(), 1)
        status, second = self.request(
            "POST", "/send_private_msg", {"user_id": 10001, "message": "second once"}
        )
        self.assertEqual(status, 200)
        self.assertGreater(second["data"]["message_id"], first_id)
        self.assertEqual(self.send_count(), 2)
        status, noop = self.switch("demo-two")
        self.assertEqual(status, 200)
        self.assertFalse(noop["changed"])
        self.assertEqual(len(self.instances), 2)

    def test_stale_switch_epoch_is_409_with_current_epoch_and_no_factory_or_send_retry(self):
        epoch = self.manager.session_epoch
        self.assertEqual(self.switch(epoch=epoch)[0], 200)
        current = self.manager.session_epoch
        before = self.send_count()
        status, result = self.switch("demo-one", epoch)
        self.assertEqual(status, 409)
        self.assertEqual(result["retcode"], 1409)
        self.assertEqual(result["details"], {"session_epoch": current})
        self.assertEqual(self.manager.active_descriptor.provider_id, "demo-two")
        self.assertEqual(self.send_count(), before)
        self.assertEqual(len(self.instances), 2)

    def test_managed_ingress_requires_matching_epoch_before_database_and_publication(self):
        for include, epoch in ((False, None), (True, "old-session")):
            with self.subTest(include=include, epoch=epoch):
                status, result = self.ingest(include_epoch=include, epoch=epoch)
                self.assertEqual(status, 409)
                self.assertEqual(result["retcode"], 1409)
        self.assertEqual(self.store.events_after(ACCOUNT), [])
        self.assertIsNone(self.store.get_message(ACCOUNT, -401))
        self.assertEqual(self.runtime.event_hub.latest_sequence, 0)
        status, result = self.ingest()
        self.assertEqual(status, 200)
        self.assertTrue(result["data"]["persisted"])
        self.assertEqual(len(self.store.events_after(ACCOUNT)), 1)
        self.assertEqual(self.runtime.event_hub.latest_sequence, result["data"]["sequence"])

    def test_old_epoch_events_after_switch_do_not_write_or_publish_into_new_session(self):
        previous = self.manager.session_epoch
        self.assertEqual(self.switch()[0], 200)
        current = self.manager.session_epoch
        old_records = self.store.events_after(ACCOUNT)
        old_sequence = self.runtime.event_hub.latest_sequence
        status, result = self.ingest(-402, previous)
        self.assertEqual(status, 409)
        self.assertEqual(result["retcode"], 1409)
        self.assertEqual(self.store.events_after(ACCOUNT), old_records)
        self.assertEqual(self.runtime.event_hub.latest_sequence, old_sequence)
        self.assertIsNone(self.store.get_message(ACCOUNT, -402))
        status, result = self.ingest(-403, current)
        self.assertEqual(status, 200)
        self.assertTrue(result["data"]["persisted"])
        record = self.store.get_message(ACCOUNT, -403)
        self.assertEqual(record["_heredity"], {
            "provider_id": "demo-two", "session_epoch": current
        })

    def test_switch_ends_old_sse_and_new_stream_can_receive_current_lifecycle(self):
        _, old_response = self.open_stream()
        self.assertTrue(old_response.readline().startswith(b":"))
        self.assertIn(old_response.readline(), (b"\n", b"\r\n"))
        previous_hub = self.runtime.event_hub
        self.assertEqual(self.switch()[0], 200)
        self.assertTrue(previous_hub.closed)
        self.assertEqual(old_response.read(), b"")
        _, response = self.open_stream("/_heredity/events?after=0")
        sequence, event = self.sse_frame(response)
        self.assertGreater(sequence, 0)
        self.assertEqual(event["self_id"], ACCOUNT)
        self.assertEqual(event["post_type"], "meta_event")
        self.assertEqual(event["meta_event_type"], "lifecycle")
        self.assertEqual(event["sub_type"], "enable")
        self.assertEqual(event["_heredity"]["session_epoch"], self.manager.session_epoch)

    def test_webui_execution_model_requires_session_auth_and_matches_panel_contract(self):
        ui, token = self.make_ui()
        for supplied in (None, "invalid-session", CONSUMER, ADMIN):
            with self.subTest(token=supplied):
                status, result = self.ui_request(
                    ui, "GET", "/api/heredity/execution", token=supplied
                )
                self.assertEqual(status, 401)
                self.assertFalse(result["success"])
        status, model = self.ui_request(
            ui, "GET", "/api/heredity/execution", token=token
        )
        self.assertEqual(status, 200)
        self.assertEqual(set(model), {"levels", "state", "canSwitch"})
        self.assertTrue(model["canSwitch"])
        self.assertEqual(model["state"]["active_provider_id"], "demo-one")
        self.assertIsNone(model["state"]["descriptor"]["execution_level"])

    def test_webui_switch_uses_admin_channel_not_consumer_gateway_token(self):
        ui, token = self.make_ui()
        observed = []
        original = ui.state.gateway.opener.open

        def observe(outgoing, *args, **kwargs):
            observed.append((outgoing.get_method(), outgoing.get_header("Authorization")))
            return original(outgoing, *args, **kwargs)

        with patch.object(ui.state.gateway.opener, "open", side_effect=observe):
            self.assertEqual(self.ui_request(
                ui, "GET", "/api/heredity/execution", token=token
            )[0], 200)
            status, result = self.ui_request(
                ui, "POST", "/api/heredity/execution/switch",
                {"provider_id": "demo-two", "expected_epoch": self.manager.session_epoch},
                token=token,
            )
        self.assertEqual(status, 200)
        self.assertTrue(result["changed"])
        self.assertEqual(result["state"]["active_provider_id"], "demo-two")
        self.assertEqual(observed, [
            ("GET", "Bearer " + CONSUMER), ("POST", "Bearer " + ADMIN)
        ])
        self.assertEqual(self.send_count(), 0)

    def test_webui_preserves_stale_epoch_409_instead_of_reporting_success(self):
        ui, token = self.make_ui()
        previous = self.manager.session_epoch
        self.assertEqual(self.switch()[0], 200)
        status, result = self.ui_request(
            ui, "POST", "/api/heredity/execution/switch",
            {"provider_id": "demo-one", "expected_epoch": previous}, token=token,
        )
        self.assertEqual(status, 409)
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "EXECUTION_CONFLICT")
        self.assertNotIn("changed", result)
        self.assertEqual(self.manager.active_descriptor.provider_id, "demo-two")
        self.assertEqual(len(self.instances), 2)

    def test_webui_unavailable_native_target_returns_503_without_claiming_success(self):
        ui, token = self.make_ui()
        previous = self.manager.session_epoch
        status, result = self.ui_request(
            ui, "POST", "/api/heredity/execution/switch",
            {"provider_id": "l1-attached", "expected_epoch": previous}, token=token,
        )
        self.assertEqual(status, 503)
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "EXECUTION_ERROR")
        self.assertNotIn("changed", result)
        self.assertEqual(self.manager.session_epoch, previous)
        self.assertEqual(self.manager.active_descriptor.provider_id, "demo-one")
        self.assertEqual(len(self.instances), 1)

    def test_webui_can_switch_requires_both_configured_admin_channels(self):
        ui, token = self.make_ui(execution_token="")
        status, model = self.ui_request(
            ui, "GET", "/api/heredity/execution", token=token
        )
        self.assertEqual(status, 200)
        self.assertFalse(model["canSwitch"])
        status, failure = self.ui_request(
            ui, "POST", "/api/heredity/execution/switch",
            {"provider_id": "demo-two", "expected_epoch": self.manager.session_epoch},
            token=token,
        )
        self.assertEqual(status, 503)
        self.assertFalse(failure["success"])
        self.assertEqual(failure["code"], "EXECUTION_DISABLED")
        disabled_gateway = self.make_gateway(execution_token="")
        configured_ui, configured_token = self.make_ui(gateway=disabled_gateway)
        status, model = self.ui_request(
            configured_ui, "GET", "/api/heredity/execution", token=configured_token
        )
        self.assertEqual(status, 200)
        self.assertFalse(model["canSwitch"])
        self.assertEqual(len(self.instances), 1)


if __name__ == "__main__":
    unittest.main()
