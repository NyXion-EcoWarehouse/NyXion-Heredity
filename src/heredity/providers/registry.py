"""Explicit provider configuration; reading a registry never contacts a live platform."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from urllib.parse import urlsplit

from ..backends.http_json import HTTPJSONBackend
from ..backends.mock import MockBackend
from ..messages import positive_id
from .contracts import ExecutionLevel, ProviderDescriptor, level_catalog
from .manager import ProviderManager, ProviderRegistration
from .placeholders import (
    AttachedProcessProvider, CoreProtocolProvider, FullClientProvider,
    UiDriverProvider, placeholder_descriptors,
)

DEMO_ACTIONS = (
    "get_login_info", "get_friend_list", "get_group_list", "send_private_msg",
    "send_group_msg", "send_msg", "get_msg", "delete_msg", "can_send_image",
    "can_send_record",
)


def _read_json(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Provider configuration exceeds 1 MiB.")
    def reject_constant(value):
        raise ValueError("Provider configuration must contain finite JSON.")
    value = json.loads(raw, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("Provider configuration must be a JSON object.")
    return value


def read_registry(path):
    data = _read_json(path)
    if data.get("schema_version") != 1 or type(data.get("schema_version")) is not int:
        raise ValueError("Provider registry requires schema_version 1.")
    account = positive_id(data.get("self_id"), "self_id")
    if account > 2**63 - 1:
        raise ValueError("Provider account exceeds the supported range.")
    entries = data.get("providers")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise ValueError("Provider registry requires 1..100 provider entries.")
    if any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("Provider entries must be objects.")
    if any(not isinstance(entry.get("kind"), str)
           or entry["kind"] not in {"demo", "http_json"} for entry in entries):
        raise ValueError("Provider kind must be demo or http_json.")
    if {entry.get("kind") for entry in entries} == {"demo", "http_json"}:
        raise ValueError("Demo and real profile providers require separate registries.")
    if not isinstance(data.get("default_provider"), str):
        raise ValueError("default_provider must identify an explicit provider.")
    data["self_id"] = account
    return data


def read_profile(path):
    return _read_json(path)


def demo_registration(store, self_id=10000, provider_id="demo", label="Heredity Demo"):
    descriptor = ProviderDescriptor(
        provider_id=provider_id, label=label, execution_level=None, source_kind="demo",
        host_kind="simulation", session_owner="heredity", target_memory_access="none",
        in_process_code=False, business_request_builder="simulation",
        primary_transport_owner="simulation", event_sources=("synthetic",),
        supported_actions=DEMO_ACTIONS, receipt_evidence=("simulation",),
        availability="ready", reason="模拟提供方；不连接真实 IM 平台。")
    return ProviderRegistration(
        descriptor, lambda: MockBackend(
            self_id=self_id, id_factory=lambda: store.allocate_message_id(self_id)))


def profile_registration(profile, *, provider_id="profile", label="HTTP JSON provider",
                         level=None, enabled=True, forbidden_port=None, mechanism=None):
    profile = deepcopy(profile)
    account = positive_id(profile.get("self_id"), "self_id")
    actions = profile.get("actions")
    origin = profile.get("base_url")
    if isinstance(origin, str) and forbidden_port and urlsplit(origin).port == forbidden_port:
        raise ValueError("A profile cannot point back to this gateway's listening port.")
    if not isinstance(actions, dict) or not actions:
        raise ValueError("Profile provider requires an explicit action map.")
    availability, reason = "ready", "显式配置的外部 HTTP 服务；不证明其原生实现或执行等级。"
    if "get_login_info" not in actions:
        availability, reason = "configuration_required", "缺少账号身份确认动作 get_login_info。"
    try:
        probe = HTTPJSONBackend(profile)
        probe.close()
    except ValueError:
        availability, reason = "configuration_required", "HTTP 配置或凭据未就绪。"
    if type(enabled) is not bool:
        raise ValueError("Provider enabled flag must be boolean.")
    if not enabled:
        availability, reason = "configuration_required", "提供方已在配置中禁用。"
    execution = ExecutionLevel(level) if level is not None else None
    boundaries = {
        None: ("external_service", "external", "external", False, "none", "mixed"),
        ExecutionLevel.L0: ("official_gui", "official_client", "official_client", False,
                            "read", "official_services"),
        ExecutionLevel.L1: ("attached_client", "official_client", "official_client", True,
                            "write", "mixed"),
        ExecutionLevel.L2: ("full_client", "heredity", "heredity", False,
                            "none", "heredity"),
        ExecutionLevel.L3: ("protocol_core", "heredity", "heredity", False, "none", "heredity"),
    }[execution]
    host, session, transport, in_process, memory, builder = boundaries
    descriptor = ProviderDescriptor(
        provider_id=provider_id, label=label, execution_level=execution,
        source_kind="profile", host_kind=host, session_owner=session,
        target_memory_access=memory, in_process_code=in_process,
        business_request_builder=builder, primary_transport_owner=transport,
        supported_actions=tuple(actions), availability=availability, reason=reason,
        level_provenance="declared" if execution is not None else "none")
    if mechanism is not None:
        allowed = {"host_kind", "session_owner", "target_memory_access", "in_process_code",
                   "business_request_builder", "primary_transport_owner",
                   "auxiliary_transports", "event_sources", "receipt_evidence"}
        if not isinstance(mechanism, dict) or any(key not in allowed for key in mechanism):
            raise ValueError("Invalid provider mechanism declaration.")
        declared = dict(mechanism)
        for key in ("auxiliary_transports", "event_sources", "receipt_evidence"):
            if key in declared:
                if not isinstance(declared[key], list):
                    raise ValueError("Mechanism collection fields must be arrays.")
                declared[key] = tuple(declared[key])
        descriptor = replace(descriptor, **declared)
    return account, ProviderRegistration(descriptor, lambda: HTTPJSONBackend(deepcopy(profile)))


def placeholder_registrations(self_id):
    classes = (UiDriverProvider, AttachedProcessProvider, FullClientProvider,
               CoreProtocolProvider)
    return [ProviderRegistration(descriptor, lambda cls=cls: cls(self_id))
            for descriptor, cls in zip(placeholder_descriptors(), classes)]


def build_manager(store, self_id, *, demo=False, profile=None, registry=None,
                  registry_directory=None, initial_provider_id=None, activate=True,
                  forbidden_port=None):
    registrations = placeholder_registrations(self_id)
    initial = None
    if registry is not None:
        directory = Path(registry_directory or ".")
        for entry in registry["providers"]:
            identifier = entry.get("id")
            if not isinstance(identifier, str):
                raise ValueError("Provider entry requires an id.")
            label = entry.get("label", identifier)
            kind = entry.get("kind")
            if kind == "demo":
                if entry.get("execution_level") is not None:
                    raise ValueError("A demo cannot declare a native execution level.")
                if entry.get("enabled", True) is not True:
                    raise ValueError("Disabled demo entries should be removed from the registry.")
                registration = demo_registration(store, self_id, identifier, label)
            elif kind == "http_json":
                filename = entry.get("profile")
                if not isinstance(filename, str) or not filename:
                    raise ValueError("HTTP provider requires an explicit profile file.")
                data = _read_json(directory / filename)
                account, registration = profile_registration(
                    data, provider_id=identifier, label=label,
                    level=entry.get("execution_level"), enabled=entry.get("enabled", True),
                    forbidden_port=forbidden_port, mechanism=entry.get("mechanism"))
                if account != self_id:
                    raise ValueError("All registry providers must bind the same account.")
            else:
                raise ValueError("Provider kind must be demo or http_json; native modules are not loaded.")
            registrations.append(registration)
        initial = registry["default_provider"]
    elif demo:
        registrations.append(demo_registration(store, self_id))
        initial = "demo"
    elif profile is not None:
        account, registration = profile_registration(profile, forbidden_port=forbidden_port)
        if account != self_id:
            raise ValueError("Profile belongs to a different account.")
        registrations.append(registration)
        initial = "profile"
    return ProviderManager(self_id, registrations,
                           initial_provider_id=(initial_provider_id or initial) if activate else None)


def execution_payload(manager):
    return {"levels": level_catalog(), "state": manager.snapshot()}
