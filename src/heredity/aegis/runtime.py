"""Isolated Wasmtime workers; untrusted code runs only inside the guest VM."""

from dataclasses import asdict, dataclass
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time

from nyxion_heredity_sdk.sandbox import SandboxModuleManifest

from .broker import CapabilityBroker, PermissionViolation, _no_links


MAX_FRAME = 256 * 1024
MAX_MODULE_BYTES = 16 * 1024 * 1024
SUPPORTED_PERMISSIONS = frozenset({
    "fs.read", "fs.write", "net.connect", "bus.call", "bus.publish", "bus.subscribe",
})
_METHOD = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")


class AegisError(RuntimeError):
    def __init__(self, code, message="Aegis stopped the module worker."):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SandboxLimits:
    memory_bytes: int = 32 * 1024 * 1024
    fuel: int = 5_000_000
    call_timeout: float = 3.0
    startup_timeout: float = 10.0

    def __post_init__(self):
        for name, low, high in (
                ("memory_bytes", 65536, 128 * 1024 * 1024),
                ("fuel", 1000, 100_000_000)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Aegis resource limit is invalid")
        for name in ("call_timeout", "startup_timeout"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 30:
                raise ValueError("Aegis timeout is invalid")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _decode(raw):
    if not raw or len(raw) > MAX_FRAME:
        raise ValueError("invalid frame size")
    value = json.loads(raw, object_pairs_hook=_unique,
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


def _encode(value):
    raw = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
    _decode(raw)
    return raw + b"\n"


class SandboxedModule:
    """One package, one process, one scoped broker; no implicit permissions."""

    def __init__(self, manifest, module_path, permissions, state_dir, *,
                 namespace=None, limits=None, broker=None, network_allowlist=(),
                 on_failure=None):
        self.manifest = (manifest if isinstance(manifest, SandboxModuleManifest)
                         else SandboxModuleManifest.from_dict(manifest))
        if set(self.manifest.permissions) - SUPPORTED_PERMISSIONS:
            raise AegisError("AEGIS_PERMISSION_UNSUPPORTED", "The package declares an unsupported capability.")
        if type(permissions) not in (tuple, list, set, frozenset):
            raise ValueError("Approved Aegis permissions must be an explicit collection")
        self.permissions = frozenset(permissions)
        if (any(not isinstance(value, str) for value in permissions)
                or not self.permissions <= set(self.manifest.permissions)):
            raise AegisError("AEGIS_PERMISSION_DENIED", "The host cannot grant undeclared capabilities.")
        self.module_path = Path(module_path).absolute()
        _no_links(self.module_path)
        if not self.module_path.is_file() or self.module_path.suffix != ".wasm":
            raise AegisError("AEGIS_INVALID_MODULE", "Aegis runs only WebAssembly packages.")
        self.state_dir = Path(state_dir).absolute()
        _no_links(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.namespace = namespace or "/modules/" + self.manifest.id
        if (not isinstance(self.namespace, str) or len(self.namespace) > 512
                or not self.namespace.startswith("/") or ".." in self.namespace.split("/")
                or "\\" in self.namespace or any(ord(char) < 33 for char in self.namespace)):
            raise ValueError("Aegis namespace must be a host-issued absolute bus path")
        self.limits = limits or SandboxLimits()
        if not isinstance(self.limits, SandboxLimits):
            raise ValueError("Aegis limits must use SandboxLimits")
        self.broker = broker or CapabilityBroker(
            self.permissions, self.state_dir, network_allowlist=network_allowlist)
        if not callable(getattr(self.broker, "dispatch", None)):
            raise ValueError("An Aegis broker must implement dispatch")
        self.on_failure = on_failure
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._process = None
        self._reader = None
        self._messages = queue.Queue(maxsize=8)
        self._sequence = 0
        self._state = "created"
        self._error_code = None
        self._reported = False
        self._cancelled = threading.Event()

    def status(self):
        with self._lock:
            process = self._process
            return {"state": self._state, "module_id": self.manifest.id,
                    "namespace": self.namespace, "sandbox": "wasmtime-process",
                    "isolated": True, "permissions": sorted(self.permissions),
                    "pid": process.pid if process is not None else None,
                    "process_running": process is not None and process.poll() is None,
                    "exit_code": process.poll() if process is not None else None,
                    "error_code": self._error_code}

    def _terminate(self):
        with self._lock:
            process = self._process
        if process is not None:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                raise AegisError("AEGIS_TERMINATION_FAILED") from None
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        close = getattr(self.broker, "close", None)
        if callable(close):
            close()

    def _fail(self, code):
        with self._lock:
            cancelled = self._cancelled.is_set()
            if cancelled:
                self._state = "stopped"
                code = "AEGIS_STOPPED"
            else:
                self._state, self._error_code = "failed", code
            notify = not self._reported
            self._reported = True
        self._terminate()
        if notify and not cancelled and self.on_failure is not None:
            try:
                self.on_failure(self.manifest.id, code)
            except Exception:
                # Reporting cannot keep a failed worker alive.
                pass
        raise AegisError(code)

    def _read_output(self, process):
        try:
            while True:
                raw = process.stdout.readline(MAX_FRAME + 2)
                if not raw:
                    self._messages.put_nowait({"kind": "eof"})
                    return
                if not raw.endswith(b"\n") or len(raw) > MAX_FRAME:
                    process.kill()
                    self._messages.put_nowait({"kind": "fault", "code": "AEGIS_IPC_LIMIT"})
                    return
                try:
                    value = _decode(raw)
                except (ValueError, TypeError, RecursionError):
                    value = {"kind": "fault", "code": "AEGIS_IPC_INVALID"}
                self._messages.put_nowait(value)
        except (OSError, ValueError, queue.Full):
            if process.poll() is None:
                process.kill()

    def _send(self, value):
        if self._cancelled.is_set():
            self._fail("AEGIS_STOPPED")
        raw = _encode(value)
        with self._lock:
            process = self._process
        if process is None or process.poll() is not None:
            self._fail("AEGIS_WORKER_EXITED")
        try:
            process.stdin.write(raw)
            process.stdin.flush()
        except (OSError, ValueError):
            self._fail("AEGIS_WORKER_EXITED")

    def _host_call(self, value, deadline):
        request = value.get("request")
        if (type(value.get("id")) is not int or type(request) is not dict
                or set(request) != {"capability", "operation", "params"}
                or not isinstance(request["capability"], str)
                or not isinstance(request["operation"], str) or type(request["params"]) is not dict):
            self._fail("AEGIS_IPC_INVALID")
        capability = request["capability"]
        if capability not in self.permissions:
            self._fail("AEGIS_PERMISSION_DENIED")
        results = queue.Queue(maxsize=1)

        def dispatch():
            try:
                result = self.broker.dispatch(capability, request["operation"], request["params"])
                _encode(result)
                results.put(("result", result))
            except PermissionViolation:
                results.put(("denied", None))
            except Exception:
                results.put(("result", {"ok": False, "code": "AEGIS_CAPABILITY_FAILED"}))

        threading.Thread(target=dispatch, name="aegis-capability-" + self.manifest.id,
                         daemon=True).start()
        while True:
            if self._cancelled.is_set():
                self._fail("AEGIS_STOPPED")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._fail("AEGIS_TIMEOUT")
            try:
                kind, result = results.get(timeout=min(remaining, 0.05))
                break
            except queue.Empty:
                continue
        if kind == "denied":
            self._fail("AEGIS_PERMISSION_DENIED")
        self._send({"kind": "host_result", "id": value["id"], "result": result})

    def _wait(self, deadline, *, expected, sequence=None):
        while True:
            if self._cancelled.is_set():
                self._fail("AEGIS_STOPPED")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._fail("AEGIS_TIMEOUT")
            try:
                value = self._messages.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue
            if type(value) is not dict:
                self._fail("AEGIS_IPC_INVALID")
            kind = value.get("kind")
            if kind == "host_call":
                self._host_call(value, deadline)
                continue
            if kind == "fault":
                code = value.get("code")
                if not isinstance(code, str) or not re.fullmatch(r"AEGIS_[A-Z_]{1,64}", code):
                    code = "AEGIS_IPC_INVALID"
                self._fail(code)
            if kind == "eof":
                self._fail("AEGIS_WORKER_EXITED")
            if kind != expected or (sequence is not None and value.get("id") != sequence):
                self._fail("AEGIS_IPC_INVALID")
            return value.get("result")

    def start(self):
        with self._operation_lock:
            with self._lock:
                if self._state != "created":
                    raise AegisError("AEGIS_ALREADY_STARTED")
                self._state = "starting"
            runtime_spec = importlib.util.find_spec("wasmtime")
            if runtime_spec is None or runtime_spec.origin is None:
                self._fail("AEGIS_RUNTIME_UNAVAILABLE")
            try:
                with self.module_path.open("rb") as stream:
                    wasm = stream.read(MAX_MODULE_BYTES + 1)
            except OSError:
                self._fail("AEGIS_INVALID_MODULE")
            if not wasm.startswith(b"\0asm\1\0\0\0") or len(wasm) > MAX_MODULE_BYTES:
                self._fail("AEGIS_INVALID_MODULE")
            environment = {name: os.environ[name] for name in (
                "SYSTEMROOT", "WINDIR", "PROCESSOR_ARCHITECTURE", "PROCESSOR_ARCHITEW6432")
                           if name in os.environ}
            trusted_dependency = str(Path(runtime_spec.origin).resolve().parent.parent)
            arguments = [sys.executable, "-I", str(Path(__file__).with_name("worker.py")),
                         trusted_dependency]
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {
                "start_new_session": True}
            with self._lock:
                if self._cancelled.is_set():
                    raise AegisError("AEGIS_STOPPED")
                try:
                    process = subprocess.Popen(
                        arguments, cwd=self.state_dir, env=environment,
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        close_fds=True, **options)
                except OSError:
                    self._fail("AEGIS_RUNTIME_UNAVAILABLE")
                self._process = process
            self._reader = threading.Thread(
                target=self._read_output, args=(process,), name="aegis-ipc-" + self.manifest.id,
                daemon=True)
            self._reader.start()
            self._send({"command": "initialize", "module_path": str(self.module_path),
                        "sha256": hashlib.sha256(wasm).hexdigest(), "limits": asdict(self.limits)})
            self._wait(time.monotonic() + self.limits.startup_timeout, expected="ready")
            result = self._call("handshake", {
                "api_major": 1, "api_minor": 0, "module_id": self.manifest.id})
            if (type(result) is not dict or result.get("module_id") != self.manifest.id
                    or type(result.get("api_major")) is not int or result["api_major"] != 1
                    or type(result.get("api_minor")) is not int or result["api_minor"] != 0):
                self._fail("AEGIS_HANDSHAKE_FAILED")
            with self._lock:
                if self._cancelled.is_set():
                    self._fail("AEGIS_STOPPED")
                self._state = "running"
            return result

    def _call(self, method, params):
        self._sequence += 1
        self._send({"command": "call", "id": self._sequence,
                    "request": {"method": method, "params": params}})
        return self._wait(time.monotonic() + self.limits.call_timeout,
                          expected="result", sequence=self._sequence)

    def call(self, method, params=None):
        if (not isinstance(method, str) or not _METHOD.fullmatch(method)
                or (params is not None and type(params) is not dict)):
            raise ValueError("Aegis calls require a method and JSON object params")
        params = {} if params is None else params
        _encode(params)
        with self._operation_lock:
            with self._lock:
                if self._state != "running":
                    raise AegisError(self._error_code or "AEGIS_NOT_RUNNING")
            return self._call(method, params)

    def stop(self):
        self._cancelled.set()
        with self._lock:
            if self._state != "failed":
                self._state = "stopped"
        self._terminate()

    close = stop
