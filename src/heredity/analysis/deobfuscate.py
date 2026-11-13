"""Statically decode bounded string candidates and report only local origins."""

from __future__ import annotations

import base64
import binascii
from collections import deque
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
from urllib.parse import unquote_to_bytes

from .endpoints import endpoint_candidates

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_STRING_BYTES = 4096
MAX_CANDIDATES = 1000
MAX_WORK_UNITS = 10000
MAX_DEPTH = 2
_CHUNK_OVERLAP = 128
_HEX = frozenset("0123456789abcdefABCDEF")
_BASE64 = re.compile(rb"[A-Za-z0-9+/]+={0,2}\Z")
_PRINTABLE_RUN = re.compile(rb"[\x21-\x7e]+")
_UTF16_LE_RUN = re.compile(rb"(?:[\x20-\x7e]\x00){8,2048}")
_UTF16_BE_RUN = re.compile(rb"(?:\x00[\x20-\x7e]){8,2048}")
_LIMITATIONS = [
    "Static string decoding only; no execution, control-flow recovery or native decompilation.",
    "Archives, PE sections and DEX bytecode are not unpacked or decompiled.",
    "Only loopback URL origins are reported; they are not verified control APIs.",
    "Transformed offsets identify the original literal or string run, not decoded character positions.",
    "Only bounded whole-string Base64/hex and supported literal/percent escapes are decoded.",
    "Duplicate origin/decoding-chain pairs retain only their first evidence location.",
]


@dataclass(frozen=True)
class _Candidate:
    value: bytes
    chain: tuple[str, ...]
    byte_offset: int
    offset_kind: str = "literal_start"
    depth: int = 0


class _Budget:
    def __init__(self) -> None:
        self.work_units = 0
        self.candidates = 0
        self.limits_hit: set[str] = set()
        self.findings: list[dict] = []
        self._seen_findings: set[tuple[str, tuple[str, ...]]] = set()

    def spend(self) -> bool:
        if self.work_units >= MAX_WORK_UNITS:
            self.limits_hit.add("work_units")
            return False
        self.work_units += 1
        return True

    def candidate(self) -> bool:
        if self.candidates >= MAX_CANDIDATES:
            self.limits_hit.add("candidates")
            return False
        self.candidates += 1
        return True

    def add(self, origin: str, chain: tuple[str, ...], offset: int, kind: str) -> None:
        key = origin, chain
        if key in self._seen_findings:
            return
        if len(self.findings) >= MAX_CANDIDATES:
            self.limits_hit.add("findings")
            return
        self._seen_findings.add(key)
        self.findings.append(
            {
                "origin": origin,
                "decode_chain": list(chain),
                "byte_offset": offset,
                "offset_kind": kind,
                "control_api_verified": False,
            }
        )


def _reject_constant(value: str):
    raise ValueError("Non-finite JSON is not supported.")


def _artifact_kind(data: bytes) -> str:
    if data.startswith(b"MZ") and len(data) >= 64:
        pe_offset = struct.unpack_from("<I", data, 60)[0]
        if 64 <= pe_offset <= len(data) - 4 and data[pe_offset:pe_offset + 4] == b"PE\0\0":
            return "pe"
    if data.startswith(b"\x7fELF"):
        return "elf"
    if re.match(rb"dex\n[0-9]{3}\x00", data[:8]):
        return "dex"
    if _is_asar(data):
        return "asar"
    if not data:
        return "text"
    sample = data[:65536]
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            sample.decode("utf-16")
            return "utf16_text"
        except UnicodeError:
            return "unknown_binary"
    try:
        text = sample.decode("utf-8")
    except UnicodeError:
        return "unknown_binary"
    if all(character.isprintable() or character in "\t\r\n" for character in text):
        return "text"
    return "unknown_binary"


def _is_asar(data: bytes) -> bool:
    """Recognize a consistent Electron ASAR pickle header, without unpacking."""
    if len(data) < 16:
        return False
    first_size, header_size, payload_size, json_size = struct.unpack_from("<IIII", data)
    if (
        first_size != 4
        or not 8 <= header_size <= 65536
        or 8 + header_size > len(data)
        or header_size != ((payload_size + 3) // 4) * 4 + 4
        or not 4 <= payload_size <= header_size - 4
        or json_size > payload_size - 4
    ):
        return False
    try:
        header = json.loads(data[16:16 + json_size], parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        return False
    return isinstance(header, dict) and isinstance(header.get("files"), dict)


def _source_label(source_name: str) -> str:
    # A caller may provide a path or URL; retain only a short artifact label.
    label = str(source_name).replace("\\", "/").rsplit("/", 1)[-1]
    label = label.split("?", 1)[0].split("#", 1)[0]
    label = "".join(character for character in label if character.isprintable())
    return label[:128] or "<artifact>"


def _raw_findings(data: bytes, budget: _Budget) -> None:
    step = MAX_STRING_BYTES - _CHUNK_OVERLAP
    for offset in range(0, len(data), step):
        if not budget.spend() or len(budget.findings) >= MAX_CANDIDATES:
            if len(budget.findings) >= MAX_CANDIDATES:
                budget.limits_hit.add("findings")
            return
        chunk = data[offset:offset + MAX_STRING_BYTES]
        lowered = chunk.lower()
        final_chunk = offset + len(chunk) == len(data)
        for origin in endpoint_candidates(chunk):
            for encoding, stage in (
                ("ascii", "raw_ascii"),
                ("utf-16le", "raw_utf16le"),
                ("utf-16be", "raw_utf16be"),
            ):
                needle = origin.encode(encoding)
                position = lowered.find(needle)
                while position >= 0:
                    ending = position + len(needle)
                    if _origin_boundary(chunk, ending, encoding, final_chunk):
                        budget.add(origin, (stage,), offset + position, "origin_start")
                        break
                    position = lowered.find(needle, position + 1)


def _origin_boundary(data: bytes, ending: int, encoding: str, final_chunk: bool) -> bool:
    if ending == len(data):
        # An artificial chunk edge must not turn part of a port into a candidate.
        return final_chunk
    width = 1 if encoding == "ascii" else 2
    following = data[ending:ending + width]
    if len(following) != width:
        return False
    try:
        character = following.decode(encoding)
    except UnicodeError:
        return False
    return character in "/\t\n\v\f\r \0\"'?#:"


def _literal_candidates(data: bytes, budget: _Budget):
    index = 0
    while index < len(data):
        quote = data[index]
        if quote not in (34, 39, 96):
            index += 1
            continue
        opening = index
        index += 1
        beginning = index
        valid = True
        while index < len(data):
            current = data[index]
            if current == quote:
                length = index - beginning
                oversized = length > MAX_STRING_BYTES
                value = b"" if oversized else data[beginning:index]
                index += 1
                if oversized:
                    budget.limits_hit.add("string_bytes")
                elif valid and value and (quote != 96 or b"${" not in value):
                    yield _Candidate(value, ("literal",), opening)
                break
            if current == 0:
                valid = False
            if current in (10, 13) and quote != 96:
                index += 1
                break
            if current == 92:
                index += 2
            else:
                index += 1
        else:
            if len(data) - beginning > MAX_STRING_BYTES:
                budget.limits_hit.add("string_bytes")


def _source_candidates(data: bytes, budget: _Budget):
    yield from _literal_candidates(data, budget)
    for match in _PRINTABLE_RUN.finditer(data):
        if match.end() - match.start() > MAX_STRING_BYTES:
            if data.find(b"%", match.start(), match.end()) >= 0:
                budget.limits_hit.add("string_bytes")
            continue
        value = match.group()
        if b"%" not in value:
            continue
        yield _Candidate(value, ("ascii_run",), match.start(), "string_start")
    for pattern, encoding in (
        (_UTF16_LE_RUN, "utf-16le"),
        (_UTF16_BE_RUN, "utf-16be"),
    ):
        for match in pattern.finditer(data):
            yield _Candidate(
                match.group().decode(encoding).encode("ascii"),
                (encoding,),
                match.start(),
                "string_start",
            )


def _literal_escapes(value: bytes) -> bytes | None:
    if b"\\" not in value:
        return None
    try:
        text = value.decode("utf-8")
    except UnicodeError:
        return None
    result: list[str] = []
    index = 0
    escapes = {
        "\\": "\\", "'": "'", '"': '"', "/": "/", "`": "`",
        "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
    }
    while index < len(text):
        if text[index] != "\\":
            result.append(text[index])
            index += 1
            continue
        index += 1
        if index >= len(text):
            return None
        character = text[index]
        index += 1
        if character in escapes:
            result.append(escapes[character])
        elif character == "\n":
            continue
        elif character == "\r":
            if index < len(text) and text[index] == "\n":
                index += 1
        elif character == "0" and (index == len(text) or not text[index].isdigit()):
            result.append("\0")
        elif character in {"x", "u"}:
            count = 2 if character == "x" else 4
            if character == "u" and index < len(text) and text[index] == "{":
                closing = text.find("}", index + 1, index + 9)
                if closing < 0:
                    return None
                digits = text[index + 1:closing]
                index = closing + 1
                if not 1 <= len(digits) <= 6:
                    return None
            else:
                digits = text[index:index + count]
                index += count
                if len(digits) != count:
                    return None
            if not all(digit in _HEX for digit in digits):
                return None
            codepoint = int(digits, 16)
            if 0xD800 <= codepoint <= 0xDBFF:
                if text[index:index + 2] != "\\u":
                    return None
                low_digits = text[index + 2:index + 6]
                if len(low_digits) != 4 or any(digit not in _HEX for digit in low_digits):
                    return None
                low = int(low_digits, 16)
                if not 0xDC00 <= low <= 0xDFFF:
                    return None
                index += 6
                codepoint = 0x10000 + ((codepoint - 0xD800) << 10) + low - 0xDC00
            if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
                return None
            result.append(chr(codepoint))
        else:
            return None
    return "".join(result).encode("utf-8")


def _percent(value: bytes) -> bytes | None:
    if b"%" not in value:
        return None
    for match in re.finditer(b"%", value):
        digits = value[match.start() + 1:match.start() + 3]
        if len(digits) != 2 or any(chr(digit) not in _HEX for digit in digits):
            return None
    return unquote_to_bytes(value)


def _base64(value: bytes) -> bytes | None:
    if len(value) < 8 or len(value) % 4 or not _BASE64.fullmatch(value):
        return None
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        return None
    return decoded if base64.b64encode(decoded) == value else None


def _hex(value: bytes) -> bytes | None:
    digits = value[2:] if value.startswith((b"0x", b"0X")) else value
    if len(digits) < 8 or len(digits) % 2:
        return None
    try:
        text = digits.decode("ascii")
    except UnicodeError:
        return None
    if any(character not in _HEX for character in text):
        return None
    return bytes.fromhex(text)


def _process_candidate(seed: _Candidate, budget: _Budget) -> bool:
    queue = deque([seed])
    seen: set[tuple[bytes, tuple[str, ...]]] = set()
    while queue:
        candidate = queue.popleft()
        key = candidate.value, candidate.chain
        if key in seen:
            continue
        seen.add(key)
        if not budget.candidate() or not budget.spend():
            return False
        for origin in endpoint_candidates(candidate.value):
            budget.add(
                origin, candidate.chain, candidate.byte_offset, candidate.offset_kind
            )
        at_depth_limit = candidate.depth >= MAX_DEPTH
        for stage, decoder in (
            ("literal_escapes", _literal_escapes),
            ("percent", _percent),
            ("base64", _base64),
            ("hex", _hex),
        ):
            if not budget.spend():
                return False
            decoded = decoder(candidate.value)
            if decoded is None or decoded == candidate.value or not decoded:
                continue
            if len(decoded) > MAX_STRING_BYTES:
                budget.limits_hit.add("string_bytes")
                continue
            if at_depth_limit:
                budget.limits_hit.add("depth")
                continue
            queue.append(
                _Candidate(
                    decoded,
                    (*candidate.chain, stage),
                    candidate.byte_offset,
                    candidate.offset_kind,
                    candidate.depth + 1,
                )
            )
    return True


def analyze_bytes(data: bytes, source_name: str = "<memory>") -> dict:
    """Analyze at most 8 MiB without executing code or revealing decoded strings."""
    if not isinstance(data, bytes):
        raise TypeError("Artifact input must be bytes.")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("Artifact exceeds the 8 MiB analysis limit.")
    budget = _Budget()
    _raw_findings(data, budget)
    if "work_units" not in budget.limits_hit:
        for candidate in _source_candidates(data, budget):
            if not _process_candidate(candidate, budget):
                break
    return {
        "source": _source_label(source_name),
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "artifact_kind": _artifact_kind(data),
        "bytes_analyzed": len(data),
        "findings": budget.findings,
        "truncated": bool(budget.limits_hit),
        "limits_hit": sorted(budget.limits_hit),
        "work_units": budget.work_units,
        "candidates_analyzed": budget.candidates,
        "static_only": True,
        "control_api_verified": False,
        "limitations": list(_LIMITATIONS),
    }


def analyze_file(path: Path) -> dict:
    """Read one ordinary offline file through a bounded read-only descriptor."""
    path = Path(path)
    resolved = path.resolve(strict=True)
    if os.name != "nt" and resolved.parts[1:2] in {("proc",), ("sys",), ("dev",)}:
        raise ValueError("Live procfs, sysfs and device paths are not offline artifacts.")
    before_open = resolved.stat()
    if not stat.S_ISREG(before_open.st_mode):
        raise ValueError("Offline analysis requires a regular file.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(resolved, flags)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Offline analysis requires a regular file.")
        if before.st_size > MAX_FILE_BYTES:
            raise ValueError("Artifact exceeds the 8 MiB analysis limit.")
        data = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Artifact changed during analysis; discard this result.")
    return analyze_bytes(data, source_name=path.name)
