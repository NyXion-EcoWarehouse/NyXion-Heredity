"""Explicit, host-managed allocation of existing interfaces to scoped modules."""

from collections import deque
from dataclasses import dataclass, field
import math
import threading
import time
from uuid import uuid4
from weakref import WeakValueDictionary

from nyxion_heredity_sdk import (
    ContractError, DeadlineExceeded, ModuleBinding, NetworkAllocationDenied,
    NetworkBindingClosed, NetworkInterfaceGrant, NetworkInterfaceRequirement,
)
from nyxion_heredity_sdk.networks import validate_interface_id, validate_module_id

from .network_policies import NetworkRestartPolicies


class NetworkAllocationError(NetworkAllocationDenied):
    """A management error with a stable HTTP projection."""

    def __init__(self, message, *, code="INVALID_NETWORK_ALLOCATION", status=409):
        super().__init__(message)
        self.code, self.status = code, status


@dataclass
class _InterfaceGrant:
    grant: NetworkInterfaceGrant
    revision: int
    bindings: list = field(default_factory=list, repr=False)


@dataclass
class _Request:
    request_id: str
    requirement: NetworkInterfaceRequirement
    created_at: float
    updated_at: float
    state: str = "pending"
    interfaces: dict = field(default_factory=dict, repr=False)
    error: str | None = None
    superseded_by: str | None = None
    restoring: bool = False
    restore_event: object = field(default=None, repr=False)
    operation: int = 0


class NetworkInterfaceAllocator:
    """A module requests capabilities; a host operator chooses the interface.

    Declarations never open sockets, select a port, expose credentials, or pick a
    fallback interface. Grants and subscriptions are in-memory session resources.
    """

    def __init__(self, manager, *, max_requests=1024, grant_ttl=None,
                 state_dir=None, restore_timeout=15.0):
        if type(max_requests) is not int or not 1 <= max_requests <= 4096:
            raise ValueError("Network allocation request limit must be in 1..4096.")
        if grant_ttl is not None and (
                type(grant_ttl) not in (int, float) or not math.isfinite(grant_ttl)
                or not 0 < grant_ttl <= 31 * 86400):
            raise ValueError("Network grant lifetime must be finite and at most 31 days.")
        if (type(restore_timeout) not in (int, float) or not math.isfinite(restore_timeout)
                or not 0 < restore_timeout <= 120):
            raise ValueError("Network restore timeout must be finite and in (0, 120].")
        self.manager = manager
        self.max_requests, self.grant_ttl = max_requests, grant_ttl
        self._condition = threading.Condition(threading.RLock())
        self._requests = {}
        self._scopes = WeakValueDictionary()
        self._policies = NetworkRestartPolicies(
            state_dir if state_dir is not None else getattr(manager, "state_dir", None),
            limit=min(max_requests, 1024))
        self.restore_timeout = float(restore_timeout)
        self._restore_queue = deque()
        self._restore_worker = None
        self._closed = False
        self._expiry_worker = None
        if grant_ttl is not None:
            self._expiry_worker = threading.Thread(
                target=self._expire_grants, name="nyxion-network-grant-expiry", daemon=True)
            self._expiry_worker.start()

    def _expire_grants(self):
        with self._condition:
            while not self._closed:
                for allocation in self._requests.values():
                    self._refresh(allocation)
                expires = min(
                    (entry.grant.expires_at for allocation in self._requests.values()
                     for entry in allocation.interfaces.values()
                     if entry.grant.expires_at is not None), default=None)
                self._condition.wait(
                    None if expires is None else max(0.001, expires - time.time()))

    def _require_open(self):
        if self._closed:
            raise NetworkBindingClosed("The host network allocator is closed.")

    def _request(self, request_id):
        if not isinstance(request_id, str) or len(request_id) > 128:
            raise ContractError("Network allocation request ID is invalid.")
        try:
            return self._requests[request_id]
        except KeyError:
            raise NetworkAllocationError(
                "Network allocation request was not found.",
                code="NETWORK_ALLOCATION_NOT_FOUND", status=404) from None

    def _snapshot(self, allocation):
        grants = [entry.grant.to_dict() for entry in allocation.interfaces.values()]
        policy = self._policies.get(allocation.requirement)
        active = self.is_module_active(allocation.requirement.module_id)
        return {
            "request_id": allocation.request_id,
            "module_id": allocation.requirement.module_id,
            "requirement": allocation.requirement.to_dict(),
            "state": allocation.state,
            "created_at": allocation.created_at,
            "updated_at": allocation.updated_at,
            "grant": grants[0] if grants else None,
            "grants": grants,
            "error": allocation.error,
            "auto_start": policy.auto_start,
            "saved_interface_ids": list(policy.interface_ids),
            "module_active": active,
            "can_reapply": active,
            "superseded_by": allocation.superseded_by,
            "restoring": allocation.restoring,
        }

    def is_module_active(self, module_id):
        validate_module_id(module_id)
        with self._condition:
            scope = self._scopes.get(module_id)
            return not self._closed and scope is not None and scope._active

    def _cancel_restore(self, allocation):
        if allocation.restore_event is not None:
            allocation.restore_event.set()
        allocation.restore_event, allocation.restoring = None, False
        allocation.operation += 1
        self._restore_queue = deque(
            (identifier, event) for identifier, event in self._restore_queue
            if identifier != allocation.request_id)

    def _restore_current(self, allocation, event):
        return (not self._closed and not event.is_set()
                and allocation.restore_event is event
                and allocation.superseded_by is None
                and self.is_module_active(allocation.requirement.module_id))

    def _schedule_restore(self, allocation):
        policy = self._policies.get(allocation.requirement)
        if (not policy.auto_start or not policy.interface_ids
                or not self.is_module_active(allocation.requirement.module_id)
                or allocation.state in {"released", "denied"}
                or allocation.superseded_by is not None):
            return
        if allocation.restoring and allocation.restore_event is not None:
            return
        if len(self._restore_queue) >= min(self.max_requests, 1024):
            allocation.error = "Automatic interface restore queue is full; request again explicitly."
            return
        self._cancel_restore(allocation)
        event = threading.Event()
        allocation.restore_event, allocation.restoring = event, True
        self._restore_queue.append((allocation.request_id, event))
        if self._restore_worker is None:
            self._restore_worker = threading.Thread(
                target=self._run_restores, name="nyxion-network-restore", daemon=True)
            self._restore_worker.start()
        self._condition.notify_all()

    def _run_restores(self):
        while True:
            with self._condition:
                while not self._closed and not self._restore_queue:
                    self._condition.wait()
                if self._closed:
                    return
                request_id, event = self._restore_queue.popleft()
                allocation = self._requests.get(request_id)
                if allocation is None or not self._restore_current(allocation, event):
                    continue
                identifiers = self._policies.get(allocation.requirement).interface_ids
            deadline = time.monotonic() + self.restore_timeout
            try:
                self._assign_many(request_id, identifiers, restore_event=event)
                for identifier in identifiers:
                    with self._condition:
                        if not self._restore_current(allocation, event):
                            break
                        if time.monotonic() >= deadline:
                            raise DeadlineExceeded("Automatic interface startup exceeded its budget.")
                        entry = allocation.interfaces.get(identifier)
                        if entry is None:
                            raise NetworkBindingClosed("An automatic interface grant was revoked.")
                        expected = entry.revision, entry.grant.generation
                    current = self.manager.allocation_state(identifier)
                    if (current["revision"], current["generation"]) != expected:
                        raise NetworkBindingClosed("An automatic interface selection changed.")
                    result = self.manager.start(identifier)
                    if result["state"] != "running":
                        raise NetworkBindingClosed("An automatic interface could not start.")
                with self._condition:
                    if self._restore_current(allocation, event):
                        allocation.restoring, allocation.restore_event = False, None
                        allocation.error, allocation.updated_at = None, time.time()
                        self._condition.notify_all()
            except Exception:
                with self._condition:
                    if self._restore_current(allocation, event):
                        for identifier in tuple(allocation.interfaces):
                            self._drop(allocation, identifier)
                        self._cancel_restore(allocation)
                        allocation.state = "pending"
                        allocation.error = (
                            "Automatic interface restoration failed; review the saved interfaces "
                            "and request again. No replacement interface was selected.")
                        allocation.updated_at = time.time()
                        self._condition.notify_all()

    def _drop(self, allocation, interface_id):
        entry = allocation.interfaces.pop(interface_id, None)
        if entry is None:
            return
        self.manager.remove_allocation_owner(interface_id, entry.grant.grant_id)
        for binding in tuple(entry.bindings):
            binding.close()
        entry.bindings.clear()

    def _revoke(self, allocation, state, reason):
        self._cancel_restore(allocation)
        allocation.state, allocation.error = state, reason
        allocation.updated_at = time.time()
        for identifier in tuple(allocation.interfaces):
            self._drop(allocation, identifier)
        self._condition.notify_all()

    def _refresh(self, allocation, interface_id=None):
        if allocation.state != "granted":
            return
        identifiers = tuple(allocation.interfaces) if interface_id is None else (interface_id,)
        for identifier in identifiers:
            entry = allocation.interfaces.get(identifier)
            if entry is None:
                continue
            grant = entry.grant
            if grant.expired():
                state, reason = "expired", "An allocated interface grant has expired."
            else:
                try:
                    current = self.manager.allocation_state(identifier)
                except (ValueError, NetworkBindingClosed):
                    current = None
                if (current is not None and current["revision"] == entry.revision
                        and current["generation"] == grant.generation
                        and current["type"] == grant.transport_type
                        and current["account"] == grant.account):
                    continue
                state, reason = "revoked", "An allocated interface configuration was revoked."
            self._drop(allocation, identifier)
            allocation.state = "granted" if allocation.interfaces else state
            allocation.error, allocation.updated_at = reason, time.time()
            self._condition.notify_all()

    def declare_interface(self, requirement):
        if not isinstance(requirement, NetworkInterfaceRequirement):
            raise ContractError("An interface declaration must use NetworkInterfaceRequirement.")
        with self._condition:
            self._require_open()
            current = self._current_request(requirement)
            if current is not None and current.requirement == requirement:
                self._refresh(current)
                if current.state in {"pending", "granted", "denied"}:
                    return self._snapshot(current)
            if len(self._requests) >= self.max_requests:
                # Only terminal, unbound records may leave the bounded session log.
                removable = next((key for key, value in self._requests.items()
                                  if value.state in {"released", "revoked", "expired"}
                                  and not value.interfaces), None)
                if removable is None:
                    raise NetworkAllocationDenied("The host network allocation limit was reached.")
                self._requests.pop(removable)
            now = time.time()
            allocation = _Request(uuid4().hex, requirement, now, now)
            if current is not None:
                self._revoke(current, "released", "A newer declaration replaced this request.")
                current.superseded_by = allocation.request_id
            self._requests[allocation.request_id] = allocation
            self._schedule_restore(allocation)
            self._condition.notify_all()
            return self._snapshot(allocation)

    def _current_request(self, requirement):
        key = self._policies.key(requirement)
        return next((value for value in reversed(tuple(self._requests.values()))
                     if value.superseded_by is None
                     and self._policies.key(value.requirement) == key), None)

    def set_auto_start(self, request_id, enabled):
        if type(enabled) is not bool:
            raise ContractError("Automatic interface startup must be boolean.")
        with self._condition:
            self._require_open()
            allocation = self._request(request_id)
            if allocation.superseded_by is not None:
                allocation = self._current_request(allocation.requirement)
            self._policies.set(allocation.requirement, auto_start=enabled)
            if enabled:
                self._schedule_restore(allocation)
            else:
                self._cancel_restore(allocation)
            allocation.updated_at = time.time()
            self._condition.notify_all()
            return self._snapshot(allocation)

    def reapply(self, request_id):
        with self._condition:
            self._require_open()
            previous = self._request(request_id)
            if not self.is_module_active(previous.requirement.module_id):
                raise NetworkAllocationError(
                    "Start the module before requesting its interfaces again.",
                    code="MODULE_INACTIVE")
            current = self._current_request(previous.requirement)
            if current is not None and current.state in {"pending", "granted"}:
                self._schedule_restore(current)
                return self._snapshot(current)
            requirement = current.requirement if current is not None else previous.requirement
            if current is not None and current.state == "denied":
                self._revoke(current, "released", "The active module requested a new declaration.")
            return self.declare_interface(requirement)

    def request_interface(self, requirement, *, timeout=30.0):
        return self.request_interfaces(requirement, timeout=timeout)[0]

    def request_interfaces(self, requirement, *, timeout=30.0):
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= 120):
            raise ContractError("Network allocation timeout must be finite and in (0, 120].")
        deadline = time.monotonic() + timeout
        declared = self.declare_interface(requirement)
        with self._condition:
            allocation = self._request(declared["request_id"])
            while True:
                self._require_open()
                self._refresh(allocation)
                if allocation.state == "granted":
                    return tuple(entry.grant for entry in allocation.interfaces.values())
                if allocation.state != "pending":
                    raise NetworkAllocationDenied(
                        allocation.error or "The host denied the requested network interface.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeadlineExceeded(
                        "Network interface allocation is pending host configuration.")
                self._condition.wait(remaining)

    def catalog(self, *, include_history=False):
        if type(include_history) is not bool:
            raise ContractError("Network request history selection must be boolean.")
        with self._condition:
            self._require_open()
            for allocation in self._requests.values():
                self._refresh(allocation)
            return {"requests": [self._snapshot(value) for value in self._requests.values()
                                 if include_history or value.superseded_by is None]}

    def assign(self, request_id, interface_id):
        return self.assign_many(request_id, [interface_id])

    @staticmethod
    def _check_selection(requirement, current):
        if current["type"] not in requirement.transport_types:
            raise NetworkAllocationError("The selected interface transport does not match the declaration.")
        if (requirement.account is not None and current["account"] is not None
                and current["account"] != requirement.account):
            raise NetworkAllocationError("The selected interface belongs to a different account.")

    def assign_many(self, request_id, interface_ids):
        return self._assign_many(request_id, interface_ids)

    def _assign_many(self, request_id, interface_ids, *, restore_event=None):
        if type(interface_ids) not in (tuple, list) or len(interface_ids) > 64:
            raise ContractError("Selected interfaces must be a bounded list of interface IDs.")
        identifiers = tuple(validate_interface_id(value) for value in interface_ids)
        if len(identifiers) != len(set(identifiers)):
            raise ContractError("Selected interface IDs must not contain duplicates.")
        with self._condition:
            self._require_open()
            allocation = self._request(request_id)
            self._refresh(allocation)
            if restore_event is None:
                self._cancel_restore(allocation)
            elif not self._restore_current(allocation, restore_event):
                raise NetworkBindingClosed("Automatic interface restoration was cancelled.")
            if allocation.state == "released":
                raise NetworkAllocationError(
                    "A released module request cannot be reassigned; declare a new request.",
                    code="NETWORK_ALLOCATION_RELEASED")
            requirement = allocation.requirement
            if len(identifiers) > requirement.max_interfaces:
                raise NetworkAllocationError("The declaration interface count limit was exceeded.")
            # Reject every incompatible selection before claiming any account or
            # closing an existing subscription. Grants change only at commit.
            states = {identifier: self.manager.allocation_state(identifier)
                      for identifier in identifiers}
            for current in states.values():
                self._check_selection(requirement, current)
            operation = allocation.operation
        # Account claims and transport lifetimes may perform I/O. A module stop
        # must be able to revoke its scope while this preparation is running.
        claimed = []
        try:
            for identifier, current in states.items():
                if requirement.account is not None and current["account"] is None:
                    self.manager.update(
                        identifier, {"account": requirement.account},
                        expected_revision=current["revision"],
                        expected_generation=current["generation"])
                    claimed.append(identifier)
                    states[identifier] = self.manager.allocation_state(identifier)
            for identifier, expected in states.items():
                current = self.manager.allocation_state(identifier)
                self._check_selection(requirement, current)
                if (current["revision"], current["generation"]) != (
                        expected["revision"], expected["generation"]):
                    raise NetworkBindingClosed("A selected interface changed before allocation.")
        except (OSError, ValueError, NetworkBindingClosed):
            if claimed:
                raise NetworkAllocationError(
                    "Allocation was not changed; some account claims were saved. "
                    "Review the selected interfaces before assigning again.",
                    code="NETWORK_ALLOCATION_CLAIM_INCOMPLETE") from None
            raise
        with self._condition:
            self._require_open()
            if (allocation.operation != operation or allocation.state == "released"
                    or (restore_event is not None
                        and not self._restore_current(allocation, restore_event))):
                raise NetworkBindingClosed("The interface assignment was cancelled or replaced.")
            expires = time.time() + self.grant_ttl if self.grant_ttl is not None else None
            staged = {}
            added = []
            for identifier in identifiers:
                current = states[identifier]
                entry = allocation.interfaces.get(identifier)
                if (entry is None or current["revision"] != entry.revision
                        or current["generation"] != entry.grant.generation
                        or current["type"] != entry.grant.transport_type
                        or current["account"] != entry.grant.account):
                    grant = NetworkInterfaceGrant(
                        allocation.request_id, requirement.module_id, identifier,
                        current["generation"], current["type"], current["account"], expires,
                        uuid4().hex)
                    entry = _InterfaceGrant(grant, current["revision"])
                staged[identifier] = entry
            try:
                for identifier, entry in staged.items():
                    if allocation.interfaces.get(identifier) is not entry:
                        self.manager.add_allocation_owner(
                            identifier, entry.grant.grant_id, entry.revision,
                            entry.grant.generation)
                        added.append((identifier, entry.grant.grant_id))
                if restore_event is None:
                    self._policies.set(requirement, interface_ids=identifiers)
            except (OSError, ValueError, NetworkBindingClosed):
                for identifier, owner in added:
                    self.manager.remove_allocation_owner(identifier, owner)
                raise
            for identifier in tuple(allocation.interfaces):
                if allocation.interfaces[identifier] is not staged.get(identifier):
                    self._drop(allocation, identifier)
            # Ordering is explicit while unchanged grants retain their identity.
            allocation.interfaces = staged
            allocation.state = "granted" if allocation.interfaces else "pending"
            allocation.error = None
            allocation.updated_at = time.time()
            allocation.operation += 1
            self._condition.notify_all()
            return self._snapshot(allocation)

    def unassign(self, request_id, interface_id=None):
        if interface_id is not None:
            validate_interface_id(interface_id)
        with self._condition:
            self._require_open()
            allocation = self._request(request_id)
            self._refresh(allocation)
            if allocation.state == "released":
                raise NetworkAllocationError(
                    "A released module request cannot be reassigned; declare a new request.",
                    code="NETWORK_ALLOCATION_RELEASED")
            self._cancel_restore(allocation)
            policy = self._policies.get(allocation.requirement)
            retained = (() if interface_id is None else
                        tuple(value for value in policy.interface_ids if value != interface_id))
            self._policies.set(allocation.requirement, interface_ids=retained)
            identifiers = tuple(allocation.interfaces) if interface_id is None else (interface_id,)
            for identifier in identifiers:
                self._drop(allocation, identifier)
            allocation.state = "granted" if allocation.interfaces else "pending"
            allocation.error, allocation.updated_at = None, time.time()
            self._condition.notify_all()
            return self._snapshot(allocation)

    def deny(self, request_id):
        with self._condition:
            self._require_open()
            allocation = self._request(request_id)
            if allocation.state == "released":
                raise NetworkAllocationError(
                    "A released module request cannot be changed; declare a new request.",
                    code="NETWORK_ALLOCATION_RELEASED")
            self._policies.set(allocation.requirement, interface_ids=())
            self._revoke(allocation, "denied", "The host denied this interface declaration.")
            return self._snapshot(allocation)

    def _require_grant(self, module_id, grant):
        self._require_open()
        if not isinstance(grant, NetworkInterfaceGrant):
            raise ContractError("An interface binding must use a host-issued grant.")
        validate_module_id(module_id)
        if module_id != grant.module_id:
            raise NetworkAllocationDenied("The interface grant belongs to a different module.")
        allocation = self._request(grant.request_id)
        self._refresh(allocation, grant.interface_id)
        entry = allocation.interfaces.get(grant.interface_id)
        if (allocation.requirement.module_id != module_id
                or allocation.state != "granted" or entry is None or entry.grant != grant):
            raise NetworkBindingClosed("The interface grant is no longer valid.")
        return allocation, entry

    def bind_grant(self, module_id, grant):
        with self._condition:
            allocation, entry = self._require_grant(module_id, grant)
            entry.bindings[:] = [item for item in entry.bindings if not item.closed]
            self._require_grant(module_id, grant)
            if len(entry.bindings) >= allocation.requirement.max_instances:
                raise NetworkAllocationDenied("The interface grant subscription limit was reached.")
            underlying = self.manager.bind_allocated_module(
                module_id, grant.interface_id, entry.revision, grant.generation)

            def require(direction):
                with self._condition:
                    current, _ = self._require_grant(module_id, grant)
                    if direction not in current.requirement.directions:
                        raise NetworkAllocationDenied(
                            "This network operation was not included in the granted declaration.")

            def call(action, params, timeout):
                require("call")
                return underlying.call(action, params, timeout=timeout)

            def publish(event):
                require("publish")
                return underlying.publish_event(event)

            def receive(timeout):
                require("receive")
                if grant.expires_at is not None:
                    timeout = min(timeout, max(0, grant.expires_at - time.time()))
                result = underlying.receive(timeout)
                require("receive")
                return result

            def closed():
                with self._condition:
                    self._refresh(allocation, grant.interface_id)
                    return (self._closed or allocation.state != "granted"
                            or allocation.interfaces.get(grant.interface_id) is not entry
                            or underlying.closed)

            binding = ModuleBinding(
                module_id, grant.interface_id, grant.generation,
                call=call, publish_event=publish, receive=receive,
                close=underlying.close, is_closed=closed)
            entry.bindings.append(binding)
            return binding

    def release_binding(self, request_id, interface_id):
        with self._condition:
            if self._closed:
                return
            self.unassign(request_id, interface_id)

    def release_interface(self, request_id):
        with self._condition:
            if self._closed:
                return
            allocation = self._request(request_id)
            if allocation.state != "released":
                self._revoke(allocation, "released", "The module released its interface allocation.")

    def release_module(self, module_id):
        validate_module_id(module_id)
        with self._condition:
            if self._closed:
                return
            scope = self._scopes.pop(module_id, None)
            if scope is not None:
                scope._active = False
            for allocation in self._requests.values():
                if allocation.requirement.module_id == module_id and allocation.state != "released":
                    self._revoke(allocation, "released", "The host released this module allocation.")

    def for_module(self, module_id):
        validate_module_id(module_id)
        with self._condition:
            self._require_open()
            scope = self._scopes.get(module_id)
            if scope is None:
                if len(self._scopes) >= self.max_requests:
                    raise NetworkAllocationDenied("The host scoped module limit was reached.")
                scope = _ScopedInterfaces(self, module_id)
                self._scopes[module_id] = scope
            return scope

    def close(self):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            for scope in self._scopes.values():
                scope._active = False
            self._scopes.clear()
            for allocation in self._requests.values():
                self._revoke(allocation, "revoked", "The host network allocator is closed.")
            self._condition.notify_all()
        if self._expiry_worker is not None and self._expiry_worker is not threading.current_thread():
            self._expiry_worker.join(timeout=2)
            if self._expiry_worker.is_alive():
                raise RuntimeError("The network grant expiration worker did not stop.")
        if self._restore_worker is not None and self._restore_worker is not threading.current_thread():
            self._restore_worker.join(timeout=self.restore_timeout + 5)
            if self._restore_worker.is_alive():
                raise RuntimeError("The automatic interface restoration worker did not stop.")


class _ScopedInterfaces:
    """A trusted module receives declarations and its own grants, not a manager."""

    __slots__ = ("_allocator", "_module_id", "_active", "__weakref__")

    def __init__(self, allocator, module_id):
        self._allocator, self._module_id = allocator, module_id
        self._active = True

    @property
    def module_id(self):
        return self._module_id

    def _requirement(self, requirement):
        if not self._active:
            raise NetworkBindingClosed("The module network service was revoked.")
        if (not isinstance(requirement, NetworkInterfaceRequirement)
                or requirement.module_id != self._module_id):
            raise NetworkAllocationDenied("A scoped module cannot declare for a different module.")
        return requirement

    def declare_interface(self, requirement):
        with self._allocator._condition:
            return self._allocator.declare_interface(self._requirement(requirement))

    def request_interface(self, requirement, *, timeout=30.0):
        with self._allocator._condition:
            return self._allocator.request_interface(self._requirement(requirement), timeout=timeout)

    def request_interfaces(self, requirement, *, timeout=30.0):
        with self._allocator._condition:
            return self._allocator.request_interfaces(self._requirement(requirement), timeout=timeout)

    def reapply_interface(self, request_id):
        with self._allocator._condition:
            self._own_request(request_id)
            return self._allocator.reapply(request_id)

    def bind_grant(self, module_id, grant):
        with self._allocator._condition:
            if not self._active:
                raise NetworkBindingClosed("The module network service was revoked.")
            if module_id != self._module_id:
                raise NetworkAllocationDenied("A scoped module cannot bind for a different module.")
            return self._allocator.bind_grant(module_id, grant)

    def _own_request(self, request_id):
        if not self._active:
            raise NetworkBindingClosed("The module network service was revoked.")
        allocation = self._allocator._request(request_id)
        if allocation.requirement.module_id != self._module_id:
            raise NetworkAllocationDenied("A scoped module cannot release another module allocation.")

    def release_binding(self, request_id, interface_id):
        with self._allocator._condition:
            if not self._active and self._allocator._closed:
                return
            if self._released_own_request(request_id):
                return
            self._own_request(request_id)
            self._allocator.release_binding(request_id, interface_id)

    def release_interface(self, request_id):
        with self._allocator._condition:
            if not self._active and self._allocator._closed:
                return
            if self._released_own_request(request_id):
                return
            self._own_request(request_id)
            self._allocator.release_interface(request_id)

    def _released_own_request(self, request_id):
        if self._active:
            return False
        allocation = self._allocator._request(request_id)
        return (allocation.requirement.module_id == self._module_id
                and allocation.state in {"released", "revoked", "expired"})
