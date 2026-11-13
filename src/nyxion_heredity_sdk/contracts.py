"""NyXion Heredity SDK v1: immutable embedded-plugin boundary objects."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import math
from pathlib import Path
import re
import time
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from .errors import CompatibilityError, ContractError, DeadlineExceeded, SessionMismatch


API_MAJOR = 1
API_MINOR = 0
CHALLENGE_KINDS = frozenset({"qr", "input", "notice"})
SESSION_STATES = frozenset({
    "starting", "login_required", "authenticating", "online", "offline", "failed", "stopped",
})
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 32768
MAX_JSON_BYTES = 1024 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_PLUGIN_ID = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
_ACTION = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}\Z")
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[A-Za-z0-9.-]+)?\Z")
_PAGE_ID = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_OUTCOMES = frozenset({"not_started", "completed", "unknown"})


def _text(value, name, *, limit=4096, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value):
        raise ContractError(f"{name} has an invalid string length")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ContractError(f"{name} must be valid UTF-8") from None
    return value


def _identifier(value, name):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ContractError(f"{name} must be a bounded identifier")
    return value


def _integer(value, name, *, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ContractError(f"{name} has an invalid integer range")
    return value


def _finite(value, name, *, positive=True):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or (positive and value <= 0)):
        raise ContractError(f"{name} must be finite" + (" and positive" if positive else ""))
    return value


def freeze_json(value):
    """Validate and freeze ordinary JSON once, without serializing it.

    Limits apply to UTF-8 content plus a conservative per-node charge. A wire
    transport must additionally bound its own encoded frame. No custom mapping
    callbacks, arbitrary objects, NaN, cycles or mutable nested data are kept.
    """
    ancestors = set()
    budget = [0, 0]

    def visit(item, depth):
        if depth > MAX_JSON_DEPTH:
            raise ContractError("JSON payload exceeds the nesting limit")
        budget[0] += 1
        budget[1] += 16
        if budget[0] > MAX_JSON_NODES or budget[1] > MAX_JSON_BYTES:
            raise ContractError("JSON payload exceeds the SDK size limit")
        if type(item) is int:
            if not -(2**63) <= item <= 2**64 - 1:
                raise ContractError("JSON integer exceeds the SDK 64-bit range")
            return item
        if item is None or type(item) is bool:
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise ContractError("JSON payload contains a non-finite number")
            return item
        if type(item) is str:
            try:
                budget[1] += len(item.encode("utf-8"))
            except UnicodeError:
                raise ContractError("JSON payload contains invalid Unicode") from None
            if budget[1] > MAX_JSON_BYTES:
                raise ContractError("JSON payload exceeds the SDK size limit")
            return item
        if type(item) not in (dict, list, tuple, MappingProxyType):
            raise ContractError("SDK payload must contain ordinary JSON values")
        identity = id(item)
        if identity in ancestors:
            raise ContractError("JSON payload contains a cycle")
        ancestors.add(identity)
        try:
            if type(item) in (dict, MappingProxyType):
                if any(type(key) is not str for key in item):
                    raise ContractError("JSON object keys must be strings")
                return MappingProxyType({
                    visit(key, depth + 1): visit(child, depth + 1)
                    for key, child in item.items()
                })
            return tuple(visit(child, depth + 1) for child in item)
        finally:
            ancestors.remove(identity)

    return visit(value, 0)


def thaw_json(value):
    """Make a mutable JSON copy only when an integration boundary needs it."""
    if type(value) in (dict, MappingProxyType):
        return {key: thaw_json(child) for key, child in value.items()}
    if type(value) in (list, tuple):
        return [thaw_json(child) for child in value]
    return value


def _mapping(value, name):
    if type(value) not in (dict, MappingProxyType):
        raise ContractError(f"{name} must be a JSON object")
    return freeze_json(value)


def _names(value, name, pattern=_ACTION):
    if type(value) not in (tuple, list) or len(value) > 4096:
        raise ContractError(f"{name} must be a bounded sequence")
    if any(not isinstance(item, str) or not pattern.fullmatch(item) for item in value):
        raise ContractError(f"{name} contains an invalid identifier")
    if len(value) != len(set(value)):
        raise ContractError(f"{name} contains duplicate values")
    return tuple(value)


@dataclass(frozen=True)
class PluginUIPage:
    """Declare a host-rendered view; plugins never supply executable page code."""

    id: str
    title: str
    view: str = "accounts"
    icon: str = "users"

    def __post_init__(self):
        if (not isinstance(self.id, str) or len(self.id) > 64
                or not _PAGE_ID.fullmatch(self.id)):
            raise ContractError("UI page ID must be a lowercase route segment")
        _text(self.title, "UI page title", limit=80)
        if not self.title.strip() or any(ord(char) < 32 or ord(char) == 127 for char in self.title):
            raise ContractError("UI page title must be a visible single-line label")
        if self.view != "accounts" or self.icon != "users":
            raise ContractError("UI page view or icon is unsupported")

    @classmethod
    def from_dict(cls, data):
        if type(data) is not dict or set(data) != {"id", "title", "view", "icon"}:
            raise ContractError("UI page fields are missing or unknown")
        return cls(**data)

    def to_dict(self):
        return {"id": self.id, "title": self.title, "view": self.view, "icon": self.icon}


UIPage = PluginUIPage


@dataclass(frozen=True)
class PluginManifest:
    plugin_id: str
    name: str
    version: str
    platform: str
    actions: tuple[str, ...]
    capabilities: tuple[str, ...] = ()
    config_schema: Mapping = field(default_factory=lambda: {"type": "object"})
    execution: str = "embedded"
    api_major: int = API_MAJOR
    api_minor: int = API_MINOR
    host_api_min_minor: int = 0
    host_api_max_minor: int = API_MINOR
    schema_version: int = 1
    ui_pages: tuple[PluginUIPage, ...] = ()

    def __post_init__(self):
        if (not isinstance(self.plugin_id, str) or len(self.plugin_id) > 128
                or not _PLUGIN_ID.fullmatch(self.plugin_id)):
            raise ContractError("plugin_id must be a lowercase namespaced identifier")
        _text(self.name, "name", limit=160)
        if (not isinstance(self.version, str) or len(self.version) > 64
                or not _VERSION.fullmatch(self.version)):
            raise ContractError("version must be a semantic major.minor.patch version")
        if (not isinstance(self.platform, str) or len(self.platform) > 128
                or not _PLUGIN_ID.fullmatch(self.platform)):
            raise ContractError("platform must be a lowercase identifier")
        if self.execution != "embedded":
            raise ContractError("SDK v1 currently implements embedded execution only")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ContractError("unsupported manifest schema")
        _integer(self.api_major, "api_major", minimum=1, maximum=65535)
        _integer(self.api_minor, "api_minor", maximum=65535)
        _integer(self.host_api_min_minor, "host_api_min_minor", maximum=65535)
        _integer(self.host_api_max_minor, "host_api_max_minor", maximum=65535)
        if self.host_api_min_minor > self.host_api_max_minor:
            raise ContractError("host API minor compatibility range is reversed")
        object.__setattr__(self, "actions", _names(self.actions, "actions"))
        object.__setattr__(self, "capabilities", _names(self.capabilities, "capabilities"))
        schema = _mapping(self.config_schema, "config_schema")
        if schema.get("type") != "object":
            raise ContractError("config_schema must describe a JSON object")
        from .config import validate_config_schema
        validate_config_schema(schema)
        object.__setattr__(self, "config_schema", schema)
        if type(self.ui_pages) not in (tuple, list) or len(self.ui_pages) > 8:
            raise ContractError("ui_pages must be a bounded sequence")
        pages = tuple(
            page if isinstance(page, PluginUIPage) else PluginUIPage.from_dict(page)
            for page in self.ui_pages
        )
        if len({page.id for page in pages}) != len(pages):
            raise ContractError("UI page IDs must be unique within a plugin")
        object.__setattr__(self, "ui_pages", pages)

    @classmethod
    def from_dict(cls, data):
        if type(data) is not dict:
            raise ContractError("manifest must be a JSON object")
        required = {"schema_version", "plugin_id", "name", "version", "platform",
                    "actions", "capabilities", "config_schema", "execution",
                    "api_major", "api_minor", "host_api_min_minor", "host_api_max_minor"}
        if not required.issubset(data) or set(data) - required - {"ui_pages"}:
            raise ContractError("manifest fields are missing or unknown")
        return cls(**data)

    def to_dict(self):
        data = {
            "schema_version": self.schema_version, "plugin_id": self.plugin_id,
            "name": self.name, "version": self.version, "platform": self.platform,
            "actions": list(self.actions), "capabilities": list(self.capabilities),
            "config_schema": thaw_json(self.config_schema), "execution": self.execution,
            "api_major": self.api_major, "api_minor": self.api_minor,
            "host_api_min_minor": self.host_api_min_minor,
            "host_api_max_minor": self.host_api_max_minor,
        }
        if self.ui_pages:
            data["ui_pages"] = [page.to_dict() for page in self.ui_pages]
        return data

    def supports_host(self, major=API_MAJOR, minor=API_MINOR):
        return (type(major) is int and type(minor) is int
                and major == self.api_major
                and self.host_api_min_minor <= minor <= self.host_api_max_minor
                and self.api_minor <= minor)


@dataclass(frozen=True)
class SessionIdentity:
    platform: str
    self_id: str
    session_epoch: str

    def __post_init__(self):
        if (not isinstance(self.platform, str) or len(self.platform) > 128
                or not _PLUGIN_ID.fullmatch(self.platform)):
            raise ContractError("session platform is invalid")
        _identifier(self.self_id, "self_id")
        _identifier(self.session_epoch, "session_epoch")

    def to_dict(self):
        return {"platform": self.platform, "self_id": self.self_id,
                "session_epoch": self.session_epoch}


@dataclass(frozen=True)
class HostHello:
    instance_id: str
    nonce: str = field(repr=False)
    api_major: int = API_MAJOR
    api_minor: int = API_MINOR

    def __post_init__(self):
        _identifier(self.instance_id, "instance_id")
        _identifier(self.nonce, "nonce")
        _integer(self.api_major, "api_major", minimum=1, maximum=65535)
        _integer(self.api_minor, "api_minor", maximum=65535)


@dataclass(frozen=True)
class PluginHello:
    instance_id: str
    nonce: str = field(repr=False)
    plugin_id: str
    api_major: int = API_MAJOR
    api_minor: int = API_MINOR

    def __post_init__(self):
        _identifier(self.instance_id, "instance_id")
        _identifier(self.nonce, "nonce")
        _identifier(self.plugin_id, "plugin_id")
        _integer(self.api_major, "api_major", minimum=1, maximum=65535)
        _integer(self.api_minor, "api_minor", maximum=65535)


@dataclass(frozen=True)
class Heartbeat:
    instance_id: str
    nonce: str = field(repr=False)
    sequence: int
    deadline: float

    def __post_init__(self):
        _identifier(self.instance_id, "instance_id")
        _identifier(self.nonce, "nonce")
        _integer(self.sequence, "sequence", minimum=1)
        _finite(self.deadline, "deadline")


@dataclass(frozen=True)
class Pong:
    instance_id: str
    nonce: str = field(repr=False)
    sequence: int

    def __post_init__(self):
        _identifier(self.instance_id, "instance_id")
        _identifier(self.nonce, "nonce")
        _integer(self.sequence, "sequence", minimum=1)


@dataclass(frozen=True)
class ActionRequest:
    request_id: str
    session: SessionIdentity
    action: str
    params: Mapping = field(repr=False)
    deadline: float

    def __post_init__(self):
        _identifier(self.request_id, "request_id")
        if not isinstance(self.session, SessionIdentity):
            raise ContractError("action must bind a session identity")
        if not isinstance(self.action, str) or not _ACTION.fullmatch(self.action):
            raise ContractError("action name is invalid")
        _finite(self.deadline, "deadline")
        object.__setattr__(self, "params", _mapping(self.params, "params"))

    def require_live(self, now=None):
        if (time.monotonic() if now is None else now) >= self.deadline:
            raise DeadlineExceeded("Action deadline expired; no request may be started")


@dataclass(frozen=True)
class ActionResult:
    request_id: str
    outcome: str
    data: Any = field(default=None, repr=False)
    error_code: int | None = None
    error_message: str = ""

    def __post_init__(self):
        _identifier(self.request_id, "request_id")
        if not isinstance(self.outcome, str) or self.outcome not in _OUTCOMES:
            raise ContractError("action outcome is invalid")
        if self.error_code is None:
            if self.outcome != "completed" or self.error_message:
                raise ContractError("a successful action must be completed")
        else:
            _integer(self.error_code, "error_code", minimum=1, maximum=2**31 - 1)
        _text(self.error_message, "error_message", limit=4096, empty=True)
        object.__setattr__(self, "data", freeze_json(self.data))

    @property
    def succeeded(self):
        return self.error_code is None


@dataclass(frozen=True)
class EventEnvelope:
    session: SessionIdentity
    event_id: str
    event: Mapping = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.session, SessionIdentity):
            raise ContractError("event must bind a session identity")
        _identifier(self.event_id, "event_id")
        object.__setattr__(self, "event", _mapping(self.event, "event"))


@dataclass(frozen=True)
class PublishAck:
    event_id: str
    persisted: bool
    duplicate: bool = False

    def __post_init__(self):
        _identifier(self.event_id, "event_id")
        if type(self.persisted) is not bool or type(self.duplicate) is not bool:
            raise ContractError("publish acknowledgement fields must be booleans")
        if self.duplicate and not self.persisted:
            raise ContractError("duplicate events must already be durably persisted")


@dataclass(frozen=True)
class SessionUpdate:
    session: SessionIdentity | None
    state: str
    reason: str = ""

    def __post_init__(self):
        if self.session is not None and not isinstance(self.session, SessionIdentity):
            raise ContractError("session update identity is invalid")
        if not isinstance(self.state, str) or self.state not in SESSION_STATES:
            raise ContractError("session state is invalid")
        if self.state == "online" and self.session is None:
            raise ContractError("an online update must bind an account session")
        _text(self.reason, "reason", limit=1024, empty=True)


@dataclass(frozen=True)
class Challenge:
    challenge_id: str
    kind: str
    title: str
    payload: Mapping = field(repr=False)
    expires_at: float | None = None

    def __post_init__(self):
        _identifier(self.challenge_id, "challenge_id")
        if not isinstance(self.kind, str) or self.kind not in CHALLENGE_KINDS:
            raise ContractError("challenge kind is unsupported")
        _text(self.title, "title", limit=160)
        if self.expires_at is not None:
            _finite(self.expires_at, "expires_at")
        object.__setattr__(self, "payload", _mapping(self.payload, "challenge payload"))


class PluginContext:
    """Scoped host services; callbacks expose no runtime, DB or global registry."""

    def __init__(self, instance_id: str, state_dir: Path, *,
                 publish_event: Callable[[EventEnvelope], PublishAck],
                 update_session: Callable[[SessionUpdate], None],
                 publish_challenge: Callable[[Challenge], None],
                 networks=None):
        self.instance_id = _identifier(instance_id, "instance_id")
        self.state_dir = Path(state_dir)
        if not self.state_dir.is_absolute():
            raise ContractError("plugin state_dir must be absolute")
        for callback in (publish_event, update_session, publish_challenge):
            if not callable(callback):
                raise ContractError("host services must be callable")
        self._publish_event = publish_event
        self._update_session = update_session
        self._publish_challenge = publish_challenge
        self.networks = networks

    def publish_event(self, envelope: EventEnvelope) -> PublishAck:
        if not isinstance(envelope, EventEnvelope):
            raise ContractError("publish_event requires an EventEnvelope")
        acknowledgement = self._publish_event(envelope)
        if (not isinstance(acknowledgement, PublishAck)
                or acknowledgement.event_id != envelope.event_id):
            raise ContractError("host returned an invalid event acknowledgement")
        return acknowledgement

    def update_session(self, update: SessionUpdate) -> None:
        if not isinstance(update, SessionUpdate):
            raise ContractError("update_session requires a SessionUpdate")
        self._update_session(update)

    def publish_challenge(self, challenge: Challenge) -> None:
        if not isinstance(challenge, Challenge):
            raise ContractError("publish_challenge requires a Challenge")
        self._publish_challenge(challenge)


@runtime_checkable
class Plugin(Protocol):
    manifest: PluginManifest

    def handshake(self, hello: HostHello) -> PluginHello: ...
    def start(self, context: PluginContext, config: Mapping) -> None: ...
    def ping(self, heartbeat: Heartbeat) -> Pong: ...
    def call(self, request: ActionRequest) -> ActionResult: ...
    def stop(self, reason: str) -> None: ...


def validate_handshake(manifest: PluginManifest, hello: HostHello, reply: PluginHello):
    if not isinstance(manifest, PluginManifest) or not isinstance(hello, HostHello):
        raise ContractError("handshake inputs must be SDK contract types")
    if not manifest.supports_host(hello.api_major, hello.api_minor):
        raise CompatibilityError("plugin does not support this host API")
    if (not isinstance(reply, PluginHello) or reply.instance_id != hello.instance_id
            or reply.nonce != hello.nonce or reply.plugin_id != manifest.plugin_id):
        raise ContractError("handshake reply does not bind this plugin instance")
    if (reply.api_major != manifest.api_major or reply.api_minor != manifest.api_minor
            or reply.api_major != hello.api_major or reply.api_minor > hello.api_minor):
        raise CompatibilityError("plugin handshake API differs from its manifest")
    return reply


def validate_pong(heartbeat: Heartbeat, pong: Pong, *, now=None):
    if not isinstance(heartbeat, Heartbeat):
        raise ContractError("heartbeat must be an SDK Heartbeat")
    if (time.monotonic() if now is None else now) >= heartbeat.deadline:
        raise DeadlineExceeded("plugin heartbeat deadline expired")
    if (not isinstance(pong, Pong) or pong.instance_id != heartbeat.instance_id
            or pong.nonce != heartbeat.nonce or pong.sequence != heartbeat.sequence):
        raise ContractError("heartbeat reply belongs to another probe or instance")
    return pong


def validate_action_result(request: ActionRequest, result: ActionResult):
    if not isinstance(result, ActionResult) or result.request_id != request.request_id:
        raise ContractError("action result does not match its request")
    return result


def require_session(observed: SessionIdentity, expected: SessionIdentity):
    if not isinstance(observed, SessionIdentity) or observed != expected:
        raise SessionMismatch("operation belongs to another account or session epoch")
    return observed
