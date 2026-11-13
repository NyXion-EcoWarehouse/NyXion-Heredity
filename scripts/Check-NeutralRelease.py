"""Verify core release integrity and package independence without importing artifacts."""

import argparse
import ast
import base64
from collections import Counter
import csv
from email.parser import BytesParser
import hashlib
from io import BytesIO, StringIO
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile
import zipfile


MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_ENTRY_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
PRIVATE_PARTS = {".env", ".heredity", ".venv", ".git", "__pycache__", "node_modules"}
PRIVATE_NAMES = {"ui-password.txt"}
PRIVATE_SUFFIXES = {".pyc", ".key", ".sqlite", ".sqlite3", ".db", ".log", ".dmp"}
NATIVE_SUFFIXES = {".node", ".dll", ".so", ".dylib", ".exe"}
PACKAGE_NAMES = {"host": "nyxion-heredity", "sdk": "nyxion-heredity-sdk"}
PACKAGE_ROOTS = {"host": "heredity", "sdk": "nyxion_heredity_sdk"}


def unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("JSON contains duplicate keys.")
        result[key] = value
    return result


def safe_name(name):
    if (not isinstance(name, str) or not name or "\\" in name or ":" in name
            or PurePosixPath(name).is_absolute()
            or any(part in {"", ".", ".."} for part in name.split("/"))):
        raise ValueError("Unsafe artifact path.")
    return PurePosixPath(name)


def entry_allowed(name):
    path = safe_name(name)
    if (set(path.parts) & PRIVATE_PARTS or path.name in PRIVATE_NAMES
            or path.suffix.lower() in PRIVATE_SUFFIXES | NATIVE_SUFFIXES):
        raise ValueError("Private state or a native executable reached the release.")
    return path


def read_file(path):
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Artifact must be a bounded regular file.")
    return path.read_bytes()


def wheel_kind(name):
    base = PurePosixPath(name).name
    for prefix, kind in (("nyxion_heredity_sdk-", "sdk"), ("nyxion_heredity-", "host")):
        if base.startswith(prefix) and base.endswith(".whl"):
            return kind
    raise ValueError("Unexpected distribution in the core release.")


def check_imports(raw, kind):
    allowed = set(sys.stdlib_module_names) | {PACKAGE_ROOTS[kind]}
    if kind == "host":
        allowed |= {"nyxion_heredity_sdk", "websockets", "wasmtime"}
    tree = ast.parse(raw.decode("utf-8-sig"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = [alias.name.split(".", 1)[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            roots = [(node.module or "").split(".", 1)[0]]
        else:
            continue
        if any(root not in allowed for root in roots):
            raise ValueError("A core or SDK source imports an undeclared implementation package.")


def check_wheel(raw, kind):
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Wheel contains duplicate members.")
        total = 0
        for info in archive.infolist():
            if info.is_dir():
                continue
            entry_allowed(info.filename)
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("Wheel contains a link.")
            total += info.file_size
            if info.file_size > MAX_ENTRY_BYTES or total > MAX_ARCHIVE_BYTES:
                raise ValueError("Wheel expansion exceeds its bound.")
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = BytesParser().parsebytes(archive.read(metadata_name))
        if metadata.get("Name") != PACKAGE_NAMES[kind]:
            raise ValueError("Unexpected wheel package metadata.")
        roots = {
            name.split("/", 1)[0] for name in names if "/" in name
            and not name.split("/", 1)[0].endswith(".dist-info")
        }
        if roots != {PACKAGE_ROOTS[kind]}:
            raise ValueError("Wheel contains a bundled implementation or dependency.")
        if kind == "sdk" and metadata.get_all("Requires-Dist"):
            raise ValueError("The SDK must have no runtime dependencies.")
        if kind == "host" and "heredity/ui/index.html" not in names:
            raise ValueError("The core UI is missing.")
        record_name = next(name for name in names if name.endswith(".dist-info/RECORD"))
        records = {}
        for name, digest, size in csv.reader(StringIO(archive.read(record_name).decode("utf-8"))):
            if name in records:
                raise ValueError("Wheel RECORD contains duplicate paths.")
            records[name] = (digest, size)
            entry_allowed(name)
            payload = archive.read(name)
            if digest:
                algorithm, expected = digest.split("=", 1)
                actual = base64.urlsafe_b64encode(
                    hashlib.new(algorithm, payload).digest()).rstrip(b"=").decode("ascii")
                if actual != expected or len(payload) != int(size):
                    raise ValueError("Wheel RECORD digest or size does not match.")
        if set(records) != {name for name in names if not name.endswith("/")}:
            raise ValueError("Wheel RECORD does not cover every file.")
        for name in names:
            if name.endswith(".py"):
                check_imports(archive.read(name), kind)
        return len(names)


def check_archive(raw, kind=None, depth=0):
    if depth > 3:
        raise ValueError("Nested archive depth exceeded.")
    nested = 0
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Archive contains duplicate members.")
        total = 0
        for info in archive.infolist():
            if info.is_dir():
                continue
            path = entry_allowed(info.filename)
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("Archive contains a link.")
            total += info.file_size
            if info.file_size > MAX_ENTRY_BYTES or total > MAX_ARCHIVE_BYTES:
                raise ValueError("Archive expansion exceeds its bound.")
            payload = archive.read(info)
            if path.suffix == ".whl":
                check_wheel(payload, wheel_kind(path.name))
                nested += 1
            elif path.suffix == ".zip":
                nested += check_archive(payload, depth=depth + 1)
            elif kind == "host" and info.filename.startswith("src/heredity/") and path.suffix == ".py":
                check_imports(payload, "host")
        return nested


def check_source_archive(raw):
    with tarfile.open(fileobj=BytesIO(raw), mode="r:gz") as archive:
        total = 0
        for member in archive:
            name = member.name.rstrip("/")
            if not name:
                continue
            entry_allowed(name)
            if not (member.isdir() or member.isfile()):
                raise ValueError("Source archive contains a non-regular member.")
            total += member.size
            if member.size > MAX_ENTRY_BYTES or total > MAX_ARCHIVE_BYTES:
                raise ValueError("Source archive expansion exceeds its bound.")
            if member.isfile() and name.endswith(".py"):
                check_imports(archive.extractfile(member).read(), "sdk")


def check_release(directory):
    root = Path(directory).resolve(strict=True)
    report = json.loads(read_file(root / "release.json"), object_pairs_hook=unique_object)
    if report.get("adapters_bundled") is not False or report.get("runtime_state_included") is not False:
        raise ValueError("Release boundary declarations are invalid.")
    expected = {}
    roles = Counter()
    nested = 0
    records = report.get("artifacts")
    if not isinstance(records, list) or not records:
        raise ValueError("Release manifest is empty.")
    for record in records:
        if type(record) is not dict or set(record) != {"file", "bytes", "sha256"}:
            raise ValueError("Invalid artifact record.")
        name, size, digest = record["file"], record["bytes"], record["sha256"]
        path = entry_allowed(name)
        if name in expected or type(size) is not int or size < 0:
            raise ValueError("Invalid or duplicate artifact size.")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid artifact digest.")
        physical = root.joinpath(*path.parts)
        if not physical.resolve(strict=True).is_relative_to(root):
            raise ValueError("Artifact escapes the release directory.")
        raw = read_file(physical)
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("Artifact size or SHA-256 differs from release.json.")
        expected[name] = digest
        if path.suffix == ".whl":
            kind = wheel_kind(name)
            check_wheel(raw, kind)
            roles[kind + "_wheel"] += 1
        elif name == report["host"]["archive"]:
            nested += check_archive(raw, "host")
            roles["host_zip"] += 1
        elif path.suffix == ".zip":
            nested += check_archive(raw)
        elif name.endswith(".tar.gz"):
            check_source_archive(raw)
            roles["sdk_sdist"] += 1
    if any(roles[role] != 1 for role in ("host_wheel", "sdk_wheel", "host_zip", "sdk_sdist")):
        raise ValueError("Release must contain one core wheel/archive and one SDK wheel/source archive.")
    sums = {}
    for line in read_file(root / "SHA256SUMS.txt").decode("utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match or match[2] in sums:
            raise ValueError("Invalid or duplicate checksum entry.")
        sums[match[2]] = match[1]
    if sums != expected:
        raise ValueError("Checksum records differ from release.json.")
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if actual != set(expected) | {"release.json", "SHA256SUMS.txt"}:
        raise ValueError("Release directory contains unlisted or missing files.")
    return {"passed": True, "artifacts_checked": len(expected),
            "nested_wheels": nested, "adapters_bundled": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_directory", type=Path)
    args = parser.parse_args(argv)
    try:
        report = check_release(args.release_directory)
    except (OSError, ValueError, KeyError, TypeError, SyntaxError, StopIteration,
            UnicodeError, zipfile.BadZipFile, tarfile.TarError) as failure:
        report = {"passed": False, "error": str(failure)}
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
