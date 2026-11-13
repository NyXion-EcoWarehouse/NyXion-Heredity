"""Multiple logical accounts of one SDK plugin, using no platform network."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from heredity.plugins.host import PluginHost, PluginHostError
from nyxion_heredity_sdk import (
    BackendError, Challenge, EventEnvelope, PluginUIPage, SessionUpdate,
)
from nyxion_heredity_sdk.example import OfflineExamplePlugin


PLUGIN_ID = "test.multi"


class AccountPlugin(OfflineExamplePlugin):
    manifest = replace(
        OfflineExamplePlugin.manifest,
        plugin_id=PLUGIN_ID, name="Multi-account offline fixture", platform="example",
        capabilities=("synthetic", "messages.text"),
        config_schema={
            "type": "object",
            "properties": {
                "self_id": {"type": "string", "minLength": 1, "maxLength": 32},
                "token": {"type": "string", "writeOnly": True},
            },
            "required": ["self_id"], "additionalProperties": False,
        },
        ui_pages=(PluginUIPage("accounts", "平台账号"),))

    def __init__(self, *, mode="online"):
        super().__init__()
        self.mode = mode
        self.start_calls = 0
        self.stop_calls = 0
        self.ping_count = 0
        self.stop_entered = threading.Event()
        self.stop_release = threading.Event()

    @property
    def context(self):
        return self._context

    @property
    def session(self):
        return self._session

    def start(self, context, config):
        self.start_calls += 1
        super().start(context, config)

    def ping(self, heartbeat):
        self.ping_count += 1
        return super().ping(heartbeat)

    def stop(self, reason):
        self.stop_calls += 1
        self.stop_entered.set()
        if self.mode == "stop_error":
            raise RuntimeError("do-not-expose-this-stop-secret")
        if self.mode == "blocked_stop":
            self.stop_release.wait(2)
        super().stop(reason)


class PluginMultiaccountTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="NyXion multi-account ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.hosts = []
        self.plugins = []
        self.addCleanup(self.close_hosts)
        self.network = patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def close_hosts(self):
        for plugin in self.plugins:
            plugin.stop_release.set()
        for host in self.hosts:
            host.close()

    def host(self, directory=None, factory=None):
        value = PluginHost(
            directory or self.root / str(len(self.hosts)),
            heartbeat_interval=0.02, lease_seconds=0.5, control_timeout=0.2)
        self.hosts.append(value)
        if factory is None:
            def factory():
                plugin = AccountPlugin()
                self.plugins.append(plugin)
                return plugin
        value.register(AccountPlugin.manifest, factory=factory)
        return value

    def wait(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.005)
        self.fail("local account state did not converge")

    def snapshot(self, host, key):
        return next(item for item in host.instances(PLUGIN_ID) if item["instance_key"] == key)

    def start(self, host, key, account):
        count = len(self.plugins)
        host.start(PLUGIN_ID, {"self_id": account}, instance_key=key)
        self.wait(lambda: len(self.plugins) > count)
        plugin = self.plugins[-1]
        self.wait(lambda: self.snapshot(host, key)["session"] is not None
                  and self.snapshot(host, key)["session"]["state"] == "online")
        self.wait(lambda: self.snapshot(host, key)["state"] == "running")
        return plugin

    def stop(self, host, key):
        host.stop(PLUGIN_ID, instance_key=key)
        self.wait(lambda: self.snapshot(host, key)["state"] == "stopped")

    def test_declared_page_registers_one_sidebar_route_without_loading_factory(self):
        host = self.host(factory=lambda: self.fail("metadata access loaded platform code"))
        self.assertEqual(host.instances(PLUGIN_ID), [])
        self.assertEqual(host.catalog()["plugins"][0]["instances"], [])
        host.create_instance(PLUGIN_ID, "work", "Work account")
        catalog = host.catalog()
        self.assertEqual(catalog["navigation"], [{
            "plugin_id": PLUGIN_ID, "id": "accounts", "title": "平台账号",
            "view": "accounts", "icon": "users",
            "path": "/plugins/test.multi/accounts",
        }])
        plugin = catalog["plugins"][0]
        self.assertEqual(plugin["ui_pages"][0]["path"], "/plugins/test.multi/accounts")
        self.assertEqual(len(plugin["instances"]), 1)
        self.assertEqual([item["instance_key"] for item in plugin["instances"]], ["work"])
        self.assertEqual(plugin["instances"][0]["state"], "stopped")
        self.assertIsNone(plugin["instances"][0]["instance_id"])
        self.assertFalse(plugin["instances"][0]["is_default"])
        self.assertEqual(catalog["sessions"], [])

    def test_same_plugin_two_accounts_run_concurrently_without_identity_or_state_collision(self):
        host = self.host()
        host.create_instance(PLUGIN_ID, "home", "Home account")
        host.create_instance(PLUGIN_ID, "work", "Work account")
        home = self.start(host, "home", "10001")
        work = self.start(host, "work", "10002")
        self.wait(lambda: home.ping_count and work.ping_count)
        self.assertIsNot(home, work)
        self.assertNotEqual(home.context.instance_id, work.context.instance_id)
        self.assertNotEqual(home.context.state_dir, work.context.state_dir)
        self.assertEqual(home.context.state_dir.name, "home")
        self.assertEqual(work.context.state_dir.name, "work")
        self.assertTrue(home.context.state_dir.is_relative_to(host.state_dir))
        self.assertTrue(work.context.state_dir.is_relative_to(host.state_dir))
        self.assertNotEqual(home.session.session_epoch, work.session.session_epoch)
        runtimes = {identity.self_id: runtime for identity, runtime in host.account_runtimes()}
        self.assertEqual(set(runtimes), {"10001", "10002"})
        self.assertEqual(runtimes["10001"].call("get_login_info", {})["user_id"], 10001)
        self.assertEqual(runtimes["10002"].call("get_login_info", {})["user_id"], 10002)
        self.assertEqual(runtimes["10001"].call("echo", {"marker": "home"}), {"marker": "home"})
        self.assertEqual(runtimes["10002"].call("echo", {"marker": "work"}), {"marker": "work"})

    def test_stopping_one_instance_keeps_the_other_handshake_and_account_online(self):
        host = self.host()
        host.create_instance(PLUGIN_ID, "home", "Home account")
        host.create_instance(PLUGIN_ID, "work", "Work account")
        home = self.start(host, "home", "10001")
        work = self.start(host, "work", "10002")
        self.stop(host, "home")
        initial_ping = work.ping_count
        self.wait(lambda: work.ping_count > initial_ping)
        self.assertEqual(home.stop_calls, 1)
        self.assertEqual(work.stop_calls, 0)
        self.assertEqual(self.snapshot(host, "work")["session"]["state"], "online")
        self.assertTrue(self.snapshot(host, "work")["heartbeat"]["healthy"])
        self.assertEqual([identity.self_id for identity, _ in host.account_runtimes()], ["10002"])

    def test_same_account_cannot_be_owned_by_two_instances_of_the_same_plugin(self):
        host = self.host()
        host.create_instance(PLUGIN_ID, "first", "First")
        host.create_instance(PLUGIN_ID, "second", "Second")
        first = self.start(host, "first", "10001")
        host.start(PLUGIN_ID, {"self_id": "10001"}, instance_key="second")
        self.wait(lambda: self.snapshot(host, "second")["state"] == "failed")
        self.assertEqual(len(host.account_runtimes()), 1)
        self.assertEqual(host.account_runtimes()[0][0], first.session)
        self.assertEqual(self.snapshot(host, "first")["session"]["state"], "online")

    def test_restart_gets_fresh_runtime_epoch_and_revokes_old_callbacks_only(self):
        host = self.host()
        host.create_instance(PLUGIN_ID, "home", "Home")
        host.create_instance(PLUGIN_ID, "work", "Work")
        old = self.start(host, "home", "10001")
        work = self.start(host, "work", "10002")
        old_session = old.session
        old_context = old.context
        self.stop(host, "home")
        fresh = self.start(host, "home", "10001")
        self.assertNotEqual(fresh.session.session_epoch, old_session.session_epoch)
        self.assertNotEqual(fresh.context.instance_id, old_context.instance_id)
        old_context.update_session(SessionUpdate(old_session, "offline"))
        old_context.publish_challenge(Challenge(
            "old-qr", "notice", "Old instance", {"message": "Should not be shown"}))
        stale = old_context.publish_event(EventEnvelope(old_session, "stale-event", {
            "time": 1, "self_id": 10001, "post_type": "meta_event",
            "meta_event_type": "lifecycle", "sub_type": "enable",
        }))
        self.assertFalse(stale.persisted)
        self.assertIsNone(host.challenge(PLUGIN_ID, instance_key="home"))
        self.assertEqual(self.snapshot(host, "home")["session"]["session_epoch"],
                         fresh.session.session_epoch)
        self.assertEqual(self.snapshot(host, "work")["session"]["session_epoch"],
                         work.session.session_epoch)
        self.assertEqual(len(host.account_runtimes()), 2)

    def test_login_challenges_are_scoped_to_each_instance(self):
        host = self.host()
        host.create_instance(PLUGIN_ID, "home", "Home")
        host.create_instance(PLUGIN_ID, "work", "Work")
        home = self.start(host, "home", "10001")
        work = self.start(host, "work", "10002")
        home.context.publish_challenge(Challenge(
            "home-challenge", "notice", "Home", {"message": "Home status"}))
        work.context.publish_challenge(Challenge(
            "work-challenge", "notice", "Work", {"message": "Work status"}))
        self.assertEqual(host.challenge(PLUGIN_ID, instance_key="home")["challenge_id"],
                         "home-challenge")
        self.assertEqual(host.challenge(PLUGIN_ID, instance_key="work")["challenge_id"],
                         "work-challenge")
        self.stop(host, "home")
        self.assertIsNone(host.challenge(PLUGIN_ID, instance_key="home"))
        self.assertEqual(host.challenge(PLUGIN_ID, instance_key="work")["challenge_id"],
                         "work-challenge")

    def test_profiles_survive_host_restart_without_persisting_secrets_or_autostart(self):
        directory = self.root / "persisted"
        host = self.host(directory)
        host.create_instance(PLUGIN_ID, "home", " Home account ")
        secret = "private-config-not-a-profile"
        host.start(PLUGIN_ID, {"self_id": "10001", "token": secret}, instance_key="home")
        self.wait(lambda: self.snapshot(host, "home")["state"] == "running")
        self.assertEqual(len(self.plugins), 1)
        host.close()
        metadata = (directory / "plugin-instances.json").read_text(encoding="utf-8")
        self.assertNotIn(secret, metadata)
        persisted = json.loads(metadata)
        self.assertEqual(persisted["schema_version"], 2)
        self.assertEqual(persisted["plugins"][PLUGIN_ID], {
            "home": {"display_name": "Home account", "config": {"self_id": "10001"}},
        })
        second = self.host(directory, factory=lambda: self.fail("restored profiles automatically started"))
        profile = self.snapshot(second, "home")
        self.assertEqual(profile["state"], "stopped")
        self.assertIsNone(profile["instance_id"])
        self.assertIsNone(profile["session"])
        self.assertEqual(profile["display_name"], "Home account")
        self.assertEqual(profile["config_values"], {"self_id": "10001"})
        self.assertTrue(profile["has_saved_config"])
        self.assertEqual(second.account_runtimes(), [])

    def test_only_clean_stopped_profiles_can_be_removed_and_history_is_retained(self):
        host = self.host()
        host.create_instance(PLUGIN_ID, "home", "Home")
        plugin = self.start(host, "home", "10001")
        marker = plugin.context.state_dir / "retained-local-state"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaises(PluginHostError):
            host.remove_instance(PLUGIN_ID, "home")
        with self.assertRaises(PluginHostError) as missing:
            host.remove_instance(PLUGIN_ID, "default")
        self.assertEqual(missing.exception.status, 404)
        self.assertEqual(missing.exception.code, "INSTANCE_NOT_FOUND")
        self.stop(host, "home")
        result = host.remove_instance(PLUGIN_ID, "home")
        self.assertEqual(result, {"removed": True, "instance_key": "home"})
        self.assertEqual(host.instances(PLUGIN_ID), [])
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertTrue((host.state_dir / "accounts/example/10001/test.multi/onebot.sqlite3").is_file())

    def test_instance_name_validation_rejects_path_traversal_controls_and_duplicates(self):
        host = self.host()
        for key in ("", ".", "..", "../outside", "a/b", "a\\b", "a.b", "UPPER",
                    "-start", "a:stream", "a" * 65):
            with self.subTest(key=key), self.assertRaises(PluginHostError):
                host.create_instance(PLUGIN_ID, key, "Account")
        for name in ("", "   ", "\nline", "line\rbreak", "a" * 81):
            with self.subTest(name=name), self.assertRaises(PluginHostError):
                host.create_instance(PLUGIN_ID, "valid", name)
        host.create_instance(PLUGIN_ID, "valid", "Account")
        with self.assertRaises(PluginHostError):
            host.create_instance(PLUGIN_ID, "valid", "Another")
        explicit_default = host.create_instance(PLUGIN_ID, "default", "Explicit account")
        self.assertEqual(explicit_default["instance_key"], "default")
        self.assertFalse(explicit_default["is_default"])
        with self.assertRaises(PluginHostError):
            host.create_instance(PLUGIN_ID, "default", "Duplicate account")
        self.assertEqual(len(host.instances(PLUGIN_ID)), 2)

    def test_failed_stop_reserves_account_and_does_not_affect_another_online_account(self):
        modes = iter(("stop_error", "online", "online"))
        def factory():
            plugin = AccountPlugin(mode=next(modes))
            self.plugins.append(plugin)
            return plugin
        host = self.host(factory=factory)
        for key in ("broken", "other", "contender"):
            host.create_instance(PLUGIN_ID, key, key)
        broken = self.start(host, "broken", "10001")
        other = self.start(host, "other", "10002")
        host.stop(PLUGIN_ID, instance_key="broken")
        self.wait(lambda: self.snapshot(host, "broken")["state"] == "failed")
        with self.assertRaises(PluginHostError):
            host.remove_instance(PLUGIN_ID, "broken")
        host.start(PLUGIN_ID, {"self_id": "10001"}, instance_key="contender")
        self.wait(lambda: self.snapshot(host, "contender")["state"] == "failed")
        self.assertEqual(self.snapshot(host, "other")["session"]["state"], "online")
        self.assertEqual([identity.self_id for identity, _ in host.account_runtimes()], ["10002"])
        self.assertEqual(broken.stop_calls, 1)
        self.assertEqual(other.stop_calls, 0)
        self.assertNotIn("do-not-expose-this-stop-secret", json.dumps(host.catalog()))

    def test_legacy_default_start_stop_and_challenge_routes_still_work(self):
        host = self.host()
        self.assertEqual(host.instances(PLUGIN_ID), [])
        host.start(PLUGIN_ID, {"self_id": "10001"})
        self.wait(lambda: self.snapshot(host, "default")["state"] == "running")
        self.assertEqual([item["instance_key"] for item in host.instances(PLUGIN_ID)], ["default"])
        self.assertFalse(self.snapshot(host, "default")["is_default"])
        plugin = self.plugins[0]
        plugin.context.publish_challenge(Challenge("legacy", "notice", "Legacy", {}))
        self.assertEqual(host.challenge(PLUGIN_ID)["challenge_id"], "legacy")
        host.stop(PLUGIN_ID)
        self.wait(lambda: self.snapshot(host, "default")["state"] == "stopped")
        self.assertEqual(plugin.stop_calls, 1)
        self.wait(lambda: self.snapshot(host, "default")["state"] == "stopped"
                  and not host._instances[PLUGIN_ID].thread.is_alive())
        self.assertEqual(host.remove_instance(PLUGIN_ID, "default"),
                         {"removed": True, "instance_key": "default"})
        self.assertEqual(host.instances(PLUGIN_ID), [])

    def test_unknown_or_uncreated_instance_does_not_start_platform_factory(self):
        host = self.host(factory=lambda: self.fail("unknown instance loaded platform code"))
        with self.assertRaises(PluginHostError):
            host.start(PLUGIN_ID, {"self_id": "10001"}, instance_key="missing")
        with self.assertRaises(PluginHostError):
            host.stop(PLUGIN_ID, instance_key="missing")
        with self.assertRaises(PluginHostError):
            host.challenge(PLUGIN_ID, instance_key="missing")
        with self.assertRaises(PluginHostError):
            host.instances("missing.plugin")


if __name__ == "__main__":
    unittest.main()
