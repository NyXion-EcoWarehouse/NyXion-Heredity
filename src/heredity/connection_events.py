"""Authenticated loopback receiver for standard OneBot v11 HTTP events."""

from __future__ import annotations

import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import secrets
import threading
import time

from .backends.base import BackendError
from .messages import positive_id
from .onebot import _decode_json


MAX_EVENT_BYTES = 1024 * 1024
_CONTENT_LENGTH = re.compile(r"[0-9]{1,10}\Z")
_SIGNATURE = re.compile(r"sha1=([0-9a-fA-F]{40})\Z")


class _ReceiverServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, receiver):
        self.receiver = receiver
        super().__init__(("127.0.0.1", 0), _EventHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5.0)
        return connection, address

    def service_actions(self):
        self.receiver._loop_started = True
        self.receiver._startup_complete.set()


class EventReceiver:
    """Accept signed event data for one fixed account session epoch.

    The receiver never owns the runtime. Callers explicitly retrieve its secret
    through config(), then configure the external OneBot event producer.
    """

    def __init__(self, runtime, epoch):
        if (
            not callable(getattr(runtime, "ingest", None))
            or type(getattr(runtime, "self_id", None)) is not int
            or runtime.self_id <= 0
        ):
            raise ValueError("Event receiver requires a bound account runtime.")
        if (
            type(epoch) is not str or not 1 <= len(epoch) <= 256
            or any(ord(character) < 33 or ord(character) > 126 for character in epoch)
        ):
            raise ValueError("Event receiver requires a bounded session epoch.")
        self._runtime = runtime
        self._epoch = epoch
        self._path = "/events/" + secrets.token_urlsafe(32)
        self._secret = secrets.token_urlsafe(48)
        self._lock = threading.RLock()
        self._startup_complete = threading.Event()
        self._loop_started = False
        self._thread = None
        self._stop_thread = None
        self._closing = False
        self._closed = False
        self._epoch_stale = False
        self._healthy = None
        self._last_event_at = None
        self._accepted = 0
        self._duplicates = 0
        self._reason = "Waiting for a signed OneBot event; reception has not been verified."
        self._server = _ReceiverServer(self)
        self._url = f"http://127.0.0.1:{self._server.server_port}{self._path}"

    @property
    def url(self):
        return self._url

    @property
    def secret(self):
        return self._secret

    def config(self):
        with self._lock:
            if not self._current_epoch():
                raise BackendError(
                    "Event session changed; reconnect to generate a new event callback.", 1409
                )
            return {"url": self._url, "secret": self._secret}

    def _current_epoch(self):
        if self._epoch_stale:
            return False
        try:
            backend = getattr(self._runtime, "backend", None)
            current = getattr(backend, "session_epoch", self._epoch)
        except Exception:
            current = None
        if current != self._epoch:
            self._epoch_stale = True
        return not self._epoch_stale

    def snapshot(self):
        with self._lock:
            current_epoch = self._current_epoch()
            if not current_epoch and not self._closing:
                self._healthy = False
                self._reason = (
                    "The event session changed; reconnect to generate a new event callback."
                )
            return {
                "enabled": self._loop_started and not self._closing and current_epoch,
                "healthy": self._healthy,
                "last_event_at": self._last_event_at,
                "accepted": self._accepted,
                "duplicates": self._duplicates,
                "reason": self._reason,
            }

    def _run(self):
        try:
            with self._lock:
                if self._closing:
                    return
            self._server.serve_forever(poll_interval=0.05)
        except Exception:
            with self._lock:
                self._healthy = False
                self._reason = "The local event receiver is unavailable."
        finally:
            self._startup_complete.set()

    def start(self):
        with self._lock:
            if self._closing or self._closed:
                raise BackendError("Event receiver is closed.", 1503)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="heredity-onebot-events", daemon=True
                )
                self._thread.start()
            worker = self._thread
        if not self._startup_complete.wait(2.0) or not self._loop_started or not worker.is_alive():
            self.close()
            raise BackendError("Event receiver could not start.", 1503)

    def _stop(self):
        worker = self._thread
        if worker is not None and worker.is_alive():
            self._startup_complete.wait(2.0)
            if self._loop_started:
                self._server.shutdown()
        self._server.server_close()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=1.0)
        with self._lock:
            self._closed = True

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closing = True
            self._healthy = False
            self._reason = "The local event receiver is closed."
            if self._stop_thread is None:
                self._stop_thread = threading.Thread(
                    target=self._stop, name="heredity-onebot-events-stop", daemon=True
                )
                self._stop_thread.start()
            stopper = self._stop_thread
        if stopper is not threading.current_thread():
            stopper.join(timeout=2.5)

    def _failed_delivery(self):
        with self._lock:
            if self._closing:
                return
            self._healthy = False
            self._reason = "A signed event could not be accepted for the bound account session."

    def _accept(self, event):
        with self._lock:
            if self._closing:
                raise BackendError("Event receiver is closed.", 1503)
        account = positive_id(event.get("self_id"), "self_id")
        if account != self._runtime.self_id:
            raise BackendError("Event account does not match this runtime.", 1400)
        result = self._runtime.ingest(event, session_epoch=self._epoch)
        if (
            type(result) is not dict or result.get("persisted") is not True
            or type(result.get("duplicate")) is not bool
        ):
            raise BackendError("Event persistence was not confirmed.", 1500)
        with self._lock:
            self._last_event_at = int(time.time())
            if result["duplicate"]:
                self._duplicates += 1
            else:
                self._accepted += 1
            if not self._closing:
                self._healthy = True
                self._reason = "Signed OneBot events have been accepted and persisted."


class _EventHandler(BaseHTTPRequestHandler):
    server_version = "Heredity-Events"
    sys_version = ""

    @property
    def receiver(self):
        return self.server.receiver

    def log_message(self, *_args):
        # The private receiver path and producer credentials must not enter logs.
        return

    def _json(self, status, value):
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(encoded)
        except OSError:
            pass

    def _failure(self, status, message, retcode=1400):
        self._json(status, {
            "status": "failed", "retcode": retcode, "data": None, "message": message
        })

    def do_GET(self):
        self._failure(405, "Only signed POST events are accepted.", 1405)

    do_PUT = do_GET
    do_DELETE = do_GET
    do_PATCH = do_GET
    do_HEAD = do_GET

    def do_POST(self):
        receiver = self.receiver
        if self.path != receiver._path:
            self._failure(404, "Event endpoint was not found.", 1404)
            return
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1:
            self._failure(400, "A single unambiguous Content-Length is required.")
            return
        if not _CONTENT_LENGTH.fullmatch(lengths[0].strip()):
            self._failure(400, "Content-Length is invalid.")
            return
        length = int(lengths[0])
        if length > MAX_EVENT_BYTES:
            self._failure(413, "Event body exceeds the 1 MiB limit.", 1413)
            return
        if len(self.headers.get_all("Content-Type", [])) != 1 or (
            self.headers.get_content_type() != "application/json"
        ):
            self._failure(415, "Event body must use application/json.", 1415)
            return
        signatures = self.headers.get_all("X-Signature", [])
        if not signatures:
            self._failure(401, "A OneBot event signature is required.", 1401)
            return
        match = _SIGNATURE.fullmatch(signatures[0]) if len(signatures) == 1 else None
        if match is None:
            self._failure(403, "OneBot event signature is invalid.", 1403)
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError
        except (OSError, ValueError):
            self._failure(400, "Event body could not be read.")
            return
        expected = hmac.new(receiver._secret.encode("ascii"), raw, hashlib.sha1).hexdigest()
        if not hmac.compare_digest(match.group(1).lower().encode("ascii"), expected.encode("ascii")):
            self._failure(403, "OneBot event signature is invalid.", 1403)
            return
        try:
            event = _decode_json(raw.decode("utf-8"))
            if type(event) is not dict:
                raise ValueError
        except (ValueError, UnicodeError, TypeError, RecursionError):
            receiver._failed_delivery()
            self._failure(400, "Event body must be a finite JSON object.")
            return
        try:
            receiver._accept(event)
        except BackendError as error:
            receiver._failed_delivery()
            if error.retcode == 1409:
                self._failure(409, "Event session epoch is stale or missing.", 1409)
            elif type(error.retcode) is int and 1400 <= error.retcode < 1500:
                self._failure(400, "Event account or event data does not match this runtime.", error.retcode)
            else:
                self._failure(503, "Event could not be persisted for this session.", 1500)
            return
        except Exception:
            receiver._failed_delivery()
            self._failure(503, "Event processing failed.", 1500)
            return
        self._json(200, {})
