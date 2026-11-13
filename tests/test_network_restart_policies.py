"""Saved interface intent survives restarts without reviving stale module scopes."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from heredity.network_allocator import NetworkAllocationError, NetworkInterfaceAllocator
from heredity.network_interfaces import NetworkInterfaceManager
from nyxion_heredity_sdk import (
    NetworkAllocationDenied, NetworkBindingClosed, NetworkInterfaceGrant,
    NetworkInterfaceRequirement,
)


class EmptyAccounts:
    def account_descriptors(self):
        return []

    def account_runtimes(self):
        return []


def wait_for(check, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        threading.Event().wait(0.005)
    raise AssertionError("The local interface policy did not converge.")


class NetworkRestartPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="NyXion interface policies ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = NetworkInterfaceManager(self.root, EmptyAccounts())
        self.addCleanup(self.manager.close)
        self.allocator = NetworkInterfaceAllocator(self.manager)
        self.addCleanup(self.allocator.close)
        self.requirement = NetworkInterfaceRequirement(
            "local.module", ("http_server",), account="example:12345678")
        self.scope = self.allocator.for_module(self.requirement.module_id)
        self.request = self.scope.declare_interface(self.requirement)

    def interface(self, **changes):
        return self.manager.create({
            "name": "Explicit saved endpoint", "type": "http_server", "enabled": False,
            "account": self.requirement.account, "settings": {"port": 0},
        } | changes)

    def current(self):
        return next((item for item in self.allocator.catalog()["requests"]
                     if item["module_id"] == self.requirement.module_id), None)

    def restored(self):
        value = self.current()
        return value if value and value["state"] == "granted" and not value["restoring"] else None

    def seed(self, interfaces=None):
        interfaces = interfaces or [self.interface()]
        assigned = self.allocator.assign_many(
            self.request["request_id"], [item["interface_id"] for item in interfaces])
        return assigned, interfaces

    def restart_scope(self, requirement=None):
        self.allocator.release_module(self.requirement.module_id)
        self.scope = self.allocator.for_module(self.requirement.module_id)
        return self.scope.declare_interface(requirement or self.requirement)

    def test_default_is_automatic_but_a_first_request_never_selects_an_existing_interface(self):
        interface = self.interface()
        self.assertTrue(self.request["auto_start"])
        self.assertTrue(self.request["module_active"])
        self.assertTrue(self.request["can_reapply"])
        self.assertEqual(self.request["saved_interface_ids"], [])
        self.assertEqual(self.current()["state"], "pending")
        self.assertIsNone(self.allocator._restore_worker)
        self.assertEqual(self.manager.get(interface["interface_id"])["state"], "stopped")

    def test_module_restart_preserves_explicit_intent_and_issues_fresh_current_request_and_grants(self):
        assigned, interfaces = self.seed()
        identifier = interfaces[0]["interface_id"]
        old_grant = NetworkInterfaceGrant(**assigned["grant"])
        old_scope = self.scope
        old_binding = old_scope.bind_grant(self.requirement.module_id, old_grant)
        self.assertEqual(self.manager.get(identifier)["state"], "stopped")
        declared = self.restart_scope()
        self.assertNotEqual(declared["request_id"], assigned["request_id"])
        self.assertEqual(declared["saved_interface_ids"], [identifier])
        current = wait_for(self.restored)
        self.assertEqual(current["grants"][0]["interface_id"], identifier)
        self.assertNotEqual(current["grants"][0]["grant_id"], old_grant.grant_id)
        self.assertEqual(self.manager.get(identifier)["state"], "running")
        self.assertTrue(old_binding.closed)
        with self.assertRaises(NetworkBindingClosed):
            old_scope.declare_interface(self.requirement)
        with self.assertRaises(NetworkBindingClosed):
            self.scope.bind_grant(self.requirement.module_id, old_grant)
        history = self.allocator.catalog(include_history=True)["requests"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["superseded_by"], current["request_id"])
        self.assertEqual(len(self.allocator.catalog()["requests"]), 1)

    def test_policy_survives_a_full_host_restart_without_credentials_or_runtime_grants(self):
        assigned, interfaces = self.seed()
        identifier = interfaces[0]["interface_id"]
        token = self.manager.credentials(identifier)["access_token"]
        self.allocator.close()
        self.manager.close()
        policy = json.loads((self.root / "network-module-policies.json").read_bytes())
        self.assertEqual(set(policy), {"schema_version", "policies"})
        self.assertEqual(policy["policies"][0], {
            "module_id": self.requirement.module_id, "account": self.requirement.account,
            "auto_start": True, "interface_ids": [identifier]})
        self.assertNotIn(token, json.dumps(policy))
        self.assertNotIn(assigned["grant"]["grant_id"], json.dumps(policy))
        self.manager = NetworkInterfaceManager(self.root, EmptyAccounts())
        self.addCleanup(self.manager.close)
        self.allocator = NetworkInterfaceAllocator(self.manager)
        self.addCleanup(self.allocator.close)
        self.scope = self.allocator.for_module(self.requirement.module_id)
        self.scope.declare_interface(self.requirement)
        restored = wait_for(self.restored)
        self.assertEqual(restored["saved_interface_ids"], [identifier])
        self.assertEqual(self.manager.get(identifier)["state"], "running")
        self.assertNotEqual(restored["grant"]["grant_id"], assigned["grant"]["grant_id"])

    def test_opt_out_persists_and_does_not_start_or_regrant_on_restart(self):
        _, interfaces = self.seed()
        updated = self.allocator.set_auto_start(self.request["request_id"], False)
        self.assertFalse(updated["auto_start"])
        declared = self.restart_scope()
        self.assertFalse(declared["auto_start"])
        self.assertFalse(declared["restoring"])
        self.assertEqual(declared["grants"], [])
        self.assertEqual(declared["saved_interface_ids"], [interfaces[0]["interface_id"]])
        self.assertEqual(self.manager.get(interfaces[0]["interface_id"])["state"], "stopped")
        self.allocator.close()
        restored = NetworkInterfaceAllocator(self.manager)
        self.addCleanup(restored.close)
        scope = restored.for_module(self.requirement.module_id)
        record = scope.declare_interface(self.requirement)
        self.assertFalse(record["auto_start"])
        self.assertFalse(record["restoring"])
        self.assertEqual(record["grants"], [])

    def test_partial_unassign_and_deny_clear_saved_intent_so_restart_cannot_revive_it(self):
        first, second = self.interface(), self.interface()
        _, _ = self.seed([first, second])
        self.allocator.unassign(self.request["request_id"], first["interface_id"])
        self.assertEqual(self.current()["saved_interface_ids"], [second["interface_id"]])
        self.restart_scope()
        restored = wait_for(self.restored)
        self.assertEqual([item["interface_id"] for item in restored["grants"]],
                         [second["interface_id"]])
        self.assertEqual(self.manager.get(first["interface_id"])["state"], "stopped")
        self.allocator.deny(restored["request_id"])
        self.assertEqual(self.current()["saved_interface_ids"], [])
        declared = self.restart_scope()
        self.assertEqual(declared["grants"], [])
        self.assertFalse(declared["restoring"])

    def test_live_reapply_creates_a_new_request_once_and_never_revives_old_handles(self):
        assigned, interfaces = self.seed()
        old = NetworkInterfaceGrant(**assigned["grant"])
        self.scope.release_interface(old.request_id)
        first = self.scope.reapply_interface(old.request_id)
        again = self.scope.reapply_interface(old.request_id)
        self.assertNotEqual(first["request_id"], old.request_id)
        self.assertEqual(first["request_id"], again["request_id"])
        current = wait_for(self.restored)
        self.assertEqual(current["grants"][0]["interface_id"], interfaces[0]["interface_id"])
        self.assertEqual(len(self.allocator.catalog()["requests"]), 1)
        self.assertEqual(len(self.allocator.catalog(include_history=True)["requests"]), 2)
        self.allocator.release_module(self.requirement.module_id)
        with self.assertRaises(NetworkAllocationError) as failure:
            self.allocator.reapply(current["request_id"])
        self.assertEqual(failure.exception.code, "MODULE_INACTIVE")
        with self.assertRaises(NetworkBindingClosed):
            self.scope.reapply_interface(current["request_id"])

    def test_policy_key_ignores_purpose_but_keeps_accounts_and_capabilities_separate(self):
        _, interfaces = self.seed()
        changed_purpose = replace(self.requirement, purpose="Updated visible description")
        declared = self.restart_scope(changed_purpose)
        self.assertEqual(declared["saved_interface_ids"], [interfaces[0]["interface_id"]])
        wait_for(self.restored)
        self.allocator.release_module(self.requirement.module_id)
        self.scope = self.allocator.for_module(self.requirement.module_id)
        different_account = replace(self.requirement, account="example:87654321")
        other = self.scope.declare_interface(different_account)
        self.assertEqual(other["saved_interface_ids"], [])
        self.assertEqual(other["grants"], [])
        self.assertFalse(other["restoring"])

    def test_missing_wrong_account_or_incompatible_saved_interfaces_fail_pending_without_fallback(self):
        assigned, interfaces = self.seed()
        selected = interfaces[0]["interface_id"]
        unused = self.interface()
        self.manager.update(selected, {"account": "example:87654321"})
        self.restart_scope()
        current = wait_for(lambda: self.current()
                           if self.current()["error"] and not self.current()["restoring"] else None)
        self.assertEqual(current["state"], "pending")
        self.assertEqual(current["grants"], [])
        self.assertEqual(current["saved_interface_ids"], [selected])
        self.assertEqual(self.manager.get(unused["interface_id"])["state"], "stopped")
        self.assertEqual(self.manager.get(unused["interface_id"])["allocation_owner_count"], 0)
        self.assertEqual(self.manager.get(selected)["account"], "example:87654321")
        self.manager.delete(selected)
        pending = self.allocator.reapply(current["request_id"])
        self.assertEqual(pending["request_id"], current["request_id"])
        wait_for(lambda: not self.current()["restoring"])
        self.assertEqual(self.current()["grants"], [])

    def test_restart_validates_new_interface_count_and_transport_limits_without_choosing_alternatives(self):
        self.seed([self.interface(), self.interface()])
        changed = replace(self.requirement, max_interfaces=1)
        self.restart_scope(changed)
        current = wait_for(lambda: self.current()
                           if self.current()["error"] and not self.current()["restoring"] else None)
        self.assertEqual(current["state"], "pending")
        self.assertEqual(current["grants"], [])
        self.assertEqual(len(current["saved_interface_ids"]), 2)
        self.allocator.set_auto_start(current["request_id"], False)
        self.restart_scope(replace(self.requirement, transport_types=("websocket_server",)))
        current = self.current()
        self.allocator.set_auto_start(current["request_id"], True)
        wait_for(lambda: not self.current()["restoring"])
        self.assertEqual(self.current()["grants"], [])

    def test_slow_auto_start_does_not_block_declaration_or_module_release_and_cannot_resurrect_grants(self):
        _, interfaces = self.seed([self.interface(), self.interface()])
        entered, release = threading.Event(), threading.Event()
        original = self.manager.start
        started = []

        def delayed(identifier):
            started.append(identifier)
            entered.set()
            if not release.wait(3):
                raise AssertionError("The local automatic startup barrier was not released.")
            return original(identifier)

        with patch.object(self.manager, "start", side_effect=delayed):
            start = time.monotonic()
            declared = self.restart_scope()
            self.assertLess(time.monotonic() - start, 0.2)
            try:
                self.assertTrue(entered.wait(1))
                with ThreadPoolExecutor(max_workers=1) as pool:
                    releasing = pool.submit(self.allocator.release_module, self.requirement.module_id)
                    releasing.result(timeout=0.2)
                self.assertFalse(self.allocator.is_module_active(self.requirement.module_id))
            finally:
                release.set()
        wait_for(lambda: self.manager.get(interfaces[0]["interface_id"])["state"] == "running")
        self.assertEqual(started, [interfaces[0]["interface_id"]])
        self.assertEqual(self.manager.get(interfaces[0]["interface_id"])["allocation_owner_count"], 0)
        self.assertEqual(self.manager.get(interfaces[1]["interface_id"])["state"], "stopped")
        self.assertEqual(self.current()["state"], "released")
        self.assertEqual(self.current()["grants"], [])
        self.assertFalse(declared["superseded_by"])

    def test_concurrent_reapply_is_idempotent_and_scoped_request_cannot_cross_modules(self):
        assigned, _ = self.seed()
        self.scope.release_interface(assigned["request_id"])
        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(lambda _: self.allocator.reapply(assigned["request_id"]),
                                    range(24)))
        self.assertEqual(len({item["request_id"] for item in records}), 1)
        wait_for(self.restored)
        other_scope = self.allocator.for_module("other.module")
        with self.assertRaises(NetworkAllocationDenied):
            other_scope.reapply_interface(assigned["request_id"])

    def test_policy_validation_rejects_duplicate_fields_or_ids_and_unsafe_schema(self):
        self.allocator.close()
        path = self.root / "network-module-policies.json"
        for raw in (
                '{"schema_version":1,"schema_version":1,"policies":[]}',
                '{"schema_version":1,"policies":[{"module_id":"local.module","account":"example:123",'
                '"auto_start":true,"interface_ids":["1234567890abcdef","1234567890abcdef"]}]}',
                '{"schema_version":1,"policies":[{"module_id":"local.module","account":"example:123",'
                '"auto_start":true,"interface_ids":["bad"]}]}',
                '{"schema_version":1,"policies":[],"token":"unexpected"}'):
            path.write_text(raw, encoding="utf-8")
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                NetworkInterfaceAllocator(self.manager)


if __name__ == "__main__":
    unittest.main()
