"""HTTP network transports and interface-local event replay, owned by the host."""

from collections import deque
from copy import deepcopy
from http.client import HTTPException
import json
import math
import threading
import time
from urllib import error, request

from nyxion_heredity_sdk import BackendError
from nyxion_heredity_sdk.onebot import _json_copy

from .event_hub import EventGapError
from .onebot import _NoRedirect


class InterfaceEventHub:
    """A bounded replay window for one network interface, never a plugin bus."""

    def __init__(self, self_id=0, capacity=256):
        self.self_id = self_id
        self._capacity = capacity
        self._condition = threading.Condition()
        self._records = deque(maxlen=capacity)
        self._sequence = 0
        self.closed = False

    @property
    def latest_sequence(self):
        with self._condition:
            return self._sequence

    def publish(self, event):
        event = _json_copy(event, "network event")
        with self._condition:
            if self.closed:
                return
            self._sequence += 1
            self._records.append({"sequence": self._sequence, "event": event})
            self._condition.notify_all()

    def read(self, after, timeout=0, limit=100):
        if (type(after) is not int or after < 0 or type(limit) is not int or limit < 1
                or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Invalid interface event cursor or read deadline.")
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if after > self._sequence:
                    raise ValueError("Network event cursor is ahead of this interface.")
                if self._records and after < self._records[0]["sequence"] - 1:
                    raise EventGapError(self._records[0]["sequence"], self._sequence)
                values = [deepcopy(item) for item in self._records
                          if item["sequence"] > after][:limit]
                if values or self.closed or time.monotonic() >= deadline:
                    return values
                self._condition.wait(max(0, deadline - time.monotonic()))

    def close(self):
        with self._condition:
            self.closed = True
            self._condition.notify_all()


class HTTPClientTransport:
    """One explicit destination, no redirect, proxy or automatic action replay."""

    def __init__(self, *, url, token="", secret="", on_message):
        self.url, self.token, self.secret, self.on_message = url, token, secret, on_message
        self._opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
        self._closed = False

    def start(self):
        self._closed = False

    def stop(self):
        self._closed = True

    def _post(self, payload, timeout, *, event=False):
        if self._closed:
            raise BackendError("Network interface is stopped.", 1503)
        raw = json.dumps(_json_copy(payload, "network request"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "NyXion-Heredity"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if event:
            headers["X-Self-ID"] = str(payload.get("self_id", 0))
            if self.secret:
                import hashlib
                import hmac
                headers["X-Signature"] = "sha1=" + hmac.new(
                    self.secret.encode(), raw, hashlib.sha1).hexdigest()
        try:
            with self._opener.open(request.Request(
                    self.url, data=raw, headers=headers, method="POST"), timeout=timeout) as response:
                data = response.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024:
                    raise ValueError("oversized HTTP response")
                if event:
                    return None
                result = json.loads(data)
                if (type(result) is not dict or result.get("status") not in {"ok", "failed"}
                        or type(result.get("retcode")) is not int):
                    raise ValueError("invalid OneBot response")
                return _json_copy(result, "network response")
        except error.HTTPError as failure:
            failure.close()
            raise BackendError("The configured HTTP interface rejected the request.", 1502,
                               details={"action_outcome": "unknown", "retry_safe": False}) from None
        except (OSError, HTTPException, ValueError, RecursionError):
            raise BackendError("The configured HTTP request failed; it was not replayed.", 1502,
                               details={"action_outcome": "unknown", "retry_safe": False}) from None

    def call_remote(self, action, params, timeout=5):
        payload = {"action": action, "params": params}
        self.on_message("action", payload)
        result = self._post(payload, timeout)
        self.on_message("response", result)
        return result

    def publish(self, event):
        self._post(event, 3, event=True)
        self.on_message("event", event)

    def snapshot(self):
        return {"state": "stopped" if self._closed else "running",
                "endpoint": self.url, "actual_port": None, "error": None}
