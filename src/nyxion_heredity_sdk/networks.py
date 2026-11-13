"""Public handles for host-owned network interfaces, independent of plugins."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import math
import re
import threading
import time
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from .contracts import freeze_json
from .errors import ContractError, DeadlineExceeded


_INTERFACE_ID = re.compile(r"[0-9a-f]{16}\Z")
_MODULE_ID = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
_MESSAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_ACTION = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_TRANSPORT = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ACCOUNT = re.compile(r"[a-z][a-z0-9_.-]{0,127}:[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
NETWORK_MESSAGE_KINDS = frozenset({"action", "event", "response", "state"})
NETWORK_TRANSPORT_TYPES = frozenset({
    "http_server", "http_client", "websocket_server", "websocket_client",
})
NETWORK_DIRECTIONS = frozenset({"call", "publish", "receive"})


class NetworkBindingClosed(RuntimeError):
    """The module subscription or interface generation has been revoked."""


class NetworkMessageGap(NetworkBindingClosed):
    """A bounded subscription overflowed; delivery can no longer be complete."""

    def __init__(self, interface_id: str, dropped_messages: int):
        self.interface_id = validate_interface_id(interface_id)
        self.dropped_messages = dropped_messages
        super().__init__(
            f"Network subscription overflowed; {dropped_messages} messages were not delivered. "
            "Bind a new subscription explicitly."
        )


class NetworkAllocationDenied(ContractError):
    """The host refused a module's declared interface capability request."""

    def __init__(self, message="The host denied the requested network interface capability."):
        super().__init__(message)


def _bounded_names(value, name, pattern, *, limit=8, required=True):
    if type(value) not in (tuple, list) or len(value) > limit:
        raise ContractError(f"{name} must be a bounded sequence")
    if required and not value:
        raise ContractError(f"{name} must contain at least one value")
    if any(type(item) is not str or not pattern.fullmatch(item) for item in value):
        raise ContractError(f"{name} contains an invalid identifier")
    if len(set(value)) != len(value):
        raise ContractError(f"{name} contains duplicate values")
    return tuple(value)


def validate_interface_id(value: str) -> str:
    if not isinstance(value, str) or not _INTERFACE_ID.fullmatch(value):
        raise ContractError("Network interface ID must be exactly 16 lowercase hexadecimal characters")
    return value


def validate_module_id(value: str) -> str:
    if (not isinstance(value, str) or len(value) > 128
            or not _MODULE_ID.fullmatch(value)):
        raise ContractError("Network module ID must be a bounded lowercase namespaced identifier")
    return value


def _timeout(value, *, allow_zero=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or value > 120 or value < 0 or (not allow_zero and value == 0)):
        raise ContractError("Network operation timeout must be finite and in "
                            + ("[0, 120]" if allow_zero else "(0, 120]"))
    return float(value)


def _object(value, name):
    if type(value) not in (dict, MappingProxyType):
        raise ContractError(f"{name} must be a JSON object")
    return freeze_json(value)


@dataclass(frozen=True)
class NetworkInterfaceRequirement:
    """A capability request, without endpoint settings or credentials.

    The host decides whether a matching configured interface may be allocated;
    this declaration never creates a listener or discloses a token.
    ``max_instances`` bounds simultaneous subscriptions for each granted interface.
    ``max_interfaces`` separately bounds the interfaces in one allocation.
    """

    module_id: str
    transport_types: tuple[str, ...]
    directions: tuple[str, ...] = ("call", "receive")
    account: str | None = None
    purpose: str = ""
    max_instances: int = 1
    max_interfaces: int = 16

    def __post_init__(self):
        validate_module_id(self.module_id)
        transports = _bounded_names(
            self.transport_types, "transport_types", _TRANSPORT)
        if any(value not in NETWORK_TRANSPORT_TYPES for value in transports):
            raise ContractError("transport_types contains an unsupported host interface type")
        directions = _bounded_names(
            self.directions, "directions", re.compile(r"(?:call|publish|receive)\Z"))
        if self.account is not None and (
                not isinstance(self.account, str) or not _ACCOUNT.fullmatch(self.account)):
            raise ContractError("Network requirement account must be platform:self_id")
        if (not isinstance(self.purpose, str) or not 0 <= len(self.purpose) <= 256
                or any(ord(char) < 32 or ord(char) == 127 for char in self.purpose)):
            raise ContractError("Network requirement purpose is invalid")
        try:
            self.purpose.encode("utf-8")
        except UnicodeError:
            raise ContractError("Network requirement purpose is not valid Unicode") from None
        if type(self.max_instances) is not int or not 1 <= self.max_instances <= 64:
            raise ContractError("Network requirement max_instances must be in 1..64")
        if type(self.max_interfaces) is not int or not 1 <= self.max_interfaces <= 64:
            raise ContractError("Network requirement max_interfaces must be in 1..64")
        object.__setattr__(self, "transport_types", transports)
        object.__setattr__(self, "directions", directions)

    def to_dict(self):
        return {
            "module_id": self.module_id,
            "transport_types": list(self.transport_types),
            "directions": list(self.directions),
            "account": self.account,
            "purpose": self.purpose,
            "max_instances": self.max_instances,
            "max_interfaces": self.max_interfaces,
        }


@dataclass(frozen=True)
class NetworkInterfaceGrant:
    """A host-issued allocation; it is still not an access token."""

    request_id: str
    module_id: str
    interface_id: str
    generation: int
    transport_type: str
    account: str | None = None
    expires_at: float | None = None
    grant_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.request_id, str) or not _MESSAGE_ID.fullmatch(self.request_id):
            raise ContractError("Network allocation request_id is invalid")
        validate_module_id(self.module_id)
        validate_interface_id(self.interface_id)
        if type(self.generation) is not int or not 1 <= self.generation <= 2**63 - 1:
            raise ContractError("Network grant generation is invalid")
        if not isinstance(self.transport_type, str) or not _TRANSPORT.fullmatch(self.transport_type):
            raise ContractError("Network grant transport_type is invalid")
        if self.transport_type not in NETWORK_TRANSPORT_TYPES:
            raise ContractError("Network grant transport_type is unsupported")
        if self.account is not None and (
                not isinstance(self.account, str) or not _ACCOUNT.fullmatch(self.account)):
            raise ContractError("Network grant account must be platform:self_id")
        if self.expires_at is not None and (
                type(self.expires_at) not in (int, float)
                or not math.isfinite(self.expires_at) or self.expires_at <= 0):
            raise ContractError("Network grant expires_at is invalid")
        if self.grant_id is not None and (
                not isinstance(self.grant_id, str) or not _MESSAGE_ID.fullmatch(self.grant_id)):
            raise ContractError("Network grant grant_id is invalid")

    def expired(self, now=None):
        return (self.expires_at is not None
                and (time.time() if now is None else now) >= self.expires_at)

    def to_dict(self):
        return {
            "request_id": self.request_id,
            "module_id": self.module_id,
            "interface_id": self.interface_id,
            "generation": self.generation,
            "transport_type": self.transport_type,
            "account": self.account,
            "expires_at": self.expires_at,
            "grant_id": self.grant_id,
        }


@dataclass(frozen=True)
class NetworkMessage:
    """One immutable observation from exactly one configured host interface.

    ``timestamp`` is Unix time in seconds. This object does not acknowledge a
    platform event, confirm a message send, or change OneBot payload semantics.
    """

    id: str
    interface_id: str
    kind: str
    payload: Mapping = field(repr=False)
    timestamp: float

    def __post_init__(self):
        if not isinstance(self.id, str) or not _MESSAGE_ID.fullmatch(self.id):
            raise ContractError("Network message ID must be a bounded identifier")
        validate_interface_id(self.interface_id)
        if not isinstance(self.kind, str) or self.kind not in NETWORK_MESSAGE_KINDS:
            raise ContractError("Network message kind is unsupported")
        if (type(self.timestamp) not in (int, float)
                or not math.isfinite(self.timestamp) or self.timestamp < 0):
            raise ContractError("Network message timestamp must be finite Unix seconds")
        object.__setattr__(self, "payload", _object(self.payload, "Network message payload"))


class ModuleBinding:
    """A scoped handle supplied by the host, without its registry or database.

    The host owns interface configuration, transport lifetimes and permissions.
    Interface IDs identify resources; they are not access tokens. No operation
    here retries a business action or registers a platform plugin.
    """

    __slots__ = (
        "_module_id", "_interface_id", "_generation", "_call", "_publish",
        "_receive", "_close", "_is_closed", "_lock", "_closed",
    )

    def __init__(
        self, module_id: str, interface_id: str, generation: int, *,
        call: Callable[[str, Mapping, float], Any],
        publish_event: Callable[[Mapping], Any],
        receive: Callable[[float], NetworkMessage | None],
        close: Callable[[], None],
        is_closed: Callable[[], bool] | None = None,
    ):
        self._module_id = validate_module_id(module_id)
        self._interface_id = validate_interface_id(interface_id)
        if type(generation) is not int or not 1 <= generation <= 2**63 - 1:
            raise ContractError("Network interface generation must be a positive integer")
        for callback in (call, publish_event, receive, close):
            if not callable(callback):
                raise ContractError("Module binding host operations must be callable")
        if is_closed is not None and not callable(is_closed):
            raise ContractError("Module binding state probe must be callable")
        self._generation = generation
        self._call, self._publish = call, publish_event
        self._receive, self._close = receive, close
        self._is_closed = is_closed
        self._lock = threading.Lock()
        self._closed = False

    @property
    def module_id(self):
        return self._module_id

    @property
    def interface_id(self):
        return self._interface_id

    @property
    def generation(self):
        return self._generation

    @property
    def closed(self):
        with self._lock:
            closed = self._closed
        return closed or (self._is_closed is not None and self._is_closed())

    def _require_open(self):
        with self._lock:
            if self._closed:
                raise NetworkBindingClosed("Network module binding is closed")

    def call(self, action: str, params: Mapping, *, timeout: float = 30.0):
        """Call this interface once; return an immutable JSON result.

        The host determines whether an interface dispatches to a local account
        or a configured remote endpoint. The timeout is cooperative in embedded
        mode and cannot cancel or replay an already-started network action.
        """
        self._require_open()
        if not isinstance(action, str) or not _ACTION.fullmatch(action):
            raise ContractError("Network action name is invalid")
        budget = _timeout(timeout)
        deadline = time.monotonic() + budget
        params = _object(params, "Network action params")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeadlineExceeded("Network action expired before dispatch; it was not sent")
        return freeze_json(self._call(action, params, remaining))

    def publish_event(self, event: Mapping):
        """Offer one event to this interface's configured outbound transport."""
        self._require_open()
        return freeze_json(self._publish(_object(event, "Network event")))

    def receive(self, timeout: float = 0) -> NetworkMessage | None:
        """Consume this subscription only; timeout zero is a nonblocking poll."""
        self._require_open()
        result = self._receive(_timeout(timeout, allow_zero=True))
        if result is not None and (
                not isinstance(result, NetworkMessage)
                or result.interface_id != self.interface_id):
            raise ContractError("Host returned a message for a different network interface")
        return result

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._close()

    def __enter__(self):
        self._require_open()
        return self

    def __exit__(self, *error):
        self.close()


@runtime_checkable
class NetworkInterfaces(Protocol):
    """Host service a module can depend on without importing host internals."""

    def bind_module(self, module_id: str, interface_id: str) -> ModuleBinding: ...


@runtime_checkable
class NetworkInterfaceAllocator(Protocol):
    """Optional host service for capability declaration and explicit grants."""

    def request_interface(
        self, requirement: NetworkInterfaceRequirement, *, timeout: float = 30.0
    ) -> NetworkInterfaceGrant: ...

    def bind_grant(
        self, module_id: str, grant: NetworkInterfaceGrant
    ) -> ModuleBinding: ...

    def release_interface(self, request_id: str) -> None: ...


@runtime_checkable
class NetworkInterfaceDeclarations(NetworkInterfaceAllocator, Protocol):
    """A nonblocking declaration service exposed to a scoped host module."""

    def declare_interface(self, requirement: NetworkInterfaceRequirement) -> Mapping: ...


@runtime_checkable
class NetworkInterfaceMultiAllocator(NetworkInterfaceDeclarations, Protocol):
    """Optional SDK 1.2 service for independent grants under one declaration."""

    def request_interfaces(
        self, requirement: NetworkInterfaceRequirement, *, timeout: float = 30.0
    ) -> tuple[NetworkInterfaceGrant, ...]:
        """Return an immutable snapshot; it never broadcasts a business action."""
        ...

    def release_binding(self, request_id: str, interface_id: str) -> None:
        """Revoke only this request's grant for one interface, without a retry."""
        ...


@runtime_checkable
class NetworkInterfaceLifecycle(NetworkInterfaceMultiAllocator, Protocol):
    """SDK 1.3 service for explicitly requesting a fresh current allocation."""

    def reapply_interface(self, request_id: str) -> Mapping:
        """Reapply from an active module; old request IDs and grants stay revoked."""
        ...


__all__ = [
    "ModuleBinding", "NetworkBindingClosed", "NetworkInterfaces", "NetworkMessage",
    "NetworkMessageGap", "NetworkAllocationDenied", "NetworkInterfaceAllocator",
    "NetworkInterfaceDeclarations", "NetworkInterfaceMultiAllocator",
    "NetworkInterfaceLifecycle",
    "NetworkInterfaceGrant", "NetworkInterfaceRequirement",
    "NETWORK_MESSAGE_KINDS",
    "NETWORK_TRANSPORT_TYPES", "NETWORK_DIRECTIONS", "validate_interface_id",
    "validate_module_id",
]
