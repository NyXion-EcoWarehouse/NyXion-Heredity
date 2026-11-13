"""Aegis marketplace nodes use real worker processes and scoped host routes."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from heredity.aegis import AegisError, PermissionViolation
from heredity.module_bus import ModuleBusError, SandboxModuleBus
from heredity.network_allocator import NetworkInterfaceAllocator
from heredity.network_interfaces import NetworkInterfaceManager

from test_aegis_runtime import MODULE_ID, fixture, literal


class EmptyAccounts:
    def account_descriptors(self):
        return []

    def account_runtimes(self):
        return []


def requests_fixture(requests):
    imports = ['(import "nyxion_aegis" "host_call" (func $host (param i32 i32 i32 i32) (result i32)))']
    selections = []
    for index, request in enumerate(requests, 1):
        raw = json.dumps(request, separators=(",", ":")).encode()
        pointer = 4096 * index
        imports.append(f'(data (i32.const {pointer}) "{literal(raw)}")')
        selections.append(
            f"(if (i32.eq (global.get $called) (i32.const {index}))"
            f"(then (local.set $pointer (i32.const {pointer}))"
            f"(local.set $count (i32.const {len(raw)}))))")
    body = "\n".join(selections) + """
      (global.set $called (i32.add (global.get $called) (i32.const 1)))
      (local.set $count (call $host (local.get $pointer) (local.get $count)
                             (i32.const 32768) (i32.const 65536)))
      (i64.or (i64.const 140737488355328) (i64.extend_i32_u (local.get $count)))
    """
    return fixture(body, imports="\n".join(imports), memory_pages=2)


class SandboxModuleBusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="NyXion Aegis tree bus ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manager = NetworkInterfaceManager(self.root / "network", EmptyAccounts())
        self.addCleanup(self.manager.close)
        self.allocator = NetworkInterfaceAllocator(self.manager)
        self.addCleanup(self.allocator.close)
        self.bus = SandboxModuleBus(self.root, self.allocator, "0.3.0")
        self.addCleanup(self.bus.close)

    def record(self, request=None, *, permissions=("bus.call",)):
        path = self.root / ("module-" + str(len(list(self.root.glob("*.wasm")))) + ".wasm")
        path.write_bytes(fixture(host_request=request))
        return {"plugin_id": MODULE_ID, "version": "1.0.0", "status": "active",
                "installed_at": 1, "module_path": str(path),
                "approved_permissions": list(permissions),
                "manifest": {"schema_version": 1, "id": MODULE_ID, "name": "Aegis tree fixture",
                             "version": "1.0.0", "sdk_api": {"major": 1, "minor": 0},
                             "min_heredity_version": "0.3.0", "permissions": list(permissions),
                             "module": "module.wasm"}}

    def node(self):
        return self.bus.snapshot()["modules"][0]

    def interface(self):
        return self.manager.create({
            "name": "Tree-owned local interface", "type": "http_server", "enabled": False,
            "settings": {"port": 0},
        })["interface_id"]

    def wait_job(self, job):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = self.bus.job(job["job_id"])
            if current["state"] not in {"queued", "running"}:
                return current
            threading.Event().wait(0.005)
        self.fail("The local module control job did not finish")

    def test_registration_autostarts_a_real_worker_at_the_host_assigned_namespace(self):
        self.bus.register(self.record({
            "capability": "bus.call", "operation": "tree.self", "params": {}}), autostart=True)
        self.assertEqual(self.bus.call(MODULE_ID, "self"), {
            "namespace": "/modules/" + MODULE_ID, "module_id": "market." + MODULE_ID})
        node = self.node()
        self.assertEqual(node["state"], "running")
        self.assertTrue(node["sandbox"]["isolated"])
        self.assertTrue(node["sandbox"]["process_running"])
        self.assertEqual(self.bus.snapshot()["tree"]["children"][0]["children"][0]["namespace"],
                         "/modules/" + MODULE_ID)

    def test_guest_cannot_inject_a_module_id_into_its_network_declaration(self):
        self.bus.register(self.record({
            "capability": "bus.call", "operation": "interfaces.declare",
            "params": {"transport_types": ["http_server"], "module_id": "market.other"}}),
            autostart=True)
        with self.assertRaises(AegisError) as error:
            self.bus.call(MODULE_ID, "declare")
        self.assertEqual(error.exception.code, "AEGIS_PERMISSION_DENIED")
        self.assertEqual(self.node()["state"], "failed")
        self.assertFalse(self.node()["sandbox"]["process_running"])
        self.assertFalse(self.allocator.is_module_active("market." + MODULE_ID))
        self.assertEqual(self.allocator.catalog()["requests"], [])

    def test_guest_cannot_look_up_another_namespace_even_with_bus_call(self):
        self.bus.register(self.record({
            "capability": "bus.call", "operation": "tree.self",
            "params": {"namespace": "/modules/another"}}), autostart=True)
        with self.assertRaises(AegisError) as error:
            self.bus.call(MODULE_ID, "hijack")
        self.assertEqual(error.exception.code, "AEGIS_PERMISSION_DENIED")
        self.assertFalse(self.node()["sandbox"]["process_running"])

    def test_real_guest_can_declare_but_a_private_interface_id_never_authorizes_itself(self):
        identifier = self.interface()
        self.bus.register(self.record({
            "capability": "bus.call", "operation": "onebot.call",
            "params": {"interface_id": identifier, "action": "get_status", "params": {}}}),
            autostart=True)
        with self.assertRaises(AegisError) as error:
            self.bus.call(MODULE_ID, "unallocated")
        self.assertEqual(error.exception.code, "AEGIS_PERMISSION_DENIED")
        self.assertEqual(self.manager.get(identifier)["allocation_owner_count"], 0)

    def test_permission_fault_releases_the_nodes_actual_interface_owner(self):
        record = self.record()
        Path(record["module_path"]).write_bytes(requests_fixture([{
            "capability": "bus.call", "operation": "interfaces.declare",
            "params": {"transport_types": ["http_server"], "directions": ["call"]}},
            {"capability": "bus.call", "operation": "tree.self",
             "params": {"namespace": "/modules/other"}}]))
        self.bus.register(record, autostart=True)
        declaration = self.bus.call(MODULE_ID, "declare")
        identifier = self.interface()
        self.allocator.assign(declaration["request_id"], identifier)
        self.assertEqual(self.manager.get(identifier)["allocation_owner_count"], 1)
        with self.assertRaises(AegisError) as error:
            self.bus.call(MODULE_ID, "violate")
        self.assertEqual(error.exception.code, "AEGIS_PERMISSION_DENIED")
        self.assertEqual(self.manager.get(identifier)["allocation_owner_count"], 0)
        self.assertFalse(self.allocator.is_module_active("market." + MODULE_ID))
        self.assertFalse(self.node()["sandbox"]["process_running"])

    def test_allocated_guest_calls_publishes_and_receives_on_only_its_interface(self):
        identifier = self.interface()
        record = self.record(permissions=("bus.call", "bus.publish", "bus.subscribe"))
        event = {"time": 17, "self_id": 12345678, "post_type": "meta_event",
                 "meta_event_type": "lifecycle", "sub_type": "enable"}
        Path(record["module_path"]).write_bytes(requests_fixture([
            {"capability": "bus.call", "operation": "interfaces.declare",
             "params": {"transport_types": ["http_server"],
                        "directions": ["call", "publish", "receive"]}},
            {"capability": "bus.call", "operation": "onebot.call",
             "params": {"interface_id": identifier, "action": "get_status", "params": {}}},
            {"capability": "bus.publish", "operation": "onebot.event",
             "params": {"interface_id": identifier, "event": event}},
            {"capability": "bus.subscribe", "operation": "onebot.receive",
             "params": {"interface_id": identifier}},
        ]))
        self.bus.register(record, autostart=True)
        declaration = self.bus.call(MODULE_ID, "declare")
        self.allocator.assign(declaration["request_id"], identifier)
        self.manager.start(identifier)
        result = self.bus.call(MODULE_ID, "call")
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["data"]["online"])
        self.assertTrue(self.bus.call(MODULE_ID, "publish")["published"])
        observed = self.bus.call(MODULE_ID, "receive")
        self.assertEqual(observed["interface_id"], identifier)
        self.assertEqual(observed["kind"], "action")
        self.assertTrue(self.node()["sandbox"]["process_running"])

    def test_restart_creates_a_new_worker_and_ignores_old_failure_and_capability_callbacks(self):
        self.bus.register(self.record(), autostart=True)
        node = self.bus._nodes[MODULE_ID]
        old_generation = node.generation
        old_worker = node.worker
        old_pid = old_worker.status()["pid"]
        job = self.wait_job(self.bus.submit("restart", MODULE_ID))
        self.assertEqual(job["state"], "completed", job)
        self.assertNotEqual(node.worker.status()["pid"], old_pid)
        self.assertFalse(old_worker.status()["process_running"])
        self.bus._failure(node, "AEGIS_PERMISSION_DENIED", old_generation)
        self.assertEqual(self.node()["state"], "running")
        self.assertTrue(self.allocator.is_module_active(node.module_id))
        with self.assertRaises(PermissionViolation):
            self.bus._host_call(node, "bus.call", "tree.self", {}, generation=old_generation)
        self.assertEqual(self.bus.call(MODULE_ID, "echo")["method"], "echo")

    def test_restart_revalidates_the_provider_permission_confirmation(self):
        record = self.record()
        selected = deepcopy(record)
        self.bus.record_provider = lambda identifier: deepcopy(selected)
        self.bus.register(record, autostart=True)
        self.assertEqual(self.wait_job(self.bus.submit("stop", MODULE_ID))["state"], "completed")
        selected["approved_permissions"] = ["bus.call", "fs.write"]
        failed = self.wait_job(self.bus.submit("start", MODULE_ID))
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["error"]["code"], "MODULE_PERMISSION_MISMATCH")
        self.assertFalse(self.node()["sandbox"]["process_running"])
        self.assertFalse(self.allocator.is_module_active("market." + MODULE_ID))

    def test_runtime_constructor_failure_does_not_leave_an_active_scope(self):
        with patch.object(self.bus, "runtime_factory", side_effect=AegisError("AEGIS_INVALID_MODULE")):
            with self.assertRaises(AegisError):
                self.bus.register(self.record(), autostart=True)
        self.assertFalse(self.allocator.is_module_active("market." + MODULE_ID))
        self.assertEqual(self.node()["state"], "failed")

    def test_busy_worker_start_does_not_hold_the_global_tree_lock(self):
        from heredity.aegis import SandboxedModule
        entered, release = threading.Event(), threading.Event()
        original = SandboxedModule.start

        def slow_start(worker):
            entered.set()
            if not release.wait(3):
                raise AssertionError("The offline module start barrier was not released")
            return original(worker)

        self.bus.register(self.record())
        with patch.object(SandboxedModule, "start", slow_start):
            job = self.bus.submit("start", MODULE_ID)
            try:
                self.assertTrue(entered.wait(1))
                before = time.monotonic()
                self.assertEqual(self.node()["state"], "starting")
                self.assertLess(time.monotonic() - before, 0.2)
            finally:
                release.set()
            self.assertEqual(self.wait_job(job)["state"], "completed")

    def test_failure_between_handshake_and_start_commit_cannot_restore_running_state(self):
        from heredity.aegis import SandboxedModule
        entered, release = threading.Event(), threading.Event()
        original = SandboxedModule.start

        def gated_start(worker):
            result = original(worker)
            entered.set()
            if not release.wait(3):
                raise AssertionError("The offline handshake commit barrier was not released")
            return result

        self.bus.register(self.record())
        with patch.object(SandboxedModule, "start", gated_start):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.bus._start_node, self.bus._nodes[MODULE_ID])
                try:
                    self.assertTrue(entered.wait(1))
                    node = self.bus._nodes[MODULE_ID]
                    self.bus._failure(node, "AEGIS_PERMISSION_DENIED", node.generation)
                    node.worker.stop()
                finally:
                    release.set()
                with self.assertRaises(ModuleBusError) as error:
                    pending.result(timeout=2)
        self.assertEqual(error.exception.code, "AEGIS_START_FAILED")
        self.assertEqual(self.node()["state"], "failed")
        self.assertEqual(self.node()["error"]["code"], "AEGIS_PERMISSION_DENIED")
        self.assertFalse(self.node()["sandbox"]["process_running"])
        self.assertFalse(self.allocator.is_module_active("market." + MODULE_ID))


if __name__ == "__main__":
    unittest.main()
