"""Account-scoped actions, durable event ingestion and explicit session readiness."""

from copy import deepcopy
import hashlib
import json
import threading
import time

from .backends.base import BackendError
from .event_hub import EventHub
from .messages import _json_copy, cq_string, message_id, normalize_message, positive_id
from .storage import MessageStore
from .providers.contracts import ProviderDescriptor, level_catalog
from .providers.manager import ProviderManager
from .providers.placeholders import placeholder_descriptors

SEND_ACTIONS = frozenset({"send_private_msg", "send_group_msg", "send_msg"})


def _snapshot(value):
    return _json_copy(value, "event")


def normalize_event(event, self_id):
    event = _snapshot(event)
    if not isinstance(event, dict) or positive_id(event.get("self_id"), "self_id") != self_id:
        raise BackendError("Event account does not match this runtime.")
    timestamp = event.get("time")
    if type(timestamp) is not int or timestamp < 0:
        raise BackendError("Event time must be a nonnegative integer.")
    kind = event.get("post_type")
    if kind in {"message", "message_sent"}:
        message_type = event.get("message_type")
        if message_type not in {"private", "group"}:
            raise BackendError("Unsupported message_type.")
        event["message_id"] = message_id(event.get("message_id"))
        event["user_id"] = positive_id(event.get("user_id"), "user_id")
        if message_type == "group":
            event["group_id"] = positive_id(event.get("group_id"), "group_id")
        elif event["user_id"] == self_id:
            event["target_id"] = positive_id(event.get("target_id"), "target_id")
        event["message"] = normalize_message(event.get("message"))
        raw_message = event.get("raw_message")
        if raw_message is not None and not isinstance(raw_message, str):
            raise BackendError("raw_message must be a string.")
        event["raw_message"] = raw_message if raw_message is not None else cq_string(event["message"])
        sender = event.get("sender", {"user_id": event["user_id"]})
        if not isinstance(sender, dict):
            raise BackendError("sender must be an object.")
        sender_id = positive_id(sender.get("user_id", event["user_id"]), "sender.user_id")
        if sender_id != event["user_id"]:
            raise BackendError("Sender identity conflicts with event user_id.")
        event["sender"] = {**sender, "user_id": sender_id}
        subtype = event.get("sub_type", "normal" if message_type == "group" else "friend")
        if not isinstance(subtype, str) or not subtype:
            raise BackendError("sub_type must be a nonempty string.")
        event["sub_type"] = subtype
    elif kind == "notice":
        notice = event.get("notice_type")
        if notice not in {"friend_recall", "group_recall"}:
            raise BackendError("Unsupported notice type.", 1404)
        event["message_id"] = message_id(event.get("message_id"))
        event["user_id"] = positive_id(event.get("user_id"), "user_id")
        if notice == "group_recall":
            event["group_id"] = positive_id(event.get("group_id"), "group_id")
            event["operator_id"] = positive_id(event.get("operator_id"), "operator_id")
    elif kind == "request":
        request_type = event.get("request_type")
        if request_type not in {"friend", "group"}:
            raise BackendError("Unsupported request type.", 1404)
        event["user_id"] = positive_id(event.get("user_id"), "user_id")
        if request_type == "group":
            event["group_id"] = positive_id(event.get("group_id"), "group_id")
            if event.get("sub_type") not in {"add", "invite"}:
                raise BackendError("Group request sub_type must be add or invite.")
        if not isinstance(event.get("flag"), str) or not 1 <= len(event["flag"]) <= 4096:
            raise BackendError("A bounded request flag is required.")
        if not isinstance(event.get("comment", ""), str):
            raise BackendError("Request comment must be a string.")
    elif kind == "meta_event":
        if event.get("meta_event_type") == "lifecycle":
            if event.get("sub_type") not in {"enable", "disable"}:
                raise BackendError("Unsupported lifecycle subtype.")
        elif event.get("meta_event_type") == "heartbeat":
            status = event.get("status")
            if not isinstance(status, dict) or type(status.get("online")) is not bool:
                raise BackendError("Heartbeat requires a boolean online status.")
            if "good" in status and type(status["good"]) is not bool:
                raise BackendError("Heartbeat good status must be a boolean.")
            if ("receive_healthy" in status and status["receive_healthy"] is not None
                    and type(status["receive_healthy"]) is not bool):
                raise BackendError("Heartbeat receive_healthy must be boolean or null.")
        else:
            raise BackendError("Unsupported meta event type.", 1404)
    else:
        raise BackendError("Unsupported event post_type.", 1404)
    event["self_id"] = self_id
    return event


def _event_key(event):
    if event["post_type"] in {"message", "message_sent"}:
        identity = {key: event.get(key) for key in
                    ("post_type", "message_type", "message_id", "user_id", "group_id")}
    elif event["post_type"] == "request":
        identity = {key: event.get(key) for key in ("post_type", "request_type", "flag")}
    else:
        identity = event
    encoded = json.dumps(identity, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()
    return "event:" + hashlib.sha256(encoded).hexdigest()


class AccountRuntime:
    """One explicitly bound account. Network success is not identity verification."""

    def __init__(self, backend, store: MessageStore, *, mode="profile", event_capacity=256):
        self.backend = backend
        self.store = store
        self._legacy_mode = mode
        self._self_id = backend.self_id
        if type(self._self_id) is not int or not 0 <= self._self_id <= 2**63 - 1:
            raise ValueError("Runtime requires a nonnegative signed-64-bit self_id.")
        self.event_hub = EventHub(self._self_id, event_capacity) if self._self_id else None
        self._lock = threading.RLock()
        self._verified = False
        self._closed = False
        self._state = "unconfigured" if not self._self_id else "disconnected"
        self._receive_healthy = None
        self._provider_good = None
        self._switching = threading.Event()
        self._transition_lock = threading.Lock()

    @property
    def mode(self):
        if isinstance(self.backend, ProviderManager):
            return self.backend.source_kind
        return self._legacy_mode

    def _provider_context(self):
        if not isinstance(self.backend, ProviderManager):
            context = getattr(self.backend, "session_context", None)
            if callable(context):
                return context()
            return None
        snapshot = self.backend.snapshot()
        return {"provider_id": snapshot["active_provider_id"],
                "session_epoch": snapshot["session_epoch"]}

    def execution_info(self):
        describe = getattr(self.backend, "execution_info", None)
        if not isinstance(self.backend, ProviderManager) and callable(describe):
            return describe()
        if isinstance(self.backend, ProviderManager):
            state = self.backend.snapshot()
        else:
            source = self.mode if self.mode in {"demo", "profile", "unconfigured"} else "profile"
            descriptor = ProviderDescriptor(
                provider_id="legacy", label="Legacy account provider", execution_level=None,
                source_kind=source, host_kind="legacy_adapter", session_owner="external",
                target_memory_access="none", in_process_code=False,
                business_request_builder="mixed", primary_transport_owner="external",
                availability="ready" if self.self_id else "configuration_required")
            state = {"state": self._state, "active_provider_id": "legacy" if self.self_id else None,
                     "session_epoch": "legacy-unversioned",
                     "descriptor": descriptor.to_dict() if self.self_id else None, "in_flight": 0,
                     "providers": [descriptor.to_dict(),
                                   *(item.to_dict() for item in placeholder_descriptors())]}
        return {"levels": level_catalog(), "state": state,
                "switch_supported": isinstance(self.backend, ProviderManager)}

    @property
    def self_id(self):
        return self._self_id

    @property
    def online(self):
        with self._lock:
            return not self._closed and self._verified and bool(self.backend.online)

    def status(self):
        with self._lock:
            online = self.online
            context = self._provider_context() or {}
            descriptor = (self.backend.active_descriptor
                          if isinstance(self.backend, ProviderManager) else None)
            return {"online": online, "good": (online and self._receive_healthy is True
                                               and self._provider_good is not False),
                    "state": self._state, "identity_verified": self._verified,
                    "receive_healthy": self._receive_healthy, "mode": self.mode,
                    "self_id": self.self_id, "provider_good": self._provider_good,
                    "execution_level": (descriptor.execution_level.value
                                        if descriptor is not None
                                        and descriptor.execution_level is not None
                                        else None),
                    "switching": self._switching.is_set(), **context}

    def initialize(self):
        if not self.self_id:
            return False
        try:
            self.call("get_login_info", {})
            return True
        except BackendError:
            return False

    def _verify_login(self, result):
        try:
            observed = positive_id(result.get("user_id"), "user_id") if isinstance(result, dict) else None
        except BackendError:
            observed = None
        if observed != self.self_id:
            self._verified = False
            self._state = "identity_mismatch"
            raise BackendError("Backend login identity does not match configured account.", 1503)
        self._verified = True
        self._state = "ready"

    def _provider_call(self, action, params):
        try:
            return self.backend.call(action, params)
        except BackendError as exc:
            if exc.retcode >= 1500:
                self._verified = False
                self._receive_healthy = None
                self._provider_good = None
                self._state = "disconnected"
            raise
        except Exception:
            self._verified = False
            self._receive_healthy = None
            self._provider_good = None
            self._state = "disconnected"
            raise BackendError("Provider request failed; its outcome may be unknown.", 1502) from None

    def call(self, action, params):
        if self._switching.is_set():
            raise BackendError("Provider transition is in progress.", 1503)
        with self._lock:
            if self._switching.is_set():
                raise BackendError("Provider transition is in progress.", 1503)
            if self._closed:
                raise BackendError("Account runtime is closed.", 1503)
            if not isinstance(params, dict):
                raise BackendError("Action params must be an object.")
            params = deepcopy(params)
            if action == "get_runtime_info":
                return {**self.status(), "persistent_messages": True,
                        "event_ingress": self.event_hub is not None,
                        "execution": self.execution_info()}
            if action == "get_execution_info":
                return self.execution_info()
            if not self.self_id:
                raise BackendError("No account backend is configured.", 1503)
            if action == "get_local_history":
                kind = params.get("message_type")
                target = params.get("group_id" if kind == "group" else "user_id")
                return {"messages": self.store.history(
                    self.self_id, kind, target, limit=positive_id(params.get("limit", 20), "limit"),
                    before_message_id=params.get("before_message_id"))}
            if action in {"get_msg", "delete_msg"}:
                params["message_id"] = message_id(params.get("message_id"))
                mid = params["message_id"]
                if self.store.is_deleted(self.self_id, mid):
                    raise BackendError("Message has been recalled or deleted.", 1404)
                if action == "get_msg":
                    stored = self.store.get_message(self.self_id, mid)
                    if stored is not None:
                        return stored
                elif self.mode != "demo":
                    self._check_message_owner(mid)
            if action in SEND_ACTIONS:
                auto_escape = params.get("auto_escape", False)
                params["message"] = normalize_message(params.get("message"), auto_escape=auto_escape)
                raw_message = cq_string(params["message"])
                kind = ("group" if action == "send_group_msg" else
                        "private" if action == "send_private_msg" else params.get("message_type"))
                if kind is None:
                    if ("group_id" in params) == ("user_id" in params):
                        raise BackendError("send_msg requires an unambiguous target.")
                    kind = "group" if "group_id" in params else "private"
                if kind not in {"group", "private"}:
                    raise BackendError("message_type must be group or private.")
                target_key = "group_id" if kind == "group" else "user_id"
                params[target_key] = positive_id(params.get(target_key), target_key)
                # Validate the durable shape before an external send can occur.
                # Signed-ID placeholders use maximum-width representations.
                outbound_context = self._provider_context()
                record = _json_copy({
                    "time": 2**63 - 1, "message_id": -(2**31), "real_id": -(2**31),
                    "message_type": kind, target_key: params[target_key],
                    "sender": {"user_id": self.self_id}, "message": params["message"],
                    "raw_message": raw_message,
                    **({"_heredity": outbound_context}
                       if outbound_context is not None else {})}, "outbound message record")
            if action == "get_login_info":
                result = self._provider_call(action, params)
                self._verify_login(result)
                return result
            # Profiles can expose additional writes; unknown actions need the
            # same account check as standard sends. Local reads returned above.
            self._verify_login(self._provider_call("get_login_info", {}))
            if action == "delete_msg" and self.mode == "demo":
                if self.store.get_message(self.self_id, params["message_id"]) is None:
                    raise BackendError("Demo message not found.", 1404)
                self.store.mark_deleted(self.self_id, params["message_id"])
                return None
            try:
                result = self._provider_call(action, params)
            except BackendError as exc:
                details = exc.details if isinstance(exc.details, dict) else {}
                if action in SEND_ACTIONS and details.get("action_completed") is True:
                    try:
                        confirmed = message_id(details.get("message_id"))
                    except BackendError:
                        confirmed = None
                    if confirmed is not None:
                        self._persist_outbound(record, confirmed)
                raise
            if action in SEND_ACTIONS:
                try:
                    if not isinstance(result, dict):
                        raise BackendError("Missing send response.")
                    mid = message_id(result.get("message_id"))
                except BackendError:
                    self._verified = False
                    self._state = "disconnected"
                    raise BackendError(
                        "Send response has no valid message identity; do not resend automatically.",
                        1502, details={"action_outcome": "unknown", "retry_safe": False}) from None
                invalid_response = False
                try:
                    result = _json_copy(result, "send response")
                except BackendError:
                    invalid_response = True
                self._persist_outbound(record, mid)
                if invalid_response:
                    self._verified = False
                    self._state = "disconnected"
                    raise BackendError(
                        "Backend confirmed a message but returned invalid response data; do not resend.",
                        1502, details={"action_completed": True, "message_id": mid,
                                       "retry_safe": False})
            elif action == "delete_msg":
                try:
                    self.store.mark_deleted(self.self_id, params["message_id"])
                except BackendError:
                    raise BackendError(
                        "Backend recall completed but local persistence failed; do not retry automatically.",
                        1501, details={"action_completed": True,
                                       "message_id": params["message_id"]}) from None
            return result

    def _persist_outbound(self, record, mid):
        record.update(time=int(time.time()), message_id=mid, real_id=mid)
        context = self._provider_context()
        if context is not None:
            record["_heredity"] = context
        try:
            self._check_message_collision(mid)
            self.store.put_message(self.self_id, mid, record)
        except BackendError as exc:
            details = {"action_completed": True, "message_id": mid}
            if exc.retcode == 1409:
                details["persistence_error"] = "provider_identity_conflict"
            raise BackendError(
                "Backend action completed but local persistence failed; do not resend automatically.",
                1501, details=details) from None

    def _check_message_owner(self, identifier):
        context = self._provider_context()
        if context is None:
            return
        record = self.store.get_message(self.self_id, identifier, include_deleted=True)
        if record is None:
            return
        provenance = record.get("_heredity")
        owner = provenance.get("provider_id") if isinstance(provenance, dict) else None
        if owner != context["provider_id"]:
            raise BackendError("Message identity belongs to a different or unresolved provider.",
                               1409, details={"action_completed": False})

    def _check_message_collision(self, identifier):
        context = self._provider_context()
        if context is None:
            return
        record = self.store.get_message(self.self_id, identifier, include_deleted=True)
        if record is None:
            return
        provenance = record.get("_heredity")
        owner = provenance.get("provider_id") if isinstance(provenance, dict) else None
        if owner != context["provider_id"]:
            raise BackendError("Message identifier collides with a different provider.", 1409)

    def switch_provider(self, provider_id, expected_epoch, timeout=5):
        if not isinstance(self.backend, ProviderManager):
            raise BackendError("Provider switching is unavailable for this runtime.", 1503)
        if type(timeout) not in {int, float} or not 0 < timeout <= 60:
            raise BackendError("Transition timeout must be in (0,60].")
        if not self._transition_lock.acquire(blocking=False):
            raise BackendError("Another provider transition is already active.", 1504)
        locked = False
        self._switching.set()
        try:
            started = time.monotonic()
            locked = self._lock.acquire(timeout=timeout)
            if not locked:
                raise BackendError("Runtime actions did not drain before the transition deadline.", 1504)
            if self._closed:
                raise BackendError("Account runtime is closed.", 1503)
            current = self.backend.snapshot()
            target = next((item for item in current["providers"]
                           if item["provider_id"] == provider_id), None)
            active = current["descriptor"]
            if (active is not None and target is not None
                    and (active["source_kind"] == "demo") != (target["source_kind"] == "demo")
                    and target["selectable"]):
                raise BackendError("Demo and real providers require separate account runtimes.")
            old_epoch = self.backend.session_epoch
            try:
                state = self.backend.switch(
                    provider_id, expected_epoch, timeout=max(0.001, timeout - (time.monotonic() - started)))
            finally:
                if self.backend.session_epoch != old_epoch:
                    self._verified = self.backend.snapshot()["state"] == "ready"
                    self._state = "ready" if self._verified else "disconnected"
                    self._receive_healthy = None
                    self._provider_good = None
                    if self.event_hub is not None:
                        capacity = self.event_hub.capacity
                        self.event_hub.close()
                        self.event_hub = EventHub(self.self_id, capacity)
                        self.emit_meta({"meta_event_type": "lifecycle",
                                        "sub_type": "enable" if self._verified else "disable"})
            return state
        finally:
            if locked:
                self._lock.release()
            self._switching.clear()
            self._transition_lock.release()

    def ingest(self, event, event_key=None, session_epoch=None, *, strict_event_id=False):
        if self._switching.is_set():
            raise BackendError("Provider transition is in progress.", 1503)
        with self._lock:
            if self._switching.is_set():
                raise BackendError("Provider transition is in progress.", 1503)
            if self._closed or self.event_hub is None:
                raise BackendError("Account event input is unavailable.", 1503)
            context = self._provider_context()
            if context is not None:
                if context["provider_id"] is None:
                    raise BackendError("There is no active event provider.", 1503)
                if session_epoch != context["session_epoch"]:
                    raise BackendError("Event session epoch is stale or missing.", 1409,
                                       details={"action_completed": False})
            event = normalize_event(event, self.self_id)
            key = event_key if event_key is not None else _event_key(event)
            if context is not None:
                encoded = f"{context['provider_id']}:{key}".encode("utf-8")
                key = "provider:" + hashlib.sha256(encoded).hexdigest()
                event["_heredity"] = context
            record = None
            deleted = None
            suppressed = False
            if event["post_type"] in {"message", "message_sent"}:
                self._check_message_collision(event["message_id"])
                record = deepcopy(event)
                record["real_id"] = event["message_id"]
                if event["message_type"] == "private" and event["user_id"] == self.self_id:
                    record["user_id"] = event["target_id"]
                suppressed = self.store.is_deleted(self.self_id, event["message_id"])
            elif event["post_type"] == "notice":
                deleted = event["message_id"]
            sequence = self.store.commit_event(
                self.self_id, event, event_key=key, message_record=record,
                deleted_message_id=deleted, strict_duplicate=strict_event_id)
            if sequence is None:
                return {"duplicate": True, "persisted": True, "sequence": None}
            self._receive_healthy = True
            if event["post_type"] == "meta_event":
                source_status = (event["status"]
                                 if event.get("meta_event_type") == "heartbeat" else {})
                if "good" in source_status:
                    self._provider_good = source_status["good"]
                if "receive_healthy" in source_status:
                    self._receive_healthy = source_status["receive_healthy"]
                if (event.get("sub_type") == "disable"
                        or source_status.get("online") is False):
                    self._verified = False
                    self._receive_healthy = False
                    self._state = "disconnected"
            if not suppressed:
                self.event_hub.publish(event, sequence)
            return {"duplicate": False, "persisted": True, "sequence": sequence,
                    "suppressed": suppressed}

    def emit_meta(self, fields):
        with self._lock:
            if self._closed or self.event_hub is None:
                return
            event = {"time": int(time.time()), "self_id": self.self_id,
                     "post_type": "meta_event", **fields}
            context = self._provider_context()
            if context is not None:
                event["_heredity"] = context
            sequence = self.store.append_event(self.self_id, event)
            self.event_hub.publish(event, sequence)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._state = "closed"
            self._verified = False
            if self.event_hub is not None:
                self.event_hub.close()
            try:
                self.backend.close()
            finally:
                self.store.close()
