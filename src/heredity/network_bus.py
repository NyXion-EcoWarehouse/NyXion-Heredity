"""Bounded in-memory observations and dispatch for host network modules."""

from collections import deque
from dataclasses import dataclass, field
import threading
import time
from uuid import uuid4

from nyxion_heredity_sdk import ContractError
from nyxion_heredity_sdk.networks import (
    ModuleBinding, NetworkBindingClosed, NetworkMessage, NetworkMessageGap,
    validate_interface_id, validate_module_id,
)


@dataclass
class _Subscription:
    module_id: str
    interface_id: str
    generation: int
    condition: threading.Condition = field(default_factory=threading.Condition)
    messages: deque = field(default_factory=deque)
    closed: bool = False
    reason: str = "Network module binding is closed"
    gap: int = 0


class NetworkModuleBus:
    """The core module bus; it knows neither platform protocols nor their plugins.

    Network operations run outside the global subscription lock. The manager
    must capture its interface target before performing I/O, and must not
    re-route a failed operation to another interface or account.
    """

    def __init__(self, *, exists, dispatch, publish, capacity=256, max_bindings=1024):
        for operation in (exists, dispatch, publish):
            if not callable(operation):
                raise ValueError("Network bus host operations must be callable")
        if type(capacity) is not int or not 1 <= capacity <= 4096:
            raise ValueError("Network subscription capacity must be in 1..4096")
        if type(max_bindings) is not int or not 1 <= max_bindings <= 1024:
            raise ValueError("Network binding limit must be in 1..1024")
        self._exists, self._dispatch, self._publish = exists, dispatch, publish
        self.capacity, self.max_bindings = capacity, max_bindings
        self._lock = threading.RLock()
        self._subscriptions = {}
        self._generations = {}
        self._closed = False

    def bind(self, module_id, interface_id, *, dispatch=None, publish=None):
        validate_module_id(module_id)
        validate_interface_id(interface_id)
        if not self._exists(interface_id):
            raise NetworkBindingClosed("The configured network interface does not exist")
        with self._lock:
            if self._closed:
                raise NetworkBindingClosed("The network module bus is closed")
            if sum(len(items) for items in self._subscriptions.values()) >= self.max_bindings:
                raise ContractError("The host network subscription limit was reached")
            generation = self._generations.setdefault(interface_id, 1)
            state = _Subscription(module_id, interface_id, generation)
            self._subscriptions.setdefault(interface_id, []).append(state)
        return ModuleBinding(
            module_id, interface_id, generation,
            call=lambda action, params, timeout: self._call(
                state, action, params, timeout, dispatch=dispatch),
            publish_event=lambda event: self._event(state, event, publish=publish),
            receive=lambda timeout: self._receive(state, timeout),
            close=lambda: self._close_binding(state),
            is_closed=lambda: self._is_closed(state),
        )

    def _is_closed(self, state):
        with state.condition:
            return state.closed or bool(state.gap)

    def _require_binding(self, state):
        with self._lock:
            with state.condition:
                if state.gap:
                    raise NetworkMessageGap(state.interface_id, state.gap)
                if (self._closed or state.closed
                        or self._generations.get(state.interface_id, 1) != state.generation):
                    raise NetworkBindingClosed(state.reason)
        if not self._exists(state.interface_id):
            self._close_binding(state)
            raise NetworkBindingClosed("The configured network interface does not exist")

    def _call(self, state, action, params, timeout, *, dispatch=None):
        self._require_binding(state)
        return (self._dispatch if dispatch is None else dispatch)(
            state.interface_id, action, params, timeout)

    def _event(self, state, event, *, publish=None):
        self._require_binding(state)
        return (self._publish if publish is None else publish)(state.interface_id, event)

    def generation(self, interface_id):
        validate_interface_id(interface_id)
        with self._lock:
            return self._generations.get(interface_id, 1)

    def _receive(self, state, timeout):
        deadline = time.monotonic() + timeout
        with state.condition:
            while True:
                if state.gap:
                    raise NetworkMessageGap(state.interface_id, state.gap)
                if state.closed:
                    raise NetworkBindingClosed(state.reason)
                if state.messages:
                    return state.messages.popleft()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                state.condition.wait(remaining)

    def _close_binding(self, state, reason="Network module binding is closed"):
        with self._lock:
            subscriptions = self._subscriptions.get(state.interface_id, [])
            if state in subscriptions:
                subscriptions.remove(state)
            if not subscriptions:
                self._subscriptions.pop(state.interface_id, None)
            with state.condition:
                state.closed = True
                state.reason = reason
                state.messages.clear()
                state.condition.notify_all()

    def emit(self, interface_id, kind, payload):
        """Freeze one payload, then share its immutable observation to subscribers."""
        validate_interface_id(interface_id)
        message = NetworkMessage(uuid4().hex, interface_id, kind, payload, time.time())
        with self._lock:
            if self._closed:
                raise NetworkBindingClosed("The network module bus is closed")
            for state in tuple(self._subscriptions.get(interface_id, ())):
                with state.condition:
                    if state.closed or state.gap:
                        continue
                    if len(state.messages) >= self.capacity:
                        state.gap = len(state.messages) + 1
                        state.messages.clear()
                        # An overflowed subscription has no complete replay.
                        # Detach it; the retained handle reports the gap.
                        self._subscriptions[interface_id].remove(state)
                    else:
                        state.messages.append(message)
                    state.condition.notify_all()
            if not self._subscriptions.get(interface_id):
                self._subscriptions.pop(interface_id, None)
        return message

    def mark_gap(self, interface_id, dropped_messages=1):
        """Revoke existing replay windows after an observation cannot be kept."""
        validate_interface_id(interface_id)
        if type(dropped_messages) is not int or dropped_messages < 1:
            raise ValueError("A network observation gap must lose at least one message")
        with self._lock:
            states = tuple(self._subscriptions.pop(interface_id, ()))
            for state in states:
                with state.condition:
                    if state.closed or state.gap:
                        continue
                    state.gap = len(state.messages) + dropped_messages
                    state.messages.clear()
                    state.condition.notify_all()

    def bindings(self, interface_id):
        """Return labels and subscription counts, never handles or queue contents."""
        validate_interface_id(interface_id)
        with self._lock:
            counts = {}
            for state in self._subscriptions.get(interface_id, ()):
                if not state.closed and not state.gap:
                    counts[state.module_id] = counts.get(state.module_id, 0) + 1
            return [{"module_id": name, "subscriptions": count}
                    for name, count in sorted(counts.items())]

    def invalidate(self, interface_id, reason="Network interface generation was revoked"):
        """Revoke this runtime generation; keep its stable configuration ID distinct."""
        validate_interface_id(interface_id)
        if not isinstance(reason, str) or not 1 <= len(reason) <= 512:
            raise ValueError("Network revocation reason must be a bounded string")
        with self._lock:
            self._generations[interface_id] = self._generations.get(interface_id, 1) + 1
            states = tuple(self._subscriptions.get(interface_id, ()))
            for state in states:
                self._close_binding(state, reason)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            states = tuple(state for values in self._subscriptions.values() for state in values)
            for state in states:
                self._close_binding(state, "The network module bus is closed")
