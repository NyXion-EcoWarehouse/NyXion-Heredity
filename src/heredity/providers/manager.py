"""One-account provider ownership with explicit, bounded session transitions."""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from ..backends.base import BackendError
from ..messages import _json_copy, message_id, positive_id
from .contracts import ProviderDescriptor


_DEFAULT_TIMEOUT = 5.0
_MAX_TIMEOUT = 60.0
_SEND_ACTIONS = frozenset({"send_private_msg", "send_group_msg", "send_msg"})


@dataclass(frozen=True)
class ProviderRegistration:
    descriptor: ProviderDescriptor
    factory: Callable[[], Any]

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, ProviderDescriptor) or not callable(self.factory):
            raise ValueError("Provider registration requires a descriptor and callable factory.")


@dataclass
class _Transition:
    kind: str
    epoch: str
    registration: ProviderRegistration | None = None
    cancelled: bool = False
    done: bool = False
    succeeded: bool = False
    error_code: int = 1503


def _timeout(value: Any) -> float:
    if type(value) not in (int, float):
        raise BackendError("Provider timeout must be a finite number in (0, 60].", 1400)
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise BackendError("Provider timeout must be a finite number in (0, 60].", 1400) from None
    if not math.isfinite(result) or not 0 < result <= _MAX_TIMEOUT:
        raise BackendError("Provider timeout must be a finite number in (0, 60].", 1400)
    return result


def _safe_details(value: Any) -> dict | None:
    """Retain only bounded confirmation fields, never provider payloads."""

    if type(value) is not dict:
        return None
    result = {}
    for key in ("action_completed", "retry_safe"):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    if type(value.get("action_outcome")) is str and value["action_outcome"] == "unknown":
        result["action_outcome"] = "unknown"
    try:
        if type(value.get("message_id")) in (int, str):
            result["message_id"] = message_id(value["message_id"])
    except BackendError:
        pass
    epoch = value.get("session_epoch")
    if (
        type(epoch) is str and 0 < len(epoch) <= 128
        and all(32 <= ord(character) <= 126 for character in epoch)
    ):
        result["session_epoch"] = epoch
    return result or None


def _invalid_send_details(result: Any) -> dict:
    try:
        identifier = message_id(result.get("message_id")) if type(result) is dict else None
    except BackendError:
        identifier = None
    if identifier is not None:
        return {"action_completed": True, "message_id": identifier, "retry_safe": False}
    return {"action_outcome": "unknown", "retry_safe": False}


class ProviderManager:
    """Keep one action owner; switching never retries a business action.

    Provider calls execute outside the condition lock. Lifecycle work runs in a
    daemon worker so its caller can return a timeout without closing resources
    beneath an in-flight action. A timed-out worker cannot activate a candidate.
    """

    def __init__(
        self,
        self_id: int,
        registrations: list[ProviderRegistration],
        initial_provider_id: str | None = None,
    ) -> None:
        if type(self_id) is not int or not 0 <= self_id <= 2**63 - 1:
            raise ValueError("Provider manager self_id must be a nonnegative signed-64-bit integer.")
        if self_id == 0 and initial_provider_id is not None:
            raise ValueError("An initial provider requires a configured positive account.")
        self._self_id = self_id
        self._registrations: dict[str, ProviderRegistration] = {}
        for registration in tuple(registrations):
            if not isinstance(registration, ProviderRegistration):
                raise ValueError("Provider registrations must use ProviderRegistration.")
            identifier = registration.descriptor.provider_id
            if identifier in self._registrations:
                raise ValueError("Provider identifiers must be unique.")
            self._registrations[identifier] = registration
        self._condition = threading.Condition(threading.RLock())
        self._epoch = str(uuid4())
        self._provider = None
        self._active_registration = None
        self._retained = None
        self._verified = False
        self._in_flight = 0
        self._transitioning = False
        self._task: _Transition | None = None
        self._closing_requested = False
        self._closed = False
        self._state = "unconfigured"
        if initial_provider_id is not None:
            with self._condition:
                registration = self._preflight(initial_provider_id)
                task = _Transition("initial", self._epoch, registration)
                self._state = "initializing"
                self._start_worker(task)
                self._wait_task(task, time.monotonic() + _DEFAULT_TIMEOUT)

    @property
    def self_id(self) -> int:
        return self._self_id

    @property
    def session_epoch(self) -> str:
        with self._condition:
            return self._epoch

    @property
    def active_descriptor(self) -> ProviderDescriptor | None:
        with self._condition:
            return (
                self._active_registration.descriptor
                if self._active_registration is not None else None
            )

    @property
    def source_kind(self) -> str:
        descriptor = self.active_descriptor
        return descriptor.source_kind if descriptor is not None else "unconfigured"

    @property
    def online(self) -> bool:
        with self._condition:
            if (
                self._closed or self._closing_requested or self._transitioning
                or not self._verified or self._provider is None
            ):
                return False
            try:
                return bool(self._provider.online)
            except Exception:
                return False

    def _snapshot(self) -> dict:
        descriptor = (
            self._active_registration.descriptor
            if self._active_registration is not None else None
        )
        return {
            "state": self._state,
            "active_provider_id": descriptor.provider_id if descriptor is not None else None,
            "session_epoch": self._epoch,
            "descriptor": descriptor.to_dict() if descriptor is not None else None,
            "providers": [
                registration.descriptor.to_dict() for registration in self._registrations.values()
            ],
            "in_flight": self._in_flight,
        }

    def snapshot(self) -> dict:
        with self._condition:
            return self._snapshot()

    def _preflight(self, provider_id: str) -> ProviderRegistration:
        if not isinstance(provider_id, str) or provider_id not in self._registrations:
            raise BackendError("Provider is not registered.", 1404)
        registration = self._registrations[provider_id]
        if not registration.descriptor.selectable:
            raise BackendError("Provider is unavailable and cannot be selected.", 1503)
        if "get_login_info" not in registration.descriptor.supported_actions:
            raise BackendError("Provider does not declare account verification support.", 1503)
        if not self.self_id:
            raise BackendError("A positive account must be configured before selecting a provider.", 1503)
        return registration

    def _check_epoch(self, expected_epoch: str) -> None:
        if not isinstance(expected_epoch, str) or expected_epoch != self._epoch:
            raise BackendError(
                "Provider session changed; refresh its state before switching.",
                1409,
                details={"session_epoch": self._epoch},
            )

    def _wait_condition(self, deadline: float) -> bool:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        self._condition.wait(min(remaining, threading.TIMEOUT_MAX))
        return True

    def _wait_task(self, task: _Transition, deadline: float) -> None:
        while not task.done:
            if not self._wait_condition(deadline):
                task.cancelled = True
                self._state = "closing" if self._closing_requested else "failed"
                raise BackendError("Provider transition timed out; no action was replayed.", 1504)
        if not task.succeeded:
            raise BackendError("Provider transition failed; no fallback was selected.", task.error_code)

    def _start_worker(self, task: _Transition) -> None:
        self._task = task
        self._transitioning = True
        worker = threading.Thread(
            target=self._run_transition, args=(task,),
            name="heredity-provider-lifecycle", daemon=True,
        )
        worker.start()

    def _still_wanted(self, task: _Transition) -> bool:
        with self._condition:
            return (
                self._task is task and self._epoch == task.epoch
                and not task.cancelled and not self._closing_requested
            )

    def _close_retained(self) -> bool:
        with self._condition:
            provider = self._retained
        if provider is None:
            return True
        try:
            provider.close()
        except BaseException:
            return False
        with self._condition:
            if self._retained is provider:
                self._retained = None
        return True

    def _run_transition(self, task: _Transition) -> None:
        try:
            if not self._close_retained():
                return
            if task.kind == "close":
                with self._condition:
                    self._closed = True
                    self._state = "closed"
                    task.succeeded = True
                return
            if not self._still_wanted(task):
                return
            try:
                candidate = task.registration.factory()
            except BaseException:
                return
            with self._condition:
                self._retained = candidate
            if (
                candidate is None
                or not callable(getattr(candidate, "call", None))
                or not callable(getattr(candidate, "close", None))
            ):
                self._close_retained()
                return
            binding = getattr(candidate, "self_id", None)
            if type(binding) is not int or binding != self.self_id:
                self._close_retained()
                return
            if not self._still_wanted(task):
                self._close_retained()
                return
            if task.kind != "initial":
                try:
                    login = candidate.call("get_login_info", {})
                    observed = (
                        positive_id(login.get("user_id"), "user_id")
                        if isinstance(login, dict) else None
                    )
                    verified = observed == self.self_id
                except BaseException:
                    verified = False
                if not verified:
                    self._close_retained()
                    return
            with self._condition:
                if (
                    self._task is task and self._epoch == task.epoch
                    and not task.cancelled and not self._closing_requested
                ):
                    self._provider = candidate
                    self._active_registration = task.registration
                    self._retained = None
                    self._verified = task.kind != "initial"
                    self._state = "ready" if self._verified else "disconnected"
                    task.succeeded = True
            if not task.succeeded:
                self._close_retained()
        except BaseException:
            self._close_retained()
        finally:
            with self._condition:
                task.done = True
                if self._task is task:
                    self._transitioning = False
                    if not task.succeeded:
                        self._state = (
                            "close_failed" if self._retained is not None
                            else "closing" if self._closing_requested else "failed"
                        )
                self._condition.notify_all()

    def call(self, action: str, params: dict):
        with self._condition:
            if (
                self._closed or self._closing_requested or self._transitioning
                or self._provider is None
            ):
                raise BackendError("Provider is not accepting actions.", 1503)
            if action not in self._active_registration.descriptor.supported_actions:
                raise BackendError("Action is not supported by the active provider.", 1404)
            if action != "get_login_info" and not self._verified:
                raise BackendError("Provider account has not been verified.", 1503)
            if self._in_flight:
                raise BackendError("Provider already has an in-flight action.", 1504)
            if type(params) is not dict:
                raise BackendError("Provider action params must be an object.", 1400)
            outgoing = _json_copy(params, "provider action params")
            provider, epoch = self._provider, self._epoch
            self._in_flight += 1
        succeeded = False
        login_valid = False
        error_code = 1502
        try:
            result = provider.call(action, outgoing)
            try:
                result = _json_copy(result, "provider action result")
            except BackendError:
                details = _invalid_send_details(result) if action in _SEND_ACTIONS else None
                raise BackendError(
                    "Provider returned an invalid result; do not repeat it automatically.",
                    1502,
                    details=details,
                ) from None
            if action == "get_login_info":
                try:
                    observed = (
                        positive_id(result.get("user_id"), "user_id")
                        if isinstance(result, dict) else None
                    )
                except BackendError:
                    observed = None
                if observed != self.self_id:
                    error_code = 1503
                    raise BackendError("Provider account does not match its binding.", 1503)
                login_valid = True
            succeeded = True
            return result
        except BackendError as error:
            if type(error.retcode) is int and 1000 <= error.retcode <= 1999:
                error_code = error.retcode
            raise BackendError(
                "Provider action failed; do not repeat it automatically.",
                error_code,
                details=_safe_details(error.details),
            ) from None
        except Exception:
            raise BackendError(
                "Provider action failed; its outcome may be unknown.",
                1502,
                details=(
                    {"action_outcome": "unknown", "retry_safe": False}
                    if action != "get_login_info" else None
                ),
            ) from None
        finally:
            with self._condition:
                self._in_flight -= 1
                if self._provider is provider and self._epoch == epoch:
                    if action == "get_login_info":
                        self._verified = succeeded and login_valid
                    elif not succeeded and error_code >= 1500:
                        self._verified = False
                    if not self._transitioning and not self._closing_requested:
                        self._state = "ready" if self._verified else "disconnected"
                self._condition.notify_all()

    def switch(
        self, provider_id: str, expected_epoch: str, timeout: float = _DEFAULT_TIMEOUT
    ) -> dict:
        deadline = time.monotonic() + _timeout(timeout)
        with self._condition:
            self._check_epoch(expected_epoch)
            registration = self._preflight(provider_id)
            if self._closed or self._closing_requested:
                raise BackendError("Provider manager is closing or closed.", 1503)
            while self._transitioning:
                if not self._wait_condition(deadline):
                    raise BackendError("Another provider transition is still running.", 1504)
                self._check_epoch(expected_epoch)
                if self._closed or self._closing_requested:
                    raise BackendError("Provider manager is closing or closed.", 1503)
            if (
                self._active_registration is registration
                and self._verified and self._state == "ready"
            ):
                return {**self._snapshot(), "changed": False}
            self._transitioning = True
            self._state = "draining"
            while self._in_flight:
                if not self._wait_condition(deadline):
                    self._transitioning = False
                    self._state = "ready" if self._verified else "disconnected"
                    self._condition.notify_all()
                    raise BackendError("Provider actions did not drain before the deadline.", 1504)
                if self._closing_requested:
                    self._transitioning = False
                    self._condition.notify_all()
                    raise BackendError("Provider manager is closing.", 1503)
            if self._provider is not None:
                self._retained = self._provider
            self._provider = None
            self._active_registration = None
            self._verified = False
            self._epoch = str(uuid4())
            self._state = "switching"
            task = _Transition("switch", self._epoch, registration)
            self._start_worker(task)
            self._wait_task(task, deadline)
            return {**self._snapshot(), "changed": True}

    def close(self, timeout: float = _DEFAULT_TIMEOUT) -> None:
        deadline = time.monotonic() + _timeout(timeout)
        with self._condition:
            if self._closed:
                return
            self._closing_requested = True
            self._state = "closing"
            if self._task is not None and not self._task.done:
                self._task.cancelled = True
            while self._transitioning:
                if not self._wait_condition(deadline):
                    raise BackendError("Provider closure is waiting for lifecycle work.", 1504)
                if self._closed:
                    return
            self._transitioning = True
            self._state = "closing"
            while self._in_flight:
                if not self._wait_condition(deadline):
                    self._transitioning = False
                    self._condition.notify_all()
                    raise BackendError("Provider closure is waiting for an in-flight action.", 1504)
            if self._provider is not None:
                self._retained = self._provider
                self._provider = None
                self._active_registration = None
                self._verified = False
                self._epoch = str(uuid4())
            task = _Transition("close", self._epoch)
            self._start_worker(task)
            self._wait_task(task, deadline)
