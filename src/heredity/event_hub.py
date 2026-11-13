"""Account-isolated, bounded event fan-out without per-subscriber queues."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
import json
import math
import threading
import time
from typing import Any

MAX_EVENT_BYTES = 1024 * 1024
MAX_EVENT_DEPTH = 64


class EventGapError(ValueError):
    """The requested cursor cannot establish a complete retained replay."""

    def __init__(self, oldest_sequence: int, latest_sequence: int) -> None:
        self.oldest_sequence = oldest_sequence
        self.latest_sequence = latest_sequence
        super().__init__(
            "Event replay is unavailable for this cursor; "
            f"retained sequences span {oldest_sequence}..{latest_sequence}."
        )


class EventHubClosed(RuntimeError):
    """Publication was attempted after an event hub closed."""


def _json_snapshot(event: dict[str, Any]) -> dict[str, Any]:
    """Validate ordinary JSON types, then create an independent bounded copy."""
    active_containers: set[int] = set()
    nodes = 0

    def validate(value: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_EVENT_BYTES or depth > MAX_EVENT_DEPTH:
            raise ValueError("Event exceeds the JSON complexity limit.")
        kind = type(value)
        if kind is str:
            if len(value) > MAX_EVENT_BYTES:
                raise ValueError("Event exceeds the 1 MiB limit.")
            return
        if value is None or kind in {int, bool}:
            return
        if kind is float:
            if not math.isfinite(value):
                raise ValueError("Event numbers must be finite.")
            return
        if kind not in {dict, list}:
            raise ValueError("Events must contain only ordinary JSON values.")
        identity = id(value)
        if identity in active_containers:
            raise ValueError("Event contains a circular reference.")
        active_containers.add(identity)
        try:
            if kind is dict:
                for key, child in value.items():
                    if type(key) is not str:
                        raise ValueError("Event object keys must be strings.")
                    if len(key) > MAX_EVENT_BYTES:
                        raise ValueError("Event exceeds the 1 MiB limit.")
                    validate(child, depth + 1)
            else:
                for child in value:
                    validate(child, depth + 1)
        finally:
            active_containers.remove(identity)

    try:
        validate(event, 0)
        encoder = json.JSONEncoder(
            ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        size = 0
        pieces: list[str] = []
        for piece in encoder.iterencode(event):
            size += len(piece.encode("utf-8"))
            if size > MAX_EVENT_BYTES:
                raise ValueError("Event exceeds the 1 MiB limit.")
            pieces.append(piece)
        return json.loads("".join(pieces))
    except (RecursionError, RuntimeError, UnicodeError) as exc:
        raise ValueError("Event is not a stable, valid JSON object.") from exc


class EventHub:
    """A single account's in-memory reconnect window.

    Callers commit durable events before publishing their store sequence here.
    Sequences increase strictly but need not be consecutive: a shared store may
    assign intervening values to other accounts. Readers own their cursors.
    """

    def __init__(self, self_id: int, capacity: int = 256) -> None:
        if type(self_id) is not int or self_id <= 0:
            raise ValueError("Event hub self_id must be a positive integer.")
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("Event hub capacity must be a positive integer.")
        self._self_id = self_id
        self._capacity = capacity
        self._condition = threading.Condition()
        self._records: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._latest_sequence = 0
        self._evicted = False
        self._closed = False

    @property
    def self_id(self) -> int:
        return self._self_id

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def latest_sequence(self) -> int:
        with self._condition:
            return self._latest_sequence

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def publish(self, event: dict, sequence: int) -> None:
        if type(sequence) is not int or sequence <= 0:
            raise ValueError("Event sequence must be a positive integer.")
        if type(event) is not dict:
            raise ValueError("Event must be a JSON object.")
        if type(event.get("self_id")) is not int or event["self_id"] != self.self_id:
            raise ValueError("Event self_id does not match this account.")
        with self._condition:
            if self._closed:
                raise EventHubClosed("Event hub is closed.")
        snapshot = _json_snapshot(event)
        if type(snapshot.get("self_id")) is not int or snapshot["self_id"] != self.self_id:
            raise ValueError("Event self_id changed while creating its snapshot.")
        with self._condition:
            if self._closed:
                raise EventHubClosed("Event hub is closed.")
            if sequence <= self._latest_sequence:
                raise ValueError("Event sequences must increase strictly.")
            if len(self._records) == self.capacity:
                self._evicted = True
            self._records.append({"sequence": sequence, "event": snapshot})
            self._latest_sequence = sequence
            self._condition.notify_all()

    def _validate_cursor(self, after: int) -> None:
        if after > self._latest_sequence:
            raise ValueError("Event cursor is ahead of the latest sequence.")
        if after == self._latest_sequence:
            return
        if after == 0 and not self._evicted:
            return
        if after > 0 and any(record["sequence"] == after for record in self._records):
            return
        oldest = self._records[0]["sequence"] if self._records else 0
        raise EventGapError(oldest, self._latest_sequence)

    def read(self, after: int, timeout: float = 0, limit: int = 100) -> list[dict]:
        """Return snapshots after a valid cursor, waiting for at most timeout.

        Closing does not discard retained events. A reader at the latest
        sequence returns immediately once closed, while a stale cursor continues
        to raise an explicit gap instead of silently losing history.
        """
        if type(after) is not int or after < 0:
            raise ValueError("Event cursor must be a nonnegative integer.")
        if type(timeout) not in {int, float}:
            raise ValueError("Event read timeout must be finite and nonnegative.")
        try:
            timeout = float(timeout)
        except OverflowError as exc:
            raise ValueError("Event read timeout must be finite and nonnegative.") from exc
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("Event read timeout must be finite and nonnegative.")
        if type(limit) is not int or limit <= 0:
            raise ValueError("Event read limit must be a positive integer.")
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                self._validate_cursor(after)
                records = []
                for record in self._records:
                    if record["sequence"] > after:
                        records.append(deepcopy(record))
                        if len(records) >= limit:
                            break
                if records or self._closed:
                    return records
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._condition.wait(min(remaining, threading.TIMEOUT_MAX))

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
