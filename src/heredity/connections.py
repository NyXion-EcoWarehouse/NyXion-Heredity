"""Explicit loopback OneBot connections owned by the local management service.

The external service keeps its platform session. Heredity supplies its own authenticated
gateway, account runtime and durable store; it does not load a platform integration or
configure an external event producer. Credentials live only in this process.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from http.client import HTTPException
import ipaddress
import json
import math
from pathlib import Path
import re
import secrets
import threading
import time
from urllib import error, parse, request

from .backends.base import BackendError
from .messages import _json_copy
from .onebot import _decode_json, create_server
from .providers.contracts import ExecutionLevel
from .providers.manager import ProviderManager, ProviderRegistration
from .providers.registry import placeholder_registrations, profile_registration
from .runtime import AccountRuntime
from .storage import MessageStore


MAX_RESPONSE_BYTES = 1024 * 1024
STANDARD_ACTIONS = (
    "get_login_info",
    "get_friend_list",
    "get_group_list",
    "get_msg",
    "send_private_msg",
    "send_group_msg",
    "send_msg",
    "delete_msg",
    "can_send_image",
    "can_send_record",
)
_WRITES = frozenset({"send_private_msg", "send_group_msg", "send_msg", "delete_msg"})
_ACCOUNT = re.compile(r"[0-9]{1,19}\Z")
class ConnectionManagementError(Exception):
    def __init__(self, message, status=400, code="INVALID_CONNECTION"):
        super().__init__(message)
        self.status = status
        self.code = code


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _origin(value):
    try:
        if not isinstance(value, str) or not value or len(value) > 2048:
            raise ValueError
        if any(ord(character) <= 32 or ord(character) >= 127 for character in value):
            raise ValueError
        parsed = parse.urlsplit(value)
        hostname = parsed.hostname
        if not hostname:
            raise ValueError
        if hostname.lower() == "localhost":
            hostname = "localhost"
        else:
            address = ipaddress.ip_address(hostname)
            if not address.is_loopback or "%" in hostname:
                raise ValueError
            hostname = address.compressed
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.port is None
            or not 1 <= parsed.port <= 65535
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "?" in value
            or "#" in value
        ):
            raise ValueError
        rendered_host = f"[{hostname}]" if ":" in hostname else hostname
        return f"{parsed.scheme}://{rendered_host}:{parsed.port}"
    except (ValueError, TypeError):
        raise ConnectionManagementError(
            "An explicit loopback HTTP(S) origin with a port and no path or credentials is required."
        ) from None


def _account(value):
    if isinstance(value, str) and _ACCOUNT.fullmatch(value):
        value = int(value)
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise BackendError("External service returned an invalid account identity.", 1502)
    return value


def _is_demo(status):
    for field in ("mode", "source_kind", "source"):
        if isinstance(status.get(field), str) and status[field].lower() == "demo":
            return True
    for field in ("runtime", "descriptor"):
        nested = status.get(field)
        if isinstance(nested, dict) and _is_demo(nested):
            return True
    execution = status.get("execution")
    if isinstance(execution, dict):
        state = execution.get("state")
        if isinstance(state, dict):
            descriptor = state.get("descriptor")
            if isinstance(descriptor, dict) and _is_demo(descriptor):
                return True
    return False


class _ExternalClient:
    """Strict OneBot envelopes, finite responses, no redirects or retries."""

    def __init__(self, origin, token, timeout):
        self.origin = origin
        self._token = token
        self._timeout = timeout
        self._opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
        self._lock = threading.RLock()
        self._closed = False
        self._blocked = False
        self._online = False
        self._verified = False
        self._nickname = ""
        self._self_id = None
        self._checked_at = None

    def _invalidate(self):
        self._online = False
        self._verified = False

    def action(self, action, params):
        with self._lock:
            if self._closed:
                raise BackendError("External connection is closed.", 1503)
            if self._blocked:
                raise BackendError("External source changed; reconnect explicitly.", 1503)
            if action not in STANDARD_ACTIONS and action != "get_status":
                raise BackendError("External action is not configured.", 1404)
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            if self._token:
                headers["Authorization"] = "Bearer " + self._token
            try:
                outgoing = json.dumps(
                    params, ensure_ascii=True, allow_nan=False, separators=(",", ":")
                ).encode("utf-8")
                if len(outgoing) > MAX_RESPONSE_BYTES:
                    raise ValueError
                query = request.Request(
                    self.origin + "/" + action, data=outgoing, headers=headers, method="POST"
                )
                with self._opener.open(query, timeout=self._timeout) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError
                envelope = _decode_json(raw.decode("utf-8"))
                envelope = _json_copy(envelope, "external OneBot response")
                if (
                    type(envelope) is not dict
                    or envelope.get("status") not in {"ok", "failed"}
                    or type(envelope.get("retcode")) is not int
                    or "data" not in envelope
                ):
                    raise ValueError
                if envelope["status"] != "ok" or envelope["retcode"] != 0:
                    code = envelope["retcode"]
                    if not 1000 <= code <= 1999:
                        code = 1502
                    raise BackendError("External OneBot action was rejected.", code)
                result = envelope["data"]
                if action == "get_login_info":
                    if not isinstance(result, dict):
                        raise ValueError
                    result = {**result, "user_id": _account(result.get("user_id"))}
                    if self._self_id is not None and result["user_id"] != self._self_id:
                        self._blocked = True
                        raise BackendError("External account changed; reconnect explicitly.", 1503)
                return result
            except BackendError as exc:
                if exc.retcode >= 1500:
                    self._invalidate()
                raise
            except error.HTTPError as exc:
                exc.close()
                self._invalidate()
                raise BackendError(
                    "External OneBot request was rejected; redirects are not followed.",
                    1502,
                    details={"action_outcome": "unknown", "retry_safe": False}
                    if action in _WRITES else None,
                ) from None
            except (error.URLError, HTTPException, OSError, ValueError, TypeError,
                    UnicodeError, RecursionError):
                self._invalidate()
                raise BackendError(
                    "External OneBot request failed or returned invalid data.",
                    1502,
                    details={"action_outcome": "unknown", "retry_safe": False}
                    if action in _WRITES else None,
                ) from None

    def verify(self, expected_account=None):
        with self._lock:
            self._checked_at = time.monotonic()
            try:
                identity = self.action("get_login_info", {})
                status = self.action("get_status", {})
                account = _account(identity.get("user_id"))
                if (
                    not isinstance(status, dict)
                    or type(status.get("online")) is not bool
                    or ("good" in status and type(status["good"]) is not bool)
                ):
                    raise BackendError("External service returned invalid online status.", 1502)
                if expected_account is not None and account != expected_account:
                    self._blocked = True
                    raise BackendError("External account changed; repeat the identity probe.", 1503)
                if "self_id" in status and _account(status["self_id"]) != account:
                    self._blocked = True
                    raise BackendError("External status conflicts with its account identity.", 1503)
                if _is_demo(status):
                    self._blocked = True
                    raise BackendError("A demonstration source cannot be connected as a real account.", 1503)
                if status["online"] is not True:
                    raise BackendError("External account is offline.", 1503)
                nickname = identity.get("nickname", "")
                if not isinstance(nickname, str) or len(nickname) > 256:
                    raise BackendError("External service returned an invalid nickname.", 1502)
                if self._token:
                    nickname = nickname.replace(self._token, "[redacted]")
                self._self_id = account
                self._nickname = nickname
                self._online = True
                self._verified = True
                return {"self_id": account, "nickname": nickname, "online": True}
            except BackendError:
                self._invalidate()
                raise

    def state(self):
        with self._lock:
            return {
                "online": self._online and not self._closed,
                "verified": self._verified and not self._closed,
                "nickname": self._nickname,
                "checked_at": self._checked_at,
            }

    def close(self):
        with self._lock:
            self._closed = True
            self._invalidate()
            self._token = ""


class _ExternalProvider:
    def __init__(self, client, self_id):
        self._client = client
        self.self_id = self_id

    @property
    def online(self):
        return self._client.state()["online"]

    def call(self, action, params):
        if action not in STANDARD_ACTIONS:
            raise BackendError("External action is not configured.", 1404)
        if action in _WRITES:
            try:
                self._client.verify(self.self_id)
            except BackendError as exc:
                raise BackendError(
                    "External account is not ready for a write.", exc.retcode,
                    details={"action_completed": False},
                ) from None
        return self._client.action(action, params)

    def close(self):
        self._client.close()


class ConnectionHandle:
    """Own the bridge only; closing never stops or modifies the external client."""

    def __init__(self, client, runtime, store, server, provider_id, execution_level):
        from .connection_events import EventReceiver

        self.origin = client.origin
        self.self_id = runtime.self_id
        self.provider_id = provider_id
        self.execution_level = execution_level
        self.gateway_origin = f"http://127.0.0.1:{server.server_port}"
        self.gateway_token = server.token
        self.execution_token = server.execution_token
        self._client = client
        self._runtime = runtime
        self._store = store
        self._server = server
        self._receiver = EventReceiver(runtime, runtime.backend.session_epoch)
        self._lock = threading.RLock()
        self._closed = False
        self._thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.1},
            name="heredity-connection-gateway", daemon=True,
        )

    def _start(self):
        self._receiver.start()
        self._server.start_events()
        self._thread.start()

    def _snapshot(self, refresh_after):
        with self._lock:
            state = self._client.state()
            checked = state["checked_at"]
            if not self._closed and (
                checked is None or time.monotonic() - checked >= refresh_after
            ):
                try:
                    self._client.verify(self.self_id)
                except BackendError:
                    pass
                state = self._client.state()
            return {
                "kind": "external",
                "origin": self.origin,
                "gateway_origin": self.gateway_origin,
                "self_id": self.self_id,
                "nickname": state["nickname"],
                "online": bool(state["online"] and self._runtime.online),
                "verified": bool(state["verified"]),
                "execution_level": self.execution_level,
                "level_provenance": "declared" if self.execution_level is not None else "none",
                "provider_id": self.provider_id,
                "event_ingress": self._receiver.snapshot(),
            }

    def _event_config(self):
        with self._lock:
            if self._closed:
                raise ConnectionManagementError("Connection event input is closed.", 503, "CONNECTION_CLOSED")
            try:
                return self._receiver.config()
            except BackendError as exc:
                if exc.retcode == 1409:
                    raise ConnectionManagementError(
                        "Event input belongs to an expired provider session; reconnect explicitly.",
                        409, "EVENT_SESSION_STALE",
                    ) from None
                raise ConnectionManagementError(
                    "Connection event input is unavailable.", 503, "CONNECTION_UNAVAILABLE"
                ) from None

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._server._closing.set()
            try:
                self._receiver.close()
                if self._thread.is_alive():
                    self._server.shutdown()
                self._server.server_close()
            finally:
                try:
                    with self._server.backend_lock:
                        self._runtime.close()
                finally:
                    self._client.close()
                    self._store.close()
                    if self._thread.is_alive() and threading.current_thread() is not self._thread:
                        self._thread.join(timeout=2)
                    self.gateway_token = ""
                    self.execution_token = ""


@dataclass
class _Probe:
    client: _ExternalClient
    self_id: int
    nickname: str
    execution_level: str | None
    deadline: float
    expires_at: float


class ConnectionManager:
    def __init__(
        self, state_dir, default_gateway_origin, forbidden_origins=(), *,
        probe_ttl=60.0, request_timeout=3.0, status_ttl=2.0,
    ):
        for name, value, maximum in (
            ("probe_ttl", probe_ttl, 60),
            ("request_timeout", request_timeout, 3),
            ("status_ttl", status_ttl, 60),
        ):
            if (type(value) not in {int, float} or not math.isfinite(value)
                    or value < (0 if name == "status_ttl" else 0.001) or value > maximum):
                raise ValueError(f"{name} is outside its supported range.")
        self._state_dir = Path(state_dir)
        self._probe_ttl = float(probe_ttl)
        self._timeout = float(request_timeout)
        self._status_ttl = float(status_ttl)
        self._lock = threading.RLock()
        self._forbidden_ports = {5700, 5099, 5178}
        self._probes = {}
        self._active = None
        self._closed = False
        self._error = None
        if default_gateway_origin:
            self.add_forbidden_origin(default_gateway_origin)
        for origin in forbidden_origins:
            self.add_forbidden_origin(origin)

    def add_forbidden_origin(self, origin):
        origin = _origin(origin)
        with self._lock:
            self._forbidden_ports.add(parse.urlsplit(origin).port)

    def _check_open(self):
        if self._closed:
            raise ConnectionManagementError("Connection management is closed.", 503, "CONNECTION_CLOSED")

    def _check_origin(self, value):
        origin = _origin(value)
        if parse.urlsplit(origin).port in self._forbidden_ports:
            raise ConnectionManagementError(
                "Heredity gateway and management ports cannot be used as external sources.",
                400, "CONNECTION_LOOP",
            )
        return origin

    def _prune(self):
        now = time.monotonic()
        for identifier, probe in tuple(self._probes.items()):
            if probe.deadline <= now:
                self._probes.pop(identifier).client.close()
        while len(self._probes) >= 32:
            identifier = next(iter(self._probes))
            self._probes.pop(identifier).client.close()

    def probe(self, payload):
        with self._lock:
            self._check_open()
            if not isinstance(payload, dict):
                raise ConnectionManagementError("Connection settings must be an object.")
            if "access_token" in payload and "token" in payload:
                raise ConnectionManagementError("Supply access_token only once.")
            origin = self._check_origin(payload.get("origin"))
            token = payload.get("access_token", payload.get("token", ""))
            if (not isinstance(token, str) or len(token) > 4096
                    or any(not 33 <= ord(character) <= 126 for character in token)):
                raise ConnectionManagementError("Access token must be a bounded visible ASCII string.")
            supplied_level = payload.get("execution_level")
            try:
                execution_level = (
                    ExecutionLevel(supplied_level).value if supplied_level is not None else None
                )
            except (TypeError, ValueError):
                raise ConnectionManagementError("Unknown execution level.") from None
            self._prune()
            client = _ExternalClient(origin, token, self._timeout)
            try:
                identity = client.verify()
            except BackendError:
                client.close()
                self._error = "External identity or online status could not be verified."
                raise ConnectionManagementError(self._error, 502, "PROBE_FAILED") from None
            probe_id = secrets.token_urlsafe(32)
            expires_at = time.time() + self._probe_ttl
            self._probes[probe_id] = _Probe(
                client, identity["self_id"], identity["nickname"], execution_level,
                time.monotonic() + self._probe_ttl, expires_at,
            )
            self._error = None
            return {
                "probe_id": probe_id, "origin": origin, **identity,
                "execution_level": execution_level, "expires_at": expires_at,
            }

    def _create_handle(self, probe):
        store = manager = runtime = server = handle = None
        try:
            store = MessageStore(
                self._state_dir / "connections" / f"{probe.self_id}.sqlite3"
            )
            provider_id = "external-" + hashlib.sha256(
                probe.client.origin.encode("ascii")
            ).hexdigest()[:20]
            profile = {
                "self_id": probe.self_id,
                "base_url": probe.client.origin,
                "actions": {
                    action: {"method": "POST", "path": "/" + action, "response_path": ["data"]}
                    for action in STANDARD_ACTIONS
                },
            }
            _, registration = profile_registration(
                profile, provider_id=provider_id, label="External OneBot HTTP",
                level=probe.execution_level,
            )
            registration = ProviderRegistration(
                registration.descriptor,
                lambda: _ExternalProvider(probe.client, probe.self_id),
            )
            manager = ProviderManager(
                probe.self_id, [*placeholder_registrations(probe.self_id), registration],
                initial_provider_id=provider_id,
            )
            runtime = AccountRuntime(manager, store)
            if not runtime.initialize() or not runtime.online:
                raise ConnectionManagementError(
                    "External account verification failed during setup.", 502, "CONNECT_FAILED"
                )
            server = create_server(
                "127.0.0.1", 0, secrets.token_urlsafe(32), runtime,
                self_id=probe.self_id, execution_token=secrets.token_urlsafe(32),
                enable_sse=True,
            )
            handle = ConnectionHandle(
                probe.client, runtime, store, server, provider_id, probe.execution_level
            )
            handle._start()
            return handle
        except BaseException:
            if handle is not None:
                handle.close()
            else:
                try:
                    if server is not None:
                        server.server_close()
                    if runtime is not None:
                        runtime.close()
                    elif manager is not None:
                        manager.close()
                finally:
                    if store is not None:
                        store.close()
                    probe.client.close()
            raise

    def connect(self, probe_id):
        with self._lock:
            self._check_open()
            self._prune()
            if not isinstance(probe_id, str) or probe_id not in self._probes:
                raise ConnectionManagementError(
                    "Identity probe is missing, expired or already used.", 409, "PROBE_EXPIRED"
                )
            probe = self._probes.pop(probe_id)
            try:
                self._check_origin(probe.client.origin)
                probe.client.verify(probe.self_id)
                candidate = self._create_handle(probe)
            except Exception:
                probe.client.close()
                self._error = "External connection setup failed; the previous connection was retained."
                raise ConnectionManagementError(self._error, 502, "CONNECT_FAILED") from None
            previous = self._active
            self._active = candidate
            self._forbidden_ports.add(parse.urlsplit(candidate.gateway_origin).port)
            self._forbidden_ports.add(parse.urlsplit(candidate._event_config()["url"]).port)
            self._error = None
            if previous is not None:
                previous.close()
            return candidate

    def snapshot(self):
        with self._lock:
            active = self._active._snapshot(self._status_ttl) if self._active is not None else None
            result = {"active": active, "configured": self._active is not None}
            if self._error is not None:
                result["error"] = self._error
            elif active is not None and not active["verified"]:
                result["error"] = "External account identity or online status is currently unverified."
            return result

    def gateway_config(self):
        with self._lock:
            if self._closed or self._active is None:
                raise ConnectionManagementError(
                    "No external account gateway is connected.", 503, "CONNECTION_UNAVAILABLE"
                )
            return {
                "origin": self._active.gateway_origin,
                "access_token": self._active.gateway_token,
            }

    def event_config(self):
        with self._lock:
            if self._closed or self._active is None:
                raise ConnectionManagementError(
                    "No external account event input is connected.", 503, "CONNECTION_UNAVAILABLE"
                )
            return self._active._event_config()

    def disconnect(self):
        with self._lock:
            previous, self._active = self._active, None
            if previous is not None:
                previous.close()
            self._error = None

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.disconnect()
            for probe in self._probes.values():
                probe.client.close()
            self._probes.clear()
