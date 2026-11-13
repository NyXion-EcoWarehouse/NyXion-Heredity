"""Host-owned module namespaces backed by separate Aegis worker processes."""

from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
import importlib.util
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from nyxion_heredity_sdk import (
    BackendError, ContractError, NetworkBindingClosed, NetworkInterfaceGrant,
    NetworkInterfaceRequirement, thaw_json,
)
from nyxion_heredity_sdk.sandbox import SandboxModuleManifest

from .aegis import AegisError, CapabilityBroker, PermissionViolation, SandboxedModule


_ID = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
_METHOD = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}\Z")


class ModuleBusError(ValueError):
    def __init__(self, message, code="MODULE_OPERATION_REJECTED", status=400):
        super().__init__(message)
        self.code, self.status = code, status


@dataclass
class _Node:
    record: dict = field(repr=False)
    manifest: SandboxModuleManifest
    state: str = "installed"
    worker: object = None
    scope: object = None
    requirements: dict = field(default_factory=dict, repr=False)
    bindings: dict = field(default_factory=dict, repr=False)
    error: dict | None = None
    generation: str = field(default_factory=lambda: uuid4().hex)
    lifecycle: threading.RLock = field(default_factory=threading.RLock)

    @property
    def namespace(self):
        return "/modules/" + self.manifest.id

    @property
    def module_id(self):
        identifier = "market." + self.manifest.id
        return (identifier if len(identifier) <= 128 else
                identifier[:95] + "." + hashlib.sha256(identifier.encode()).hexdigest()[:24])


class SandboxModuleBus:
    """Namespace registration never imports downloaded Python or native code."""

    def __init__(self, state_dir, allocator, host_version, *, record_provider=None,
                 runtime_factory=SandboxedModule):
        self.state_dir = Path(state_dir).resolve() / "aegis" / "modules"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.allocator, self.host_version = allocator, host_version
        self.record_provider = record_provider
        self.runtime_factory = runtime_factory
        self._lock = threading.Condition(threading.RLock())
        self._nodes, self._jobs = {}, {}
        self._registration_locks = {}
        self._queue = deque()
        self._closed = False
        self._events = deque(maxlen=128)
        self._worker = threading.Thread(
            target=self._run, name="nyxion-aegis-module-control", daemon=True)
        self._worker.start()

    @staticmethod
    def sandbox_info():
        try:
            available = importlib.util.find_spec("wasmtime") is not None
            if available:
                importlib.import_module("wasmtime")
        except (ImportError, OSError, RuntimeError):
            available = False
        return {
            "name": "Aegis", "runtime": "wasmtime", "isolation": "process+wasm",
            "available": available, "supported": available, "required": True,
            "scope": "market-wasm-modules",
            "permissions": ["fs.read", "fs.write", "net.connect",
                            "bus.call", "bus.publish", "bus.subscribe"],
            "reason": (None if available else "Aegis 的 WebAssembly 运行时尚未就绪。"),
            "legacy_embedded_isolated": False,
        }

    @staticmethod
    def _version(value):
        if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
            raise ModuleBusError("模块版本格式无效。", "INVALID_MODULE_VERSION")
        return tuple(int(part) for part in value.split("."))

    @staticmethod
    def _host_version(value):
        from .versioning import host_version
        try:
            return host_version(value)
        except ValueError:
            raise ModuleBusError("宿主版本格式无效。", "INVALID_MODULE_VERSION") from None

    def _get(self, identifier):
        try:
            return self._nodes[identifier]
        except (KeyError, TypeError):
            raise ModuleBusError("模块不存在。", "MODULE_NOT_FOUND", 404) from None

    def _public(self, node):
        info = node.worker.status() if node.worker is not None else {}
        return {
            "id": node.manifest.id, "name": node.manifest.name,
            "version": node.manifest.version, "namespace": node.namespace,
            "state": node.state, "permissions": list(node.record["approved_permissions"]),
            "sandbox": {
                "runtime": "wasmtime", "isolation": "process+wasm",
                "isolated": info.get("isolated") is True,
                "process_running": info.get("process_running") is True,
            },
            "error": deepcopy(node.error), "installed_at": node.record["installed_at"],
        }

    def snapshot(self):
        with self._lock:
            modules = [self._public(node) for node in self._nodes.values()]
            return {
                "modules": modules,
                "tree": {"namespace": "/", "children": [
                    {"namespace": "/modules", "children": [
                        {"namespace": node.namespace, "module_id": node.manifest.id,
                         "state": node.state} for node in self._nodes.values()]}]},
                "jobs": deepcopy(list(self._jobs.values())),
                "events": deepcopy(list(self._events)),
            }

    def register(self, record, *, autostart=False):
        manifest = SandboxModuleManifest.from_dict(record["manifest"])
        if self._host_version(self.host_version) < self._version(manifest.min_heredity_version):
            raise ModuleBusError("宿主版本过低，请先更新 Heredity。",
                                 "HEREDITY_VERSION_TOO_OLD", 409)
        if record["plugin_id"] != manifest.id or record["version"] != manifest.version:
            raise ModuleBusError("模块身份与安装记录不一致。", "MODULE_IDENTITY_MISMATCH", 409)
        if record.get("status") == "banned":
            raise ModuleBusError("该模块已禁止运行。", "MODULE_BANNED", 403)
        approved = record.get("approved_permissions")
        if (type(approved) is not list or any(not isinstance(value, str) for value in approved)
                or len(approved) != len(set(approved))
                or set(approved) != set(manifest.permissions)):
            raise ModuleBusError("模块权限确认与安装包不一致。", "MODULE_PERMISSION_MISMATCH", 409)
        with self._lock:
            if self._closed:
                raise ModuleBusError("模块总线已关闭。", "MODULE_BUS_CLOSED", 503)
            registration_lock = self._registration_locks.setdefault(manifest.id, threading.RLock())
        with registration_lock:
            with self._lock:
                previous = self._nodes.get(manifest.id)
            if previous is not None:
                self._stop_node(previous)
            node = _Node(deepcopy(record), manifest)
            with self._lock:
                if self._closed:
                    raise ModuleBusError("模块总线已关闭。", "MODULE_BUS_CLOSED", 503)
                self._nodes[manifest.id] = node
            if autostart:
                self._start_node(node)
            return {"started": node.state == "running", "module": self._public(node)}

    def _failure(self, node, code, generation=None):
        with self._lock:
            if (self._nodes.get(node.manifest.id) is not node
                    or (generation is not None and node.generation != generation)):
                return
            node.state = "failed"
            node.error = {"code": code, "message": "Aegis 已终止异常模块进程。"}
            self._events.append({
                "module_id": node.manifest.id, "namespace": node.namespace,
                "code": code, "time": time.time(),
            })
            # A replacement cannot acquire its scope until this revocation ends.
            self.allocator.release_module(node.module_id)

    def _start_node(self, node):
        with node.lifecycle:
            with self._lock:
                if self._closed:
                    raise ModuleBusError("模块总线已关闭。", "MODULE_BUS_CLOSED", 503)
                if self._nodes.get(node.manifest.id) is not node:
                    raise ModuleBusError("模块已更换。", "MODULE_GENERATION_CHANGED", 409)
                if node.state == "running":
                    return self._public(node)
            record = (self.record_provider(node.manifest.id) if self.record_provider is not None
                      else node.record)
            manifest = SandboxModuleManifest.from_dict(record["manifest"])
            if record.get("status") == "banned":
                raise ModuleBusError("该模块已禁止运行。", "MODULE_BANNED", 403)
            if manifest.to_dict() != node.manifest.to_dict():
                raise ModuleBusError("模块包已更换，请重新登记。", "MODULE_PACKAGE_CHANGED", 409)
            if self._host_version(self.host_version) < self._version(manifest.min_heredity_version):
                raise ModuleBusError("宿主版本过低，请先更新 Heredity。",
                                     "HEREDITY_VERSION_TOO_OLD", 409)
            if (record.get("plugin_id") != manifest.id or record.get("version") != manifest.version
                    or type(record.get("approved_permissions")) is not list
                    or any(not isinstance(value, str) for value in record["approved_permissions"])
                    or len(record["approved_permissions"]) != len(set(record["approved_permissions"]))
                    or set(record["approved_permissions"]) != set(manifest.permissions)):
                raise ModuleBusError("模块权限或身份记录已变化，请重新确认安装。",
                                     "MODULE_PERMISSION_MISMATCH", 409)
            with self._lock:
                node.state, node.error = "starting", None
                node.record = deepcopy(record)
                node.generation = uuid4().hex
                generation = node.generation
                node.requirements, node.bindings = {}, {}
                node.scope = self.allocator.for_module(node.module_id)
            permissions = record["approved_permissions"]
            worker = None
            try:
                broker = CapabilityBroker(
                    permissions, self.state_dir / manifest.id,
                    network_allowlist=("*public*",) if "net.connect" in permissions else (),
                    host_handler=lambda capability, operation, params:
                        self._host_call(node, capability, operation, params, generation=generation))
                worker = self.runtime_factory(
                    manifest, record["module_path"], permissions, self.state_dir / manifest.id,
                    namespace=node.namespace, broker=broker,
                    on_failure=lambda module_id, code: self._failure(node, code, generation))
                node.worker = worker
                worker.start()
            except BaseException:
                if worker is not None:
                    worker.stop()
                elif "broker" in locals():
                    broker.close()
                self.allocator.release_module(node.module_id)
                with self._lock:
                    node.state = "failed"
                    if node.error is None:
                        node.error = {"code": "AEGIS_START_FAILED",
                                      "message": "模块无法在 Aegis 沙箱中启动。"}
                raise
            with self._lock:
                closed = self._closed
                valid = (not closed and self._nodes.get(node.manifest.id) is node
                         and node.generation == generation and node.state == "starting"
                         and worker.status().get("process_running") is True)
                if closed:
                    node.state = "stopped"
                elif valid:
                    node.state = "running"
                else:
                    node.state = "failed"
                    if node.error is None:
                        node.error = {"code": "AEGIS_START_FAILED",
                                      "message": "模块启动期间进程已停止。"}
            if closed:
                worker.stop()
                self.allocator.release_module(node.module_id)
                raise ModuleBusError("模块总线已关闭。", "MODULE_BUS_CLOSED", 503)
            if not valid:
                worker.stop()
                self.allocator.release_module(node.module_id)
                raise ModuleBusError("模块启动期间进程已停止。", "AEGIS_START_FAILED", 409)
            return self._public(node)

    def _stop_node(self, node):
        with node.lifecycle:
            with self._lock:
                current = self._nodes.get(node.manifest.id) is node
                node.generation = uuid4().hex
                node.scope = None
            if current:
                self.allocator.release_module(node.module_id)
            try:
                if node.worker is not None:
                    node.worker.stop()
            except Exception:
                with self._lock:
                    node.state = "failed"
                    node.error = {"code": "AEGIS_STOP_FAILED",
                                  "message": "Aegis 未确认模块进程停止。"}
                raise ModuleBusError("Aegis 未确认模块进程停止。", "AEGIS_STOP_FAILED", 409) from None
            with self._lock:
                node.bindings.clear()
                if node.state != "failed":
                    node.state = "stopped"
            return self._public(node)

    def _require_generation(self, node, generation):
        with self._lock:
            if (self._closed or self._nodes.get(node.manifest.id) is not node
                    or node.generation != generation
                    or node.state not in {"starting", "running"} or node.scope is None):
                raise PermissionViolation("The module namespace is not active")
            return node.scope

    def _request(self, node, params, generation):
        allowed = {"transport_types", "directions", "account", "purpose",
                   "max_instances", "max_interfaces"}
        if set(params) - allowed or "transport_types" not in params:
            raise PermissionViolation("Module interface declaration fields are invalid")
        try:
            requirement = NetworkInterfaceRequirement(module_id=node.module_id, **params)
            scope = self._require_generation(node, generation)
            declaration = scope.declare_interface(requirement)
        except (ContractError, NetworkBindingClosed):
            raise PermissionViolation("Module interface declaration is outside its scope") from None
        with self._lock:
            self._require_generation(node, generation)
            node.requirements[declaration["request_id"]] = requirement
        return declaration

    def _binding(self, node, interface_id, generation):
        with self._lock:
            scope = self._require_generation(node, generation)
            requirements = tuple(node.requirements.values())
        for requirement in requirements:
            current = scope.declare_interface(requirement)
            for value in current["grants"]:
                if value["interface_id"] != interface_id:
                    continue
                grant = NetworkInterfaceGrant(**value)
                with self._lock:
                    self._require_generation(node, generation)
                    binding = node.bindings.get(grant.grant_id)
                if binding is None or binding.closed:
                    binding = scope.bind_grant(node.module_id, grant)
                    with self._lock:
                        try:
                            self._require_generation(node, generation)
                        except PermissionViolation:
                            binding.close()
                            raise
                        node.bindings[grant.grant_id] = binding
                return binding
        raise PermissionViolation("The interface was not allocated to this module")

    def _host_call(self, node, capability, operation, params, *, generation=None):
        generation = node.generation if generation is None else generation
        self._require_generation(node, generation)
        if capability == "bus.call" and operation == "interfaces.declare":
            return self._request(node, params, generation)
        if capability == "bus.call" and operation == "tree.self":
            if params:
                raise PermissionViolation("Tree lookup does not accept another namespace")
            return {"namespace": node.namespace, "module_id": node.module_id}
        expected = (
            {"interface_id", "action", "params"} if operation == "onebot.call" else
            {"interface_id", "event"} if operation == "onebot.event" else
            {"interface_id"} if operation == "onebot.receive" else None)
        pairs = {"onebot.call": "bus.call", "onebot.event": "bus.publish",
                 "onebot.receive": "bus.subscribe"}
        if (expected is None or capability != pairs[operation] or set(params) != expected
                or not isinstance(params.get("interface_id"), str)):
            raise PermissionViolation("The bus operation does not match its granted capability")
        try:
            binding = self._binding(node, params["interface_id"], generation)
            if operation == "onebot.call":
                result = binding.call(params["action"], params["params"], timeout=2)
            elif operation == "onebot.event":
                result = binding.publish_event(params["event"])
            else:
                record = binding.receive(timeout=0)
                result = (None if record is None else {
                    "id": record.id, "interface_id": record.interface_id, "kind": record.kind,
                    "payload": thaw_json(record.payload), "timestamp": record.timestamp})
            return thaw_json(result)
        except BackendError as failure:
            return {"status": "failed", "retcode": failure.retcode,
                    "data": None, "message": "The allocated interface could not complete this action."}
        except (ContractError, NetworkBindingClosed, TypeError):
            raise PermissionViolation("The bus operation exceeded the module allocation") from None

    def call(self, identifier, method, params=None):
        if (not isinstance(method, str) or not _METHOD.fullmatch(method)
                or (params is not None and type(params) is not dict)):
            raise ModuleBusError("模块方法无效。")
        with self._lock:
            node = self._get(identifier)
            worker, generation = node.worker, node.generation
            if node.state != "running" or worker is None:
                raise ModuleBusError("模块未运行。", "MODULE_NOT_RUNNING", 409)
        result = worker.call(method, params or {})
        with self._lock:
            if node.generation != generation:
                raise ModuleBusError("模块已更换，调用结果属于旧实例。",
                                     "MODULE_GENERATION_CHANGED", 409)
        return result

    def submit(self, operation, identifier):
        if operation not in {"start", "stop", "restart"}:
            raise ModuleBusError("模块操作无效。")
        with self._lock:
            self._get(identifier)
            if self._closed or len(self._queue) >= 32:
                raise ModuleBusError("模块任务队列已满或已关闭。", "MODULE_QUEUE_UNAVAILABLE", 503)
            for job in self._jobs.values():
                if (job["plugin_id"] == identifier and job["kind"] == "module_" + operation
                        and job["state"] in {"queued", "running"}):
                    return deepcopy(job)
            if len(self._jobs) >= 128:
                obsolete = next((key for key, value in self._jobs.items()
                                 if value["state"] in {"completed", "failed", "cancelled"}), None)
                if obsolete is not None:
                    self._jobs.pop(obsolete)
            job_id, now = uuid4().hex, time.time()
            job = {"job_id": job_id, "kind": "module_" + operation,
                   "plugin_id": identifier, "state": "queued", "created_at": now,
                   "updated_at": now, "result": None, "error": None}
            self._jobs[job_id] = job
            self._queue.append((job_id, operation, identifier))
            self._lock.notify_all()
            return deepcopy(job)

    def job(self, identifier):
        with self._lock:
            try:
                return deepcopy(self._jobs[identifier])
            except KeyError:
                raise ModuleBusError("模块任务不存在。", "MODULE_JOB_NOT_FOUND", 404) from None

    def enforce_catalog(self):
        if self.record_provider is None:
            return
        with self._lock:
            identifiers = tuple(self._nodes)
        for identifier in identifiers:
            try:
                self.record_provider(identifier)
            except Exception:
                with self._lock:
                    node = self._nodes.get(identifier)
                if node is not None:
                    self._stop_node(node)
                    self._failure(node, "AEGIS_PACKAGE_REVOKED")

    def _run(self):
        while True:
            with self._lock:
                while not self._closed and not self._queue:
                    self._lock.wait()
                if self._closed:
                    return
                job_id, operation, identifier = self._queue.popleft()
                job = self._jobs[job_id]
                job.update(state="running", updated_at=time.time())
            try:
                node = self._get(identifier)
                if operation in {"stop", "restart"}:
                    self._stop_node(node)
                result = self._start_node(node) if operation in {"start", "restart"} else self._public(node)
                failure = None
            except Exception as error:
                result = None
                code = getattr(error, "code", "MODULE_OPERATION_FAILED")
                failure = {"code": code, "message": "模块操作未完成，请检查 Aegis 状态和安装包。"}
            with self._lock:
                job.update(state="failed" if failure else "completed",
                           updated_at=time.time(), result=result, error=failure)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._queue.clear()
            nodes = tuple(self._nodes.values())
            self._lock.notify_all()
        for node in nodes:
            self._stop_node(node)
        self._worker.join(timeout=12)
        if self._worker.is_alive():
            raise RuntimeError("The Aegis module control worker did not stop.")
