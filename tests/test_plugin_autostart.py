"""Saved public account profiles drive explicit retry and opt-in autostart."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from heredity.plugins.host import PluginHost, PluginHostError
from nyxion_heredity_sdk import PluginUIPage
from nyxion_heredity_sdk.example import OfflineExamplePlugin


class AutoFixture(OfflineExamplePlugin):
    manifest = replace(
        OfflineExamplePlugin.manifest, plugin_id="test.autostart", platform="example",
        config_schema={"type": "object", "additionalProperties": False,
                       "required": ["self_id"],
                       "properties": {"self_id": {"type": "string"},
                                      "auto_login": {"type": "boolean", "default": False},
                                      "secret": {"type": "string", "writeOnly": True}}},
        ui_pages=(PluginUIPage("accounts", "Accounts"),))


class PluginAutostartTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="NyXion automatic account ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.created = []

    def host(self, cls=AutoFixture):
        host = PluginHost(self.root, heartbeat_interval=0.05, lease_seconds=1, control_timeout=0.5)
        self.addCleanup(host.close)
        def factory():
            plugin = cls()
            self.created.append(plugin)
            return plugin
        host.register(cls.manifest, factory=factory)
        return host

    def wait(self, check):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(0.005)
        self.fail("local account state did not converge")

    def test_accounts_page_starts_empty_and_explicit_default_is_ordinary_removable_profile(self):
        host = self.host()
        self.assertEqual(host.instances(AutoFixture.manifest.plugin_id), [])
        self.assertEqual(host.catalog()["plugins"][0]["instances"], [])
        host.start(AutoFixture.manifest.plugin_id, {"self_id": "123"})
        self.wait(lambda: host.account_runtimes())
        profile = host.instances(AutoFixture.manifest.plugin_id)[0]
        self.assertEqual(profile["instance_key"], "default")
        self.assertFalse(profile["is_default"])
        host.stop(AutoFixture.manifest.plugin_id)
        instance = host._instances[AutoFixture.manifest.plugin_id]
        self.wait(lambda: instance.clean_stop and not instance.stopper.is_alive())
        self.assertTrue(host.remove_instance(AutoFixture.manifest.plugin_id, "default")["removed"])
        self.assertEqual(host.instances(AutoFixture.manifest.plugin_id), [])

    def test_opt_in_saved_profile_autostarts_but_other_profiles_stay_stopped(self):
        first = self.host()
        first.create_instance(AutoFixture.manifest.plugin_id, "enabled", "Enabled")
        first.create_instance(AutoFixture.manifest.plugin_id, "manual", "Manual")
        first.start(AutoFixture.manifest.plugin_id,
                    {"self_id": "123", "auto_login": True, "secret": "must-not-save"},
                    instance_key="enabled")
        self.wait(lambda: first.account_runtimes())
        first.start(AutoFixture.manifest.plugin_id,
                    {"self_id": "456", "auto_login": False}, instance_key="manual")
        self.wait(lambda: len(first.account_runtimes()) == 2)
        first.close()
        persisted = json.loads((self.root / "plugin-instances.json").read_text())
        self.assertEqual(persisted["schema_version"], 2)
        self.assertNotIn("must-not-save", json.dumps(persisted))
        second = self.host()
        self.assertEqual(second.account_runtimes(), [])
        second.start_enabled()
        self.wait(lambda: second.account_runtimes())
        self.assertEqual([session.self_id for session, _ in second.account_runtimes()], ["123"])
        profiles = second.instances(AutoFixture.manifest.plugin_id)
        manual = next(item for item in profiles if item["instance_key"] == "manual")
        self.assertEqual(manual["state"], "stopped")
        enabled = next(item for item in profiles if item["instance_key"] == "enabled")
        self.assertEqual(enabled["config_values"]["self_id"], "123")
        self.assertNotIn("secret", enabled["config_values"])

    def test_retry_uses_saved_configuration_and_a_new_session_epoch(self):
        host = self.host()
        host.create_instance(AutoFixture.manifest.plugin_id, "test", "Test")
        host.start(AutoFixture.manifest.plugin_id, {"self_id": "123"}, instance_key="test")
        self.wait(lambda: host.account_runtimes())
        first = host.account_runtimes()[0][0]
        host.retry(AutoFixture.manifest.plugin_id, instance_key="test")
        self.wait(lambda: host.account_runtimes() and host.account_runtimes()[0][0] != first)
        self.assertEqual(host.account_runtimes()[0][0].self_id, first.self_id)
        self.assertEqual(len(self.created), 2)

    def test_nested_writeonly_values_are_removed_from_storage_and_public_snapshots(self):
        class Nested(AutoFixture):
            manifest = replace(AutoFixture.manifest, config_schema={
                "type": "object", "additionalProperties": False, "required": ["self_id"],
                "properties": {
                    "self_id": {"type": "string"},
                    "auth": {"type": "object", "properties": {
                        "label": {"type": "string"}, "token": {"type": "string", "writeOnly": True}}},
                    "items": {"type": "array", "items": {"type": "object", "properties": {
                        "name": {"type": "string"}, "password": {"type": "string", "writeOnly": True}}}},
                }})
        host = self.host(Nested)
        host.create_instance(Nested.manifest.plugin_id, "nested", "Nested")
        host.start(Nested.manifest.plugin_id, {
            "self_id": "123", "auth": {"label": "public", "token": "private-one"},
            "items": [{"name": "public-item", "password": "private-two"}]},
            instance_key="nested")
        self.wait(lambda: host.account_runtimes())
        rendered = json.dumps(host.catalog()) + (self.root / "plugin-instances.json").read_text()
        self.assertNotIn("private-one", rendered)
        self.assertNotIn("private-two", rendered)
        self.assertIn("public-item", rendered)

    def test_retry_missing_required_secret_does_not_stop_a_running_account(self):
        class Required(AutoFixture):
            manifest = replace(AutoFixture.manifest, config_schema={
                "type": "object", "required": ["self_id", "secret"],
                "properties": {"self_id": {"type": "string"},
                               "secret": {"type": "string", "writeOnly": True}},
                "additionalProperties": False})
        host = self.host(Required)
        host.create_instance(Required.manifest.plugin_id, "live", "Live")
        host.start(Required.manifest.plugin_id,
                   {"self_id": "123", "secret": "runtime-secret"}, instance_key="live")
        self.wait(lambda: host.account_runtimes())
        original = host.account_runtimes()[0][0]
        with self.assertRaises(PluginHostError) as error:
            host.retry(Required.manifest.plugin_id, instance_key="live")
        self.assertEqual(error.exception.status, 422)
        self.assertEqual(host.account_runtimes()[0][0], original)
        self.assertEqual(len(self.created), 1)


if __name__ == "__main__":
    unittest.main()
