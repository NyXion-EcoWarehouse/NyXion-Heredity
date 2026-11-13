"""Immutable descriptions of execution boundaries, independent of action ability."""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
import re


class ExecutionLevel(str, Enum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


_PROVIDER_ID = re.compile(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*\Z")
_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ACTION = re.compile(r"[A-Za-z0-9_]{1,128}\Z")
_SOURCE_KINDS = frozenset({"demo", "profile", "native", "unconfigured"})
_AVAILABILITY = frozenset(
    {"ready", "partial", "unimplemented", "configuration_required"}
)
_PROVENANCE = frozenset({"none", "declared", "implementation"})


def _text(value, name, *, limit, allow_empty=False):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{name} must be a string of at most {limit} characters.")
    if not allow_empty and not value.strip():
        raise ValueError(f"{name} must not be empty.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError(f"{name} must contain valid Unicode.") from None
    return value


def _token(value, name):
    _text(value, name, limit=64)
    if not _TOKEN.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase identifier.")


def _tuple_tokens(value, name, pattern=_TOKEN):
    if type(value) is not tuple:
        raise ValueError(f"{name} must be an immutable tuple.")
    if len(value) > 4096:
        raise ValueError(f"{name} contains too many entries.")
    for item in value:
        if not isinstance(item, str) or not pattern.fullmatch(item):
            raise ValueError(f"{name} contains an invalid identifier.")
    if len(set(value)) != len(value):
        raise ValueError(f"{name} must not contain duplicate entries.")


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    label: str
    execution_level: ExecutionLevel | None
    source_kind: str
    host_kind: str
    session_owner: str
    target_memory_access: str
    in_process_code: bool
    business_request_builder: str
    primary_transport_owner: str
    auxiliary_transports: tuple[str, ...] = ()
    event_sources: tuple[str, ...] = ()
    supported_actions: tuple[str, ...] = ()
    receipt_evidence: tuple[str, ...] = ()
    availability: str = "unimplemented"
    reason: str = ""
    level_provenance: str = "none"

    def __post_init__(self) -> None:
        _text(self.provider_id, "provider_id", limit=64)
        if not _PROVIDER_ID.fullmatch(self.provider_id):
            raise ValueError("provider_id must be a lowercase identifier with optional separators.")
        _text(self.label, "label", limit=160)
        if any(ord(character) < 32 for character in self.label):
            raise ValueError("label must not contain control characters.")
        _text(self.reason, "reason", limit=4096, allow_empty=True)
        if self.execution_level is not None and not isinstance(
            self.execution_level, ExecutionLevel
        ):
            raise ValueError("execution_level must be ExecutionLevel or None.")
        for name in (
            "source_kind",
            "host_kind",
            "session_owner",
            "target_memory_access",
            "business_request_builder",
            "primary_transport_owner",
            "availability",
            "level_provenance",
        ):
            _token(getattr(self, name), name)
        if self.source_kind not in _SOURCE_KINDS:
            raise ValueError("source_kind is unsupported.")
        if self.target_memory_access not in {"none", "read", "write"}:
            raise ValueError("target_memory_access must be none, read or write.")
        if type(self.in_process_code) is not bool:
            raise ValueError("in_process_code must be a boolean.")
        if self.availability not in _AVAILABILITY:
            raise ValueError("availability is unsupported.")
        if self.level_provenance not in _PROVENANCE:
            raise ValueError("level_provenance is unsupported.")
        for name in ("auxiliary_transports", "event_sources", "receipt_evidence"):
            _tuple_tokens(getattr(self, name), name)
        _tuple_tokens(self.supported_actions, "supported_actions", _ACTION)
        self._validate_level()

    def _validate_level(self) -> None:
        level = self.execution_level
        if level is None:
            if self.level_provenance != "none":
                raise ValueError("An unspecified execution level requires provenance none.")
            if self.source_kind == "native":
                raise ValueError("A native execution provider requires an explicit level.")
            return
        if self.source_kind in {"demo", "unconfigured"}:
            raise ValueError("Demo and unconfigured sources have no native execution level.")
        if self.level_provenance == "none":
            raise ValueError("An execution level requires declared or implementation provenance.")
        expected = {
            ExecutionLevel.L0: ("official_gui", "official_client", "official_client"),
            ExecutionLevel.L1: ("attached_client", "official_client", "official_client"),
            ExecutionLevel.L2: ("full_client", "heredity", "heredity"),
            ExecutionLevel.L3: ("protocol_core", "heredity", "heredity"),
        }[level]
        actual = (self.host_kind, self.session_owner, self.primary_transport_owner)
        if actual != expected:
            raise ValueError(f"{level.value} host, session and primary transport boundaries conflict.")
        if level is ExecutionLevel.L0:
            if self.in_process_code or self.target_memory_access == "write":
                raise ValueError("L0 cannot run in-process code or write target memory.")
            if self.business_request_builder != "official_services":
                raise ValueError("L0 business requests must remain inside official services.")
            if any(source not in {"ui", "memory_observer"} for source in self.event_sources):
                raise ValueError("L0 event sources must be UI or read-only memory observations.")
        elif level is ExecutionLevel.L1 and not self.in_process_code:
            raise ValueError("L1 requires code in the attached official client.")
        elif level in {ExecutionLevel.L2, ExecutionLevel.L3}:
            if self.business_request_builder != "heredity":
                raise ValueError(f"{level.value} business requests must be built by Heredity.")
            if self.target_memory_access != "none":
                raise ValueError(f"{level.value} does not access another client's memory.")
            if self.in_process_code:
                raise ValueError(f"{level.value} does not inject code into another client.")

    @property
    def selectable(self) -> bool:
        return self.availability == "ready"

    def to_dict(self) -> dict[str, object]:
        result = {}
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, ExecutionLevel):
                value = value.value
            elif isinstance(value, tuple):
                value = list(value)
            result[field.name] = value
        result["selectable"] = self.selectable
        return result


def level_catalog() -> list[dict]:
    """Return technical mode labels; these do not imply action support."""
    return [
        {
            "level": "L0",
            "label": "界面驱动",
            "description": "完整官方客户端维护会话；通过系统输入或可访问性执行动作，目标内存仅查询或读取。",
        },
        {
            "level": "L1",
            "label": "进程附着",
            "description": "在已运行的官方客户端内附着扩展；动作借用原客户端的会话与主传输。",
        },
        {
            "level": "L2",
            "label": "完整客户端",
            "description": "Heredity 自研完整客户端与协议引擎，独立维护会话、主传输和业务能力。",
        },
        {
            "level": "L3",
            "label": "协议核心",
            "description": "在 L2 基础上裁去客户端外壳，以最小协议核心直接维护会话和主传输。",
        },
    ]
