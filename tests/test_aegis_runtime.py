"""Execute hostile and permitted WebAssembly in real separate Wasmtime workers."""

import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import wasmtime

from heredity.aegis import AegisError, PermissionViolation, SandboxLimits, SandboxedModule


MODULE_ID = "test.aegis"


def literal(raw):
    return "".join("\\" + format(value, "02x") for value in raw)


def fixture(body="", *, host_request=None, imports="", memory_pages=1):
    handshake = json.dumps({"api_major": 1, "api_minor": 0, "module_id": MODULE_ID},
                           separators=(",", ":")).encode()
    host_import = ""
    request_data = ""
    if host_request is not None:
        request = json.dumps(host_request, separators=(",", ":")).encode()
        host_import = '(import "nyxion_aegis" "host_call" (func $host (param i32 i32 i32 i32) (result i32)))'
        request_data = f'(data (i32.const 4096) "{literal(request)}")'
        body = f"""
          (local.set $count (call $host (i32.const 4096) (i32.const {len(request)})
                                       (i32.const 8192) (i32.const 65536)))
          (i64.or (i64.const {8192 << 32}) (i64.extend_i32_u (local.get $count)))
        """
    body = body or "(i64.or (i64.shl (i64.extend_i32_u (local.get $pointer)) (i64.const 32)) (i64.extend_i32_u (local.get $count)))"
    wat = f"""(module
      {host_import} {imports}
      (memory (export "memory") {memory_pages})
      (global $called (mut i32) (i32.const 0))
      (data (i32.const 128) "{literal(handshake)}")
      {request_data}
      (func (export "aegis_alloc") (param i32) (result i32) (i32.const 1024))
      (func (export "aegis_call") (param $pointer i32) (param $count i32) (result i64)
        (if (result i64) (i32.eqz (global.get $called))
          (then (global.set $called (i32.const 1)) (i64.const {(128 << 32) | len(handshake)}))
          (else {body})))
    )"""
    return bytes(wasmtime.wat2wasm(wat))


class AegisRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="NyXion Aegis runtime ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.notifications = []

    def module(self, wasm=None, *, declared=(), granted=(), limits=None, broker=None):
        path = self.root / ("fixture-" + str(len(list(self.root.glob("*.wasm")))) + ".wasm")
        path.write_bytes(fixture() if wasm is None else wasm)
        manifest = {"schema_version": 1, "id": MODULE_ID, "name": "Aegis fixture",
                    "version": "1.0.0", "sdk_api": {"major": 1, "minor": 0},
                    "min_heredity_version": "0.3.0", "permissions": list(declared),
                    "module": "module.wasm"}
        value = SandboxedModule(
            manifest, path, granted, self.root / "state", limits=limits, broker=broker,
            on_failure=lambda module, code: self.notifications.append((module, code)))
        self.addCleanup(value.stop)
        return value

    def assert_dead(self, module, code):
        status = module.status()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["error_code"], code)
        self.assertFalse(status["process_running"])
        self.assertIsNotNone(status["exit_code"])
        self.assertEqual(self.notifications, [(MODULE_ID, code)])

    def test_real_worker_handshakes_and_returns_a_method_result_without_host_environment(self):
        module = self.module()
        with patch.dict(os.environ, {"NYXION_TEST_HOST_SECRET": "must-never-inherit"}):
            handshake = module.start()
        self.assertEqual(handshake["module_id"], MODULE_ID)
        result = module.call("get_status", {"marker": "local"})
        self.assertEqual(result, {"method": "get_status", "params": {"marker": "local"}})
        status = module.status()
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["sandbox"], "wasmtime-process")
        self.assertNotEqual(status["pid"], os.getpid())
        self.assertTrue(status["process_running"])
        module.stop()
        self.assertEqual(module.status()["state"], "stopped")
        self.assertFalse(module.status()["process_running"])

    def test_ungranted_host_capability_kills_and_reaps_the_worker(self):
        request = {"capability": "fs.read", "operation": "fs.read",
                   "params": {"path": "secret.txt"}}
        module = self.module(fixture(host_request=request), declared=("fs.read",))
        module.start()
        with self.assertRaises(AegisError) as error:
            module.call("read")
        self.assertEqual(error.exception.code, "AEGIS_PERMISSION_DENIED")
        self.assert_dead(module, "AEGIS_PERMISSION_DENIED")
        with self.assertRaises(AegisError):
            module.call("read")

    def test_granted_file_read_is_scoped_to_private_state(self):
        request = {"capability": "fs.read", "operation": "fs.read",
                   "params": {"path": "hello.txt"}}
        module = self.module(fixture(host_request=request), declared=("fs.read",), granted=("fs.read",))
        (self.root / "state" / "hello.txt").write_bytes(b"private module state")
        module.start()
        result = module.call("read")
        self.assertEqual(base64.b64decode(result["data_base64"]), b"private module state")
        self.assertTrue(module.status()["process_running"])

    def test_granted_file_write_uses_only_private_state(self):
        request = {"capability": "fs.write", "operation": "fs.write",
                   "params": {"path": "nested/hello.txt", "data_base64": base64.b64encode(b"state").decode()}}
        module = self.module(fixture(host_request=request), declared=("fs.write",), granted=("fs.write",))
        module.start()
        self.assertEqual(module.call("write"), {"bytes": 5})
        self.assertEqual((self.root / "state" / "nested" / "hello.txt").read_bytes(), b"state")

    def test_path_escape_even_with_file_permission_kills_the_worker(self):
        (self.root / "secret.txt").write_bytes(b"outside private state")
        request = {"capability": "fs.read", "operation": "fs.read",
                   "params": {"path": "../secret.txt"}}
        module = self.module(fixture(host_request=request), declared=("fs.read",), granted=("fs.read",))
        module.start()
        with self.assertRaises(AegisError) as error:
            module.call("read")
        self.assertEqual(error.exception.code, "AEGIS_PERMISSION_DENIED")
        self.assert_dead(module, "AEGIS_PERMISSION_DENIED")
        self.assertEqual((self.root / "secret.txt").read_bytes(), b"outside private state")

    def test_direct_wasi_filesystem_syscall_is_fatal_instead_of_bypassing_the_broker(self):
        imports = '(import "wasi_snapshot_preview1" "path_open" (func $open (param i32 i32 i32 i32 i32 i64 i64 i32 i32) (result i32)))'
        body = """(drop (call $open (i32.const 3) (i32.const 0) (i32.const 0) (i32.const 0)
                      (i32.const 0) (i64.const 0) (i64.const 0) (i32.const 0) (i32.const 0)))
                  (i64.const 0)"""
        module = self.module(fixture(body, imports=imports), declared=("fs.read",), granted=("fs.read",))
        module.start()
        with self.assertRaises(AegisError) as error:
            module.call("direct-open")
        self.assertEqual(error.exception.code, "AEGIS_PERMISSION_DENIED")
        self.assert_dead(module, "AEGIS_PERMISSION_DENIED")

    def test_wasi_environment_is_empty_even_when_the_host_has_a_secret(self):
        imports = '(import "wasi_snapshot_preview1" "environ_sizes_get" (func $env (param i32 i32) (result i32)))'
        body = """(drop (call $env (i32.const 32) (i32.const 36)))
             (if (i32.or (i32.load (i32.const 32)) (i32.load (i32.const 36)))
                 (then unreachable))
             (i64.or (i64.shl (i64.extend_i32_u (local.get $pointer)) (i64.const 32))
                     (i64.extend_i32_u (local.get $count)))"""
        module = self.module(fixture(body, imports=imports))
        with patch.dict(os.environ, {"NYXION_TEST_HOST_SECRET": "must-never-inherit"}):
            module.start()
            self.assertEqual(module.call("env")["method"], "env")

    def test_fuel_exhaustion_terminates_only_the_guest_worker(self):
        body = "(loop $spin (br $spin)) (i64.const 0)"
        module = self.module(fixture(body), limits=SandboxLimits(fuel=10000))
        module.start()
        with self.assertRaises(AegisError) as error:
            module.call("spin")
        self.assertEqual(error.exception.code, "AEGIS_FUEL_EXHAUSTED")
        self.assert_dead(module, "AEGIS_FUEL_EXHAUSTED")
        healthy = self.module()
        healthy.start()
        self.assertEqual(healthy.call("healthy")["method"], "healthy")

    def test_out_of_bounds_memory_is_fatal_and_the_host_remains_alive(self):
        module = self.module(fixture("(i32.store (i32.const 2147483647) (i32.const 1)) (i64.const 0)"))
        module.start()
        with self.assertRaises(AegisError) as error:
            module.call("overflow")
        self.assertEqual(error.exception.code, "AEGIS_MEMORY_LIMIT")
        self.assert_dead(module, "AEGIS_MEMORY_LIMIT")
        self.assertTrue(self.root.is_dir())

    def test_non_wasm_package_is_refused_without_starting_a_process(self):
        module = self.module(b"print('host python must never run')")
        with self.assertRaises(AegisError) as error:
            module.start()
        self.assertEqual(error.exception.code, "AEGIS_INVALID_MODULE")
        self.assertIsNone(module.status()["pid"])

    def test_unknown_import_is_refused_in_the_separate_process(self):
        imports = '(import "host" "system" (func))'
        module = self.module(fixture(imports=imports))
        with self.assertRaises(AegisError) as error:
            module.start()
        self.assertEqual(error.exception.code, "AEGIS_IMPORT_DENIED")
        self.assert_dead(module, "AEGIS_IMPORT_DENIED")

    def test_initial_memory_above_the_vm_limit_is_rejected(self):
        module = self.module(fixture(memory_pages=2), limits=SandboxLimits(memory_bytes=65536))
        with self.assertRaises(AegisError) as error:
            module.start()
        self.assertEqual(error.exception.code, "AEGIS_MEMORY_LIMIT")
        self.assert_dead(module, "AEGIS_MEMORY_LIMIT")

    def test_wall_deadline_kills_a_guest_waiting_on_a_slow_host_capability(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        class SlowBroker:
            def dispatch(self, capability, operation, params):
                entered.set()
                release.wait(3)
                return {"done": True}

            def close(self):
                pass

        request = {"capability": "bus.call", "operation": "bus.call", "params": {}}
        module = self.module(fixture(host_request=request), declared=("bus.call",), granted=("bus.call",),
                             limits=SandboxLimits(call_timeout=0.2), broker=SlowBroker())
        module.start()
        before = time.monotonic()
        try:
            with self.assertRaises(AegisError) as error:
                module.call("slow")
            self.assertTrue(entered.is_set())
            self.assertEqual(error.exception.code, "AEGIS_TIMEOUT")
            self.assertLess(time.monotonic() - before, 1)
            self.assert_dead(module, "AEGIS_TIMEOUT")
        finally:
            release.set()

    def test_explicit_stop_during_a_host_capability_reaps_the_process_without_a_fault(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        class SlowBroker:
            def dispatch(self, capability, operation, params):
                entered.set()
                release.wait(3)
                return {"done": True}

            def close(self):
                pass

        request = {"capability": "bus.call", "operation": "bus.call", "params": {}}
        module = self.module(fixture(host_request=request), declared=("bus.call",), granted=("bus.call",),
                             broker=SlowBroker())
        module.start()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(module.call, "slow")
            try:
                self.assertTrue(entered.wait(1))
                before = time.monotonic()
                module.stop()
                self.assertLess(time.monotonic() - before, 1)
                with self.assertRaises(AegisError) as error:
                    pending.result(timeout=1)
                self.assertEqual(error.exception.code, "AEGIS_STOPPED")
            finally:
                release.set()
        status = module.status()
        self.assertEqual(status["state"], "stopped")
        self.assertFalse(status["process_running"])
        self.assertEqual(self.notifications, [])

    def test_oversized_guest_hostcall_kills_before_dispatch(self):
        imports = '(import "nyxion_aegis" "host_call" (func $host (param i32 i32 i32 i32) (result i32)))'
        body = "(drop (call $host (i32.const 0) (i32.const 300000) (i32.const 0) (i32.const 1))) (i64.const 0)"
        module = self.module(fixture(body, imports=imports))
        module.start()
        with self.assertRaises(AegisError) as error:
            module.call("oversized")
        self.assertEqual(error.exception.code, "AEGIS_MEMORY_BOUNDS")
        self.assert_dead(module, "AEGIS_MEMORY_BOUNDS")

    def test_a_rejected_broker_capability_is_fatal_even_when_declared_and_granted(self):
        class RejectBroker:
            def dispatch(self, capability, operation, params):
                raise PermissionViolation("private denial must not escape")

        request = {"capability": "bus.call", "operation": "bus.call", "params": {}}
        module = self.module(fixture(host_request=request), declared=("bus.call",), granted=("bus.call",),
                             broker=RejectBroker())
        module.start()
        with self.assertRaises(AegisError) as error:
            module.call("denied")
        self.assertEqual(error.exception.code, "AEGIS_PERMISSION_DENIED")
        self.assert_dead(module, "AEGIS_PERMISSION_DENIED")


if __name__ == "__main__":
    unittest.main()
