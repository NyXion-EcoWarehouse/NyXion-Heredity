"""Host-owned WebSocket transports backed by the websockets library.

Actions use OneBot v11 request/response envelopes. Events are JSON objects.
Reconnect restores a transport connection only; in-flight actions and queued
outbound messages are never replayed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hmac
from http import HTTPStatus
import json
import logging
import math
import queue
import re
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response
from websockets.sync.client import connect
from websockets.sync.server import serve

from nyxion_heredity_sdk import BackendError, freeze_json, thaw_json

from .network_errors import listener_failure


MAX_MESSAGE_BYTES = 1024 * 1024
_ACTION = re.compile(r"[A-Za-z0-9_]{1,128}\Z")
_LOGGER = logging.getLogger("heredity.network.websocket")
_LOGGER.addHandler(logging.NullHandler())
_LOGGER.propagate = False


class WebSocketTransportError(ValueError):
    """A sanitized transport error; callers must not infer safe replay."""

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


def _budget(value):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 < value <= 120):
        raise ValueError("WebSocket timeout must be finite and in (0, 120].")
    return float(value)


def _token(value):
    if (not isinstance(value, str) or len(value) > 4096
            or any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise ValueError("WebSocket token must contain bounded printable ASCII.")
    return value


def _callback(callback):
    if callback is not None and not callable(callback):
        raise ValueError("WebSocket callbacks must be callable.")
    return callback


def _encode(value):
    if type(value) is not dict:
        raise ValueError("WebSocket payload must be a JSON object.")
    frozen = freeze_json(value)
    encoded = json.dumps(thaw_json(frozen), ensure_ascii=False,
                         allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("WebSocket payload exceeds the message limit.")
    return encoded


def _decode(value):
    if not isinstance(value, str):
        raise ValueError("WebSocket requires text JSON objects.")
    if len(value.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("WebSocket payload exceeds the message limit.")
    def pairs(items):
        result = {}
        for key, child in items:
            if key in result:
                raise ValueError("Duplicate JSON keys.")
            result[key] = child
        return result
    def invalid_constant(value):
        raise ValueError("JSON numbers must be finite.")
    decoded = json.loads(value, object_pairs_hook=pairs, parse_constant=invalid_constant)
    if type(decoded) is not dict:
        raise ValueError("WebSocket payload must be a JSON object.")
    # Validate complexity and own a stable, ordinary JSON snapshot.
    return thaw_json(freeze_json(decoded))


def _failure(message, retcode=1400, *, unknown=False):
    result = {"status": "failed", "retcode": retcode, "data": None,
              "message": message, "wording": message}
    if unknown:
        result["details"] = {"action_outcome": "unknown", "retry_safe": False}
    return result


def _validate_response(message):
    if (message.get("status") not in {"ok", "failed"}
            or type(message.get("retcode")) is not int or "data" not in message):
        raise ValueError("Invalid OneBot response envelope.")
    return message


def _disconnect(connection):
    """Interrupt a blocked socket write before doing protocol-level cleanup."""
    try:
        connection.socket.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    connection.socket.close()
    try:
        connection.close()
    except OSError:
        pass


def _action_response(message, callback, stop):
    """Isolate a non-cooperating callback from connection handler shutdown."""
    action = message.get("action")
    params = message.get("params", {})
    if (not isinstance(action, str) or not _ACTION.fullmatch(action)
            or type(params) is not dict):
        response = _failure("Invalid OneBot action request.")
    elif callback is None:
        response = _failure("No action handler is configured.", 1404)
    else:
        result = queue.Queue(maxsize=1)
        def invoke():
            try:
                response = callback(message)
                _encode(response)
                _validate_response(response)
            except BackendError as error:
                response = _failure("Action handler rejected the request.", error.retcode)
                if type(error.details) is dict:
                    response["details"] = error.details
            except Exception:
                response = _failure("Action handler failed; do not automatically resend.",
                                    1502, unknown=True)
            result.put(response)
        worker = threading.Thread(target=invoke, name="nyxion-ws-action", daemon=True)
        worker.start()
        deadline = time.monotonic() + 120
        while True:
            try:
                response = result.get(timeout=0.1)
                break
            except queue.Empty:
                if stop.is_set() or time.monotonic() >= deadline:
                    response = _failure("Action outcome is unknown; do not automatically resend.",
                                        1504, unknown=True)
                    break
    if "echo" in message:
        response = {**response, "echo": message["echo"]}
    return response


@dataclass(eq=False)
class _Peer:
    connection: object
    outgoing: queue.Queue
    stop: threading.Event = field(default_factory=threading.Event)
    writer: object = None


class WebSocketServerTransport:
    def __init__(self, *, host, port, path="/", token="", on_action=None,
                 on_message=None, max_peers=64, queue_size=64):
        if (not isinstance(host, str) or not host or type(port) is not int
                or not 0 <= port <= 65535):
            raise ValueError("WebSocket listener address is invalid.")
        if (not isinstance(path, str) or not path.startswith("/")
                or "?" in path or "#" in path or "\\" in path
                or any(ord(char) < 33 or ord(char) > 126 for char in path)):
            raise ValueError("WebSocket listener path must be a literal URL path.")
        if type(max_peers) is not int or not 1 <= max_peers <= 1024:
            raise ValueError("WebSocket peer limit is invalid.")
        if type(queue_size) is not int or not 1 <= queue_size <= 1024:
            raise ValueError("WebSocket queue limit is invalid.")
        self.host, self.port, self.path = host, port, path
        self.token = _token(token)
        self.on_action, self.on_message = _callback(on_action), _callback(on_message)
        self.max_peers, self.queue_size = max_peers, queue_size
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._peers = set()
        self._server = self._thread = None
        self._state = "stopped"
        self._error = None
        self._error_code = None
        self.actual_port = None

    @property
    def endpoint(self):
        host = "127.0.0.1" if self.host == "0.0.0.0" else "::1" if self.host == "::" else self.host
        authority = f"[{host}]" if ":" in host else host
        return f"ws://{authority}:{self.actual_port or self.port}{self.path}"

    def snapshot(self):
        with self._lock:
            return {"state": self._state, "endpoint": self.endpoint,
                    "actual_port": self.actual_port, "connected_peers": len(self._peers),
                    "error": self._error, "error_code": self._error_code}

    def _emit(self, kind, payload):
        if self.on_message is not None:
            self.on_message(kind, payload)

    def _process_request(self, connection, request):
        parsed = urlsplit(request.path)
        if parsed.path != self.path:
            return self._http_response(HTTPStatus.NOT_FOUND)
        if self._stop.is_set():
            return self._http_response(HTTPStatus.SERVICE_UNAVAILABLE)
        if self.token:
            try:
                authorization = request.headers.get("Authorization")
                if authorization is not None:
                    scheme, separator, supplied = authorization.partition(" ")
                    supplied = supplied if separator and scheme.lower() == "bearer" else ""
                else:
                    values = parse_qs(parsed.query, max_num_fields=32)
                    supplied = values.get("access_token", [""])[-1]
            except (ValueError, KeyError):
                supplied = ""
            if not hmac.compare_digest(supplied.encode(), self.token.encode()):
                return self._http_response(HTTPStatus.UNAUTHORIZED)
        with self._lock:
            if len(self._peers) >= self.max_peers:
                return self._http_response(HTTPStatus.SERVICE_UNAVAILABLE)
        return None

    @staticmethod
    def _http_response(status):
        body = (status.phrase + "\n").encode()
        return Response(status.value, status.phrase,
                        Headers({"Content-Type": "text/plain", "Content-Length": str(len(body))}),
                        body)

    def start(self):
        with self._lock:
            if self._thread is not None or self._state != "stopped":
                raise WebSocketTransportError("WebSocket listener is already started.")
            self._state = "starting"
            self._error = self._error_code = None
            try:
                self._server = serve(
                    self._handle, self.host, self.port, process_request=self._process_request,
                    open_timeout=3, close_timeout=1, ping_interval=20, ping_timeout=20,
                    max_size=MAX_MESSAGE_BYTES, max_queue=16, compression=None,
                    logger=_LOGGER, server_header="NyXion Heredity")
                self.actual_port = self._server.socket.getsockname()[1]
                self._thread = threading.Thread(target=self._server.serve_forever,
                                                name="nyxion-ws-listener", daemon=True)
                self._thread.start()
                self._state = "running"
            except Exception as error:
                self._state = "error"
                self._error_code, self._error = listener_failure(error, self.port, host=self.host)
                self.actual_port = None
                if self._server is not None and self._thread is None:
                    self._server.socket.close()
                    self._server = None
                raise WebSocketTransportError(self._error, code=self._error_code) from None
        self._emit("state", {"state": "running"})

    def _write(self, peer):
        while not self._stop.is_set() and not peer.stop.is_set():
            try:
                encoded = peer.outgoing.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                peer.connection.send(encoded)
            except Exception:
                peer.stop.set()
                peer.connection.close(code=1011, reason="Transport write failed")
                return

    def _send(self, peer, message):
        encoded = _encode(message)
        try:
            peer.outgoing.put_nowait(encoded)
        except queue.Full:
            peer.stop.set()
            _disconnect(peer.connection)
            raise WebSocketTransportError("WebSocket peer queue is full.") from None

    def _handle(self, connection):
        peer = _Peer(connection, queue.Queue(maxsize=self.queue_size))
        with self._lock:
            if self._stop.is_set() or len(self._peers) >= self.max_peers:
                connection.close(code=1013, reason="Peer limit reached")
                return
            self._peers.add(peer)
        peer.writer = threading.Thread(target=self._write, args=(peer,),
                                       name="nyxion-ws-events", daemon=True)
        peer.writer.start()
        try:
            while not self._stop.is_set() and not peer.stop.is_set():
                try:
                    raw = connection.recv(timeout=0.2)
                except TimeoutError:
                    continue
                try:
                    message = _decode(raw)
                except (ValueError, RecursionError):
                    self._send(peer, _failure("Invalid JSON message."))
                    continue
                if "action" in message:
                    self._emit("action", message)
                    response = _action_response(message, self.on_action, self._stop)
                    if not self._stop.is_set():
                        self._send(peer, response)
                        self._emit("response", response)
                elif "post_type" in message:
                    self._emit("event", message)
                elif message.get("status") in {"ok", "failed"}:
                    _validate_response(message)
                    self._emit("response", message)
                else:
                    self._send(peer, _failure("Unsupported OneBot message."))
        except (ConnectionClosed, OSError):
            pass
        except Exception:
            connection.close(code=1011, reason="Message handling failed")
        finally:
            peer.stop.set()
            connection.close(code=1001, reason="Connection closed")
            peer.writer.join(timeout=2)
            with self._lock:
                self._peers.discard(peer)

    def publish(self, event):
        _encode(event)
        with self._lock:
            if self._state != "running" or self._stop.is_set():
                raise WebSocketTransportError("WebSocket listener is stopped.")
            peers = tuple(self._peers)
        for peer in peers:
            try:
                self._send(peer, event)
            except WebSocketTransportError:
                # Close only the slow peer; other peers still receive the event.
                pass
        self._emit("event", event)

    def stop(self):
        with self._lock:
            if self._state == "stopped" and self._stop.is_set():
                return
            self._stop.set()
            server, worker = self._server, self._thread
            peers = tuple(self._peers)
        for peer in peers:
            peer.stop.set()
            _disconnect(peer.connection)
        if server is not None:
            shutdown = threading.Thread(target=server.shutdown,
                                        name="nyxion-ws-shutdown", daemon=True)
            shutdown.start()
            shutdown.join(timeout=4)
            if shutdown.is_alive():
                with self._lock:
                    self._state = "error"
                    self._error = "WebSocket shutdown is still pending."
                raise WebSocketTransportError(self._error)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2)
            if worker.is_alive():
                raise WebSocketTransportError("WebSocket listener did not stop.")
        with self._lock:
            self._state = "stopped"
            self.actual_port = None
            self._error = self._error_code = None
        self._emit("state", {"state": "stopped"})


@dataclass
class _Pending:
    ready: threading.Event = field(default_factory=threading.Event)
    response: object = None
    failure: object = None
    deadline: float = 0
    started: bool = False


class WebSocketClientTransport:
    def __init__(self, *, url, token="", on_message=None, on_action=None,
                 connect_timeout=3, reconnect=True, queue_size=64, self_id="0"):
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (parsed is None or parsed.scheme not in {"ws", "wss"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or "\\" in url or len(url) > 2048
                or any(ord(char) < 33 or ord(char) > 126 for char in url)):
            raise ValueError("WebSocket client URL is invalid.")
        try:
            parsed.port
        except ValueError:
            raise ValueError("WebSocket client port is invalid.") from None
        if type(reconnect) is not bool or type(queue_size) is not int or not 1 <= queue_size <= 1024:
            raise ValueError("WebSocket client reconnect or queue setting is invalid.")
        if not callable(self_id) and not isinstance(self_id, str):
            raise ValueError("WebSocket client account must be a string or resolver.")
        self.url, self.token = url, _token(token)
        self.on_message, self.on_action = _callback(on_message), _callback(on_action)
        self.connect_timeout = _budget(connect_timeout)
        self.reconnect = reconnect
        self.self_id = self_id
        self._outgoing = queue.Queue(maxsize=queue_size)
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._first_attempt = threading.Event()
        self._connection = self._thread = self._writer = None
        self._state = "stopped"
        self._error = None
        self._pending = {}
        self._request_limit = queue_size

    @property
    def endpoint(self):
        # Query strings can contain deployment credentials.
        value = urlsplit(self.url)
        return value._replace(query="").geturl()

    def snapshot(self):
        with self._lock:
            return {"state": self._state, "endpoint": self.endpoint, "actual_port": None,
                    "connected_peers": int(self._connection is not None),
                    "error": self._error}

    def _emit(self, kind, payload):
        if self.on_message is not None:
            self.on_message(kind, payload)

    def _set_state(self, state, error=None):
        with self._lock:
            self._state, self._error = state, error
        self._emit("state", {"state": state, "error": error})

    def start(self):
        with self._lock:
            if self._thread is not None or self._state != "stopped":
                raise WebSocketTransportError("WebSocket client is already started.")
            self._state = "starting"
            self._thread = threading.Thread(target=self._run, name="nyxion-ws-client", daemon=True)
            self._thread.start()
        if not self._first_attempt.wait(self.connect_timeout + 1):
            self.stop()
            raise WebSocketTransportError("WebSocket client connection deadline expired.")

    def _fail_pending(self):
        with self._lock:
            requests, self._pending = tuple(self._pending.values()), {}
        for request in requests:
            request.failure = (
                BackendError("WebSocket disconnected; action outcome may be unknown.", 1502,
                             details={"action_outcome": "unknown", "retry_safe": False})
                if request.started else BackendError(
                    "WebSocket disconnected before the action was sent.", 1503,
                    details={"action_completed": False}))
            request.ready.set()

    def _clear_outgoing(self):
        while True:
            try:
                self._outgoing.get_nowait()
            except queue.Empty:
                return

    def _run(self):
        delay = 0.25
        try:
            while not self._stop.is_set():
                connection = None
                try:
                    account = self.self_id() if callable(self.self_id) else self.self_id
                    if (not isinstance(account, str) or not account
                            or any(ord(char) < 33 or ord(char) > 126 for char in account)):
                        raise ValueError("Resolved WebSocket account is invalid.")
                    headers = {"X-Client-Role": "Universal", "X-Self-ID": account}
                    if self.token:
                        headers["Authorization"] = "Bearer " + self.token
                    connection = connect(
                        self.url, additional_headers=headers, proxy=None,
                        open_timeout=self.connect_timeout, close_timeout=1,
                        ping_interval=20, ping_timeout=20, max_size=MAX_MESSAGE_BYTES,
                        max_queue=16, compression=None, logger=_LOGGER,
                        user_agent_header="NyXion Heredity", legacy=True)
                    with self._lock:
                        if self._stop.is_set():
                            connection.close()
                            break
                        self._connection = connection
                    self._set_state("running")
                    self._first_attempt.set()
                    delay = 0.25
                    writer_stop = threading.Event()
                    self._writer = threading.Thread(target=self._write, args=(connection, writer_stop),
                                                    name="nyxion-ws-client-writer", daemon=True)
                    self._writer.start()
                    try:
                        while not self._stop.is_set():
                            try:
                                raw = connection.recv(timeout=0.2)
                            except TimeoutError:
                                continue
                            message = _decode(raw)
                            if "action" in message:
                                self._emit("action", message)
                                response = _action_response(message, self.on_action, self._stop)
                                if not self._stop.is_set():
                                    self._queue_message(connection, response, "response")
                            elif message.get("status") in {"ok", "failed"}:
                                _validate_response(message)
                                self._emit("response", message)
                                echo = message.get("echo")
                                if isinstance(echo, str):
                                    with self._lock:
                                        request = self._pending.pop(echo, None)
                                    if request is not None:
                                        request.response = message
                                        request.ready.set()
                            elif "post_type" in message:
                                self._emit("event", message)
                            else:
                                raise ValueError("Unsupported OneBot WebSocket message.")
                    finally:
                        writer_stop.set()
                        connection.close()
                        self._writer.join(timeout=2)
                except Exception:
                    if not self._stop.is_set():
                        self._set_state("error", "WebSocket connection is unavailable.")
                finally:
                    with self._lock:
                        self._connection = None
                    if connection is not None:
                        connection.close()
                    self._fail_pending()
                    self._clear_outgoing()
                    self._first_attempt.set()
                if not self.reconnect or self._stop.wait(delay):
                    break
                delay = min(5.0, delay * 2)
        finally:
            self._set_state("stopped" if self._stop.is_set() else "error",
                            None if self._stop.is_set() else "WebSocket connection is unavailable.")

    def _write(self, connection, local_stop):
        while not self._stop.is_set() and not local_stop.is_set():
            try:
                target, message, kind, request_id = self._outgoing.get(timeout=0.1)
            except queue.Empty:
                continue
            if target is not connection:
                continue
            if request_id is not None:
                with self._lock:
                    request = self._pending.get(request_id)
                    if request is None:
                        continue
                    if time.monotonic() >= request.deadline:
                        self._pending.pop(request_id, None)
                        request.failure = BackendError(
                            "WebSocket send queue deadline expired; no request was sent.", 1504,
                            details={"action_completed": False})
                        request.ready.set()
                        continue
                    request.started = True
            try:
                with self._send_lock:
                    connection.send(_encode(message))
                self._emit(kind, message)
            except Exception:
                _disconnect(connection)
                return

    def _queue_message(self, connection, message, kind, request_id=None):
        _encode(message)
        try:
            self._outgoing.put_nowait((connection, message, kind, request_id))
        except queue.Full:
            _disconnect(connection)
            raise WebSocketTransportError("WebSocket client queue is full.") from None

    def publish(self, event):
        # Own the JSON payload before returning; caller mutations cannot race
        # asynchronous serialization in the writer.
        message = _decode(_encode(event))
        with self._lock:
            connection = self._connection
            if connection is None or self._stop.is_set():
                raise WebSocketTransportError("WebSocket client is disconnected.")
            self._queue_message(connection, message, "event")

    def call_remote(self, action, params, timeout=5):
        timeout = _budget(timeout)
        if not isinstance(action, str) or not _ACTION.fullmatch(action) or type(params) is not dict:
            raise ValueError("WebSocket action request is invalid.")
        identifier = "nyxion-" + uuid4().hex
        message = {"action": action, "params": params, "echo": identifier}
        message = _decode(_encode(message))
        request = _Pending()
        deadline = time.monotonic() + timeout
        request.deadline = deadline
        with self._lock:
            connection = self._connection
            if connection is None or self._stop.is_set():
                raise BackendError("WebSocket client is disconnected; no request was sent.", 1503,
                                   details={"action_completed": False})
            if len(self._pending) >= self._request_limit:
                raise BackendError("WebSocket pending action limit reached; no request was sent.",
                                   1504, details={"action_completed": False})
            self._pending[identifier] = request
            try:
                self._queue_message(connection, message, "action", identifier)
            except WebSocketTransportError:
                self._pending.pop(identifier, None)
                raise BackendError("WebSocket action queue is full; no request was sent.", 1504,
                                   details={"action_completed": False}) from None
        if not request.ready.wait(max(0, deadline - time.monotonic())):
            with self._lock:
                self._pending.pop(identifier, None)
                started = request.started
            if not started:
                raise BackendError("WebSocket send deadline expired; no request was sent.",
                                   1504, details={"action_completed": False})
            _disconnect(connection)
            raise BackendError("WebSocket response deadline expired; do not automatically resend.",
                               1504, details={"action_outcome": "unknown", "retry_safe": False})
        if request.failure is not None:
            raise request.failure
        return request.response

    def stop(self):
        if self._stop.is_set() and (self._thread is None or not self._thread.is_alive()):
            return
        self._stop.set()
        with self._lock:
            connection, worker = self._connection, self._thread
        if connection is not None:
            _disconnect(connection)
        self._fail_pending()
        self._clear_outgoing()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=self.connect_timeout + 3)
            if worker.is_alive():
                raise WebSocketTransportError("WebSocket client shutdown is still pending.")
        self._set_state("stopped")
