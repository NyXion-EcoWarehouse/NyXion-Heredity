from dataclasses import replace
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from nyxion_heredity_sdk import (
    ActionResult, BackendError, Challenge, ContractError, EventEnvelope,
    PluginHello, PluginManifest, Pong, SessionIdentity, SessionUpdate,
)
from heredity.plugins.host import PluginHost, PluginHostError


ACCOUNT = "10000"


class LocalPlugin:
    """A deterministic SDK platform fixture; it never creates network sockets."""

    def __init__(self, *, plugin_id="test.local", mode="online"):
        self.manifest = PluginManifest(
            plugin_id=plugin_id, name="Local contract fixture", version="1.0.0",
            platform="example",
            actions=("get_login_info", "echo", "send_group_msg"),
            config_schema={"type": "object", "properties": {}, "additionalProperties": False},
            capabilities=("synthetic",))
        self.mode = mode
        self.hello = self.context = self.session = None
        self.started = False
        self.stopped = threading.Event()
        self.ping_entered = threading.Event()
        self.ping_release = threading.Event()
        self.calls = []
        self.pings = []
        self.stop_calls = []
        self.action_outcome = None
        self.action_delay = 0
        self.stop_entered = threading.Event()
        self.stop_release = threading.Event()

    def handshake(self, hello):
        self.hello = hello
        nonce = "wrong-nonce" if self.mode == "wrong_nonce" else hello.nonce
        major = 2 if self.mode == "wrong_version" else 1
        return PluginHello(hello.instance_id, nonce, self.manifest.plugin_id, major, 0)

    def start(self, context, config):
        self.started = True
        self.context = context
        if self.mode == "start_error":
            raise RuntimeError("private-native-token")
        if self.mode == "login_required":
            context.update_session(SessionUpdate(None, "login_required"))
            context.publish_challenge(Challenge(
                "login-qr", "qr", "Scan login code",
                {"image": "private-qr-material"}, expires_at=time.time() + 60))
            return
        self.session = SessionIdentity(
            self.manifest.platform, ACCOUNT, "epoch-" + context.instance_id)
        if self.mode == "wrong_platform":
            self.session = replace(self.session, platform="different")
        context.update_session(SessionUpdate(self.session, "online"))

    def ping(self, heartbeat):
        self.ping_entered.set()
        self.pings.append(heartbeat)
        if self.mode == "blocked_ping":
            self.ping_release.wait(2)
        if self.mode == "ping_error":
            raise RuntimeError("private-native-token")
        nonce = "wrong-pong" if self.mode == "wrong_pong" else heartbeat.nonce
        return Pong(heartbeat.instance_id, nonce, heartbeat.sequence)

    def call(self, request):
        self.calls.append(request)
        if self.action_delay:
            time.sleep(self.action_delay)
        if request.action == "get_login_info":
            account = int(ACCOUNT) + (1 if self.mode == "wrong_account" else 0)
            return ActionResult(request.request_id, "completed",
                                {"user_id": account, "nickname": "Local fixture"})
        if self.action_outcome is not None:
            return ActionResult(
                request.request_id, self.action_outcome,
                {"message_id": 42} if self.action_outcome == "completed" else None,
                error_code=1502, error_message="fixture failure")
        request_id = "wrong-result" if self.mode == "wrong_result" else request.request_id
        return ActionResult(
            request_id, "completed",
            {"message_id": 42} if request.action == "send_group_msg" else request.params)

    def stop(self, reason):
        self.stop_calls.append(reason)
        self.stop_entered.set()
        if self.mode == "stop_error":
            raise RuntimeError("private-stop-token")
        if self.mode == "blocked_stop":
            self.stop_release.wait(2)
        self.ping_release.set()
        self.stopped.set()


def event(session, *, event_id="event-1", self_id=int(ACCOUNT), subtype="enable"):
    return EventEnvelope(session, event_id, {
        "time": 1, "self_id": self_id, "post_type": "meta_event",
        "meta_event_type": "lifecycle", "sub_type": subtype,
    })


class PluginHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="NyXion host tests ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hosts = []
        self.addCleanup(self.close_hosts)
        self.network = patch("socket.create_connection", side_effect=AssertionError("network"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def close_hosts(self):
        for host in self.hosts:
            host.close()

    def host(self, **options):
        value = PluginHost(
            self.root / str(len(self.hosts)), heartbeat_interval=0.02,
            lease_seconds=0.15, control_timeout=0.1, **options)
        self.hosts.append(value)
        return value

    def wait(self, predicate, *, timeout=2, message="condition did not become true"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.005)
        self.fail(message)

    def snapshot(self, host, identifier="test.local"):
        return next(item for item in host.catalog()["plugins"] if item["id"] == identifier)

    def attach(self, host, *, plugin_id="test.local", mode="online"):
        created = []
        declaration = LocalPlugin(plugin_id=plugin_id, mode=mode).manifest

        def factory():
            plugin = LocalPlugin(plugin_id=plugin_id, mode=mode)
            created.append(plugin)
            return plugin

        host.register(declaration, factory=factory)
        host.start(plugin_id, {})
        self.wait(lambda: created)
        return created

    def online(self, host, *, plugin_id="test.local", mode="online"):
        created = self.attach(host, plugin_id=plugin_id, mode=mode)
        self.wait(lambda: self.snapshot(host, plugin_id)["state"] == "running")
        self.wait(lambda: host.account_runtimes())
        return created[0], host.account_runtimes()[0][1]

    def test_discovery_and_catalog_do_not_create_or_import_plugins(self):
        host = self.host()
        manifest = LocalPlugin().manifest
        with patch("importlib.import_module", side_effect=AssertionError("import")):
            host.register(manifest, factory=lambda: self.fail("factory called during discovery"))
            catalog = host.catalog()
        self.assertEqual(catalog["plugins"][0]["state"], "discovered")
        self.assertFalse(catalog["plugins"][0]["handshake"]["verified"])
        self.assertEqual(host.account_runtimes(), [])

    def test_invalid_api_and_conflicting_registration_fail_before_factory(self):
        host = self.host()
        manifest = LocalPlugin().manifest
        with self.assertRaises(PluginHostError) as error:
            host.register(replace(manifest, api_major=2),
                          factory=lambda: self.fail("incompatible factory"))
        self.assertEqual(error.exception.code, "PLUGIN_API_MISMATCH")
        host.register(manifest, factory=LocalPlugin)
        with self.assertRaises(PluginHostError):
            host.register(manifest, factory=LocalPlugin)
        with self.assertRaises(PluginHostError):
            host.register(replace(manifest, plugin_id="test.other"),
                          entrypoint="module:factory;code")

    def test_online_identity_and_heartbeat_use_real_sdk_boundary(self):
        host = self.host()
        plugin, runtime = self.online(host)
        self.wait(lambda: len(plugin.pings) >= 2)
        report = self.snapshot(host)
        self.assertTrue(report["handshake"]["verified"])
        self.assertTrue(report["heartbeat"]["healthy"])
        self.assertGreaterEqual(report["heartbeat"]["sequence"], 2)
        self.assertEqual(runtime.call("get_login_info", {})["user_id"], int(ACCOUNT))
        self.assertEqual(runtime.call("echo", {"nested": [1, 2]}), {"nested": [1, 2]})
        self.assertEqual(runtime.status()["session_epoch"], plugin.session.session_epoch)
        self.assertEqual(runtime.status()["provider_id"], plugin.manifest.plugin_id)
        self.assertEqual(plugin.pings[0].instance_id, plugin.context.instance_id)
        self.assertNotEqual(plugin.pings[0].nonce, plugin.pings[1].nonce)

    def test_configuration_errors_do_not_start_factory(self):
        host = self.host()
        host.register(LocalPlugin().manifest,
                      factory=lambda: self.fail("invalid configuration started plugin"))
        with self.assertRaises(ContractError):
            host.start("test.local", {"unknown": "secret"})
        self.assertEqual(self.snapshot(host)["state"], "discovered")

    def test_wrong_handshake_nonce_version_or_platform_never_routes_account(self):
        for mode in ("wrong_nonce", "wrong_version", "wrong_platform"):
            with self.subTest(mode=mode):
                host = self.host()
                plugin = self.attach(host, mode=mode)[0]
                self.wait(lambda: self.snapshot(host)["state"] == "failed")
                self.assertEqual(host.account_runtimes(), [])
                self.wait(plugin.stopped.is_set)
                self.assertEqual(plugin.calls, [])

    def test_loaded_manifest_must_match_reviewed_declaration(self):
        host = self.host()
        declaration = LocalPlugin().manifest
        plugin = LocalPlugin()
        plugin.manifest = replace(declaration, actions=("get_login_info",))
        host.register(declaration, factory=lambda: plugin)
        host.start("test.local", {})
        self.wait(lambda: self.snapshot(host)["state"] == "failed")
        self.assertFalse(plugin.started)
        self.assertEqual(host.account_runtimes(), [])

    def test_login_required_plugin_can_be_healthy_without_online_account(self):
        host = self.host()
        plugin = self.attach(host, mode="login_required")[0]
        self.wait(lambda: self.snapshot(host)["state"] == "running")
        self.wait(lambda: len(plugin.pings) >= 1)
        report = self.snapshot(host)
        self.assertTrue(report["heartbeat"]["healthy"])
        self.assertEqual(report["session"]["state"], "login_required")
        self.assertEqual(host.account_runtimes(), [])
        self.assertNotIn("private-qr-material", json.dumps(host.catalog()))
        self.assertEqual(host.challenge("test.local")["payload"]["image"], "private-qr-material")
        host.stop("test.local")
        self.wait(plugin.stopped.is_set)
        self.assertIsNone(host.challenge("test.local"))

    def test_expired_challenge_is_not_served(self):
        host = self.host()
        plugin = self.attach(host, mode="login_required")[0]
        self.wait(lambda: plugin.context is not None)
        plugin.context.publish_challenge(Challenge(
            "expired", "qr", "Expired", {"secret": "expired-code"}, time.time() - 1))
        self.assertIsNone(host.challenge("test.local"))

    def test_wrong_account_identity_fails_even_after_online_announcement(self):
        host = self.host()
        plugin = self.attach(host, mode="wrong_account")[0]
        self.wait(lambda: self.snapshot(host)["state"] == "failed")
        self.assertEqual(host.account_runtimes(), [])
        self.wait(plugin.stopped.is_set)

    def test_two_plugins_cannot_own_same_platform_account(self):
        host = self.host()
        first, runtime = self.online(host, plugin_id="test.first")
        second = self.attach(host, plugin_id="test.second")[0]
        self.wait(lambda: self.snapshot(host, "test.second")["state"] == "failed")
        self.assertEqual(len(host.account_runtimes()), 1)
        self.assertTrue(runtime.online)
        self.assertEqual(runtime.call("get_login_info", {})["user_id"], int(ACCOUNT))
        self.assertEqual(first.stop_calls, [])
        self.wait(second.stopped.is_set)

    def test_valid_event_is_durably_committed_before_ack_and_duplicate_is_idempotent(self):
        host = self.host()
        plugin, runtime = self.online(host)
        envelope = event(plugin.session)
        first = plugin.context.publish_event(envelope)
        second = plugin.context.publish_event(envelope)
        self.assertTrue(first.persisted)
        self.assertFalse(first.duplicate)
        self.assertTrue(second.persisted)
        self.assertTrue(second.duplicate)
        records = runtime.store.events_after(int(ACCOUNT))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["event"]["_heredity"]["session_epoch"],
                         plugin.session.session_epoch)
        self.assertEqual(len(runtime.event_hub.read(0)), 1)
        path = (host.state_dir / "accounts" / "example" / ACCOUNT
                / "test.local" / "onebot.sqlite3")
        with closing(sqlite3.connect(path)) as database:
            self.assertEqual(database.execute("SELECT count(*) FROM events").fetchone()[0], 1)

    def test_event_persistence_failure_does_not_ack_or_fake_receipt(self):
        host = self.host()
        plugin, runtime = self.online(host)
        with patch.object(runtime.store, "commit_event", side_effect=BackendError("disk full", 1501)):
            ack = plugin.context.publish_event(event(plugin.session))
        self.assertFalse(ack.persisted)
        self.assertEqual(runtime.store.events_after(int(ACCOUNT)), [])

    def test_duplicate_event_id_with_conflicting_content_is_rejected(self):
        host = self.host()
        plugin, runtime = self.online(host)
        self.assertTrue(plugin.context.publish_event(event(plugin.session)).persisted)
        with self.assertRaises(ContractError):
            plugin.context.publish_event(event(plugin.session, subtype="disable"))
        self.assertEqual(len(runtime.store.events_after(int(ACCOUNT))), 1)
        self.assertTrue(runtime.online)

    def test_wrong_account_or_epoch_cannot_publish(self):
        host = self.host()
        plugin, runtime = self.online(host)
        with self.assertRaises(ContractError):
            plugin.context.publish_event(event(plugin.session, self_id=int(ACCOUNT) + 1))
        stale = replace(plugin.session, session_epoch="old-session")
        ack = plugin.context.publish_event(event(stale))
        self.assertFalse(ack.persisted)
        self.assertEqual(runtime.store.events_after(int(ACCOUNT)), [])
        with self.assertRaises(BackendError) as error:
            runtime.ingest(event(plugin.session).event, session_epoch="old-session")
        self.assertEqual(error.exception.retcode, 1409)

    def test_offline_update_revokes_actions_and_incoming_events(self):
        host = self.host()
        plugin, runtime = self.online(host)
        plugin.context.update_session(SessionUpdate(plugin.session, "offline"))
        self.assertEqual(host.account_runtimes(), [])
        self.assertFalse(runtime.online)
        self.assertFalse(plugin.context.publish_event(event(plugin.session)).persisted)
        with self.assertRaises(BackendError):
            host.call(host._instances["test.local"], plugin.session, "echo", {})
        self.assertEqual(self.snapshot(host)["session"]["state"], "offline")
        self.assertEqual(self.snapshot(host)["state"], "running")

    def test_stale_offline_update_cannot_revoke_current_owner(self):
        host = self.host()
        plugin, runtime = self.online(host)
        stale = replace(plugin.session, session_epoch="revoked-epoch")
        with self.assertRaises(ContractError):
            plugin.context.update_session(SessionUpdate(stale, "offline"))
        self.assertTrue(runtime.online)
        self.assertEqual(len(host.account_runtimes()), 1)
        self.assertTrue(plugin.context.publish_event(event(plugin.session)).persisted)

    def test_concurrent_identical_online_announcements_attach_one_runtime(self):
        host = self.host()
        plugin, _ = self.online(host)
        plugin.context.update_session(SessionUpdate(plugin.session, "offline"))
        plugin.session = replace(plugin.session, session_epoch="new-online-epoch")
        entered, release, second_done = threading.Event(), threading.Event(), threading.Event()
        errors = []
        original = host._attach_account

        def delayed_attach(instance, identity):
            entered.set()
            if not release.wait(1):
                raise AssertionError("test attachment was not released")
            return original(instance, identity)

        def announce(done=None):
            try:
                plugin.context.update_session(SessionUpdate(plugin.session, "online"))
            except Exception as error:
                errors.append(error)
            finally:
                if done is not None:
                    done.set()

        with patch.object(host, "_attach_account", side_effect=delayed_attach) as attachment:
            first = threading.Thread(target=announce)
            second = threading.Thread(target=announce, args=(second_done,))
            first.start()
            try:
                self.assertTrue(entered.wait(1))
                second.start()
                self.assertTrue(second_done.wait(1))
            finally:
                release.set()
                first.join(timeout=1)
                second.join(timeout=1)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(attachment.call_count, 1)
        self.assertEqual(errors, [])
        self.assertEqual(len(host.account_runtimes()), 1)
        self.assertTrue(host.account_runtimes()[0][1].online)

    def test_invalid_pong_revokes_plugin_and_account(self):
        host = self.host()
        plugin = self.attach(host, mode="wrong_pong")[0]
        self.wait(lambda: self.snapshot(host)["state"] == "failed")
        self.assertFalse(self.snapshot(host)["heartbeat"]["healthy"])
        self.assertEqual(host.account_runtimes(), [])
        self.wait(plugin.stopped.is_set)

    def test_blocked_ping_expires_lease_and_late_reply_cannot_resurrect_it(self):
        host = self.host()
        plugin = self.attach(host, mode="blocked_ping")[0]
        self.wait(plugin.ping_entered.is_set)
        self.wait(lambda: self.snapshot(host)["state"] == "failed")
        self.assertEqual(host.account_runtimes(), [])
        self.wait(plugin.stopped.is_set)
        plugin.ping_release.set()
        time.sleep(0.03)
        self.assertEqual(self.snapshot(host)["state"], "failed")
        self.assertFalse(self.snapshot(host)["heartbeat"]["healthy"])

    def test_control_errors_expose_type_only_not_private_payload(self):
        for mode in ("start_error", "ping_error"):
            with self.subTest(mode=mode):
                host = self.host()
                self.attach(host, mode=mode)
                self.wait(lambda: self.snapshot(host)["state"] == "failed")
                rendered = json.dumps(host.catalog())
                self.assertNotIn("private-native-token", rendered)
                self.assertIn("RuntimeError", rendered)

    def test_stop_restart_creates_new_instance_and_rejects_old_callbacks_without_replay(self):
        host = self.host()
        created = []

        def factory():
            plugin = LocalPlugin()
            created.append(plugin)
            return plugin

        host.register(LocalPlugin().manifest, factory=factory)
        host.start("test.local", {})
        self.wait(lambda: host.account_runtimes())
        first = created[0]
        self.wait(lambda: self.snapshot(host)["state"] == "running")
        first_runtime = host.account_runtimes()[0][1]
        self.assertEqual(first_runtime.call("send_group_msg", {
            "group_id": 123, "message": "local fixture only"})["message_id"], 42)
        self.assertEqual(len([item for item in first.calls if item.action == "send_group_msg"]), 1)
        host.stop("test.local")
        self.wait(lambda: self.snapshot(host)["state"] == "stopped")
        self.wait(lambda: not host._instances["test.local"].stopper.is_alive())
        host.start("test.local", {})
        self.wait(lambda: len(created) == 2 and host.account_runtimes())
        self.wait(lambda: self.snapshot(host)["state"] == "running")
        second = created[1]
        self.assertNotEqual(first.context.instance_id, second.context.instance_id)
        self.assertNotEqual(first.session.session_epoch, second.session.session_epoch)
        self.assertFalse(first.context.publish_event(event(first.session)).persisted)
        first.context.update_session(SessionUpdate(first.session, "online"))
        self.assertEqual(host.account_runtimes()[0][0], second.session)
        self.assertEqual([item.action for item in second.calls], ["get_login_info"])

    def test_unsupported_actions_and_wrong_result_ids_do_not_retry(self):
        host = self.host()
        plugin, runtime = self.online(host, mode="wrong_result")
        instance = host._instances["test.local"]
        count = len(plugin.calls)
        with self.assertRaises(BackendError) as error:
            host.call(instance, plugin.session, "undeclared_action", {})
        self.assertEqual(error.exception.retcode, 1404)
        self.assertEqual(len(plugin.calls), count)
        with self.assertRaises(BackendError) as error:
            host.call(instance, plugin.session, "echo", {"value": 1})
        self.assertEqual(error.exception.details["action_outcome"], "unknown")
        self.assertEqual(len(plugin.calls), count + 1)

    def test_action_failure_outcomes_are_preserved(self):
        host = self.host()
        plugin, _ = self.online(host)
        for outcome in ("not_started", "completed", "unknown"):
            plugin.action_outcome = outcome
            with self.subTest(outcome=outcome), self.assertRaises(BackendError) as error:
                host.call(host._instances["test.local"], plugin.session, "send_group_msg",
                          {"group_id": 123, "message": "synthetic"})
            self.assertFalse(error.exception.details["retry_safe"])
            if outcome == "unknown":
                self.assertEqual(error.exception.details["action_outcome"], "unknown")
            else:
                self.assertEqual(error.exception.details["action_completed"], outcome == "completed")
            if outcome == "completed":
                self.assertEqual(error.exception.details["message_id"], 42)
        self.assertEqual(len([item for item in plugin.calls if item.action == "send_group_msg"]), 3)

    def test_close_stops_control_threads_and_rejects_new_work(self):
        host = self.host()
        plugin, _ = self.online(host)
        instance = host._instances["test.local"]
        host.close()
        self.assertTrue(plugin.stopped.is_set())
        self.assertFalse(host._monitor.is_alive())
        self.assertFalse(instance.thread.is_alive())
        self.assertFalse(instance.stopper.is_alive())
        self.assertEqual(host.account_runtimes(), [])
        with self.assertRaises(PluginHostError):
            host.start("test.local", {})
        host.close()

    def test_stop_exception_blocks_replacement_even_after_stopper_exits(self):
        host = self.host()
        plugin, _ = self.online(host, mode="stop_error")
        instance = host._instances["test.local"]
        host.stop("test.local")
        self.wait(lambda: self.snapshot(host)["state"] == "failed")
        self.wait(lambda: instance.stopper is not None and not instance.stopper.is_alive())
        self.assertFalse(instance.clean_stop)
        with self.assertRaises(PluginHostError) as error:
            host.start("test.local", {})
        self.assertEqual(error.exception.code, "PLUGIN_STOP_UNCONFIRMED")
        self.assertEqual(len(plugin.stop_calls), 1)
        self.assertNotIn("private-stop-token", json.dumps(host.catalog()))

    def test_hung_stop_deadline_blocks_replacement_until_explicit_clean_finish(self):
        host = self.host()
        created = []

        def factory():
            plugin = LocalPlugin(mode="blocked_stop" if not created else "online")
            created.append(plugin)
            return plugin

        host.register(LocalPlugin().manifest, factory=factory)
        host.start("test.local", {})
        self.wait(lambda: self.snapshot(host)["state"] == "running")
        plugin = created[0]
        self.addCleanup(plugin.stop_release.set)
        host.stop("test.local")
        self.wait(plugin.stop_entered.is_set)
        self.wait(lambda: self.snapshot(host)["state"] == "failed")
        self.assertIn("shutdown deadline", self.snapshot(host)["error"])
        with self.assertRaises(PluginHostError):
            host.start("test.local", {})
        self.assertEqual(len(created), 1)
        plugin.stop_release.set()
        instance = host._instances["test.local"]
        self.wait(lambda: instance.clean_stop and not instance.stopper.is_alive())
        self.assertEqual(len(created), 1)
        self.assertEqual(self.snapshot(host)["state"], "failed")
        host.start("test.local", {})
        self.wait(lambda: len(created) == 2 and self.snapshot(host)["state"] == "running")

    def test_blocked_stop_keeps_account_reserved_against_different_plugin(self):
        host = self.host()
        first, _ = self.online(host, plugin_id="test.first", mode="blocked_stop")
        second = self.attach(host, plugin_id="test.second", mode="login_required")[0]
        self.wait(lambda: self.snapshot(host, "test.second")["state"] == "running")
        self.addCleanup(first.stop_release.set)
        second.session = SessionIdentity(
            "example", ACCOUNT, "epoch-" + second.context.instance_id)
        host.stop("test.first")
        self.wait(first.stop_entered.is_set)
        with self.assertRaises(ContractError):
            second.context.update_session(SessionUpdate(second.session, "online"))
        self.assertEqual(host.account_runtimes(), [])
        first_instance = host._instances["test.first"]
        self.assertIs(host._owners[("example", ACCOUNT)], first_instance)
        self.assertFalse(second.context.publish_event(event(second.session)).persisted)
        first.stop_release.set()
        self.wait(lambda: first_instance.clean_stop and not first_instance.stopper.is_alive())
        self.assertNotIn(("example", ACCOUNT), host._owners)
        self.assertEqual(host.account_runtimes(), [])
        self.assertEqual(self.snapshot(host, "test.second")["session"]["state"], "login_required")
        second.context.update_session(SessionUpdate(second.session, "online"))
        self.wait(lambda: host.account_runtimes())
        self.assertEqual(host.account_runtimes()[0][0], second.session)
        self.assertIs(host._owners[("example", ACCOUNT)], host._instances["test.second"])

    def test_failed_stop_cannot_be_bypassed_by_another_plugin_for_same_account(self):
        host = self.host()
        first, _ = self.online(host, plugin_id="test.first", mode="stop_error")
        second = self.attach(host, plugin_id="test.second", mode="login_required")[0]
        self.wait(lambda: self.snapshot(host, "test.second")["state"] == "running")
        second.session = SessionIdentity(
            "example", ACCOUNT, "epoch-" + second.context.instance_id)
        host.stop("test.first")
        first_instance = host._instances["test.first"]
        self.wait(lambda: self.snapshot(host, "test.first")["state"] == "failed")
        self.wait(lambda: first_instance.stopper is not None
                  and not first_instance.stopper.is_alive())
        self.assertFalse(first_instance.clean_stop)
        self.assertIs(host._owners[("example", ACCOUNT)], first_instance)
        with self.assertRaises(ContractError):
            second.context.update_session(SessionUpdate(second.session, "online"))
        self.assertEqual(host.account_runtimes(), [])
        with self.assertRaises(BackendError):
            host.call(host._instances["test.second"], second.session,
                      "send_group_msg", {"group_id": 123, "message": "synthetic"})
        self.assertEqual([request.action for request in second.calls], [])
        self.assertEqual(len(first.stop_calls), 1)

    def test_host_close_atomically_blocks_concurrent_start_and_registration(self):
        host = self.host()
        plugin, _ = self.online(host, mode="blocked_stop")
        self.addCleanup(plugin.stop_release.set)
        closer = threading.Thread(target=host.close)
        closer.start()
        try:
            self.wait(plugin.stop_entered.is_set)
            with self.assertRaises(PluginHostError) as error:
                host.start("test.local", {})
            self.assertEqual(error.exception.code, "PLUGIN_HOST_CLOSED")
            with self.assertRaises(PluginHostError):
                host.register(LocalPlugin(plugin_id="test.other").manifest,
                              factory=lambda: LocalPlugin(plugin_id="test.other"))
        finally:
            plugin.stop_release.set()
            closer.join(timeout=2)
        self.assertFalse(closer.is_alive())
        self.assertFalse(host.catalog()["host_running"])
        self.assertEqual(host.account_runtimes(), [])

    def test_slow_factory_is_stopped_after_lease_revocation_and_late_completion(self):
        host = self.host()
        entered, release = threading.Event(), threading.Event()
        plugin = LocalPlugin()

        def slow_factory():
            entered.set()
            release.wait(2)
            return plugin

        self.addCleanup(release.set)
        host.register(plugin.manifest, factory=slow_factory)
        host.start("test.local", {})
        self.wait(entered.is_set)
        self.wait(lambda: self.snapshot(host)["state"] == "failed")
        self.assertEqual(host.account_runtimes(), [])
        release.set()
        self.wait(plugin.stopped.is_set)
        self.assertFalse(plugin.started)
        self.assertEqual(self.snapshot(host)["state"], "failed")

    def test_late_completed_business_result_is_preserved_without_replay(self):
        host = self.host(action_timeout=0.01)
        plugin, runtime = self.online(host)
        plugin.action_delay = 0.04
        result = runtime.call("send_group_msg", {"group_id": 123, "message": "fixture only"})
        self.assertEqual(result["message_id"], 42)
        sends = [request for request in plugin.calls if request.action == "send_group_msg"]
        self.assertEqual(len(sends), 1)
        self.assertLess(sends[0].deadline, time.monotonic())

    def test_persisted_event_deduplication_survives_host_restart_and_new_session_epoch(self):
        directory = self.root / "durable"
        first_host = PluginHost(directory, heartbeat_interval=0.02, lease_seconds=0.15,
                                control_timeout=0.1)
        self.hosts.append(first_host)
        first, _ = self.online(first_host)
        self.assertTrue(first.context.publish_event(event(first.session)).persisted)
        first_host.close()
        second_host = PluginHost(directory, heartbeat_interval=0.02, lease_seconds=0.15,
                                 control_timeout=0.1)
        self.hosts.append(second_host)
        second, runtime = self.online(second_host)
        self.assertNotEqual(first.session.session_epoch, second.session.session_epoch)
        ack = second.context.publish_event(event(second.session))
        self.assertTrue(ack.persisted)
        self.assertTrue(ack.duplicate)
        self.assertEqual(len(runtime.store.events_after(int(ACCOUNT))), 1)

    def test_registry_metadata_loading_does_not_import_entrypoint(self):
        host = self.host()
        directory = self.root / "registry"
        declarations = directory / "declarations"
        source = directory / "source"
        declarations.mkdir(parents=True)
        source.mkdir()
        (declarations / "fixture.json").write_text(
            json.dumps(LocalPlugin().manifest.to_dict()), encoding="utf-8")
        registry = directory / "registry.json"
        registry.write_text(json.dumps({
            "schema_version": 1, "plugins": [{
                "manifest": "declarations/fixture.json",
                "source_root": "source",
                "entrypoint": "unimported_fixture:create_plugin",
            }],
        }), encoding="utf-8")
        with patch("importlib.import_module", side_effect=AssertionError("plugin import")):
            host.load_registry(registry)
            self.assertEqual(self.snapshot(host)["state"], "discovered")

    def test_registry_path_escape_and_atomic_overlap_do_not_partially_register(self):
        host = self.host()
        directory = self.root / "registry"
        directory.mkdir()
        registry = directory / "registry.json"
        for relative in ("../outside", "/absolute", "C:/outside", "a/../source"):
            registry.write_text(json.dumps({
                "schema_version": 1, "plugins": [{
                    "manifest": relative, "source_root": "source",
                    "entrypoint": "fixture:create_plugin",
                }],
            }), encoding="utf-8")
            with self.subTest(relative=relative), self.assertRaises(PluginHostError):
                host.load_registry(registry)
        self.assertEqual(host.catalog()["plugins"], [])


if __name__ == "__main__":
    unittest.main()
