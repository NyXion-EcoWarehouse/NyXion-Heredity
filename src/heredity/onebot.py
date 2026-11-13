"""OneBot v11 forward HTTP transport.

Reference: https://github.com/botuniverse/onebot-11/tree/master/communication
WebSocket and reverse HTTP quick operations are not implemented.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import math
import re
import threading
import time
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib import error, parse, request

from .backends.base import BackendError
from .event_hub import EventGapError
from . import __version__

LOGGER = logging.getLogger(__name__)
MAX_BODY_BYTES = 1024 * 1024
ACTION_PATTERN = re.compile(r"[A-Za-z0-9_]{1,128}\Z")


def _failure(message: str, retcode: int = 1400) -> dict[str, Any]:
    return {
        "status": "failed",
        "retcode": retcode,
        "data": None,
        "message": message,
        "wording": message,
    }


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON value: {value}")


def _decode_json(value: str) -> Any:
    result = json.loads(value, parse_constant=_reject_constant)
    pending = [(result, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 64:
            raise ValueError("JSON nesting exceeds 64 levels.")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("Non-finite JSON number.")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return result


def _query_parameters(encoded: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in parse.parse_qsl(
        encoded, keep_blank_values=True, max_num_fields=256
    ):
        if key == "message" and value.lstrip().startswith(("[", "{")):
            try:
                value = _decode_json(value)
            except (ValueError, TypeError, RecursionError):
                pass
        elif key == "auto_escape" and value.lower() in {"true", "false"}:
            value = value.lower() == "true"
        result[key] = value
    return result


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_event_url(url: str) -> None:
    parsed = parse.urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("Event URLs must be HTTP(S) URLs without credentials or fragments.")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Event URL has an invalid port.") from exc


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OneBotHTTPServer(ThreadingHTTPServer):
    """HTTP API server with one bounded, periodic event delivery worker."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        token: str,
        backend: Any,
        event_urls: tuple[str, ...],
        heartbeat_interval: float,
        self_id: int,
        provider_token: str,
        event_secret: str,
        enable_sse: bool,
        execution_token: str,
    ) -> None:
        self.token = token
        self.backend = backend
        self.backend_lock = threading.Lock()
        self.event_urls = event_urls
        self.heartbeat_interval = heartbeat_interval
        self.self_id = self_id or backend.self_id
        self.provider_token = provider_token
        self.event_secret = event_secret
        self.enable_sse = enable_sse
        self.execution_token = execution_token
        self._closing = threading.Event()
        self._event_stop = threading.Event()
        self._event_thread: threading.Thread | None = None
        self._event_lock = threading.Lock()
        self._events_started = False
        self._settings_lock = threading.RLock()
        super().__init__(address, _Handler)

    def status(self):
        method = getattr(self.backend, "status", None)
        if callable(method):
            return method()
        online = bool(self.backend.online)
        return {"online": online, "good": online}

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5.0)
        return connection, address

    def dispatch(self, action: str, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        self._observe_network("action", {"action": action, "params": params})
        if action.endswith("_async"):
            return 404, _failure("Asynchronous actions are not supported.", 1404)
        try:
            with self.backend_lock:
                if self._closing.is_set():
                    return 200, _failure("Gateway is shutting down.", 1503)
                if action == "get_status":
                    data = self.status()
                elif action == "get_version_info":
                    data = {
                        "app_name": "Heredity",
                        "display_name": "NyXion Heredity",
                        "app_version": __version__,
                        "protocol_version": "v11",
                    }
                else:
                    data = self.backend.call(action, params)
            result = {"status": "ok", "retcode": 0, "data": data}
            # Validate before writing headers so backend bugs cannot produce a partial response.
            json.dumps(result, allow_nan=False)
            self._observe_network("response", result)
            return 200, result
        except BackendError as exc:
            retcode = int(exc.retcode)
            failure = _failure(str(exc), retcode)
            if exc.details is not None:
                failure["details"] = exc.details
            self._observe_network("response", failure)
            return (404 if retcode == 1404 else 200), failure
        except Exception:
            LOGGER.warning("Backend request failed.")
            return 200, _failure("Backend request failed.", 1500)

    def start_events(self) -> None:
        with self._event_lock:
            if self._closing.is_set() or self._events_started:
                return
            self._events_started = True
            if getattr(self.backend, "event_hub", None) is not None:
                self.backend.emit_meta({"meta_event_type": "lifecycle", "sub_type": "enable"})
            if (not self.event_urls and getattr(self.backend, "event_hub", None) is None
                    and not callable(getattr(self.backend, "event_runtime", None))):
                return
            self._event_stop.clear()
            self._event_thread = threading.Thread(
                target=self._run_events, name="heredity-events", daemon=True
            )
            self._event_thread.start()

    def stop_events(self) -> None:
        """Signal stop and wait at most four seconds for event delivery.

        This is best-effort: DNS resolution and trickling response headers can
        outlive urllib's per-operation socket timeout and this bounded join.
        """
        self._event_stop.set()
        with self._event_lock:
            worker = self._event_thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=4.0)

    def server_close(self) -> None:
        self._closing.set()
        self.stop_events()
        super().server_close()

    def _run_events(self) -> None:
        opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
        if callable(getattr(self.backend, "event_runtime", None)):
            self._run_plugin_events(opener)
            return
        hub = getattr(self.backend, "event_hub", None)
        if hub is not None:
            self._run_runtime_events(opener, hub)
            return
        self._post_event(
            opener,
            {"meta_event_type": "lifecycle", "sub_type": "enable"},
        )
        while not self._event_stop.wait(self.heartbeat_interval):
            self._post_event(
                opener,
                {
                    "meta_event_type": "heartbeat",
                    "status": self.status(),
                    "interval": round(self.heartbeat_interval * 1000),
                },
            )

    def _run_plugin_events(self, opener):
        """Keep the listener alive while plugins log in, stop or change epoch."""
        current, hub, cursor = None, None, 0
        current_stamp = None
        heartbeat_at = time.monotonic() + self.heartbeat_interval
        while not self._event_stop.is_set():
            backend = self.backend
            route_stamp = getattr(backend, "event_route_stamp", lambda: 0)
            start_cursor = getattr(backend, "event_start_cursor", lambda runtime: 0)
            route_current = getattr(
                backend, "event_route_current",
                lambda runtime, stamp: backend.event_runtime() is runtime)
            stamp = route_stamp()
            runtime = backend.event_runtime()
            if runtime is None:
                current, hub, cursor = None, None, 0
                self._event_stop.wait(0.1)
                continue
            if runtime is not current or current_stamp != stamp:
                current, hub = runtime, runtime.event_hub
                cursor, current_stamp = start_cursor(runtime), stamp
                try:
                    runtime.emit_meta({"meta_event_type": "lifecycle", "sub_type": "enable"})
                except BackendError:
                    current, hub = None, None
                    continue
            now = time.monotonic()
            if now >= heartbeat_at:
                try:
                    runtime.emit_meta({"meta_event_type": "heartbeat", "status": runtime.status(),
                                       "interval": round(self.heartbeat_interval * 1000)})
                except BackendError:
                    current, hub = None, None
                    continue
                heartbeat_at = now + self.heartbeat_interval
            try:
                records = hub.read(cursor, timeout=0.1)
            except EventGapError:
                cursor = hub.latest_sequence
                LOGGER.warning("Plugin event replay window was exceeded.")
                continue
            for record in records:
                if self._event_stop.is_set():
                    return
                if not route_current(runtime, stamp):
                    break
                self._deliver_event(opener, record["event"])
                cursor = record["sequence"]

    def _run_runtime_events(self, opener, hub):
        cursor = 0
        heartbeat_at = time.monotonic() + self.heartbeat_interval
        while not self._event_stop.is_set():
            current_hub = getattr(self.backend, "event_hub", None)
            if current_hub is not None and current_hub is not hub:
                hub, cursor = current_hub, 0
            now = time.monotonic()
            if now >= heartbeat_at:
                try:
                    self.backend.emit_meta({"meta_event_type": "heartbeat", "status": self.status(),
                                            "interval": round(self.heartbeat_interval * 1000)})
                except BackendError:
                    LOGGER.warning("Heartbeat persistence failed.")
                heartbeat_at = now + self.heartbeat_interval
            try:
                records = hub.read(cursor, timeout=min(0.5, max(0, heartbeat_at - time.monotonic())))
            except EventGapError:
                LOGGER.warning("Event delivery gap; retained replay window was exceeded.")
                cursor = hub.latest_sequence
                continue
            for record in records:
                if self._event_stop.is_set():
                    return
                self._deliver_event(opener, record["event"])
                cursor = record["sequence"]

    def _post_event(self, opener: Any, fields: dict[str, Any]) -> None:
        event = {
            "time": int(time.time()),
            "self_id": self.self_id,
            "post_type": "meta_event",
            **fields,
        }
        self._deliver_event(opener, event)

    def _deliver_event(self, opener, event, *, observe=True):
        if observe:
            self._observe_network("event", event)
        with self._settings_lock:
            event_secret, event_urls = self.event_secret, self.event_urls
        payload = json.dumps(event, ensure_ascii=True, allow_nan=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-Self-ID": str(event.get("self_id", self.self_id)),
                   "User-Agent": "Heredity"}
        if event_secret:
            headers["X-Signature"] = "sha1=" + hmac.new(
                event_secret.encode("utf-8"), payload, "sha1").hexdigest()
        for url in event_urls:
            if self._event_stop.is_set():
                return
            outgoing = request.Request(
                url,
                data=payload,
                method="POST",
                headers=headers,
            )
            try:
                with opener.open(outgoing, timeout=3.0):
                    pass
            except (error.URLError, HTTPException, OSError, ValueError):
                # URLs may contain secrets in their query; never include them in logs.
                LOGGER.warning("Event delivery failed.")

    def _observe_network(self, kind, payload):
        observer = getattr(self, "network_observer", None)
        if observer is None:
            return
        try:
            observer(kind, payload)
        except Exception:
            gap = getattr(self, "network_observer_gap", None)
            if gap is not None:
                try:
                    gap()
                except Exception:
                    LOGGER.warning("Network observation gap notification failed.")
            LOGGER.warning("Network observation failed; transport processing continued.")


class _Handler(BaseHTTPRequestHandler):
    server: OneBotHTTPServer
    server_version = f"Heredity/{__version__}"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        # The default request logger would expose access_token query parameters.
        return

    def do_GET(self) -> None:
        self._handle(False)

    def do_POST(self) -> None:
        self._handle(True)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def _authenticate(self, query: dict[str, Any], *, provider=False, execution=False) -> bool:
        privileged = provider or execution
        with self.server._settings_lock:
            required = (self.server.execution_token if execution else
                        self.server.provider_token if provider else self.server.token)
        if not required:
            if privileged:
                self._send(404, _failure("This privileged input is disabled.", 1404))
                return False
            return True
        authorization = self.headers.get("Authorization")
        supplied = None if privileged else query.get("access_token")
        if authorization is not None:
            scheme, separator, credential = authorization.partition(" ")
            supplied = credential if separator and scheme.lower() == "bearer" else ""
        if supplied is None:
            self._send(401, _failure("Access token is required.", 1401))
            return False
        if not isinstance(supplied, str) or not hmac.compare_digest(
            supplied.encode("utf-8"), required.encode("utf-8")
        ):
            self._send(403, _failure("Access token is invalid.", 1403))
            return False
        return True

    def _stream_events(self, query):
        provide = getattr(self.server, "network_hub_provider", None)
        hub = provide() if callable(provide) else getattr(self.server, "network_event_hub", None)
        if hub is None:
            hub = getattr(self.server.backend, "event_hub", None)
        with self.server._settings_lock:
            enabled = self.server.enable_sse
            original_token = self.server.token
        if not enabled or hub is None:
            self._send(404, _failure("Event streaming is disabled.", 1404))
            return
        try:
            supplied = self.headers.get("Last-Event-ID", query.get("after"))
            cursor = hub.latest_sequence if supplied is None else int(supplied)
            records = hub.read(cursor)
        except EventGapError as exc:
            self._send(409, {**_failure("Event cursor is outside the retained window.", 1409),
                             "oldest_sequence": exc.oldest_sequence,
                             "latest_sequence": exc.latest_sequence})
            return
        except (ValueError, TypeError):
            self._send(400, _failure("Invalid event cursor."))
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Self-ID", str(hub.self_id))
        self.end_headers()
        self.close_connection = True
        try:
            while not self.server._closing.is_set() and not hub.closed:
                with self.server._settings_lock:
                    if not self.server.enable_sse or self.server.token != original_token:
                        return
                for record in records:
                    raw = json.dumps(record["event"], ensure_ascii=True, allow_nan=False)
                    self.wfile.write(f"id: {record['sequence']}\ndata: {raw}\n\n".encode())
                    cursor = record["sequence"]
                if not records:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                records = hub.read(cursor, timeout=1.0)
        except EventGapError:
            # A slow reader must reconnect and explicitly handle the replay gap.
            try:
                self.wfile.write(b"event: gap\ndata: {\"reason\":\"replay_window_exceeded\"}\n\n")
                self.wfile.flush()
            except OSError:
                pass
        except (OSError, ValueError):
            pass

    def _handle(self, has_body: bool) -> None:
        try:
            parsed = parse.urlsplit(self.path)
            query = _query_parameters(parsed.query)
        except (ValueError, RecursionError):
            self._send(400, _failure("Invalid request URL."))
            return
        route = parse.unquote(parsed.path).strip("/")
        prefix = getattr(self.server, "api_path", "/").strip("/")
        if prefix:
            if route == prefix:
                route = ""
            elif route.startswith(prefix + "/"):
                route = route[len(prefix) + 1:]
            else:
                self._send(404, _failure("Unknown interface path.", 1404))
                return
        provider_input = has_body and route == "_heredity/events"
        execution_input = has_body and route == "_heredity/providers/switch"
        if not self._authenticate(query, provider=provider_input, execution=execution_input):
            return
        query.pop("access_token", None)
        if not has_body and route == "_heredity/providers":
            describe = getattr(self.server.backend, "execution_info", None)
            if not callable(describe):
                self._send(404, _failure("Execution provider metadata is unavailable.", 1404))
            else:
                payload = describe()
                payload["switch_enabled"] = bool(self.server.execution_token) and payload.get("switch_supported", False)
                self._send(200, payload)
            return
        if not has_body and route == "_heredity/events":
            self._stream_events(query)
            return
        body: Any = {}
        if has_body:
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) > 1:
                self._send(400, _failure("Ambiguous request framing."))
                return
            try:
                content_length = int(lengths[0]) if lengths else 0
                if content_length < 0:
                    raise ValueError
            except ValueError:
                self._send(400, _failure("Invalid Content-Length."))
                return
            if content_length > MAX_BODY_BYTES:
                self._send(413, _failure("Request body exceeds 1 MiB."))
                return
            media_type = self.headers.get_content_type()
            if (provider_input or execution_input) and media_type != "application/json":
                self._send(406, _failure("Privileged requests require application/json."))
                return
            if media_type not in {
                "application/json",
                "application/x-www-form-urlencoded",
            } and content_length:
                self._send(406, _failure("Unsupported Content-Type."))
                return
            try:
                raw = self.rfile.read(content_length)
                if len(raw) != content_length:
                    raise ValueError
                text = raw.decode("utf-8")
                if text:
                    body = (
                        _query_parameters(text)
                        if media_type == "application/x-www-form-urlencoded"
                        else _decode_json(text)
                    )
                if not isinstance(body, dict):
                    raise ValueError
            except (UnicodeError, ValueError, OSError, RecursionError):
                self._send(400, _failure("Request body must be a valid parameter object."))
                return
        if execution_input:
            switch = getattr(self.server.backend, "switch_provider", None)
            if not callable(switch):
                self._send(404, _failure("Provider switching is unavailable.", 1404))
                return
            if (not isinstance(body.get("provider_id"), str)
                    or not isinstance(body.get("expected_epoch"), str)):
                self._send(400, _failure("provider_id and expected_epoch are required."))
                return
            try:
                if self.server._closing.is_set():
                    raise BackendError("Gateway is shutting down.", 1503)
                state = switch(body["provider_id"], body["expected_epoch"],
                               timeout=body.get("timeout", 5))
                changed = state.pop("changed", True)
                self._send(200, {"state": state, "changed": changed})
            except BackendError as exc:
                payload = _failure(str(exc), exc.retcode)
                if exc.details is not None:
                    payload["details"] = exc.details
                status = 409 if exc.retcode == 1409 else 400 if exc.retcode == 1400 else 503
                self._send(status, payload)
            except Exception:
                LOGGER.warning("Provider transition failed.")
                self._send(503, _failure("Provider transition failed.", 1500))
            return
        if provider_input:
            ingest = getattr(self.server.backend, "ingest", None)
            if not callable(ingest):
                self._send(404, _failure("Backend has no event input.", 1404))
                return
            try:
                with self.server.backend_lock:
                    if self.server._closing.is_set():
                        raise BackendError("Gateway is shutting down.", 1503)
                    result = ingest(body, event_key=self.headers.get("X-Heredity-Event-ID"),
                                    session_epoch=self.headers.get("X-Heredity-Session-Epoch"))
                self._send(200, {"status": "ok", "retcode": 0, "data": result})
            except BackendError as exc:
                self._send(409 if exc.retcode == 1409 else 400 if exc.retcode < 1500 else 503,
                           _failure(str(exc), exc.retcode))
            except Exception:
                LOGGER.warning("Provider event input failed.")
                self._send(503, _failure("Provider event input failed.", 1500))
            return
        path = route
        echo_present = False
        echo = None
        if not path and has_body:
            action = body.get("action")
            params = body.get("params", {})
            echo_present = "echo" in body
            echo = body.get("echo")
            if isinstance(params, dict):
                params = {**query, **params}
        else:
            action = path
            params = {**query, **body}
        if not isinstance(action, str) or not ACTION_PATTERN.fullmatch(action):
            self._send(400, _failure("A valid action name is required."))
            return
        if not isinstance(params, dict):
            self._send(400, _failure("Action params must be an object."))
            return
        status, result = self.server.dispatch(action, params)
        if echo_present:
            result["echo"] = echo
        self._send(status, result)


def create_server(
    host: str,
    port: int,
    token: str,
    backend: Any,
    event_urls=(),
    heartbeat_interval: float = 15.0,
    self_id: int = 0,
    provider_token: str = "",
    event_secret: str = "",
    enable_sse: bool = False,
    execution_token: str = "",
) -> OneBotHTTPServer:
    """Build a server without starting a listener loop or event worker."""
    if not isinstance(token, str):
        raise ValueError("Access token must be a string.")
    if not isinstance(provider_token, str) or not isinstance(event_secret, str):
        raise ValueError("Provider token and event signing secret must be strings.")
    if not isinstance(execution_token, str):
        raise ValueError("Execution administration token must be a string.")
    for existing in (token, provider_token):
        if execution_token and existing and hmac.compare_digest(execution_token.encode(), existing.encode()):
            raise ValueError("Execution administration token must differ from other access tokens.")
    if provider_token and token and hmac.compare_digest(provider_token.encode(), token.encode()):
        raise ValueError("Provider event token must differ from the consumer access token.")
    if provider_token and not callable(getattr(backend, "ingest", None)):
        raise ValueError("Provider event input requires an account runtime.")
    if not _is_loopback(host) and not token:
        raise ValueError("A non-loopback listener requires an access token.")
    if not isinstance(heartbeat_interval, (int, float)) or not (
        0 < heartbeat_interval <= 86400
    ):
        raise ValueError("Heartbeat interval must be finite and between 0 and 86400 seconds.")
    event_urls = tuple(event_urls)
    for url in event_urls:
        _validate_event_url(url)
    server_type = OneBotHTTPServer
    if ":" in host:
        import socket

        class IPv6Server(OneBotHTTPServer):
            address_family = socket.AF_INET6

        server_type = IPv6Server
    return server_type(
        (host, port), token, backend, event_urls, float(heartbeat_interval), self_id,
        provider_token, event_secret, enable_sse, execution_token
    )
