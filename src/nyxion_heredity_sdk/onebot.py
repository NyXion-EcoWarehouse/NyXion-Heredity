"""Independent OneBot message validation and CQ string conversion."""

from __future__ import annotations

from copy import deepcopy
import json
import math
import re
from typing import Any

from .errors import BackendError


MAX_JSON_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 64
MIN_MESSAGE_ID = -(2**31)
MAX_MESSAGE_ID = 2**31 - 1
_INTEGER = re.compile(r"[+-]?[0-9]+\Z")
_SEGMENT_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
_PARAMETER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _integer(value: Any, name: str) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if len(stripped) <= 128 and _INTEGER.fullmatch(stripped):
            try:
                return int(stripped)
            except ValueError:
                pass
    raise BackendError(f"{name} must be an integer.", 1400)


def positive_id(value: Any, name: str = "id") -> int:
    result = _integer(value, name)
    if result <= 0:
        raise BackendError(f"{name} must be a positive integer.", 1400)
    return result


def message_id(value: Any) -> int:
    result = _integer(value, "message_id")
    if result == 0 or not MIN_MESSAGE_ID <= result <= MAX_MESSAGE_ID:
        raise BackendError("message_id must be a nonzero signed 32-bit integer.", 1400)
    return result


def _json_copy(value: Any, name: str = "value") -> Any:
    """Validate bounded ordinary JSON values before copying or serialization."""

    ancestors: set[int] = set()

    def inspect(item: Any, depth: int) -> None:
        if depth > MAX_JSON_DEPTH:
            raise BackendError(f"{name} exceeds the JSON nesting limit.", 1400)
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise BackendError(f"{name} contains a non-finite number.", 1400)
            return
        if type(item) not in (dict, list):
            raise BackendError(f"{name} must contain only JSON values.", 1400)
        identity = id(item)
        if identity in ancestors:
            raise BackendError(f"{name} contains a circular reference.", 1400)
        ancestors.add(identity)
        try:
            if type(item) is dict:
                if any(type(key) is not str for key in item):
                    raise BackendError(f"{name} contains a non-string object key.", 1400)
                children = item.values()
            else:
                children = item
            for child in children:
                inspect(child, depth + 1)
        finally:
            ancestors.remove(identity)

    inspect(value, 0)
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
            raise BackendError(f"{name} exceeds the 1 MiB JSON size limit.", 1400)
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
        raise BackendError(f"{name} cannot be represented as finite JSON.", 1400) from None
    return deepcopy(value)


def _unescape(value: str, parameter: bool = False) -> str:
    entities = {"#91": "[", "#93": "]", "amp": "&"}
    if parameter:
        entities["#44"] = ","
    # A single substitution prevents escaped entity text from being decoded twice.
    return re.sub(
        r"&(amp|#91|#93|#44);",
        lambda match: entities.get(match.group(1), match.group(0)),
        value,
    )


def _escape(value: str, parameter: bool = False) -> str:
    value = value.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;")
    return value.replace(",", "&#44;") if parameter else value


def _validate_segment(segment: Any) -> dict[str, Any]:
    if type(segment) is not dict or set(segment) != {"type", "data"}:
        raise BackendError("Each message segment requires exactly type and data.", 1400)
    kind = segment["type"]
    data = segment["data"]
    if not isinstance(kind, str) or not _SEGMENT_TYPE.fullmatch(kind):
        raise BackendError("Message segment type is invalid.", 1400)
    if type(data) is not dict:
        raise BackendError("Message segment data must be an object.", 1400)
    if kind == "text":
        if not isinstance(data.get("text"), str) or data["text"] == "":
            raise BackendError("Text segments require nonempty string data.text.", 1400)
    elif kind == "reply":
        message_id(data.get("id"))
    return segment


def normalize_message(value: Any, auto_escape: bool = False) -> list[dict[str, Any]]:
    """Validate message structure while leaving extension semantics to adapters."""

    if type(auto_escape) is not bool:
        raise BackendError("auto_escape must be a boolean.", 1400)
    value = _json_copy(value, "message")
    if isinstance(value, str):
        if not value:
            raise BackendError("message must not be empty.", 1400)
        if auto_escape:
            return [{"type": "text", "data": {"text": value}}]
        segments: list[dict[str, Any]] = []
        position = 0
        while position < len(value):
            marker = value.find("[CQ:", position)
            if marker < 0:
                segments.append({"type": "text", "data": {"text": _unescape(value[position:])}})
                break
            if marker > position:
                segments.append(
                    {"type": "text", "data": {"text": _unescape(value[position:marker])}}
                )
            ending = value.find("]", marker + 4)
            if ending < 0:
                raise BackendError("CQ segment is missing its closing bracket.", 1400)
            parts = value[marker + 4 : ending].split(",")
            kind = parts[0]
            if not _SEGMENT_TYPE.fullmatch(kind):
                raise BackendError("CQ segment type is invalid.", 1400)
            data: dict[str, Any] = {}
            for parameter in parts[1:]:
                key, separator, field = parameter.partition("=")
                if not separator or not _PARAMETER_NAME.fullmatch(key) or key in data:
                    raise BackendError("CQ segment contains an invalid or duplicate parameter.", 1400)
                data[key] = _unescape(field, parameter=True)
            segments.append(_validate_segment({"type": kind, "data": data}))
            position = ending + 1
        return segments
    if type(value) is not list or not value:
        raise BackendError("message must be a nonempty string or segment array.", 1400)
    return [_validate_segment(segment) for segment in value]


def cq_string(segments: Any) -> str:
    """Encode validated segments; reject fields CQ cannot faithfully represent."""

    if type(segments) is not list:
        raise BackendError("CQ serialization requires a message segment array.", 1400)
    normalized = normalize_message(segments)
    output: list[str] = []
    for segment in normalized:
        kind, data = segment["type"], segment["data"]
        if kind == "text":
            if set(data) != {"text"}:
                raise BackendError("CQ text cannot represent additional segment fields.", 1400)
            output.append(_escape(data["text"]))
            continue
        parameters: list[str] = []
        for key, value in data.items():
            if not _PARAMETER_NAME.fullmatch(key):
                raise BackendError("CQ parameter name is invalid.", 1400)
            if type(value) in (dict, list):
                raise BackendError("CQ parameters cannot represent nested objects or arrays.", 1400)
            text = value if isinstance(value, str) else json.dumps(value, allow_nan=False)
            parameters.append(f"{key}={_escape(text, parameter=True)}")
        suffix = "," + ",".join(parameters) if parameters else ""
        output.append(f"[CQ:{kind}{suffix}]")
    return "".join(output)
