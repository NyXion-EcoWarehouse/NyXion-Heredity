"""Local startup configuration without shell expansion or secret projection."""

import os
from pathlib import Path
import re
import stat
import threading
from uuid import uuid4


DEFAULT_UI_PORT = 5099
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class SettingsError(ValueError):
    code = "INVALID_STARTUP_SETTINGS"
    status = 400


def _check_path(path):
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
            raise SettingsError("启动配置路径不能经过符号链接。")


def _read(path):
    _check_path(path)
    try:
        with path.open("rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 64 * 1024:
                raise SettingsError("启动配置必须是有界的普通文件。")
            raw = stream.read(64 * 1024 + 1)
    except FileNotFoundError:
        return "", {}
    if len(raw) > 64 * 1024:
        raise SettingsError("启动配置文件超过大小限制。")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        raise SettingsError("启动配置必须使用 UTF-8。") from None
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        key, separator, value = stripped.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not _KEY.fullmatch(key) or key in values:
            raise SettingsError("启动配置包含重复或无效的键。")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise SettingsError("启动配置的引号未闭合。")
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if "\0" in value or "\r" in value or "\n" in value:
            raise SettingsError("启动配置包含无效字符。")
        values[key] = value
    return text, values


def ui_port(value, *, allow_ephemeral=False):
    if type(value) is int:
        port = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,5}", value):
        port = int(value)
    else:
        raise SettingsError("管理端口必须为整数。")
    if not (0 if allow_ephemeral else 1) <= port <= 65535:
        raise SettingsError("管理端口必须在 1 到 65535 之间。")
    return port


def load_environment(path, *, environ=None):
    """Explicit process values win; dotenv text never executes as shell code."""
    environment = os.environ if environ is None else environ
    _, values = _read(Path(path).absolute())
    if "HEREDITY_UI_PORT" in values:
        ui_port(values["HEREDITY_UI_PORT"])
    for key, value in values.items():
        environment.setdefault(key, value)
    if "HEREDITY_UI_PORT" in environment:
        ui_port(environment["HEREDITY_UI_PORT"])
    return values


class SettingsService:
    def __init__(self, env_file, running_port, interfaces):
        self.path = Path(env_file).absolute()
        self.running_port = running_port
        self.interfaces = interfaces
        self._lock = threading.RLock()

    def snapshot(self):
        with self._lock:
            _, values = _read(self.path)
            configured = ui_port(values.get("HEREDITY_UI_PORT", str(DEFAULT_UI_PORT)))
            services = []
            for item in self.interfaces.catalog()["interfaces"]:
                services.append({
                    "interface_id": item["interface_id"], "name": item["name"],
                    "type": item["type"], "state": item["state"],
                    "port": item["settings"].get("port"),
                    "actual_port": item["actual_port"], "endpoint": item["endpoint"],
                    "error": item["error"], "error_code": item.get("error_code"),
                })
            return {
                "startup": {
                    "ui_host": "127.0.0.1", "ui_port": self.running_port,
                    "configured_ui_port": configured, "env_file": ".env",
                    "restart_required": configured != self.running_port,
                },
                "services": services, "ports_policy": "per-interface",
            }

    def update(self, payload):
        if type(payload) is not dict or set(payload) != {"ui_port"}:
            raise SettingsError("只允许设置管理服务的 ui_port。")
        value = ui_port(payload["ui_port"])
        with self._lock:
            text, _ = _read(self.path)
            lines = text.splitlines()
            updated, found = [], False
            for line in lines:
                stripped = line.strip()
                candidate = stripped[7:].lstrip() if stripped.startswith("export ") else stripped
                if candidate.partition("=")[0].strip() == "HEREDITY_UI_PORT":
                    updated.append(f"HEREDITY_UI_PORT={value}")
                    found = True
                else:
                    updated.append(line)
            if not found:
                updated.append(f"HEREDITY_UI_PORT={value}")
            raw = ("\n".join(updated).rstrip("\n") + "\n").encode("utf-8")
            if len(raw) > 64 * 1024:
                raise SettingsError("启动配置文件超过大小限制。")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(".env-" + uuid4().hex + ".tmp")
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                _check_path(self.path)
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)
            return self.snapshot()
