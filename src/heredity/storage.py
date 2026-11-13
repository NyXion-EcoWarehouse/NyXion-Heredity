"""Account-scoped durable messages and event ingress using standard SQLite."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

from .backends.base import BackendError
from .messages import MAX_MESSAGE_ID, _json_copy, message_id as validate_message_id, positive_id


_SQLITE_MAX_INTEGER = 2**63 - 1
_SCHEMA_VERSION = 1
_SCHEMA_COLUMNS = {
    "messages": {"record_order", "self_id", "message_id", "message_type", "conversation_id", "record"},
    "events": {"sequence", "self_id", "event_key", "event"},
    "message_tombstones": {"self_id", "message_id"},
    "message_counters": {"self_id", "next_id"},
}


def _account(value: Any) -> int:
    result = positive_id(value, "self_id")
    if result > _SQLITE_MAX_INTEGER:
        raise BackendError("self_id exceeds the supported account identifier range.", 1400)
    return result


def _limit(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= 1000:
        raise BackendError("limit must be an integer between 1 and 1000.", 1400)
    return value


def _event_key(value: Any) -> str | None:
    if value is not None and (
        not isinstance(value, str) or not value or len(value) > 512
    ):
        raise BackendError("event_key must be a nonempty string of at most 512 characters.", 1400)
    if value is not None:
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise BackendError("event_key must contain valid Unicode.", 1400) from None
    return value


def _serialized(value: Any, name: str) -> tuple[dict[str, Any], str]:
    if type(value) is not dict:
        raise BackendError(f"{name} must be an object.", 1400)
    copied = _json_copy(value, name)
    encoded = json.dumps(copied, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
    return copied, encoded


def _message_record(self_id: int, identifier: int, value: Any) -> tuple[str, str, str]:
    record, encoded = _serialized(value, "message record")
    if "self_id" in record and _account(record["self_id"]) != self_id:
        raise BackendError("Message record belongs to a different account.", 1400)
    if "message_id" in record and validate_message_id(record["message_id"]) != identifier:
        raise BackendError("Message record identifier does not match its storage key.", 1400)
    kind = record.get("message_type")
    if kind not in ("private", "group"):
        raise BackendError("Message record requires message_type private or group.", 1400)
    target_field = "group_id" if kind == "group" else "user_id"
    target = positive_id(record.get(target_field), target_field)
    return kind, str(target), encoded


class MessageStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._lock = threading.RLock()
        self._closed = False
        try:
            if str(path) != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
            self._db.execute("PRAGMA busy_timeout = 5000")
            self._initialize()
        except BaseException as error:
            connection = getattr(self, "_db", None)
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            self._closed = True
            if isinstance(error, (sqlite3.Error, OSError)):
                raise BackendError("Message store could not be opened.", 1500) from error
            raise

    def _initialize(self) -> None:
        with self._transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_schema WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
            if version not in (0, _SCHEMA_VERSION):
                raise BackendError("Message store has an unsupported schema version.", 1500)
            if version == 0:
                if tables:
                    raise BackendError("Existing unversioned message store is unsupported.", 1500)
                db.execute(
                    "CREATE TABLE messages ("
                    "record_order INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "self_id INTEGER NOT NULL, message_id INTEGER NOT NULL,"
                    "message_type TEXT NOT NULL, conversation_id TEXT NOT NULL,"
                    "record TEXT NOT NULL, UNIQUE(self_id, message_id))"
                )
                db.execute(
                    "CREATE TABLE events ("
                    "sequence INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "self_id INTEGER NOT NULL, event_key TEXT, event TEXT NOT NULL,"
                    "UNIQUE(self_id, event_key))"
                )
                db.execute(
                    "CREATE TABLE message_tombstones ("
                    "self_id INTEGER NOT NULL, message_id INTEGER NOT NULL,"
                    "PRIMARY KEY(self_id, message_id))"
                )
                db.execute(
                    "CREATE TABLE message_counters ("
                    "self_id INTEGER PRIMARY KEY, next_id INTEGER NOT NULL)"
                )
                db.execute(
                    "CREATE INDEX messages_history "
                    "ON messages(self_id, message_type, conversation_id, record_order)"
                )
                db.execute("CREATE INDEX events_account ON events(self_id, sequence)")
                db.execute("PRAGMA user_version = 1")
            for table, columns in _SCHEMA_COLUMNS.items():
                table_info = list(db.execute(f"PRAGMA table_info({table})"))
                actual = {row[1] for row in table_info}
                if actual != columns:
                    raise BackendError("Message store schema is incomplete or incompatible.", 1500)
                primary_key = tuple(
                    row[1] for row in sorted(table_info, key=lambda row: row[5]) if row[5]
                )
                expected_key = {
                    "messages": ("record_order",),
                    "events": ("sequence",),
                    "message_tombstones": ("self_id", "message_id"),
                    "message_counters": ("self_id",),
                }[table]
                if primary_key != expected_key:
                    raise BackendError("Message store primary key is incompatible.", 1500)
            for table, expected_unique in (
                ("messages", ("self_id", "message_id")),
                ("events", ("self_id", "event_key")),
            ):
                unique_indexes = db.execute(
                    'SELECT name FROM pragma_index_list(?) WHERE "unique" = 1', (table,)
                ).fetchall()
                unique_columns = {
                    tuple(row[2] for row in db.execute("SELECT * FROM pragma_index_info(?)", (name,)))
                    for (name,) in unique_indexes
                }
                if expected_unique not in unique_columns:
                    raise BackendError("Message store uniqueness constraint is missing.", 1500)

    @contextmanager
    def _access(self):
        with self._lock:
            if self._closed:
                raise BackendError("Message store is closed.", 1500)
            try:
                yield self._db
            except sqlite3.Error as error:
                raise BackendError("Message store operation failed.", 1500) from error

    @contextmanager
    def _transaction(self):
        with self._access() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                try:
                    db.rollback()
                except sqlite3.Error:
                    pass
                raise

    @staticmethod
    def _put(db, account: int, identifier: int, metadata: tuple[str, str, str]) -> None:
        kind, conversation, encoded = metadata
        db.execute(
            "INSERT INTO messages(self_id, message_id, message_type, conversation_id, record) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(self_id, message_id) DO UPDATE SET "
            "message_type = excluded.message_type, conversation_id = excluded.conversation_id, "
            "record = excluded.record",
            (account, identifier, kind, conversation, encoded),
        )

    @staticmethod
    def _tombstone(db, account: int, identifier: int) -> None:
        db.execute(
            "INSERT INTO message_tombstones(self_id, message_id) VALUES (?, ?) "
            "ON CONFLICT(self_id, message_id) DO NOTHING",
            (account, identifier),
        )

    def put_message(self, self_id, message_id, record: dict) -> None:
        account, identifier = _account(self_id), validate_message_id(message_id)
        metadata = _message_record(account, identifier, record)
        with self._transaction() as db:
            self._put(db, account, identifier, metadata)

    def get_message(self, self_id, message_id, include_deleted: bool = False) -> dict | None:
        account, identifier = _account(self_id), validate_message_id(message_id)
        if type(include_deleted) is not bool:
            raise BackendError("include_deleted must be a boolean.", 1400)
        condition = "" if include_deleted else (
            " AND NOT EXISTS (SELECT 1 FROM message_tombstones t "
            "WHERE t.self_id = m.self_id AND t.message_id = m.message_id)"
        )
        with self._access() as db:
            row = db.execute(
                "SELECT m.record FROM messages m WHERE m.self_id = ? AND m.message_id = ?"
                + condition,
                (account, identifier),
            ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def mark_deleted(self, self_id, message_id) -> bool:
        account, identifier = _account(self_id), validate_message_id(message_id)
        with self._transaction() as db:
            exists = db.execute(
                "SELECT 1 FROM messages WHERE self_id = ? AND message_id = ?",
                (account, identifier),
            ).fetchone()
            self._tombstone(db, account, identifier)
        return exists is not None

    def is_deleted(self, self_id, message_id) -> bool:
        account, identifier = _account(self_id), validate_message_id(message_id)
        with self._access() as db:
            return db.execute(
                "SELECT 1 FROM message_tombstones WHERE self_id = ? AND message_id = ?",
                (account, identifier),
            ).fetchone() is not None

    def history(
        self, self_id, message_type, conversation_id, limit=20, before_message_id=None
    ) -> list[dict]:
        account = _account(self_id)
        if message_type not in ("private", "group"):
            raise BackendError("message_type must be private or group.", 1400)
        conversation = str(positive_id(conversation_id, "conversation_id"))
        count = _limit(limit)
        before = (
            None if before_message_id is None else validate_message_id(before_message_id)
        )
        with self._access() as db:
            condition, parameters = "", [account, message_type, conversation]
            if before is not None:
                anchor = db.execute(
                    "SELECT record_order FROM messages WHERE self_id = ? AND message_id = ?",
                    (account, before),
                ).fetchone()
                if anchor is None:
                    return []
                condition = " AND m.record_order < ?"
                parameters.append(anchor[0])
            parameters.append(count)
            rows = db.execute(
                "SELECT m.record FROM messages m "
                "WHERE m.self_id = ? AND m.message_type = ? AND m.conversation_id = ?"
                + condition
                + " AND NOT EXISTS (SELECT 1 FROM message_tombstones t "
                "WHERE t.self_id = m.self_id AND t.message_id = m.message_id) "
                "ORDER BY m.record_order DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def append_event(self, self_id, event: dict, event_key: str | None = None) -> int | None:
        return self.commit_event(self_id, event, event_key)

    def commit_event(
        self,
        self_id,
        event: dict,
        event_key: str | None = None,
        message_record: dict | None = None,
        deleted_message_id=None,
        strict_duplicate=False,
    ) -> int | None:
        """Commit an event and optional message or recall state atomically."""

        account, key = _account(self_id), _event_key(event_key)
        copied, encoded = _serialized(event, "event")
        if "self_id" in copied and _account(copied["self_id"]) != account:
            raise BackendError("Event belongs to a different account.", 1400)
        metadata = None
        identifier = None
        if message_record is not None:
            if type(message_record) is not dict:
                raise BackendError("message_record must be an object.", 1400)
            identifier = validate_message_id(message_record.get("message_id"))
            metadata = _message_record(account, identifier, message_record)
        deleted = None if deleted_message_id is None else validate_message_id(deleted_message_id)
        with self._transaction() as db:
            inserted = db.execute(
                "INSERT INTO events(self_id, event_key, event) VALUES (?, ?, ?) "
                "ON CONFLICT(self_id, event_key) DO NOTHING",
                (account, key, encoded),
            )
            if inserted.rowcount == 0:
                if strict_duplicate and key is not None:
                    previous = db.execute(
                        "SELECT event FROM events WHERE self_id=? AND event_key=?",
                        (account, key),
                    ).fetchone()
                    old = json.loads(previous[0])
                    # Host provenance changes when a durable event is replayed
                    # in a new session epoch; the platform content must not.
                    old.pop("_heredity", None)
                    current = dict(copied)
                    current.pop("_heredity", None)
                    if old != current:
                        raise BackendError("Event ID conflicts with committed content.", 1409)
                return None
            sequence = inserted.lastrowid
            if metadata is not None:
                self._put(db, account, identifier, metadata)
            if deleted is not None:
                self._tombstone(db, account, deleted)
            return sequence

    def events_after(self, self_id, after=0, limit=100) -> list[dict]:
        account, count = _account(self_id), _limit(limit)
        if type(after) is not int or not 0 <= after <= _SQLITE_MAX_INTEGER:
            raise BackendError("after must be a nonnegative event sequence.", 1400)
        with self._access() as db:
            rows = db.execute(
                "SELECT sequence, event FROM events WHERE self_id = ? AND sequence > ? "
                "ORDER BY sequence ASC LIMIT ?",
                (account, after, count),
            ).fetchall()
        return [{"sequence": row[0], "event": json.loads(row[1])} for row in rows]

    def allocate_message_id(self, self_id) -> int:
        """Allocate durable positive IDs for the isolated demonstration backend."""

        account = _account(self_id)
        with self._transaction() as db:
            row = db.execute(
                "SELECT next_id FROM message_counters WHERE self_id = ?", (account,)
            ).fetchone()
            largest = db.execute(
                "SELECT MAX(message_id) FROM ("
                "SELECT message_id FROM messages WHERE self_id = ? AND message_id > 0 "
                "UNION ALL SELECT message_id FROM message_tombstones "
                "WHERE self_id = ? AND message_id > 0)",
                (account, account),
            ).fetchone()[0]
            identifier = max(row[0] if row else 1, (largest or 0) + 1)
            if identifier > MAX_MESSAGE_ID:
                raise BackendError("Demonstration message identifier range is exhausted.", 1500)
            db.execute(
                "INSERT INTO message_counters(self_id, next_id) VALUES (?, ?) "
                "ON CONFLICT(self_id) DO UPDATE SET next_id = excluded.next_id",
                (account, identifier + 1),
            )
        return identifier

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True
