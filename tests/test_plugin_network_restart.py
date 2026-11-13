"""Offline SDK modules retain allocation policy across real host lifetimes."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from heredity.network_allocator import NetworkAllocationError, NetworkInterfaceAllocator
from heredity.network_interfaces import NetworkInterfaceManager
from heredity.plugins.host import PluginHost
from nyxion_heredity_sdk import (
    NetworkBindingClosed, NetworkInterfaceGrant, NetworkInterfaceRequirement,
)
from nyxion_heredity_sdk.example import OfflineExamplePlugin


ACCOUNT = "12345678"
PLUGIN = "test.network-lifecycle"
MODULE = PLUGIN + ".instance-default"


class DeclaringPlugin(OfflineExamplePlugin):
    manifest = replace(OfflineExamplePlugin.manifest, plugin_id=PLUGIN)

    def start(self, context, config):
        self.networks = context.networks
        self.requirement = NetworkInterfaceRequirement(
            context.networks.module_id, ("http_server",),
            ("call", "publish", "receive"), account="example:" + config["self_id"])
        self.declaration = context.networks.declare_interface(self.requirement)
        super().start(context, config)


class SlowStopPlugin(DeclaringPlugin):
    def __init__(self):
        super().__init__()
        self.stop_entered = threading.Event()
        self.stop_release = threading.Event()

    def stop(self, reason):
        self.stop_entered.set()
        if not self.stop_release.wait(3):
            raise AssertionError("The offline plugin stop barrier was not released")
        super().stop(reason)


class ReleasingPlugin(DeclaringPlugin):
    def stop(self, reason):
        if hasattr(self, "declaration"):
            self.networks.release_interface(self.declaration["request_id"])
        super().stop(reason)


class PluginNetworkRestartTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="NyXion plugin network lifetime ")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.host = PluginHost(root / "host", heartbeat_interval=0.025,
                               lease_seconds=1, control_timeout=0.4)
        self.manager = NetworkInterfaceManager(root / "network", self.host)
        self.allocator = NetworkInterfaceAllocator(self.manager)
        self.host.network_allocator = self.allocator
        self.addCleanup(self.manager.close)
        self.addCleanup(self.allocator.close)
        self.addCleanup(self.host.close)
        self.created = []

    def register(self, cls=DeclaringPlugin):
        def factory():
            plugin = cls()
            self.created.append(plugin)
            if isinstance(plugin, SlowStopPlugin):
                self.addCleanup(plugin.stop_release.set)
            return plugin

        self.host.register(cls.manifest, factory=factory)

    def wait(self, check, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            threading.Event().wait(0.005)
        self.fail("The offline plugin network lifecycle did not converge")

    def start(self):
        self.host.start(PLUGIN, {"self_id": ACCOUNT})
        self.wait(lambda: self.host.account_runtimes())
        self.wait(lambda: self.host.instances(PLUGIN)[0]["state"] == "running")
        return self.created[-1]

    def stop(self):
        instance = self.host._instances[PLUGIN]
        self.host.stop(PLUGIN)
        self.wait(lambda: instance.clean_stop and not instance.stopper.is_alive())

    def interface(self):
        return self.manager.create({
            "name": "Saved module interface", "type": "http_server",
            "enabled": False, "account": "example:" + ACCOUNT,
            "settings": {"port": 0, "heartbeat_seconds": 3600},
        })["interface_id"]

    def active_request(self):
        matches = [entry for entry in self.allocator.catalog()["requests"]
                   if entry["module_id"] == MODULE]
        return matches[0] if matches else None

    def allocated(self):
        current = self.active_request()
        return current if current and current["state"] == "granted" else None

    def test_restart_redeclares_and_restores_the_saved_interface_with_a_fresh_grant(self):
        self.register()
        first = self.start()
        identifier = self.interface()
        assigned = self.allocator.assign(first.declaration["request_id"], identifier)
        grant = NetworkInterfaceGrant(**assigned["grants"][0])
        binding = first.networks.bind_grant(MODULE, grant)
        self.manager.start(identifier)
        self.wait(lambda: self.manager.get(identifier)["state"] == "running")
        epoch = self.host.account_runtimes()[0][0].session_epoch
        self.stop()
        self.assertTrue(binding.closed)
        self.assertFalse(self.allocator.is_module_active(MODULE))
        self.assertEqual(self.manager.get(identifier)["allocation_owner_count"], 0)
        with self.assertRaises(NetworkBindingClosed):
            first.networks.declare_interface(first.requirement)
        self.host.retry(PLUGIN)
        self.wait(lambda: len(self.created) == 2 and self.host.account_runtimes())
        restored = self.wait(self.allocated)
        self.wait(lambda: self.manager.get(identifier)["state"] == "running")
        self.assertNotEqual(restored["request_id"], assigned["request_id"])
        self.assertNotEqual(restored["grants"][0]["grant_id"], grant.grant_id)
        self.assertEqual(restored["grants"][0]["interface_id"], identifier)
        self.assertEqual(self.manager.get(identifier)["allocation_owner_count"], 1)
        self.assertNotEqual(self.host.account_runtimes()[0][0].session_epoch, epoch)
        self.assertEqual(self.manager._module_call(
            identifier, "get_login_info", {}, 1)["data"]["user_id"], int(ACCOUNT))
        with self.assertRaises(NetworkBindingClosed):
            first.networks.declare_interface(first.requirement)

    def test_auto_start_opt_out_is_preserved_when_the_plugin_restarts(self):
        self.register()
        first = self.start()
        identifier = self.interface()
        request_id = first.declaration["request_id"]
        self.allocator.assign(request_id, identifier)
        self.allocator.set_auto_start(request_id, False)
        self.stop()
        self.host.retry(PLUGIN)
        self.wait(lambda: len(self.created) == 2 and self.host.account_runtimes())
        current = self.wait(self.active_request)
        self.assertNotEqual(current["request_id"], request_id)
        self.assertFalse(current["auto_start"])
        self.assertEqual(current["grants"], [])
        self.assertEqual(self.manager.get(identifier)["allocation_owner_count"], 0)

    def test_release_can_be_reapplied_only_while_the_module_scope_is_active(self):
        self.register()
        plugin = self.start()
        identifier = self.interface()
        assigned = self.allocator.assign(plugin.declaration["request_id"], identifier)
        old_id = assigned["request_id"]
        old_epoch = self.host.account_runtimes()[0][0].session_epoch
        plugin.networks.release_interface(old_id)
        reapplied = self.allocator.reapply(old_id)
        self.assertNotEqual(reapplied["request_id"], old_id)
        restored = self.wait(self.allocated)
        self.assertEqual(restored["grants"][0]["interface_id"], identifier)
        self.assertEqual(self.host.account_runtimes()[0][0].session_epoch, old_epoch)
        self.stop()
        with self.assertRaises(NetworkAllocationError) as error:
            self.allocator.reapply(restored["request_id"])
        self.assertEqual(error.exception.code, "MODULE_INACTIVE")

    def test_slow_plugin_stop_has_already_revoked_its_scope_and_does_not_block_control(self):
        self.register(SlowStopPlugin)
        plugin = self.start()
        request_id = plugin.declaration["request_id"]
        identifier = self.interface()
        self.allocator.assign(request_id, identifier)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                stopping = pool.submit(self.host.stop, PLUGIN)
                self.assertTrue(plugin.stop_entered.wait(1))
                self.assertEqual(stopping.result(timeout=0.2)["state"], "stopping")
            self.assertFalse(self.allocator.is_module_active(MODULE))
            self.assertEqual(self.manager.get(identifier)["allocation_owner_count"], 0)
            with self.assertRaises(NetworkBindingClosed):
                plugin.networks.declare_interface(plugin.requirement)
            with self.assertRaises(NetworkAllocationError) as error:
                self.allocator.reapply(request_id)
            self.assertEqual(error.exception.code, "MODULE_INACTIVE")
        finally:
            plugin.stop_release.set()
        self.wait(lambda: self.host._instances[PLUGIN].clean_stop)

    def test_plugin_stop_can_idempotently_release_its_already_revoked_request(self):
        self.register(ReleasingPlugin)
        plugin = self.start()
        self.allocator.assign(plugin.declaration["request_id"], self.interface())
        self.stop()
        self.assertFalse(self.allocator.is_module_active(MODULE))
        self.assertTrue(self.host._instances[PLUGIN].clean_stop)

    def test_a_scope_created_after_stop_cannot_reactivate_the_cancelled_start(self):
        self.register()
        entered, release = threading.Event(), threading.Event()
        original = self.allocator.for_module
        scopes = []

        def blocked_scope(module):
            entered.set()
            if not release.wait(3):
                raise AssertionError("The offline scope creation barrier was not released")
            scope = original(module)
            scopes.append(scope)
            return scope

        with patch.object(self.allocator, "for_module", side_effect=blocked_scope):
            self.host.start(PLUGIN, {"self_id": ACCOUNT})
            try:
                self.assertTrue(entered.wait(1))
                self.host.stop(PLUGIN)
            finally:
                release.set()
            self.wait(lambda: self.host._instances[PLUGIN].clean_stop)
        self.assertFalse(self.allocator.is_module_active(MODULE))
        self.assertEqual(self.host.account_runtimes(), [])
        self.assertEqual(len(scopes), 1)
        with self.assertRaises(NetworkBindingClosed):
            scopes[0].declare_interface(NetworkInterfaceRequirement(MODULE, ("http_server",)))
        self.assertEqual(self.allocator.catalog()["requests"], [])

    def test_slow_automatic_listener_start_does_not_block_plugin_heartbeats_or_stop(self):
        self.register()
        first = self.start()
        identifier = self.interface()
        self.allocator.assign(first.declaration["request_id"], identifier)
        self.manager.start(identifier)
        self.wait(lambda: self.manager.get(identifier)["state"] == "running")
        self.stop()
        self.manager.stop(identifier)
        entered, release = threading.Event(), threading.Event()
        original = self.manager.start

        def blocked_start(selected):
            entered.set()
            if not release.wait(3):
                raise AssertionError("The offline listener start barrier was not released")
            return original(selected)

        with patch.object(self.manager, "start", side_effect=blocked_start):
            self.host.retry(PLUGIN)
            try:
                self.assertTrue(entered.wait(1))
                current = self.wait(lambda: self.host.instances(PLUGIN)[0]
                                    if self.host.instances(PLUGIN)[0]["heartbeat"]["sequence"] >= 2
                                    else None)
                self.assertEqual(current["state"], "running")
                self.assertTrue(current["heartbeat"]["healthy"])
                with ThreadPoolExecutor(max_workers=1) as pool:
                    stopping = pool.submit(self.host.stop, PLUGIN)
                    self.assertIn(stopping.result(timeout=0.2)["state"], {"stopping", "stopped"})
                self.wait(lambda: not self.allocator.is_module_active(MODULE))
            finally:
                release.set()
        self.wait(lambda: self.host._instances[PLUGIN].clean_stop)
        self.wait(lambda: self.manager.get(identifier)["allocation_owner_count"] == 0)


if __name__ == "__main__":
    unittest.main()
