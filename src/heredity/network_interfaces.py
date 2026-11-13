"""Persistent, ID-addressed network resources owned by the Heredity host."""

from copy import deepcopy
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib import request
from urllib.parse import urlsplit

from nyxion_heredity_sdk import BackendError
from nyxion_heredity_sdk.onebot import _json_copy
from nyxion_heredity_sdk.networks import validate_interface_id

from .event_hub import EventGapError
from .host_connections import HostConnectionSettings
from .network_bus import NetworkModuleBus
from .network_http import HTTPClientTransport, InterfaceEventHub
from .network_errors import listener_failure
from .onebot import ACTION_PATTERN, _NoRedirect, create_server
from .plugins.gateway import AccountRoutingGate, PluginGatewayBackend


INTERFACE_TYPES = (
    {"type": "http_server", "label": "HTTP 服务端", "implemented": True,
     "description": "接收 OneBot 动作，提供可选 SSE 事件流。"},
    {"type": "http_client", "label": "HTTP 客户端", "implemented": True,
     "description": "向配置的地址发送事件，或调用远端 OneBot 动作。"},
    {"type": "websocket_server", "label": "正向 WebSocket", "implemented": True,
     "description": "监听 WebSocket 连接，处理动作并向客户端分发事件。"},
    {"type": "websocket_client", "label": "反向 WebSocket", "implemented": True,
     "description": "连接配置的 WebSocket 地址；断线不重放动作。"},
)
_TYPES = {item["type"] for item in INTERFACE_TYPES}
_ACCOUNT = re.compile(r"[a-z][a-z0-9_.-]{0,127}:[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class NetworkInterfaceError(ValueError):
    def __init__(self, message, code="INVALID_NETWORK_INTERFACE", status=400):
        super().__init__(message)
        self.code, self.status = code, status


def _account(value):
    if value is not None and (not isinstance(value, str) or not _ACCOUNT.fullmatch(value)):
        raise NetworkInterfaceError("Account must be platform:self_id or null.")
    return value


def _settings(kind, data):
    allowed = {"host", "port", "path", "url", "access_token",
               "heartbeat_seconds", "sse_enabled", "event_urls"}
    if type(data) is not dict or set(data) - allowed:
        raise NetworkInterfaceError("Unknown network interface settings.")
    values = {"host": "127.0.0.1", "port": 0, "path": "/", "url": "",
              "access_token": "", "heartbeat_seconds": 15.0,
              "sse_enabled": False, "event_urls": []}
    values.update(data)
    heartbeat = values["heartbeat_seconds"]
    if (type(heartbeat) not in (int, float) or not math.isfinite(heartbeat)
            or not 1 <= heartbeat <= 86400):
        raise NetworkInterfaceError("Heartbeat must be finite and in 1..86400 seconds.")
    try:
        HostConnectionSettings._secret(values["access_token"], "access_token")
    except ValueError:
        raise NetworkInterfaceError("Tokens must be bounded ASCII without whitespace.") from None
    if type(values["sse_enabled"]) is not bool:
        raise NetworkInterfaceError("SSE enabled must be boolean.")
    try:
        values["event_urls"] = list(HostConnectionSettings._urls(values["event_urls"]))
    except ValueError:
        raise NetworkInterfaceError("Event receiver URLs are invalid.") from None
    if kind.endswith("_server"):
        import ipaddress
        host, port, path = values["host"], values["port"], values["path"]
        if not isinstance(host, str):
            raise NetworkInterfaceError("Listener host is invalid.")
        if host != "localhost":
            try:
                ipaddress.ip_address(host)
            except ValueError:
                raise NetworkInterfaceError("Listener host must be an IP or localhost.") from None
        if type(port) is not int or not 0 <= port <= 65535:
            raise NetworkInterfaceError("Listener port must be in 0..65535.")
        if (not isinstance(path, str) or not path.startswith("/") or len(path) > 256
                or any(ord(char) < 33 or ord(char) > 126 for char in path)
                or any(character in path for character in ("?", "#", "\\", "%"))
                or any(part in {".", ".."} for part in path.split("/"))):
            raise NetworkInterfaceError("Listener path must be a plain absolute path.")
        values["path"] = path.rstrip("/") or "/"
        from .onebot import _is_loopback
        if not _is_loopback(host) and not values["access_token"]:
            raise NetworkInterfaceError("A non-loopback listener requires an access token.")
    else:
        url = values["url"]
        try:
            parsed = urlsplit(url)
            schemes = {"http", "https"} if kind == "http_client" else {"ws", "wss"}
            if (not isinstance(url, str) or not 1 <= len(url) <= 2048
                    or any(ord(char) < 33 or ord(char) > 126 for char in url)
                    or "\\" in url or parsed.scheme not in schemes or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.fragment or parsed.query or "#" in url or "?" in url):
                raise ValueError
            parsed.port
        except (ValueError, TypeError):
            raise NetworkInterfaceError("Destination URL must match its transport and contain no credentials.") from None
    return values


def _configuration(data, *, partial=False, previous=None, internal=False):
    allowed = {"name", "type", "enabled", "account", "settings"}
    if type(data) is not dict or set(data) - allowed:
        raise NetworkInterfaceError("Unknown network interface fields.")
    result = deepcopy(previous) if previous is not None else {
        "name": "", "type": None, "enabled": False, "account": None, "settings": {}}
    for key in ("name", "type", "enabled", "account"):
        if key in data:
            result[key] = data[key]
    if (not isinstance(result["name"], str) or not result["name"].strip()
            or len(result["name"]) > 80
            or any(ord(char) < 32 or ord(char) == 127 for char in result["name"])):
        raise NetworkInterfaceError("Interface name must be a visible 1..80 character label.")
    try:
        result["name"].encode("utf-8")
    except UnicodeError:
        raise NetworkInterfaceError("Interface name is not valid Unicode.") from None
    if (not isinstance(result["type"], str) or result["type"] not in _TYPES
            or type(result["enabled"]) is not bool):
        raise NetworkInterfaceError("Interface transport or enabled flag is invalid.")
    result["account"] = _account(result["account"])
    incoming = data.get("settings", {})
    if type(incoming) is not dict:
        raise NetworkInterfaceError("Interface settings must be an object.")
    if not internal and set(incoming).intersection({"access_token", "event_secret"}):
        raise NetworkInterfaceError(
            "Interface credentials are generated by the host; custom values are not accepted.")
    merged = {**result["settings"]}
    for key, value in incoming.items():
        merged[key] = value
    if internal:
        merged.pop("event_secret", None)
    if not merged.get("access_token"):
        merged["access_token"] = secrets.token_urlsafe(32)
    result["settings"] = _settings(result["type"], merged)
    return result


@dataclass
class _Resource:
    config: dict = field(repr=False)
    backend: object
    state: str = "stopped"
    error: str | None = None
    error_code: str | None = None
    transport: object = None
    server: object = None
    worker: object = None
    pump: object = None
    hub: object = None
    generation: int = 0
    event_runtime: object = None
    stop: threading.Event = field(default_factory=threading.Event)
    lifecycle: threading.RLock = field(default_factory=threading.RLock)
    events_lock: threading.RLock = field(default_factory=threading.RLock)
    routing_gate: AccountRoutingGate = field(default_factory=AccountRoutingGate)


class NetworkInterfaceManager:
    """An interface ID selects one resource; it never selects a plugin type."""

    def __init__(self, state_dir, platform_host):
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.state_dir / "network-interfaces.json"
        self.platform_host = platform_host
        self._lock = threading.RLock()
        self._resources = {}
        self._used_ids = set()
        self._closed = False
        self.module_bus = NetworkModuleBus(
            exists=self.exists, dispatch=self._module_call, publish=self.publish_event)
        persisted = self._read(self._path)
        if persisted is not None:
            self._used_ids.update(persisted["used_ids"])
            for source in persisted["interfaces"]:
                if (persisted["schema_version"] == 1
                        and source["interface_id"] == persisted.get("primary_id")):
                    # This entry was implicitly created by the old host.
                    # Keep its ID tombstoned, but do not recreate its listener.
                    continue
                config = _configuration(
                    {key: source[key] for key in ("name", "type", "enabled", "account", "settings")},
                    internal=True)
                config.update({key: source[key] for key in
                               ("interface_id", "created_at", "updated_at")})
                config["revision"] = source.get("revision", 1)
                config["allocation_controlled"] = source.get("allocation_controlled", False)
                gate = AccountRoutingGate(controlled=config["allocation_controlled"])
                resource = _Resource(config, PluginGatewayBackend(
                    platform_host, account=config["account"], routing_gate=gate), routing_gate=gate)
                self._resources[config["interface_id"]] = resource
        self._persist()

    @staticmethod
    def _read(path):
        if not path.exists():
            return None
        if (path.is_symlink() or getattr(path, "is_junction", lambda: False)()
                or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024):
            raise NetworkInterfaceError("Network interface registry has an invalid file type or size.")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise NetworkInterfaceError("Network interface registry contains duplicate fields.")
                result[key] = value
            return result
        data = json.loads(path.read_bytes(), object_pairs_hook=unique)
        if (type(data) is not dict
                or type(data.get("schema_version")) is not int
                or data["schema_version"] not in {1, 2}
                or set(data) != ({"schema_version", "primary_id", "used_ids", "interfaces"}
                                 if data["schema_version"] == 1 else
                                 {"schema_version", "used_ids", "interfaces"})
                or type(data["interfaces"]) is not list or len(data["interfaces"]) > 100
                or type(data["used_ids"]) is not list or len(data["used_ids"]) > 100000):
            raise NetworkInterfaceError("Network interface registry schema is invalid.")
        for identifier in data["used_ids"]:
            validate_interface_id(identifier)
        seen = set()
        for config in data["interfaces"]:
            required = {"interface_id", "name", "type", "enabled", "account", "settings",
                        "created_at", "updated_at"}
            if (type(config) is not dict or not required <= set(config)
                    or set(config) - required - {"revision", "allocation_controlled"}):
                raise NetworkInterfaceError("Network interface registry entry is invalid.")
            identifier = validate_interface_id(config["interface_id"])
            if identifier in seen or identifier not in data["used_ids"]:
                raise NetworkInterfaceError("Network interface identifiers conflict.")
            seen.add(identifier)
            _configuration({key: config[key] for key in
                            ("name", "type", "enabled", "account", "settings")}, internal=True)
            if any(type(config[key]) is not int or config[key] < 0
                   for key in ("created_at", "updated_at")):
                raise NetworkInterfaceError("Network interface timestamps are invalid.")
            if type(config.get("revision", 1)) is not int or config.get("revision", 1) < 1:
                raise NetworkInterfaceError("Network interface revision is invalid.")
            if type(config.get("allocation_controlled", False)) is not bool:
                raise NetworkInterfaceError("Network interface allocation flag is invalid.")
        return data

    def _new_id(self):
        for _ in range(1000):
            identifier = secrets.token_hex(8)
            if identifier not in self._used_ids:
                self._used_ids.add(identifier)
                return identifier
        raise NetworkInterfaceError("Could not allocate a unique network identifier.", status=503)

    def _persist(self):
        if self._path.is_symlink() or getattr(self._path, "is_junction", lambda: False)():
            raise NetworkInterfaceError("Network registry must not be a link.")
        temporary = self._path.with_name(".network-interfaces-" + secrets.token_hex(8) + ".tmp")
        payload = {"schema_version": 2, "used_ids": sorted(self._used_ids),
                   "interfaces": [resource.config for resource in self._resources.values()]}
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self._path)
        finally:
            temporary.unlink(missing_ok=True)

    def exists(self, interface_id):
        validate_interface_id(interface_id)
        with self._lock:
            return not self._closed and interface_id in self._resources

    def _get(self, identifier):
        validate_interface_id(identifier)
        try:
            return self._resources[identifier]
        except KeyError:
            raise NetworkInterfaceError("Network interface was not found.",
                                        "NETWORK_INTERFACE_NOT_FOUND", 404) from None

    def get(self, identifier):
        with self._lock:
            resource = self._get(identifier)
            config, settings = resource.config, resource.config["settings"]
            endpoint, actual_port = None, None
            if resource.server is not None and resource.state == "running":
                address, actual_port = resource.server.server_address[:2]
                host = "127.0.0.1" if address == "0.0.0.0" else "::1" if address == "::" else address
                endpoint = f"http://{'[' + host + ']' if ':' in host else host}:{actual_port}"
                endpoint += "" if settings["path"] == "/" else settings["path"]
            elif resource.transport is not None:
                info = resource.transport.snapshot()
                endpoint, actual_port = info.get("endpoint"), info.get("actual_port")
            elif config["type"].endswith("_client"):
                endpoint = settings["url"]
            state, failure, failure_code = resource.state, resource.error, resource.error_code
            if resource.transport is not None and resource.state == "running":
                state = info.get("state", state)
                failure = info.get("error", failure)
                failure_code = info.get("error_code", failure_code)
            return {
                "interface_id": identifier, "name": config["name"], "type": config["type"],
                "enabled": config["enabled"], "state": state, "error": failure,
                "error_code": failure_code,
                "account": config["account"],
                "settings": {key: deepcopy(settings[key]) for key in
                             ("host", "port", "path", "url", "heartbeat_seconds", "sse_enabled")}
                            | {"authentication": {"configured": bool(settings["access_token"])},
                               "signing_configured": bool(settings["access_token"]),
                               "signing_uses_access_token": True},
                "endpoint": endpoint, "actual_port": actual_port,
                "modules": self.module_bus.bindings(identifier),
                "created_at": config["created_at"], "updated_at": config["updated_at"],
                "revision": config["revision"],
                "generation": self.module_bus.generation(identifier),
                "allocation_controlled": resource.routing_gate.snapshot()["controlled"],
                "allocation_owner_count": resource.routing_gate.snapshot()["owners"],
            }

    def catalog(self):
        with self._lock:
            return {"schema_version": 1,
                    "interfaces": [self.get(identifier) for identifier in self._resources],
                    "types": deepcopy(list(INTERFACE_TYPES)),
                    "accounts": self.platform_host.account_descriptors(),
                    "module_bus": {"available": not self._closed, "id_format": "hex16"}}

    def create(self, payload):
        config = _configuration(payload)
        with self._lock:
            if self._closed or len(self._resources) >= 100:
                raise NetworkInterfaceError("Network interface limit reached or manager closed.", status=409)
            identifier, now = self._new_id(), int(time.time())
            config.update(interface_id=identifier, created_at=now, updated_at=now, revision=1,
                          allocation_controlled=False)
            gate = AccountRoutingGate()
            resource = _Resource(config, PluginGatewayBackend(
                self.platform_host, account=config["account"], routing_gate=gate), routing_gate=gate)
            self._resources[identifier] = resource
            try:
                self._persist()
            except BaseException:
                self._resources.pop(identifier)
                raise
        return self.start(identifier) if config["enabled"] else self.get(identifier)

    def update(self, identifier, payload, *, expected_revision=None, expected_generation=None):
        with self._lock:
            resource = self._get(identifier)
        with resource.lifecycle:
            if expected_revision is not None:
                with self._lock:
                    self._require_allocation(resource, expected_revision, expected_generation)
            checked = _configuration(payload, partial=True, previous=resource.config)
            checked["updated_at"] = int(time.time())
            checked["revision"] = resource.config["revision"] + 1
            desired = checked["enabled"]
            resource.routing_gate.clear()
            self._dispose(resource)
            self.module_bus.invalidate(identifier, "interface configuration changed")
            with self._lock:
                old = resource.config
                old_backend = resource.backend
                resource.config = checked
                resource.backend = PluginGatewayBackend(
                    self.platform_host, account=checked["account"], routing_gate=resource.routing_gate)
                resource.state = "stopped"
                resource.error = None
                resource.error_code = None
                try:
                    self._persist()
                except BaseException:
                    resource.config = old
                    resource.backend = old_backend
                    raise
            return self.start(identifier) if desired else self.get(identifier)

    def allocation_state(self, identifier):
        with self._lock:
            resource = self._get(identifier)
            return {key: resource.config[key] for key in
                    ("interface_id", "type", "account", "revision", "enabled")} | {
                        "generation": self.module_bus.generation(identifier),
                        "state": resource.state}

    def credentials(self, identifier):
        with self._lock:
            resource = self._get(identifier)
            return {"interface_id": identifier,
                    "access_token": resource.config["settings"]["access_token"],
                    "signing_uses_access_token": True}

    def _clear_event_hub(self, resource):
        with resource.events_lock:
            if resource.hub is not None:
                resource.hub.close()
            resource.hub, resource.event_runtime = None, None
            if resource.server is not None:
                resource.server.network_event_hub = None

    def add_allocation_owner(self, identifier, owner_id, revision, generation):
        if (not isinstance(owner_id, str) or not 1 <= len(owner_id) <= 128
                or not re.fullmatch(r"[A-Za-z0-9_.:-]+", owner_id)):
            raise ValueError("Allocation owner must be a bounded identifier.")
        with self._lock:
            resource = self._get(identifier)
        if not resource.lifecycle.acquire(blocking=False):
            from nyxion_heredity_sdk.networks import NetworkBindingClosed
            raise NetworkBindingClosed("The interface lifecycle is busy; request its allocation again.")
        try:
            with self._lock:
                resource = self._get(identifier)
                self._require_allocation(resource, revision, generation)
                runtime = resource.backend.selected_runtime()
                if not resource.config.get("allocation_controlled", False):
                    resource.config["allocation_controlled"] = True
                    try:
                        self._persist()
                    except BaseException:
                        resource.config["allocation_controlled"] = False
                        raise
                if resource.routing_gate.add(owner_id, runtime):
                    self._clear_event_hub(resource)
        finally:
            resource.lifecycle.release()

    def remove_allocation_owner(self, identifier, owner_id):
        validate_interface_id(identifier)
        with self._lock:
            resource = self._resources.get(identifier)
            if resource is not None and resource.routing_gate.remove(owner_id):
                self._clear_event_hub(resource)

    def regenerate_token(self, identifier):
        with self._lock:
            resource = self._get(identifier)
        with resource.lifecycle:
            desired = resource.config["enabled"]
            resource.routing_gate.clear()
            self._dispose(resource)
            self.module_bus.invalidate(identifier, "interface token regenerated")
            with self._lock:
                old = deepcopy(resource.config)
                resource.config["settings"]["access_token"] = secrets.token_urlsafe(32)
                resource.config["revision"] += 1
                resource.config["updated_at"] = int(time.time())
                resource.state, resource.error, resource.error_code = "stopped", None, None
                try:
                    self._persist()
                except BaseException:
                    resource.config = old
                    raise
            state = self.start(identifier) if desired else self.get(identifier)
            return {**self.credentials(identifier), "interface": state}

    def retry(self, identifier):
        with self._lock:
            resource = self._get(identifier)
        with resource.lifecycle:
            resource.routing_gate.clear()
            self._dispose(resource)
            self.module_bus.invalidate(identifier, "interface retry requested")
            with self._lock:
                resource.config["revision"] += 1
                resource.state, resource.error, resource.error_code = "stopped", None, None
                self._persist()
            return self.start(identifier)

    def delete(self, identifier):
        with self._lock:
            resource = self._get(identifier)
        with resource.lifecycle, self._lock:
            if resource.state not in {"stopped", "error"}:
                raise NetworkInterfaceError("Stop the interface before deleting it.", status=409)
            self._resources.pop(identifier)
            try:
                self._persist()
            except BaseException:
                self._resources[identifier] = resource
                raise
        self.module_bus.invalidate(identifier, "interface removed")
        return {"deleted": True, "interface_id": identifier}

    def _observe(self, resource, generation, kind, payload):
        if resource.generation != generation or resource.stop.is_set():
            return
        identifier = resource.config["interface_id"]
        try:
            self._record_observation(resource, kind, payload)
        except Exception:
            # Observation schemas may be narrower than a valid OneBot payload.
            # Never change an action's result or stop delivery after it ran.
            self.module_bus.mark_gap(identifier)

    def _record_observation(self, resource, kind, payload):
        identifier = resource.config["interface_id"]
        if kind == "event":
            runtime = resource.backend.event_runtime()
            signature = id(runtime) if runtime is not None else None
            account = payload.get("self_id", 0)
            configured = resource.config["account"]
            expected = configured.split(":", 1)[1] if configured else (
                str(runtime.self_id) if runtime is not None else None)
            if (not isinstance(payload.get("post_type"), str)
                    or type(account) is not int or not 0 < account <= 2**63 - 1
                    or (expected is not None and str(account) != expected)):
                self.module_bus.emit(identifier, "state",
                                     {"state": "event_rejected", "code": "ACCOUNT_MISMATCH"})
                return
            with resource.events_lock:
                if not resource.routing_gate.snapshot()["allowed"]:
                    return
                if (resource.hub is None or resource.hub.self_id != account
                        or resource.event_runtime != signature):
                    if resource.hub is not None:
                        resource.hub.close()
                    resource.hub = InterfaceEventHub(account)
                    resource.event_runtime = signature
                    if resource.server is not None:
                        resource.server.network_event_hub = resource.hub
                resource.hub.publish(payload)
        self.module_bus.emit(identifier, kind, payload)

    def _selected_hub(self, resource):
        if not resource.routing_gate.snapshot()["allowed"]:
            return None
        runtime = resource.backend.event_runtime()
        if runtime is None:
            return resource.hub
        with resource.events_lock:
            if not resource.routing_gate.snapshot()["allowed"]:
                return None
            if (resource.hub is None or resource.hub.closed
                    or resource.hub.self_id != runtime.self_id
                    or resource.event_runtime != id(runtime)):
                if resource.hub is not None:
                    resource.hub.close()
                resource.hub = InterfaceEventHub(runtime.self_id)
                resource.event_runtime = id(runtime)
                if resource.server is not None:
                    resource.server.network_event_hub = resource.hub
            return resource.hub

    def _local_action(self, resource, envelope, *, backend=None):
        backend = resource.backend if backend is None else backend
        action, params = envelope.get("action"), envelope.get("params", {})
        if not isinstance(action, str) or not ACTION_PATTERN.fullmatch(action) or type(params) is not dict:
            return {"status": "failed", "retcode": 1400, "data": None,
                    "message": "Invalid OneBot action request."}
        try:
            if action == "get_status":
                result = backend.status()
            elif action == "get_version_info":
                from . import __version__
                result = {"app_name": "NyXion Heredity", "app_version": __version__,
                          "protocol_version": "v11"}
            else:
                result = backend.call(action, params)
            return {"status": "ok", "retcode": 0, "data": result}
        except BackendError as error:
            result = {"status": "failed", "retcode": error.retcode,
                      "data": None, "message": str(error)}
            if error.details is not None:
                result["details"] = error.details
            return result

    def start(self, identifier):
        with self._lock:
            resource = self._get(identifier)
            if self._closed:
                raise NetworkInterfaceError("Network interface manager is closed.", status=503)
        with resource.lifecycle:
            if resource.state in {"running", "starting"}:
                return self.get(identifier)
            resource.state, resource.error, resource.error_code = "starting", None, None
            resource.stop = threading.Event()
            resource.generation += 1
            generation = resource.generation
            backend = resource.backend
            settings = resource.config["settings"]
            observe = lambda kind, payload: self._observe(resource, generation, kind, payload)
            try:
                kind = resource.config["type"]
                if kind == "http_server":
                    server = resource.server or create_server(
                        settings["host"], settings["port"], settings["access_token"], resource.backend,
                        event_urls=tuple(settings["event_urls"]),
                        heartbeat_interval=settings["heartbeat_seconds"],
                        event_secret=settings["access_token"], enable_sse=settings["sse_enabled"])
                    resource.server = server
                    server.api_path = settings["path"]
                    server.network_observer = observe
                    server.network_observer_gap = lambda: self.module_bus.mark_gap(identifier)
                    resource.hub = InterfaceEventHub(resource.backend.self_id)
                    server.network_event_hub = resource.hub
                    server.network_hub_provider = lambda: self._selected_hub(resource)
                    resource.worker = threading.Thread(
                        target=server.serve_forever, name="nyxion-http-" + identifier, daemon=True)
                    resource.worker.start()
                    server.start_events()
                else:
                    if kind == "http_client":
                        transport = HTTPClientTransport(
                            url=settings["url"], token=settings["access_token"],
                            secret=settings["access_token"], on_message=observe)
                    else:
                        from .network_websocket import WebSocketClientTransport, WebSocketServerTransport
                        if kind == "websocket_server":
                            transport = WebSocketServerTransport(
                                host=settings["host"], port=settings["port"], path=settings["path"],
                                token=settings["access_token"], on_message=observe,
                                on_action=lambda envelope: self._local_action(
                                    resource, envelope, backend=backend))
                        else:
                            transport = WebSocketClientTransport(
                                url=settings["url"], token=settings["access_token"],
                                on_message=observe,
                                on_action=lambda envelope: self._local_action(
                                    resource, envelope, backend=backend),
                                self_id=lambda: str(backend.self_id))
                    resource.transport = transport
                    transport.start()
                    if transport.snapshot().get("state") == "error":
                        raise NetworkInterfaceError("The configured transport could not connect.")
                    resource.pump = threading.Thread(
                        target=self._pump, args=(resource, generation),
                        name="nyxion-network-events-" + identifier, daemon=True)
                    resource.pump.start()
                resource.state = "running"
                resource.config["enabled"] = True
                resource.config["updated_at"] = int(time.time())
                with self._lock:
                    self._persist()
                self.module_bus.emit(identifier, "state", {"state": "running", "interface_id": identifier})
                return self.get(identifier)
            except Exception as error:
                self._dispose(resource)
                resource.state = "error"
                if resource.config["type"].endswith("_server"):
                    resource.error_code, resource.error = listener_failure(
                        error, settings["port"], host=settings["host"])
                else:
                    resource.error_code = "NETWORK_CONNECT_FAILED"
                    resource.error = "网络接口连接失败，请检查目标地址和服务状态后重试。"
                resource.config["enabled"] = False
                with self._lock:
                    self._persist()
                return self.get(identifier)

    def _pump(self, resource, generation):
        current, cursor = None, 0
        backend, transport, stop = resource.backend, resource.transport, resource.stop
        interval = resource.config["settings"]["heartbeat_seconds"]
        heartbeat_at = time.monotonic() + interval
        current_stamp = None
        while not stop.is_set() and generation == resource.generation:
            stamp = backend.event_route_stamp()
            runtime = backend.event_runtime()
            if runtime is None:
                stop.wait(0.1)
                current, cursor = None, 0
                continue
            if runtime is not current or current_stamp != stamp:
                current, cursor, current_stamp = runtime, backend.event_start_cursor(runtime), stamp
            try:
                records = runtime.event_hub.read(cursor, timeout=0.1)
            except EventGapError:
                self.module_bus.emit(resource.config["interface_id"], "state",
                                     {"state": "event_gap", "retry_safe": False})
                cursor = runtime.event_hub.latest_sequence
                continue
            for record in records:
                if stop.is_set():
                    return
                if not backend.event_route_current(runtime, stamp):
                    break
                try:
                    transport.publish(record["event"])
                except Exception:
                    self.module_bus.emit(resource.config["interface_id"], "state",
                                         {"state": "event_delivery_failed", "retry_safe": False})
                cursor = record["sequence"]
            if time.monotonic() >= heartbeat_at and backend.event_route_current(runtime, stamp):
                event = {"time": int(time.time()), "self_id": runtime.self_id, "post_type": "meta_event",
                         "meta_event_type": "heartbeat", "status": runtime.status(),
                         "interval": round(interval * 1000)}
                try:
                    transport.publish(event)
                except Exception:
                    pass
                heartbeat_at = time.monotonic() + interval

    def _dispose(self, resource):
        resource.stop.set()
        if resource.hub is not None:
            resource.hub.close()
        if resource.server is not None:
            if resource.worker is not None and resource.worker.is_alive():
                resource.server.shutdown()
                resource.worker.join(timeout=3)
            resource.server.server_close()
            resource.server = None
        if resource.transport is not None:
            resource.transport.stop()
        if resource.pump is not None and resource.pump is not threading.current_thread():
            resource.pump.join(timeout=4)
            if resource.pump.is_alive():
                raise NetworkInterfaceError("Network event worker did not stop.", status=504)
        resource.transport = None
        resource.worker = resource.pump = None

    def stop(self, identifier):
        with self._lock:
            resource = self._get(identifier)
        with resource.lifecycle:
            self._dispose(resource)
            resource.state, resource.config["enabled"] = "stopped", False
            resource.error = resource.error_code = None
            with self._lock:
                self._persist()
            self.module_bus.emit(identifier, "state", {"state": "stopped", "interface_id": identifier})
            return self.get(identifier)

    def start_all(self):
        with self._lock:
            identifiers = [key for key, value in self._resources.items() if value.config["enabled"]]
        for identifier in identifiers:
            self.start(identifier)

    def bind_module(self, module_id, interface_id):
        return self.module_bus.bind(module_id, interface_id)

    def bind_allocated_module(self, module_id, interface_id, revision, generation):
        with self._lock:
            resource = self._get(interface_id)
            self._require_allocation(resource, revision, generation)
            return self.module_bus.bind(
                module_id, interface_id,
                dispatch=lambda identifier, action, params, timeout: self._module_call(
                    identifier, action, params, timeout,
                    expected_revision=revision, expected_generation=generation),
                publish=lambda identifier, event: self.publish_event(
                    identifier, event, expected_revision=revision, expected_generation=generation))

    def _require_allocation(self, resource, revision, generation):
        from nyxion_heredity_sdk.networks import NetworkBindingClosed
        if (resource.config["revision"] != revision
                or self.module_bus.generation(resource.config["interface_id"]) != generation):
            raise NetworkBindingClosed("The allocated interface configuration changed")

    def _module_call(self, identifier, action, params, timeout, *,
                     expected_revision=None, expected_generation=None):
        with self._lock:
            resource = self._get(identifier)
            if expected_revision is not None:
                self._require_allocation(resource, expected_revision, expected_generation)
            if resource.state != "running":
                raise BackendError("Network interface is not running.", 1503)
            if not resource.routing_gate.snapshot()["allowed"]:
                raise BackendError("Network interface is not assigned to an active module.", 1503,
                                   details={"state": "network_interface_unassigned"})
            generation = resource.generation
            transport = resource.transport
            backend = resource.backend
            kind = resource.config["type"]
        from nyxion_heredity_sdk import thaw_json
        params = thaw_json(params)
        if kind.endswith("_client"):
            return transport.call_remote(action, params, timeout=timeout)
        envelope = {"action": action, "params": params}
        self._observe(resource, generation, "action", envelope)
        result = self._local_action(resource, envelope, backend=backend)
        self._observe(resource, generation, "response", result)
        return result

    def publish_event(self, identifier, event, *, expected_revision=None, expected_generation=None):
        from nyxion_heredity_sdk import thaw_json
        event = _json_copy(thaw_json(event), "module event")
        if type(event) is not dict or not isinstance(event.get("post_type"), str):
            raise ValueError("Module publication requires a OneBot event object.")
        with self._lock:
            resource = self._get(identifier)
            if expected_revision is not None:
                self._require_allocation(resource, expected_revision, expected_generation)
            if resource.state != "running":
                raise BackendError("Network interface is not running.", 1503)
            if not resource.routing_gate.snapshot()["allowed"]:
                raise BackendError("Network interface is not assigned to an active module.", 1503,
                                   details={"state": "network_interface_unassigned"})
            server, transport, generation = resource.server, resource.transport, resource.generation
            backend = resource.backend
            configured = resource.config["account"]
            kind = resource.config["type"]
        selected = backend.event_runtime()
        expected = configured.split(":", 1)[1] if configured else (
            str(selected.self_id) if selected is not None else None)
        account = event.get("self_id")
        if (type(account) is not int or not 0 < account <= 2**63 - 1
                or (expected is not None and str(account) != expected)):
            raise ValueError("Module event account does not match this interface.")
        if kind == "http_server":
            self._observe(resource, generation, "event", event)
            server._deliver_event(request.build_opener(request.ProxyHandler({}), _NoRedirect()), event,
                                  observe=False)
        else:
            transport.publish(event)
        return {"published": True, "interface_id": identifier}

    def close(self):
        with self._lock:
            if self._closed:
                return
            resources = list(self._resources.values())
            self._closed = True
        # Application shutdown retains the desired enabled flags for next start.
        for resource in resources:
            with resource.lifecycle:
                self._dispose(resource)
                resource.state = "stopped"
        self.module_bus.close()
