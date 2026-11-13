"""Embedded SDK host with leased control handshakes and account-scoped routing."""

from dataclasses import dataclass, field
import importlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from uuid import uuid4

from nyxion_heredity_sdk import (
    API_MAJOR, API_MINOR, ActionRequest, BackendError, Challenge, ContractError,
    EventEnvelope, Heartbeat, HostHello, Plugin, PluginContext, PluginManifest,
    PublishAck, SessionUpdate, thaw_json, validate_action_result, validate_config,
    validate_handshake, validate_pong,
)

from ..runtime import AccountRuntime
from ..storage import MessageStore


_ENTRYPOINT = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*\Z")
_SEND_ACTIONS = frozenset({"send_msg", "send_private_msg", "send_group_msg"})
_INSTANCE_KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


class PluginHostError(ValueError):
    def __init__(self, message, code="PLUGIN_REQUEST_REJECTED", status=400):
        super().__init__(message)
        self.code, self.status = code, status


@dataclass
class Registration:
    manifest: PluginManifest
    factory: object = None
    entrypoint: str | None = None
    source_root: Path | None = None


@dataclass
class Instance:
    registration: Registration
    instance_key: str = "default"
    display_name: str = ""
    instance_id: str = field(default_factory=lambda: uuid4().hex)
    state: str = "handshaking"
    plugin: object = None
    handshake_verified: bool = False
    sequence: int = 0
    last_seen: float | None = None
    last_seen_at: float | None = None
    control_deadline: float | None = None
    error: str | None = None
    session: object = None
    session_state: str = "starting"
    session_reason: str = ""
    runtime: object = None
    attaching_session: object = None
    challenge: object = None
    inflight: int = 0
    stop_event: threading.Event = field(default_factory=threading.Event)
    stop_deadline: float | None = None
    clean_stop: bool = False
    thread: object = None
    stopper: object = None
    network_lifecycle: threading.RLock = field(default_factory=threading.RLock)


class PluginAccountBackend:
    """Translate one SDK session into the existing OneBot account runtime."""

    def __init__(self, host, instance, session):
        self.host, self.instance, self.session = host, instance, session
        self.self_id = int(session.self_id)

    @property
    def online(self):
        return self.host.session_is_online(self.instance, self.session)

    def session_context(self):
        return {"provider_id": self.instance.registration.manifest.plugin_id,
                "session_epoch": self.session.session_epoch}

    def execution_info(self):
        manifest = self.instance.registration.manifest
        descriptor = {
            "provider_id": manifest.plugin_id, "label": manifest.name,
            "source_kind": "plugin", "platform": manifest.platform,
            "execution": manifest.execution, "execution_level": None,
            "supported_actions": list(manifest.actions),
            "capabilities": list(manifest.capabilities),
        }
        return {"levels": [], "switch_supported": False, "state": {
            "state": "ready" if self.online else "disconnected",
            "active_provider_id": manifest.plugin_id,
            "session_epoch": self.session.session_epoch,
            "descriptor": descriptor, "providers": [descriptor], "in_flight": 0}}

    def call(self, action, params):
        return self.host.call(self.instance, self.session, action, params)

    def close(self):
        # Plugin lifetime belongs to the host, not to an individual event store.
        pass


class PluginHost:
    """One in-memory SDK boundary; loading is always an explicit operation.

    A monitor revokes a hung control lease. Embedded code cannot be forcibly
    unloaded or sandboxed: a non-stopping instance remains failed and blocks a
    second instance of that plugin instead of silently running two owners.
    """

    def __init__(self, state_dir, *, heartbeat_interval=2.0, lease_seconds=8.0,
                 control_timeout=5.0, action_timeout=45.0):
        for name, value in (("heartbeat_interval", heartbeat_interval),
                            ("lease_seconds", lease_seconds),
                            ("control_timeout", control_timeout),
                            ("action_timeout", action_timeout)):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if lease_seconds <= heartbeat_interval:
            raise ValueError("plugin lease must exceed the heartbeat interval")
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.heartbeat_interval = float(heartbeat_interval)
        self.lease_seconds = float(lease_seconds)
        self.control_timeout = float(control_timeout)
        self.action_timeout = float(action_timeout)
        self._lock = threading.RLock()
        self._registrations = {}
        self._instances = {}
        self._owners = {}
        self._configs = {}
        self._profiles = self._read_profiles()
        self._closed = False
        self._closing = False
        self._stop = threading.Event()
        self._monitor = threading.Thread(
            target=self._monitor_leases, name="nyxion-plugin-leases", daemon=True)
        self._monitor.start()

    def _read_profiles(self):
        path = self.state_dir / "plugin-instances.json"
        if not path.exists():
            return {}
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("Plugin instance metadata must not be a link.")
        if path.stat().st_size > 256 * 1024:
            raise ValueError("Plugin instance metadata exceeds its size limit.")
        data = json.loads(path.read_text(encoding="utf-8"))
        if (type(data) is not dict or set(data) != {"schema_version", "plugins"}
                or type(data["schema_version"]) is not int or data["schema_version"] not in {1, 2}
                or type(data["plugins"]) is not dict):
            raise ValueError("Plugin instance metadata is invalid.")
        result = {}
        for plugin_id, entries in data["plugins"].items():
            if (not isinstance(plugin_id, str)
                    or not re.fullmatch(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*", plugin_id)
                    or type(entries) is not dict or len(entries) > 100):
                raise ValueError("Plugin instance metadata is invalid.")
            result[plugin_id] = {}
            for key, entry in entries.items():
                if data["schema_version"] == 1:
                    label, config = entry, {}
                else:
                    if type(entry) is not dict or set(entry) != {"display_name", "config"}:
                        raise ValueError("Plugin instance profile is invalid.")
                    label, config = entry["display_name"], entry["config"]
                    if type(config) is not dict:
                        raise ValueError("Plugin instance configuration is invalid.")
                self._validate_instance_name(key, label)
                result[plugin_id][key] = label
                self._configs.setdefault(plugin_id, {})[key] = config
        return result

    @staticmethod
    def _validate_instance_name(key, label):
        if not isinstance(key, str) or not _INSTANCE_KEY.fullmatch(key):
            raise PluginHostError("Instance key must be a bounded lowercase identifier.")
        if (not isinstance(label, str) or not label.strip() or len(label) > 80
                or any(ord(char) < 32 or ord(char) == 127 for char in label)):
            raise PluginHostError("Instance display name must contain 1..80 characters.")
        try:
            label.encode("utf-8")
        except UnicodeError:
            raise PluginHostError("Instance display name must contain valid Unicode.") from None
        return key

    @staticmethod
    def _slot(plugin_id, instance_key):
        return plugin_id if instance_key == "default" else plugin_id + "#" + instance_key

    def _profile(self, plugin_id, instance_key):
        registration = self._get(plugin_id)
        try:
            return self._profiles[plugin_id][instance_key]
        except (KeyError, TypeError):
            if instance_key == "default":
                return registration.manifest.name
            raise PluginHostError("Plugin instance was not found.", "INSTANCE_NOT_FOUND", 404) from None

    @classmethod
    def _public_configuration(cls, schema, value):
        if schema.get("writeOnly") or schema.get("format") == "password":
            return None
        if isinstance(value, dict):
            properties = schema.get("properties", {})
            return {key: cls._public_configuration(properties[key], child)
                    for key, child in value.items()
                    if key in properties and not properties[key].get("writeOnly")
                    and properties[key].get("format") != "password"}
        if isinstance(value, list):
            item_schema = schema.get("items", {})
            if item_schema.get("writeOnly") or item_schema.get("format") == "password":
                return []
            return [cls._public_configuration(item_schema, child) for child in value]
        return value

    def _save_profiles(self):
        path = self.state_dir / "plugin-instances.json"
        temporary = path.with_name("plugin-instances." + uuid4().hex + ".tmp")
        try:
            payload = {"schema_version": 2, "plugins": {
                plugin_id: {key: {"display_name": label,
                                   "config": self._configs.get(plugin_id, {}).get(key, {})}
                            for key, label in entries.items()}
                for plugin_id, entries in self._profiles.items()}}
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def create_instance(self, plugin_id, instance_key, display_name):
        self._validate_instance_name(instance_key, display_name)
        with self._lock:
            registration = self._get(plugin_id)
            if self._closed or self._closing:
                raise PluginHostError("Plugin host is closed.", "PLUGIN_HOST_CLOSED", 503)
            if instance_key in self._profiles.get(plugin_id, {}):
                raise PluginHostError("Instance key already exists.", "INSTANCE_CONFLICT", 409)
            profiles = self._profiles.setdefault(plugin_id, {})
            if len(profiles) >= 100:
                raise PluginHostError("Plugin instance limit reached.", "INSTANCE_LIMIT", 409)
            profiles[instance_key] = display_name.strip()
            try:
                self._save_profiles()
            except BaseException:
                profiles.pop(instance_key, None)
                raise
            return self._snapshot(registration, instance_key)

    def instances(self, plugin_id):
        with self._lock:
            registration = self._get(plugin_id)
            keys = list(self._profiles.get(plugin_id, {}))
            if not registration.manifest.ui_pages and "default" not in keys:
                keys.insert(0, "default")
            return [self._snapshot(registration, key) for key in keys]

    def remove_instance(self, plugin_id, instance_key):
        with self._lock:
            self._profile(plugin_id, instance_key)
            if instance_key not in self._profiles.get(plugin_id, {}):
                raise PluginHostError("Plugin instance was not found.", "INSTANCE_NOT_FOUND", 404)
            slot = self._slot(plugin_id, instance_key)
            instance = self._instances.get(slot)
            if instance is not None:
                if (instance.state not in {"stopped", "failed"} or not instance.clean_stop
                        or any(worker is not None and worker.is_alive()
                               for worker in (instance.thread, instance.stopper))
                        or instance.inflight):
                    raise PluginHostError("Stop this instance cleanly before removing it.",
                                          "INSTANCE_STILL_RUNNING", 409)
            label = self._profiles[plugin_id].pop(instance_key)
            config = self._configs.get(plugin_id, {}).pop(instance_key, {})
            try:
                self._save_profiles()
            except BaseException:
                self._profiles[plugin_id][instance_key] = label
                self._configs.setdefault(plugin_id, {})[instance_key] = config
                raise
            self._instances.pop(slot, None)
            # Account history and plugin-local state are deliberately retained.
            return {"removed": True, "instance_key": instance_key}

    def register(self, manifest, *, factory=None, entrypoint=None, source_root=None):
        if not isinstance(manifest, PluginManifest):
            raise PluginHostError("A plugin requires an SDK manifest.")
        if not manifest.supports_host():
            raise PluginHostError("Plugin SDK version is incompatible.", "PLUGIN_API_MISMATCH", 422)
        if (factory is None) == (entrypoint is None):
            raise PluginHostError("Supply one explicit plugin factory or entry point.")
        if factory is not None and not callable(factory):
            raise PluginHostError("Plugin factory must be callable.")
        if entrypoint is not None and not _ENTRYPOINT.fullmatch(entrypoint):
            raise PluginHostError("Plugin entry point is invalid.")
        root = Path(source_root).resolve(strict=True) if source_root is not None else None
        if root is not None and not root.is_dir():
            raise PluginHostError("Plugin source root must be a directory.")
        with self._lock:
            if self._closed or self._closing:
                raise PluginHostError("Plugin host is closed.", "PLUGIN_HOST_CLOSED", 503)
            if manifest.plugin_id in self._registrations:
                raise PluginHostError("Plugin identifier is already registered.", "PLUGIN_CONFLICT", 409)
            self._registrations[manifest.plugin_id] = Registration(
                manifest, factory, entrypoint, root)
            migrated = False
            for key, config in self._configs.get(manifest.plugin_id, {}).items():
                public = self._public_configuration(manifest.config_schema, config)
                if public != config:
                    self._configs[manifest.plugin_id][key] = public
                    migrated = True
            if migrated:
                self._save_profiles()

    def load_registry(self, path):
        """Read metadata only. Relative code paths stay within this registry."""
        path = Path(path).resolve(strict=True)
        raw = path.read_bytes()
        if len(raw) > 256 * 1024:
            raise PluginHostError("Plugin registry exceeds its size limit.")
        data = json.loads(raw)
        if (type(data) is not dict or set(data) != {"schema_version", "plugins"}
                or type(data["schema_version"]) is not int or data["schema_version"] != 1
                or type(data["plugins"]) is not list or len(data["plugins"]) > 100):
            raise PluginHostError("Plugin registry schema is invalid.")
        root = path.parent
        pending = []
        identifiers = set()
        for item in data["plugins"]:
            if type(item) is not dict or set(item) != {"manifest", "entrypoint", "source_root"}:
                raise PluginHostError("Plugin registry entries require exact fields.")
            resolved = {}
            for name in ("manifest", "source_root"):
                relative = item[name]
                if (not isinstance(relative, str) or not relative
                        or Path(relative).is_absolute() or ":" in relative
                        or any(part in {"", ".", ".."} for part in relative.replace("\\", "/").split("/"))):
                    raise PluginHostError("Plugin registry paths must be relative descendants.")
                candidate = root
                for part in relative.replace("\\", "/").split("/"):
                    candidate = candidate / part
                    if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
                        raise PluginHostError("Plugin registry paths cannot traverse links.")
                candidate = candidate.resolve(strict=True)
                if not candidate.is_relative_to(root):
                    raise PluginHostError("Plugin registry path escaped its root.")
                resolved[name] = candidate
            manifest_bytes = resolved["manifest"].read_bytes()
            if len(manifest_bytes) > 128 * 1024:
                raise PluginHostError("Plugin manifest exceeds its size limit.")
            manifest = PluginManifest.from_dict(json.loads(manifest_bytes))
            if manifest.plugin_id in identifiers:
                raise PluginHostError("Plugin registry contains duplicate identifiers.")
            identifiers.add(manifest.plugin_id)
            if not _ENTRYPOINT.fullmatch(item["entrypoint"]):
                raise PluginHostError("Plugin entry point is invalid.")
            if not resolved["source_root"].is_dir() or not manifest.supports_host():
                raise PluginHostError("Plugin runtime or SDK version is incompatible.")
            pending.append((manifest, item["entrypoint"], resolved["source_root"]))
        with self._lock:
            if identifiers.intersection(self._registrations):
                raise PluginHostError("Plugin registry overlaps registered identifiers.")
            for manifest, entrypoint, source in pending:
                self.register(manifest, entrypoint=entrypoint, source_root=source)

    def _snapshot(self, registration, instance_key="default"):
        current = self._instances.get(self._slot(registration.manifest.plugin_id, instance_key))
        manifest = registration.manifest
        seen = current.last_seen if current is not None else None
        healthy = (current is not None and current.state == "running"
                   and seen is not None and time.monotonic() - seen < self.lease_seconds)
        session = None
        if current is not None and (current.session is not None or current.session_state):
            session = ({**current.session.to_dict()} if current.session is not None else
                       {"platform": manifest.platform, "self_id": None, "session_epoch": None})
            session.update(state=current.session_state, reason=current.session_reason)
        return {
            "id": manifest.plugin_id, "name": manifest.name, "version": manifest.version,
            "instance_key": instance_key,
            "instance_id": current.instance_id if current else None,
            "display_name": self._profile(manifest.plugin_id, instance_key),
            "is_default": False,
            "auto_login": self._configs.get(manifest.plugin_id, {}).get(instance_key, {}).get(
                "auto_login") is True,
            "config_values": self._public_configuration(
                manifest.config_schema, self._configs.get(manifest.plugin_id, {}).get(instance_key, {})),
            "has_saved_config": bool(
                self._configs.get(manifest.plugin_id, {}).get(instance_key, {})),
            "platform": manifest.platform, "execution": manifest.execution,
            "state": current.state if current is not None else
                     "discovered" if instance_key == "default" else "stopped",
            "handshake": {"api_major": API_MAJOR, "api_minor": API_MINOR,
                          "verified": bool(current and current.handshake_verified)},
            "heartbeat": {"sequence": current.sequence if current else 0,
                          "last_seen_at": current.last_seen_at if current else None,
                          "lease_seconds": self.lease_seconds, "healthy": healthy},
            "session": session, "actions": list(manifest.actions),
            "capabilities": list(manifest.capabilities),
            "config_schema": thaw_json(manifest.config_schema),
            "error": current.error if current else None,
        }

    def catalog(self):
        with self._lock:
            plugins, navigation = [], []
            for registration in self._registrations.values():
                manifest = registration.manifest
                current = self._snapshot(registration)
                current["instances"] = self.instances(manifest.plugin_id)
                current["ui_pages"] = [
                    {**page.to_dict(), "path": f"/plugins/{manifest.plugin_id}/{page.id}"}
                    for page in getattr(manifest, "ui_pages", ())
                ]
                navigation.extend(
                    {"plugin_id": manifest.plugin_id, **page} for page in current["ui_pages"])
                plugins.append(current)
            return {"api_version": f"{API_MAJOR}.{API_MINOR}", "host_running": not self._closed,
                    "plugins": plugins, "navigation": navigation,
                    "sessions": [instance["session"] for item in plugins
                                 for instance in item["instances"] if instance["session"] is not None]}

    def _get(self, plugin_id):
        try:
            return self._registrations[plugin_id]
        except (KeyError, TypeError):
            raise PluginHostError("Plugin is not registered.", "PLUGIN_NOT_FOUND", 404) from None

    def _active(self, instance):
        return (not self._closed and not self._closing
                and self._instances.get(self._slot(instance.registration.manifest.plugin_id,
                                                  instance.instance_key)) is instance
                and instance.state in {"handshaking", "starting", "running"}
                and not instance.stop_event.is_set())

    def start(self, plugin_id, config, *, instance_key="default"):
        with self._lock:
            registration = self._get(plugin_id)
            if self._closed or self._closing:
                raise PluginHostError("Plugin host is closed.", "PLUGIN_HOST_CLOSED", 503)
            checked = validate_config(registration.manifest, config)
            if (instance_key == "default"
                    and instance_key not in self._profiles.get(plugin_id, {})):
                self._profiles.setdefault(plugin_id, {})[instance_key] = registration.manifest.name
            display_name = self._profile(plugin_id, instance_key)
            slot = self._slot(plugin_id, instance_key)
            previous = self._instances.get(slot)
            if previous is not None:
                if previous.state not in {"stopped", "failed"}:
                    raise PluginHostError("Plugin is already active.", "PLUGIN_ALREADY_RUNNING", 409)
                if any(worker is not None and worker.is_alive()
                       for worker in (previous.thread, previous.stopper)):
                    raise PluginHostError("Previous plugin instance has not stopped.",
                                          "PLUGIN_STILL_STOPPING", 409)
                if previous.stopper is not None and not previous.clean_stop:
                    raise PluginHostError("Previous plugin did not confirm shutdown.",
                                          "PLUGIN_STOP_UNCONFIRMED", 409)
            public = self._public_configuration(
                registration.manifest.config_schema, thaw_json(checked))
            self._configs.setdefault(plugin_id, {})[instance_key] = public
            self._save_profiles()
            instance = Instance(registration, instance_key=instance_key, display_name=display_name)
            instance.control_deadline = time.monotonic() + self.control_timeout
            self._instances[slot] = instance
            instance.thread = threading.Thread(
                target=self._run_instance, args=(instance, checked),
                name="nyxion-plugin-" + plugin_id, daemon=True)
            instance.thread.start()
            return self._snapshot(registration, instance_key)

    def retry(self, plugin_id, *, instance_key="default"):
        with self._lock:
            self._profile(plugin_id, instance_key)
            config = dict(self._configs.get(plugin_id, {}).get(instance_key, {}))
            if not config:
                raise PluginHostError("No saved configuration is available; submit login configuration.",
                                      "INSTANCE_CONFIGURATION_REQUIRED", 422)
            try:
                validate_config(self._get(plugin_id).manifest, config)
            except ValueError:
                raise PluginHostError("Saved configuration needs required credentials; enter them again.",
                                      "INSTANCE_CONFIGURATION_REQUIRED", 422) from None
            instance = self._instances.get(self._slot(plugin_id, instance_key))
        if instance is not None:
            self.stop(plugin_id, instance_key=instance_key)
            worker = instance.stopper
            if worker is not None:
                worker.join(timeout=self.control_timeout + 1)
            if not instance.clean_stop:
                raise PluginHostError("The previous instance has not stopped cleanly.",
                                      "PLUGIN_STILL_STOPPING", 409)
        return self.start(plugin_id, config, instance_key=instance_key)

    def start_enabled(self):
        with self._lock:
            scheduled = [(plugin_id, key, dict(config))
                         for plugin_id, entries in self._configs.items()
                         if plugin_id in self._registrations
                         for key, config in entries.items() if config.get("auto_login") is True]
        started = []
        for plugin_id, key, config in scheduled:
            try:
                started.append(self.start(plugin_id, config, instance_key=key))
            except ValueError:
                continue
        return started

    def _load(self, registration):
        if registration.factory is not None:
            return registration.factory()
        if registration.source_root is not None:
            source = str(registration.source_root)
            with self._lock:
                if source not in sys.path:
                    sys.path.insert(0, source)
        module_name, name = registration.entrypoint.split(":", 1)
        return getattr(importlib.import_module(module_name), name)()

    @staticmethod
    def _module_id(instance):
        identifier = instance.registration.manifest.plugin_id + ".instance-" + instance.instance_key
        if len(identifier) > 128:
            identifier = identifier[:95] + "." + hashlib.sha256(identifier.encode()).hexdigest()[:24]
        return identifier

    def _run_instance(self, instance, config):
        try:
            plugin = self._load(instance.registration)
            with self._lock:
                instance.plugin = plugin
                if not self._active(instance):
                    late_load = True
                else:
                    late_load = False
            if late_load:
                plugin.stop("Plugin factory completed after its host lease was revoked")
                return
            if not isinstance(plugin, Plugin):
                raise ContractError("plugin does not implement the SDK")
            if plugin.manifest.to_dict() != instance.registration.manifest.to_dict():
                raise ContractError("loaded plugin differs from the reviewed manifest")
            hello = HostHello(instance.instance_id, secrets.token_hex(32))
            validate_handshake(instance.registration.manifest, hello, plugin.handshake(hello))
            with self._lock:
                if not self._active(instance):
                    return
                instance.handshake_verified = True
                instance.state = "starting"
                instance.control_deadline = time.monotonic() + self.control_timeout
            directory = self.state_dir / "plugins" / instance.registration.manifest.plugin_id
            if instance.instance_key != "default":
                directory = directory / "instances" / instance.instance_key
            directory.mkdir(parents=True, exist_ok=True)
            allocator = getattr(self, "network_allocator", None)
            networks = None
            # A late start must not recreate a scope after stop revoked it.
            with instance.network_lifecycle:
                with self._lock:
                    if not self._active(instance):
                        return
                if allocator is not None:
                    networks = allocator.for_module(self._module_id(instance))
                with self._lock:
                    active = self._active(instance)
                if not active:
                    if allocator is not None:
                        allocator.release_module(self._module_id(instance))
                    return
            context = PluginContext(
                instance.instance_id, directory,
                publish_event=lambda event: self._publish(instance, event),
                update_session=lambda update: self._session_update(instance, update),
                publish_challenge=lambda challenge: self._challenge_update(instance, challenge),
                networks=networks)
            plugin.start(context, config)
            with self._lock:
                if not self._active(instance):
                    return
                instance.state = "running"
                instance.last_seen = time.monotonic()
                instance.last_seen_at = time.time()
                instance.control_deadline = None
            while not instance.stop_event.wait(self.heartbeat_interval):
                with self._lock:
                    if not self._active(instance):
                        return
                    instance.sequence += 1
                    deadline = time.monotonic() + min(self.control_timeout, self.lease_seconds)
                    heartbeat = Heartbeat(instance.instance_id, secrets.token_hex(32),
                                          instance.sequence, deadline)
                    instance.control_deadline = deadline
                validate_pong(heartbeat, plugin.ping(heartbeat))
                with self._lock:
                    if not self._active(instance):
                        return
                    instance.last_seen, instance.last_seen_at = time.monotonic(), time.time()
                    instance.control_deadline = None
        except Exception as error:
            self._fail(instance, "Plugin control failed: " + type(error).__name__)
        finally:
            if instance.stop_event.is_set():
                self._ensure_stopper(instance)

    def _session_update(self, instance, update):
        if not isinstance(update, SessionUpdate):
            raise ContractError("invalid session update")
        manifest = instance.registration.manifest
        if update.session is not None and update.session.platform != manifest.platform:
            raise ContractError("plugin cannot publish another platform's session")
        old_runtime = None
        with self._lock:
            if not self._active(instance) or not instance.handshake_verified:
                return
            previous = instance.session
            if (update.state != "online" and update.session is not None
                    and previous is not None and update.session != previous):
                raise ContractError("offline update belongs to another session")
            if update.state == "online":
                session = update.session
                if (not session.self_id.isdecimal() or str(int(session.self_id)) != session.self_id
                        or not 0 < int(session.self_id) <= 2**63 - 1):
                    raise ContractError("OneBot v11 requires a supported numeric account")
                owner = self._owners.get((session.platform, session.self_id))
                if owner is not None and owner is not instance:
                    raise ContractError("another plugin already owns this account")
                if previous is not None and previous != session and instance.runtime is not None:
                    raise ContractError("an existing session must go offline before replacement")
                self._owners[(session.platform, session.self_id)] = instance
                instance.session = session
            else:
                old_runtime, instance.runtime = instance.runtime, None
                instance.attaching_session = None
                if update.session is not None:
                    instance.session = update.session
            instance.session_state = update.state
            # Do not expose arbitrary plugin-provided native diagnostics.
            instance.session_reason = update.reason[:1024]
            if update.state in {"online", "offline", "failed", "stopped"}:
                instance.challenge = None
            needs_runtime = (update.state == "online" and instance.runtime is None
                             and instance.attaching_session is None)
            if needs_runtime:
                instance.attaching_session = update.session
        if old_runtime is not None:
            old_runtime.close()
        if update.state != "online" and previous is not None:
            with self._lock:
                key = (previous.platform, previous.self_id)
                if (self._owners.get(key) is instance
                        and (instance.session != previous or instance.session_state != "online")
                        and not instance.stop_event.is_set()):
                    self._owners.pop(key)
        if needs_runtime:
            self._attach_account(instance, update.session)

    def _attach_account(self, instance, session):
        directory = (self.state_dir / "accounts" / session.platform / session.self_id
                     / instance.registration.manifest.plugin_id)
        store = MessageStore(directory / "onebot.sqlite3")
        runtime = AccountRuntime(PluginAccountBackend(self, instance, session), store, mode="plugin")
        try:
            if not runtime.initialize():
                raise ContractError("plugin login identity did not match its session")
            with self._lock:
                if (not self._active(instance) or instance.session != session
                        or instance.session_state != "online"):
                    raise ContractError("session ended during identity verification")
                if instance.runtime is not None:
                    raise ContractError("session runtime is already attached")
                instance.runtime = runtime
                instance.attaching_session = None
        except BaseException:
            runtime.close()
            with self._lock:
                key = (session.platform, session.self_id)
                if instance.attaching_session == session:
                    instance.attaching_session = None
                if (self._owners.get(key) is instance and instance.runtime is None
                        and instance.session == session):
                    self._owners.pop(key)
            raise

    def _challenge_update(self, instance, challenge):
        if not isinstance(challenge, Challenge):
            raise ContractError("invalid plugin challenge")
        with self._lock:
            if self._active(instance) and instance.handshake_verified:
                instance.challenge = challenge

    def challenge(self, plugin_id, *, instance_key="default"):
        with self._lock:
            self._profile(plugin_id, instance_key)
            instance = self._instances.get(self._slot(plugin_id, instance_key))
            value = instance.challenge if instance is not None else None
            if value is None or (value.expires_at is not None and value.expires_at <= time.time()):
                return None
            return {"challenge_id": value.challenge_id, "kind": value.kind,
                    "title": value.title, "payload": thaw_json(value.payload),
                    "expires_at": value.expires_at}

    def session_is_online(self, instance, session):
        with self._lock:
            return (self._active(instance) and instance.handshake_verified
                    and instance.session == session and instance.session_state == "online")

    def _publish(self, instance, envelope):
        if not isinstance(envelope, EventEnvelope):
            raise ContractError("plugin events require an SDK envelope")
        with self._lock:
            if (not self.session_is_online(instance, envelope.session)
                    or instance.runtime is None):
                return PublishAck(envelope.event_id, False)
            runtime = instance.runtime
        event = thaw_json(envelope.event)
        if event.get("self_id") != int(envelope.session.self_id):
            raise ContractError("event identity differs from the bound account")
        try:
            receipt = runtime.ingest(
                event, event_key=envelope.event_id,
                session_epoch=envelope.session.session_epoch, strict_event_id=True)
        except BackendError as error:
            if error.retcode == 1409:
                raise ContractError("event ID conflicts with a committed event") from None
            return PublishAck(envelope.event_id, False)
        return PublishAck(envelope.event_id, True, receipt["duplicate"])

    def call(self, instance, session, action, params):
        with self._lock:
            if not self.session_is_online(instance, session):
                raise BackendError("Plugin session is unavailable.", 1503)
            if action not in instance.registration.manifest.actions:
                raise BackendError("Plugin does not support this action.", 1404)
            plugin = instance.plugin
            instance.inflight += 1
        try:
            request = ActionRequest(uuid4().hex, session, action, params,
                                    time.monotonic() + self.action_timeout)
            result = validate_action_result(request, plugin.call(request))
            # A completed result is evidence even if shutdown raced the call.
            if not result.succeeded:
                details = {"action_completed": result.outcome == "completed",
                           "retry_safe": False}
                if result.outcome == "unknown":
                    details = {"action_outcome": "unknown", "retry_safe": False}
                data = thaw_json(result.data)
                if isinstance(data, dict) and type(data.get("message_id")) is int:
                    details["message_id"] = data["message_id"]
                raise BackendError("Plugin action failed: " + result.error_message,
                                   result.error_code, details=details)
            return thaw_json(result.data)
        except BackendError:
            raise
        except Exception:
            raise BackendError("Plugin action failed; do not automatically resend.", 1502,
                               details={"action_outcome": "unknown", "retry_safe": False}) from None
        finally:
            with self._lock:
                instance.inflight -= 1

    def account_runtimes(self):
        with self._lock:
            return [(instance.session, instance.runtime) for instance in self._instances.values()
                    if instance.runtime is not None and instance.session is not None
                    and self.session_is_online(instance, instance.session)]

    def account_descriptors(self):
        with self._lock:
            return [
                {**instance.session.to_dict(),
                 "plugin_id": instance.registration.manifest.plugin_id,
                 "instance_key": instance.instance_key,
                 "display_name": instance.display_name}
                for instance in self._instances.values()
                if instance.runtime is not None and instance.session is not None
                and self.session_is_online(instance, instance.session)
            ]

    def _revoke(self, instance):
        instance.stop_event.set()
        if instance.stop_deadline is None:
            instance.stop_deadline = time.monotonic() + self.control_timeout
        instance.challenge = None
        instance.session_state = "offline"
        instance.session_reason = "Plugin instance is no longer active"
        # Keep account ownership reserved until shutdown and in-flight work are
        # confirmed complete. Revocation alone cannot cancel a platform send.

    def _fail(self, instance, reason):
        with self._lock:
            if self._instances.get(self._slot(instance.registration.manifest.plugin_id,
                                              instance.instance_key)) is not instance:
                return
            instance.state = "failed"
            instance.error = reason
            self._revoke(instance)
        self._ensure_stopper(instance)

    def _ensure_stopper(self, instance):
        with self._lock:
            if instance.stopper is not None:
                return
            instance.stopper = threading.Thread(
                target=self._stop_instance, args=(instance,),
                name="nyxion-plugin-stop", daemon=True)
            instance.stopper.start()

    def _stop_instance(self, instance):
        failed = False
        allocator = getattr(self, "network_allocator", None)
        if allocator is not None:
            try:
                with instance.network_lifecycle:
                    allocator.release_module(self._module_id(instance))
            except Exception:
                failed = True
        try:
            plugin = instance.plugin
            if plugin is not None:
                plugin.stop("Host stopped or revoked the plugin instance")
            worker = instance.thread
            if worker is not None and worker is not threading.current_thread():
                worker.join(timeout=self.control_timeout)
                if worker.is_alive():
                    failed = True
        except Exception:
            failed = True
        finally:
            if allocator is not None:
                try:
                    with instance.network_lifecycle:
                        allocator.release_module(self._module_id(instance))
                except Exception:
                    failed = True
            with self._lock:
                runtime, instance.runtime = instance.runtime, None
            if runtime is not None:
                try:
                    runtime.close()
                except Exception:
                    failed = True
            with self._lock:
                if failed:
                    instance.state = "failed"
                    instance.error = "Plugin did not stop cleanly; no replacement was started"
                elif instance.state != "failed":
                    instance.state = "stopped"
                instance.clean_stop = not failed
                instance.stop_deadline = None
                instance.session_state = "stopped"
                if not failed and instance.inflight == 0:
                    for key, owner in list(self._owners.items()):
                        if owner is instance:
                            self._owners.pop(key)
                elif instance.inflight:
                    instance.clean_stop = False
                    instance.state = "failed"
                    instance.error = "Plugin action is still in flight; account remains reserved"

    def stop(self, plugin_id, *, instance_key="default"):
        with self._lock:
            registration = self._get(plugin_id)
            self._profile(plugin_id, instance_key)
            instance = self._instances.get(self._slot(plugin_id, instance_key))
            if instance is None or instance.state == "stopped":
                return self._snapshot(registration, instance_key)
            if instance.state != "failed":
                instance.state = "stopping"
            self._revoke(instance)
        self._ensure_stopper(instance)
        with self._lock:
            return self._snapshot(registration, instance_key)

    def _monitor_leases(self):
        while not self._stop.wait(min(0.1, self.heartbeat_interval)):
            now = time.monotonic()
            with self._lock:
                expired = [
                    instance for instance in self._instances.values()
                    if self._active(instance)
                    and ((instance.control_deadline is not None and now >= instance.control_deadline)
                         or (instance.state == "running" and instance.last_seen is not None
                             and now - instance.last_seen >= self.lease_seconds))
                ]
                late_stops = [
                    instance for instance in self._instances.values()
                    if instance.stop_deadline is not None and now >= instance.stop_deadline
                    and instance.stopper is not None and instance.stopper.is_alive()
                ]
                for instance in late_stops:
                    instance.state = "failed"
                    instance.error = "Plugin shutdown deadline expired; replacement is blocked"
            for instance in expired:
                self._fail(instance, "Plugin control lease expired")

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closing = True
            instances = list(self._instances.values())
        for instance in instances:
            self.stop(instance.registration.manifest.plugin_id, instance_key=instance.instance_key)
        self._stop.set()
        self._monitor.join(timeout=1.0)
        with self._lock:
            self._closed = True
            workers = [instance.stopper for instance in self._instances.values()
                       if instance.stopper is not None]
        deadline = time.monotonic() + self.control_timeout + 1
        for worker in workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))

