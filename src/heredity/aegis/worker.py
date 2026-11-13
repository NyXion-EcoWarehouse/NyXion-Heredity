"""Standalone isolated Wasmtime worker. Its stdout is a bounded private protocol."""

import base64
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import threading


MAX_FRAME = 256 * 1024
SAFE_WASI = frozenset({
    "args_get", "args_sizes_get", "environ_get", "environ_sizes_get",
    "clock_res_get", "clock_time_get", "random_get", "sched_yield", "proc_exit",
})


def fail(code):
    emit({"kind": "fault", "code": code})
    os._exit(77)


def emit(value):
    raw = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
    if len(raw) > MAX_FRAME:
        os._exit(78)
    sys.stdout.buffer.write(raw + b"\n")
    sys.stdout.buffer.flush()


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def decode(raw):
    if not raw or len(raw) > MAX_FRAME:
        raise ValueError("invalid frame size")
    value = json.loads(raw, object_pairs_hook=unique,
                       parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
    pending, nodes = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 24 or nodes > 16384:
            raise ValueError("invalid JSON depth or size")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return value


def receive():
    raw = sys.stdin.buffer.readline(MAX_FRAME + 2)
    if not raw or not raw.endswith(b"\n"):
        raise ValueError("invalid IPC frame")
    return decode(raw)


def signature(value):
    return ([str(item) for item in value.params], [str(item) for item in value.results])


def main():
    try:
        if len(sys.argv) != 2:
            fail("AEGIS_RUNTIME_UNAVAILABLE")
        dependency_root = Path(sys.argv[1])
        if (not dependency_root.is_absolute()
                or not (dependency_root / "wasmtime" / "__init__.py").is_file()):
            fail("AEGIS_RUNTIME_UNAVAILABLE")
        sys.path.insert(0, str(dependency_root))
        import wasmtime
    except (ImportError, RuntimeError, OSError):
        fail("AEGIS_RUNTIME_UNAVAILABLE")
    try:
        initial = receive()
        if type(initial) is not dict or initial.get("command") != "initialize":
            fail("AEGIS_IPC_INVALID")
        limits = initial["limits"]
        with open(initial["module_path"], "rb") as stream:
            wasm = stream.read(16 * 1024 * 1024 + 1)
        if (not wasm.startswith(b"\0asm\1\0\0\0") or len(wasm) > 16 * 1024 * 1024
                or hashlib.sha256(wasm).hexdigest() != initial["sha256"]):
            fail("AEGIS_INVALID_MODULE")
        config = wasmtime.Config()
        config.consume_fuel = True
        config.epoch_interruption = True
        config.wasm_threads = False
        config.wasm_memory64 = False
        config.wasm_multi_memory = False
        config.wasm_component_model = False
        config.max_wasm_stack = 512 * 1024
        engine = wasmtime.Engine(config)
        stop_tick = threading.Event()

        def tick():
            while not stop_tick.wait(0.025):
                engine.increment_epoch()

        ticker = threading.Thread(target=tick, name="aegis-epoch", daemon=True)
        ticker.start()
        module = wasmtime.Module(engine, wasm)
        for exported in module.exports:
            if (isinstance(exported.type, wasmtime.MemoryType)
                    and exported.type.limits.min * 65536 > limits["memory_bytes"]):
                fail("AEGIS_MEMORY_LIMIT")
        store = wasmtime.Store(engine)
        store.set_limits(memory_size=limits["memory_bytes"], table_elements=4096,
                         instances=1, tables=2, memories=1)
        store.set_fuel(limits["fuel"])
        store.set_epoch_deadline(max(1, math.ceil(limits["call_timeout"] / 0.025)))
        wasi = wasmtime.WasiConfig()
        wasi.argv = []
        wasi.env = []
        store.set_wasi(wasi)
        linker = wasmtime.Linker(engine)
        linker.define_wasi()
        linker.allow_shadowing = True
        sequence = 0

        def read_memory(memory, caller, pointer, count):
            if (type(pointer) is not int or type(count) is not int or pointer < 0
                    or not 0 <= count <= MAX_FRAME
                    or pointer + count > memory.data_len(caller)):
                fail("AEGIS_MEMORY_BOUNDS")
            return bytes(memory.read(caller, pointer, pointer + count))

        def host_call(caller, pointer, count, out_pointer, out_capacity):
            nonlocal sequence
            memory = caller.get("memory")
            if not isinstance(memory, wasmtime.Memory):
                fail("AEGIS_INVALID_ABI")
            try:
                request = decode(read_memory(memory, caller, pointer, count))
            except (ValueError, TypeError, RecursionError):
                fail("AEGIS_IPC_INVALID")
            sequence += 1
            emit({"kind": "host_call", "id": sequence, "request": request})
            try:
                response = receive()
                if (type(response) is not dict or response.get("kind") != "host_result"
                        or response.get("id") != sequence):
                    fail("AEGIS_IPC_INVALID")
                raw = json.dumps(response["result"], ensure_ascii=True, allow_nan=False,
                                 separators=(",", ":")).encode()
                if (type(out_capacity) is not int or out_capacity < len(raw)
                        or out_pointer < 0 or out_capacity > MAX_FRAME
                        or out_pointer + len(raw) > memory.data_len(caller)):
                    fail("AEGIS_MEMORY_BOUNDS")
                memory.write(caller, raw, out_pointer)
                return len(raw)
            except (ValueError, TypeError, KeyError, RecursionError):
                fail("AEGIS_IPC_INVALID")

        for imported in module.imports:
            if imported.module == "nyxion_aegis" and imported.name == "host_call":
                expected = wasmtime.FuncType([wasmtime.ValType.i32()] * 4, [wasmtime.ValType.i32()])
                if (not isinstance(imported.type, wasmtime.FuncType)
                        or signature(imported.type) != signature(expected)):
                    fail("AEGIS_INVALID_ABI")
                linker.define_func("nyxion_aegis", "host_call", expected, host_call, access_caller=True)
            elif imported.module == "wasi_snapshot_preview1":
                if not isinstance(imported.type, wasmtime.FuncType):
                    fail("AEGIS_IMPORT_DENIED")
                if imported.name not in SAFE_WASI:
                    def denied(*args):
                        fail("AEGIS_PERMISSION_DENIED")
                    linker.define_func(imported.module, imported.name, imported.type, denied)
            else:
                fail("AEGIS_IMPORT_DENIED")
        instance = linker.instantiate(store, module)
        exports = instance.exports(store)
        memory, alloc, invoke = exports.get("memory"), exports.get("aegis_alloc"), exports.get("aegis_call")
        if (not isinstance(memory, wasmtime.Memory) or not isinstance(alloc, wasmtime.Func)
                or not isinstance(invoke, wasmtime.Func)
                or signature(alloc.type(store)) != (["i32"], ["i32"])
                or signature(invoke.type(store)) != (["i32", "i32"], ["i64"])):
            fail("AEGIS_INVALID_ABI")
        emit({"kind": "ready"})
        while True:
            message = receive()
            if message.get("command") == "stop":
                stop_tick.set()
                return
            if (message.get("command") != "call" or type(message.get("request")) is not dict
                    or type(message.get("id")) is not int):
                fail("AEGIS_IPC_INVALID")
            raw = json.dumps(message["request"], ensure_ascii=True, allow_nan=False,
                             separators=(",", ":")).encode()
            store.set_fuel(limits["fuel"])
            store.set_epoch_deadline(max(1, math.ceil(limits["call_timeout"] / 0.025)))
            pointer = alloc(store, len(raw))
            read_memory(memory, store, pointer, len(raw))
            memory.write(store, raw, pointer)
            packed = invoke(store, pointer, len(raw))
            packed &= 0xffffffffffffffff
            pointer, count = packed >> 32, packed & 0xffffffff
            result = decode(read_memory(memory, store, pointer, count))
            if type(result) is not dict:
                fail("AEGIS_INVALID_RESPONSE")
            emit({"kind": "result", "id": message["id"], "result": result})
    except wasmtime.Trap as error:
        if error.trap_code == wasmtime.TrapCode.OUT_OF_FUEL:
            fail("AEGIS_FUEL_EXHAUSTED")
        if error.trap_code == wasmtime.TrapCode.INTERRUPT:
            fail("AEGIS_TIMEOUT")
        if error.trap_code in {wasmtime.TrapCode.MEMORY_OUT_OF_BOUNDS,
                               wasmtime.TrapCode.ALLOCATION_TOO_LARGE}:
            fail("AEGIS_MEMORY_LIMIT")
        fail("AEGIS_GUEST_TRAP")
    except wasmtime.WasmtimeError:
        fail("AEGIS_INVALID_MODULE")
    except Exception:
        fail("AEGIS_IPC_INVALID")


if __name__ == "__main__":
    main()
