"""Bounded catalog jobs and verified WebAssembly packages for the host market."""

from collections import deque
from copy import deepcopy
import hashlib
import hmac
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import threading
import time
from urllib import error, request
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import uuid4
import zipfile

from nyxion_heredity_sdk import ContractError
from nyxion_heredity_sdk.sandbox import SandboxModuleManifest
from .versioning import host_version as _host_version


INDEX_URL = (
    "https://raw.githubusercontent.com/NyXion-EcoWarehouse/"
    "Nyxion-Heredity-Registry/main/plugins.json"
)
MAX_INDEX_BYTES = 2 * 1024 * 1024
MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_MODULE_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 32 * 1024
MARKET_PERMISSIONS = frozenset({
    "fs.read", "fs.write", "net.connect", "bus.call", "bus.publish", "bus.subscribe",
})
HASH_FAILURE = "安全警告：插件文件哈希校验失败，可能被篡改！"
_ID = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
_VERSION = re.compile(r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_PERMISSION = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
_INDEX_FAKE_NETWORK = ipaddress.ip_network("198.18.0.0/15")


class MarketplaceError(ValueError):
    def __init__(self, message, code="MARKETPLACE_REJECTED", status=400):
        super().__init__(message)
        self.code, self.status = code, status

    def public(self):
        return {"code": self.code, "message": str(self)}


def _version(value):
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise MarketplaceError("版本必须是稳定的三段数字版本。", "INVALID_MARKET_VERSION")
    return tuple(int(part) for part in value.split("."))


def _text(value, field, limit, *, empty=False):
    if (type(value) is not str or len(value) > limit or (not value.strip() and not empty)
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise MarketplaceError(f"插件目录的 {field} 字段无效。", "INVALID_MARKET_ENTRY")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise MarketplaceError("插件目录包含无效文字编码。", "INVALID_MARKET_ENTRY") from None
    return value


def _permissions(values):
    if (type(values) not in (list, tuple) or len(values) > 32
            or any(type(value) is not str or len(value) > 80
                   or not _PERMISSION.fullmatch(value) or value not in MARKET_PERMISSIONS
                   for value in values)
            or len(values) != len(set(values))):
        raise MarketplaceError("插件权限声明无效或重复。", "INVALID_MARKET_PERMISSIONS")
    return tuple(values)


def _url(value, *, query=True):
    try:
        if (type(value) is not str or not 1 <= len(value) <= 8192
                or any(ord(char) < 33 or ord(char) > 126 for char in value)
                or "\\" in value or "#" in value):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or (not query and parsed.query)):
            raise ValueError
        parsed.port
        if any(part in {".", "..", "..."} for part in parsed.path.split("/")):
            raise ValueError
        host = parsed.hostname.lower().rstrip(".")
        if host in {"localhost", "metadata.google.internal"} or host.endswith(".localhost"):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError
    except (TypeError, ValueError):
        raise MarketplaceError("插件地址必须是无内嵌凭据的公共 HTTPS 地址。", "INVALID_MARKET_URL") from None
    return value


def _public_url(value):
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise MarketplaceError("插件数据包含重复字段。", "INVALID_MARKET_JSON")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise MarketplaceError("插件数据不是有效的有界 JSON。", "INVALID_MARKET_JSON") from None


def _entry(source):
    required = {"id", "name", "description", "version", "author", "permissions",
                "download_url", "hash_sha256", "status", "min_heredity_version"}
    optional = {"security_advisory", "license", "repo_url", "tags"}
    if type(source) is not dict or not required <= set(source) or set(source) - required - optional:
        raise MarketplaceError("插件条目缺少必需字段或包含未知字段。", "INVALID_MARKET_ENTRY")
    identifier = source["id"]
    if type(identifier) is not str or len(identifier) > 128 or not _ID.fullmatch(identifier):
        raise MarketplaceError("插件必须提供有效的唯一 id。", "INVALID_MARKET_ID")
    result = deepcopy(source)
    for name, limit in (("name", 120), ("description", 2000)):
        _text(result[name], name, limit, empty=name == "description")
    _version(result["version"])
    _version(result["min_heredity_version"])
    author = result["author"]
    if (type(author) is not dict or "name" not in author
            or set(author) - {"name", "github", "url"}):
        raise MarketplaceError("插件作者声明无效。", "INVALID_MARKET_ENTRY")
    _text(author["name"], "author.name", 120)
    for key in ("github", "url"):
        if key in author:
            _url(author[key])
    result["permissions"] = list(_permissions(result["permissions"]))
    _url(result["download_url"])
    if type(result["hash_sha256"]) is not str or not _HASH.fullmatch(result["hash_sha256"]):
        raise MarketplaceError("插件必须提供小写 SHA256 摘要。", "INVALID_MARKET_HASH")
    if type(result["status"]) is not str or result["status"] not in {"active", "warning", "banned"}:
        raise MarketplaceError("插件安全状态无效。", "INVALID_MARKET_ENTRY")
    advisory = result.get("security_advisory", "")
    result["security_advisory"] = _text(
        advisory, "security_advisory", 2000, empty=True) if advisory is not None else ""
    if "license" in result:
        _text(result["license"], "license", 120)
    if "repo_url" in result:
        _url(result["repo_url"])
    if "tags" in result:
        if type(result["tags"]) is not list or len(result["tags"]) > 16:
            raise MarketplaceError("插件标签无效。", "INVALID_MARKET_ENTRY")
        for tag in result["tags"]:
            _text(tag, "tags", 64)
    return result


def validate_catalog(raw):
    data = _json(raw)
    if (type(data) is not dict or "plugins" not in data
            or set(data) - {"plugins", "registry_version", "last_updated"}
            or type(data["plugins"]) is not list or len(data["plugins"]) > 1000):
        raise MarketplaceError("插件目录结构无效。", "INVALID_MARKET_CATALOG")
    for key in ("registry_version", "last_updated"):
        if key in data:
            _text(data[key], key, 128)
    occurrences = {}
    for source in data["plugins"]:
        if type(source) is dict and type(source.get("id")) is str:
            occurrences[source["id"]] = occurrences.get(source["id"], 0) + 1
    accepted, rejected = [], []
    for position, source in enumerate(data["plugins"], 1):
        try:
            value = _entry(source)
            if occurrences[value["id"]] > 1:
                raise MarketplaceError("插件目录包含重复 id。", "DUPLICATE_MARKET_ID")
            accepted.append(value)
        except MarketplaceError as failure:
            name = source.get("name", "") if type(source) is dict else ""
            if type(name) is not str or any(ord(char) < 32 for char in name):
                name = ""
            rejected.append({"position": position, "name": name[:120], "error": failure.public()})
    return accepted, rejected


def _blocked_catalog_ids(raw):
    source = _json(raw)
    counts, blocked = {}, set()
    for entry in source["plugins"]:
        if type(entry) is not dict:
            continue
        identifier = entry.get("id")
        if type(identifier) is not str or len(identifier) > 128 or not _ID.fullmatch(identifier):
            continue
        counts[identifier] = counts.get(identifier, 0) + 1
        if entry.get("status") == "banned":
            blocked.add(identifier)
    blocked.update(identifier for identifier, count in counts.items() if count > 1)
    return blocked


def validate_manifest(source, entry, host_version):
    try:
        checked = SandboxModuleManifest.from_dict(source)
    except ContractError:
        raise MarketplaceError(
            "插件包 Manifest 或 SDK API 不兼容。", "INVALID_PACKAGE_MANIFEST") from None
    for key in ("id", "name", "version", "min_heredity_version"):
        if source[key] != entry[key]:
            raise MarketplaceError("插件包身份或版本与已确认目录不一致。", "PACKAGE_IDENTITY_MISMATCH")
    if set(_permissions(source["permissions"])) != set(entry["permissions"]):
        raise MarketplaceError("插件包权限与已确认目录不一致。", "PACKAGE_PERMISSIONS_MISMATCH")
    if _version(source["min_heredity_version"]) > _host_version(host_version):
        raise MarketplaceError("此插件需要更高版本的 Heredity。", "HOST_VERSION_TOO_OLD", 409)
    return checked.to_dict()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, addresses, **kwargs):
        super().__init__(host, **kwargs)
        self._addresses = addresses

    def connect(self):
        last = None
        deadline = time.monotonic() + self.timeout
        for address in self._addresses:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                self.sock = socket.create_connection((address, self.port), remaining)
                self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
                return
            except OSError as failure:
                last = failure
                if self.sock is not None:
                    self.sock.close()
                    self.sock = None
        raise last or OSError("No public destination was available.")


class _PublicHTTPSHandler(request.HTTPSHandler):
    def __init__(self, *, allow_fixed_index=False, **kwargs):
        super().__init__(**kwargs)
        self._allow_fixed_index = allow_fixed_index

    def https_open(self, req):
        _url(req.full_url)
        parsed = urlsplit(req.full_url)
        try:
            answers = socket.getaddrinfo(parsed.hostname, parsed.port or 443,
                                         type=socket.SOCK_STREAM)
            addresses = tuple(dict.fromkeys(answer[4][0] for answer in answers))
            fixed_index = self._allow_fixed_index and req.full_url == INDEX_URL
            if not addresses or any(
                    not ipaddress.ip_address(value).is_global
                    and not (fixed_index and ipaddress.ip_address(value) in _INDEX_FAKE_NETWORK)
                    for value in addresses):
                raise ValueError
        except (OSError, ValueError):
            raise MarketplaceError("插件下载目标不是可验证的公共网络地址。", "MARKET_DESTINATION_REJECTED") from None
        return self.do_open(
            lambda host, **kwargs: _PinnedHTTPSConnection(host, addresses=addresses[:8], **kwargs),
            req, context=self._context)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Marketplace:
    def __init__(self, state_dir, host_version, *, on_install=None, sandbox_info=None,
                 on_refresh=None, auto_refresh=True, timeout=12.0, _open_url=None):
        try:
            _host_version(host_version)
        except ValueError:
            raise MarketplaceError("宿主版本格式无效。", "INVALID_MARKET_VERSION") from None
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise ValueError("Marketplace timeout must be in (0, 30].")
        self.host_version, self.on_install = host_version, on_install
        self.on_refresh = on_refresh
        self._sandbox_info = sandbox_info
        self.timeout = float(timeout)
        self.root = Path(state_dir).resolve() / "market"
        self.plugins_dir = self.root / "plugins"
        self._check_path(self.root)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path = self.root / "catalog.json"
        self._registry_path = self.root / "installed.json"
        self._approval_path = self.root / "approval.key"
        self._approval_key = self._load_approval_key()
        self._lock = threading.Condition(threading.RLock())
        self._closed = False
        self._cancel = threading.Event()
        self._queue, self._jobs = deque(), {}
        self._catalog, self._invalid, self._blocked_ids = {}, [], set()
        self._installed_errors = {}
        self._installed = self._read_registry()
        self._last_attempt = self._last_success = None
        self._error, self._stale = None, True
        opener = request.build_opener(
            request.ProxyHandler({}), _PublicHTTPSHandler(context=ssl.create_default_context()),
            _NoRedirect())
        index_opener = request.build_opener(
            request.ProxyHandler({}),
            _PublicHTTPSHandler(context=ssl.create_default_context(), allow_fixed_index=True),
            _NoRedirect())
        self._open_url = _open_url or (lambda url, timeout: opener.open(
            request.Request(url, headers={"User-Agent": "NyXion-Heredity/market"}),
            timeout=timeout))
        self._open_index = _open_url or (lambda url, timeout: index_opener.open(
            request.Request(url, headers={"User-Agent": "NyXion-Heredity/market"}),
            timeout=timeout))
        self._load_cache()
        self._worker = threading.Thread(target=self._run, name="nyxion-marketplace", daemon=True)
        self._worker.start()
        if auto_refresh:
            self.refresh()

    @staticmethod
    def _check_path(path):
        for item in (path, *path.parents):
            if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
                raise MarketplaceError("插件市场存储不能经过符号链接。", "UNSAFE_MARKET_PATH")

    def _write_json(self, path, value):
        self._check_path(path)
        temporary = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=True, allow_nan=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            self._check_path(path)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_approval_key(self):
        self._check_path(self._approval_path)
        try:
            return self._read_approval_key()
        except FileNotFoundError:
            if self._registry_path.exists():
                raise MarketplaceError(
                    "已安装插件缺少宿主授权凭据，请重新确认安装。", "MARKET_APPROVAL_INVALID", 409) from None
        value = os.urandom(32)
        temporary = self.root / (".approval-" + uuid4().hex + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            self._check_path(self._approval_path)
            try:
                os.link(temporary, self._approval_path)
            except FileExistsError:
                pass
            return self._read_approval_key()
        finally:
            temporary.unlink(missing_ok=True)

    def _read_approval_key(self):
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(self._approval_path, flags), "rb") as stream:
            info = os.fstat(stream.fileno())
            value = stream.read(33)
        if not stat.S_ISREG(info.st_mode) or info.st_size != 32 or len(value) != 32:
            raise MarketplaceError("插件市场授权凭据无效。", "MARKET_APPROVAL_INVALID", 409)
        return value

    def _approval_mac(self, record):
        approved = {key: value for key, value in record.items() if key != "approval_mac"}
        encoded = json.dumps(
            approved, sort_keys=True, ensure_ascii=True, allow_nan=False,
            separators=(",", ":")).encode("utf-8")
        return hmac.new(self._approval_key, encoded, "sha256").hexdigest()

    def _load_cache(self):
        try:
            self._check_path(self._cache_path)
            if self._cache_path.stat().st_size > MAX_INDEX_BYTES:
                raise ValueError
            data = _json(self._cache_path.read_bytes())
            if (type(data) is not dict
                    or set(data) not in ({"fetched_at", "plugins", "invalid_entries"},
                                         {"fetched_at", "plugins", "invalid_entries", "blocked_ids"})):
                raise ValueError
            values = [_entry(item) for item in data["plugins"]]
            fetched = data["fetched_at"]
            if (len(values) > 1000 or len({item["id"] for item in values}) != len(values)
                    or type(fetched) not in (int, float) or not math.isfinite(fetched)
                    or fetched <= 0 or type(data["invalid_entries"]) is not list
                    or len(data["invalid_entries"]) > 1000):
                raise ValueError
            blocked = data.get("blocked_ids", [])
            if (type(blocked) is not list or len(blocked) > 1000
                    or any(type(value) is not str or len(value) > 128
                           or not _ID.fullmatch(value) for value in blocked)):
                raise ValueError
            invalid = []
            for item in data["invalid_entries"]:
                if (type(item) is not dict or set(item) != {"position", "name", "error"}
                        or type(item["position"]) is not int or not 1 <= item["position"] <= 1000
                        or type(item["error"]) is not dict
                        or set(item["error"]) != {"code", "message"}):
                    raise ValueError
                _text(item["name"], "name", 120, empty=True)
                _text(item["error"]["code"], "code", 80)
                _text(item["error"]["message"], "message", 2000)
                invalid.append(deepcopy(item))
            self._catalog = {item["id"]: item for item in values}
            self._last_success = fetched
            self._blocked_ids = set(blocked)
            self._invalid = invalid
        except (OSError, ValueError, TypeError, KeyError):
            return

    def _read_registry(self):
        try:
            self._check_path(self._registry_path)
            if self._registry_path.stat().st_size > MAX_INDEX_BYTES:
                raise ValueError
            value = _json(self._registry_path.read_bytes())
            if (type(value) is not dict or set(value) != {"schema_version", "plugins"}
                    or value["schema_version"] != 1 or type(value["plugins"]) is not dict
                    or len(value["plugins"]) > 1000):
                raise ValueError
            checked = {}
            for identifier, record in value["plugins"].items():
                try:
                    self._validate_record(record)
                    if identifier != record["plugin_id"]:
                        raise ValueError
                    checked[identifier] = record
                except (OSError, ValueError, TypeError, KeyError):
                    label = (identifier if type(identifier) is str and len(identifier) <= 128
                             and _ID.fullmatch(identifier) else "<invalid>")
                    self._installed_errors[label] = {
                        "plugin_id": label, "code": "MARKET_APPROVAL_INVALID",
                        "message": "此插件安装或权限批准记录无效，已阻止加载；其他插件不受影响。"}
            return checked
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError):
            raise MarketplaceError("已安装插件登记文件无效。", "INVALID_INSTALLED_REGISTRY") from None

    def _validate_record(self, record):
        required = {"plugin_id", "version", "sha256", "module_sha256", "manifest",
                    "status", "installed_at", "relative_path", "approved_permissions", "approval_mac"}
        if (type(record) is not dict or set(record) != required
                or type(record["plugin_id"]) is not str or len(record["plugin_id"]) > 128
                or not _ID.fullmatch(record["plugin_id"])
                or type(record["sha256"]) is not str or not _HASH.fullmatch(record["sha256"])
                or type(record["module_sha256"]) is not str
                or not _HASH.fullmatch(record["module_sha256"])
                or type(record["status"]) is not str
                or record["status"] not in {"active", "warning"}
                or type(record["installed_at"]) not in (int, float)
                or not math.isfinite(record["installed_at"]) or record["installed_at"] <= 0):
            raise ValueError("Invalid local package record.")
        if (type(record["approval_mac"]) is not str or not _HASH.fullmatch(record["approval_mac"])
                or not hmac.compare_digest(record["approval_mac"], self._approval_mac(record))):
            raise MarketplaceError(
                "已安装插件的权限批准记录无效，请重新确认安装。", "MARKET_APPROVAL_INVALID", 409)
        _version(record["version"])
        if (record["relative_path"] != f"{record['plugin_id']}/{record['version']}"
                or type(record["manifest"]) is not dict
                or record["manifest"].get("id") != record["plugin_id"]
                or record["manifest"].get("version") != record["version"]):
            raise ValueError("Invalid local package identity.")
        source = record["manifest"]
        entry = {"id": record["plugin_id"], "name": source.get("name"),
                 "version": record["version"],
                 "min_heredity_version": source.get("min_heredity_version"),
                 "permissions": list(_permissions(record["approved_permissions"]))}
        _text(entry["name"], "name", 120)
        _version(entry["min_heredity_version"])
        validate_manifest(source, entry, self.host_version)

    def snapshot(self):
        with self._lock:
            entries = []
            for entry in self._catalog.values():
                value = deepcopy(entry)
                value["download_url"] = _public_url(value["download_url"])
                installed = self._installed.get(entry["id"])
                value["installed"] = ({key: installed[key] for key in
                                       ("version", "sha256", "installed_at")}
                                      if installed is not None else None)
                value["compatible"] = _version(entry["min_heredity_version"]) <= _host_version(self.host_version)
                value["sandbox_entry_compatible"] = value["compatible"]
                entries.append(value)
            try:
                sandbox = self._sandbox_info() if callable(self._sandbox_info) else self._sandbox_info
            except Exception:
                sandbox = {"supported": False, "reason": "插件沙箱状态暂不可用。"}
            return {"index_url": INDEX_URL, "last_attempt_at": self._last_attempt,
                    "last_success_at": self._last_success, "stale": self._stale,
                    "refreshing": any(job["kind"] == "refresh" and job["state"] in
                                      {"queued", "running"} for job in self._jobs.values()),
                    "error": deepcopy(self._error), "plugins": entries,
                    "installed_errors": deepcopy(list(self._installed_errors.values())),
                    "invalid_entries": deepcopy(self._invalid),
                    "jobs": deepcopy(list(self._jobs.values())),
                    "sandbox": sandbox or {"supported": False, "reason": "插件沙箱尚未连接。"}}

    def _submit(self, kind, plugin_id=None, payload=None):
        with self._lock:
            if self._closed:
                raise MarketplaceError("插件市场已关闭。", "MARKETPLACE_CLOSED", 503)
            for job in self._jobs.values():
                if (job["kind"] == kind and job["plugin_id"] == plugin_id
                        and job["state"] in {"queued", "running"}):
                    return deepcopy(job)
            if len(self._queue) >= 32:
                raise MarketplaceError("插件市场任务队列已满。", "MARKETPLACE_QUEUE_FULL", 429)
            if len(self._jobs) >= 128:
                removable = next((key for key, job in self._jobs.items()
                                  if job["state"] not in {"queued", "running"}), None)
                if removable is not None:
                    self._jobs.pop(removable)
            now, identifier = time.time(), uuid4().hex
            job = {"job_id": identifier, "kind": kind, "plugin_id": plugin_id, "state": "queued",
                   "created_at": now, "updated_at": now, "result": None, "error": None}
            self._jobs[identifier] = job
            self._queue.append((identifier, payload))
            self._lock.notify_all()
            return deepcopy(job)

    def refresh(self):
        return self._submit("refresh")

    def _eligible(self, plugin_id, expected_version, expected_sha256, permissions, force_warning):
        current = self._catalog.get(plugin_id)
        if plugin_id in self._blocked_ids:
            raise MarketplaceError("该插件被禁止安装或目录身份不唯一。", "MARKET_PLUGIN_BANNED", 403)
        if current is None:
            raise MarketplaceError("插件不在当前有效目录中。", "MARKET_PLUGIN_NOT_FOUND", 404)
        if current["status"] == "banned":
            raise MarketplaceError("该插件已被禁止安装。", "MARKET_PLUGIN_BANNED", 403)
        if current["status"] == "warning" and not force_warning:
            raise MarketplaceError("该插件存在安全警告，需要明确确认后安装。", "MARKET_WARNING_CONFIRMATION", 409)
        if (current["version"] != expected_version
                or not hmac.compare_digest(current["hash_sha256"], expected_sha256)
                or set(current["permissions"]) != set(permissions)):
            raise MarketplaceError("插件目录已变化，请刷新并重新确认版本、哈希及权限。",
                                   "MARKET_CONFIRMATION_STALE", 409)
        if self._stale:
            raise MarketplaceError("请先成功刷新插件目录，再确认安装。", "MARKET_CATALOG_STALE", 409)
        if _version(current["min_heredity_version"]) > _host_version(self.host_version):
            raise MarketplaceError("此插件需要更高版本的 Heredity。", "HOST_VERSION_TOO_OLD", 409)
        return deepcopy(current)

    def install(self, plugin_id, *, expected_version, expected_sha256,
                confirmed_permissions, force_warning=False):
        if (type(plugin_id) is not str or len(plugin_id) > 128 or not _ID.fullmatch(plugin_id)
                or type(expected_sha256) is not str or not _HASH.fullmatch(expected_sha256)
                or type(force_warning) is not bool):
            raise MarketplaceError("安装确认参数无效。", "INVALID_INSTALL_CONFIRMATION")
        _version(expected_version)
        permissions = _permissions(confirmed_permissions)
        with self._lock:
            entry = self._eligible(plugin_id, expected_version, expected_sha256,
                                   permissions, force_warning)
            return self._submit("install", plugin_id, {
                "entry": entry, "permissions": permissions, "force_warning": force_warning})

    def job(self, identifier):
        with self._lock:
            try:
                return deepcopy(self._jobs[identifier])
            except (KeyError, TypeError):
                raise MarketplaceError("插件市场任务不存在。", "MARKET_JOB_NOT_FOUND", 404) from None

    def _download(self, url, destination, limit, *, fixed_index=False):
        if fixed_index and url != INDEX_URL:
            raise MarketplaceError("官方插件目录地址不可更改。", "INVALID_MARKET_INDEX_URL")
        deadline, redirects = time.monotonic() + self.timeout, 0
        url = _url(url)
        while True:
            if self._cancel.is_set():
                raise MarketplaceError("插件市场任务已取消。", "MARKET_JOB_CANCELLED", 409)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MarketplaceError("插件市场网络请求超时。", "MARKET_NETWORK_TIMEOUT", 504)
            try:
                response = (self._open_index if fixed_index else self._open_url)(url, remaining)
            except error.HTTPError as failure:
                if fixed_index and failure.code in {301, 302, 303, 307, 308}:
                    failure.close()
                    raise MarketplaceError(
                        "官方插件目录发生了未授权跳转，已停止刷新。", "MARKET_INDEX_REDIRECT_REJECTED", 502) from None
                if failure.code in {301, 302, 303, 307, 308} and redirects < 5:
                    target = failure.headers.get("Location", "")
                    failure.close()
                    url, redirects = _url(urljoin(url, target)), redirects + 1
                    continue
                raise MarketplaceError("插件市场服务器未提供可用文件。", "MARKET_NETWORK_FAILED", 502) from None
            break
        digest, size = hashlib.sha256(), 0
        try:
            with response, destination.open("xb") as output:
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdigit() or int(length) > limit):
                    raise MarketplaceError("插件市场文件超过大小限制。", "MARKET_FILE_TOO_LARGE")
                while True:
                    if self._cancel.is_set():
                        raise MarketplaceError("插件市场任务已取消。", "MARKET_JOB_CANCELLED", 409)
                    if time.monotonic() >= deadline:
                        raise MarketplaceError("插件市场网络请求超时。", "MARKET_NETWORK_TIMEOUT", 504)
                    stream_socket = getattr(
                        getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                    if stream_socket is not None:
                        stream_socket.settimeout(max(0.001, deadline - time.monotonic()))
                    chunk = response.read(min(65536, limit - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise MarketplaceError("插件市场文件超过大小限制。", "MARKET_FILE_TOO_LARGE")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
                if length is not None and int(length) != size:
                    raise MarketplaceError("插件市场文件未完整下载。", "MARKET_DOWNLOAD_INCOMPLETE", 502)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return digest.hexdigest()

    def _refresh(self):
        with self._lock:
            self._last_attempt = time.time()
        temporary = self.root / (".catalog-" + uuid4().hex + ".tmp")
        try:
            self._download(INDEX_URL, temporary, MAX_INDEX_BYTES, fixed_index=True)
            raw = temporary.read_bytes()
            entries, invalid = validate_catalog(raw)
            blocked = _blocked_catalog_ids(raw)
            fetched = time.time()
            self._write_json(self._cache_path, {
                "fetched_at": fetched, "plugins": entries, "invalid_entries": invalid,
                "blocked_ids": sorted(blocked)})
            with self._lock:
                self._catalog = {entry["id"]: entry for entry in entries}
                self._invalid, self._last_success, self._stale = invalid, fetched, False
                self._blocked_ids = blocked
                self._error = ({"code": "INDEX_ENTRY_INVALID",
                                "message": "部分目录条目无效，已禁止安装；请查看条目说明。"}
                               if invalid else None)
            if self.on_refresh is not None and not self._cancel.is_set():
                try:
                    self.on_refresh()
                except Exception:
                    with self._lock:
                        self._error = {
                            "code": "MARKET_ENFORCEMENT_FAILED",
                            "message": "目录已更新，但运行中插件状态同步失败；请检查模块状态。"}
            return {"accepted": len(entries), "rejected": len(invalid), "fetched_at": fetched}
        except Exception:
            with self._lock:
                self._stale = True
                self._error = {"code": "MARKET_REFRESH_FAILED",
                               "message": "插件目录刷新失败，保留上次缓存；请稍后手动刷新。"}
            raise
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _manifest_for(entry):
        return {"schema_version": 1, "id": entry["id"], "name": entry["name"],
                "version": entry["version"], "sdk_api": {"major": 1, "minor": 0},
                "min_heredity_version": entry["min_heredity_version"],
                "permissions": list(entry["permissions"]), "module": "module.wasm"}

    def _package_contents(self, package, entry):
        if zipfile.is_zipfile(package):
            with zipfile.ZipFile(package) as archive:
                entries = archive.infolist()
                if len(entries) != 2 or {item.filename for item in entries} != {"manifest.json", "module.wasm"}:
                    raise MarketplaceError("插件压缩包只能包含 manifest.json 和 module.wasm。", "UNSAFE_PLUGIN_ARCHIVE")
                for item in entries:
                    mode = item.external_attr >> 16
                    limit = MAX_MANIFEST_BYTES if item.filename == "manifest.json" else MAX_MODULE_BYTES
                    if (item.is_dir() or item.flag_bits & 1 or stat.S_ISLNK(mode)
                            or (stat.S_IFMT(mode) not in {0, stat.S_IFREG})
                            or item.file_size < 1 or item.file_size > limit
                            or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                            or item.file_size > max(1024, item.compress_size * 200)):
                        raise MarketplaceError("插件压缩包包含不安全或过大的成员。", "UNSAFE_PLUGIN_ARCHIVE")
                manifest = _json(archive.read("manifest.json"))
                module = archive.read("module.wasm")
        else:
            if package.stat().st_size > MAX_MODULE_BYTES:
                raise MarketplaceError("WebAssembly 文件超过大小限制。", "MARKET_FILE_TOO_LARGE")
            manifest, module = self._manifest_for(entry), package.read_bytes()
        manifest = validate_manifest(manifest, entry, self.host_version)
        if not module.startswith(b"\x00asm\x01\x00\x00\x00") or len(module) <= 8:
            raise MarketplaceError("插件不是受支持的 WebAssembly 模块。", "INVALID_WASM_PACKAGE")
        return manifest, module

    def _unpack(self, package, entry, staging):
        manifest, module = self._package_contents(package, entry)
        (staging / "module.wasm").write_bytes(module)
        self._write_json(staging / "manifest.json", manifest)
        return manifest, hashlib.sha256(module).hexdigest()

    def _install(self, plugin_id, payload):
        selected = payload["entry"]
        with self._lock:
            current = self._eligible(
                plugin_id, selected["version"], selected["hash_sha256"],
                payload["permissions"], payload["force_warning"])
            if current != selected:
                raise MarketplaceError("目录内容已变化，请重新确认安装。", "MARKET_CONFIRMATION_STALE", 409)
        package = self.root / (".download-" + uuid4().hex + ".tmp")
        staging = self.plugins_dir / (".install-" + uuid4().hex)
        staging.mkdir()
        try:
            digest = self._download(selected["download_url"], package, MAX_PACKAGE_BYTES)
            if not hmac.compare_digest(digest, selected["hash_sha256"]):
                raise MarketplaceError(HASH_FAILURE, "PLUGIN_HASH_MISMATCH", 409)
            manifest, module_hash = self._unpack(package, selected, staging)
            target = self.plugins_dir / plugin_id / selected["version"]
            self._check_path(target)
            with self._lock:
                self._eligible(plugin_id, selected["version"], digest,
                               payload["permissions"], payload["force_warning"])
                if self._cancel.is_set():
                    raise MarketplaceError("插件市场任务已取消。", "MARKET_JOB_CANCELLED", 409)
                if target.exists():
                    raise MarketplaceError("此插件版本已经存在，请保留原安装或选择更新版本。",
                                           "MARKET_VERSION_ALREADY_INSTALLED", 409)
                target.parent.mkdir(parents=True, exist_ok=True)
                package.replace(staging / "package.bin")
                record = {"plugin_id": plugin_id, "version": selected["version"],
                          "sha256": digest, "module_sha256": module_hash,
                          "manifest": manifest, "status": selected["status"],
                          "installed_at": time.time(),
                          "approved_permissions": list(payload["permissions"]),
                          "relative_path": f"{plugin_id}/{selected['version']}"}
                record["approval_mac"] = self._approval_mac(record)
                staging.replace(target)
                previous = self._installed.get(plugin_id)
                self._installed[plugin_id] = record
                try:
                    self._write_json(self._registry_path, {"schema_version": 1, "plugins": self._installed})
                except BaseException:
                    if previous is None:
                        self._installed.pop(plugin_id)
                    else:
                        self._installed[plugin_id] = previous
                    for child in ("module.wasm", "manifest.json", "package.bin"):
                        (target / child).unlink(missing_ok=True)
                    target.rmdir()
                    raise
                self._installed_errors.pop(plugin_id, None)
            started, start_error = False, None
            if self.on_install is not None and not self._cancel.is_set():
                try:
                    local = self._record(record)
                    outcome = self.on_install(local)
                    started = bool(isinstance(outcome, dict) and outcome.get("started") is True)
                except Exception:
                    start_error = {"code": "MARKET_PLUGIN_START_FAILED",
                                   "message": "插件已安装，但沙箱启动失败；可在检查后重试启动。"}
            return {"state": "installed" if started or self.on_install is None else "installed_not_started",
                    "plugin_id": plugin_id, "version": selected["version"],
                    "sha256": digest, "started": started, "error": start_error}
        finally:
            package.unlink(missing_ok=True)
            if staging.exists():
                for path in staging.iterdir():
                    path.unlink()
                staging.rmdir()

    def _record(self, record):
        self._validate_record(record)
        manifest = deepcopy(record["manifest"])
        target = self.plugins_dir / record["plugin_id"] / record["version"]
        self._check_path(target)
        package, module = target / "package.bin", target / "module.wasm"
        for path, expected, limit in (
                (package, record["sha256"], MAX_PACKAGE_BYTES),
                (module, record["module_sha256"], MAX_MODULE_BYTES)):
            self._check_path(path)
            if (not path.is_file() or path.stat().st_size > limit
                    or not hmac.compare_digest(hashlib.sha256(path.read_bytes()).hexdigest(), expected)):
                raise MarketplaceError(HASH_FAILURE, "PLUGIN_HASH_MISMATCH", 409)
        packaged_manifest, packaged_module = self._package_contents(package, manifest)
        if (packaged_manifest != manifest or not hmac.compare_digest(
                hashlib.sha256(packaged_module).hexdigest(), record["module_sha256"])):
            raise MarketplaceError(HASH_FAILURE, "PLUGIN_HASH_MISMATCH", 409)
        stored_manifest = target / "manifest.json"
        self._check_path(stored_manifest)
        if (not stored_manifest.is_file() or stored_manifest.stat().st_size > MAX_MANIFEST_BYTES
                or _json(stored_manifest.read_bytes()) != manifest):
            raise MarketplaceError(
                "插件包 Manifest 与已批准的原始文件不一致。", "PACKAGE_IDENTITY_MISMATCH", 409)
        current = self._catalog.get(record["plugin_id"])
        if (record["plugin_id"] in self._blocked_ids
                or current is not None and current["status"] == "banned"):
            raise MarketplaceError("该插件当前已被目录禁止启动。", "MARKET_PLUGIN_BANNED", 403)
        return {**deepcopy(record), "status": current["status"] if current else record["status"],
                "manifest": manifest, "path": str(target),
                "module_path": str(module), "package_path": str(package)}

    def installed_records(self):
        with self._lock:
            values = list(self._installed.values())
        accepted = []
        for value in values:
            identifier = value["plugin_id"]
            try:
                accepted.append(self._record(value))
            except (OSError, ValueError, TypeError, KeyError) as failure:
                public = (failure.public() if isinstance(failure, MarketplaceError) else
                          {"code": "INVALID_INSTALLED_PLUGIN",
                           "message": "插件本机文件验证失败，已阻止加载。"})
                with self._lock:
                    self._installed_errors[identifier] = {"plugin_id": identifier, **public}
            else:
                with self._lock:
                    self._installed_errors.pop(identifier, None)
        return tuple(accepted)

    def installed_record(self, plugin_id):
        if type(plugin_id) is not str or len(plugin_id) > 128 or not _ID.fullmatch(plugin_id):
            raise MarketplaceError("插件 id 无效。", "INVALID_MARKET_ID")
        with self._lock:
            value = self._installed.get(plugin_id)
            if value is None:
                raise MarketplaceError("插件尚未经过有效安装。", "MARKET_PLUGIN_NOT_INSTALLED", 404)
            value = deepcopy(value)
        try:
            verified = self._record(value)
        except (OSError, ValueError, TypeError, KeyError) as failure:
            public = (failure.public() if isinstance(failure, MarketplaceError) else
                      {"code": "INVALID_INSTALLED_PLUGIN",
                       "message": "插件本机文件验证失败，已阻止加载。"})
            with self._lock:
                self._installed_errors[plugin_id] = {"plugin_id": plugin_id, **public}
            raise MarketplaceError(public["message"], public["code"], 409) from None
        with self._lock:
            self._installed_errors.pop(plugin_id, None)
        return verified

    def _run(self):
        while True:
            with self._lock:
                while not self._closed and not self._queue:
                    self._lock.wait()
                if self._closed:
                    return
                identifier, payload = self._queue.popleft()
                job = self._jobs[identifier]
                job.update(state="running", updated_at=time.time())
            try:
                result = self._refresh() if job["kind"] == "refresh" else self._install(job["plugin_id"], payload)
                failure = None
            except MarketplaceError as caught:
                result, failure = None, caught.public()
            except Exception:
                result, failure = None, {"code": "MARKET_OPERATION_FAILED",
                                        "message": "插件市场操作失败，请检查网络或已验证的插件包。"}
            with self._lock:
                job.update(state=("cancelled" if self._cancel.is_set() else
                                  "failed" if failure else "completed"),
                           updated_at=time.time(), result=result, error=failure)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cancel.set()
            for identifier, _ in self._queue:
                self._jobs[identifier].update(
                    state="cancelled", updated_at=time.time(),
                    error={"code": "MARKET_JOB_CANCELLED", "message": "插件市场任务已取消。"})
            self._queue.clear()
            self._lock.notify_all()
        self._worker.join(timeout=self.timeout + 3)
        if self._worker.is_alive():
            raise RuntimeError("Marketplace worker did not stop within its network budget.")
