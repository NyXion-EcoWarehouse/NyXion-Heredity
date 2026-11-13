"""Heredity's local management API and optional frontend static server."""

from collections import deque
import base64
from copy import deepcopy
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPException
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib import error, parse, request

from . import __version__, __version_label__
from .backends.base import BackendError
from .connections import ConnectionManagementError, ConnectionManager
from .onebot import _decode_json
from .webui_metrics import system_info
from .network_interfaces import NetworkInterfaceError
from .network_allocator import NetworkAllocationError
from .marketplace import MarketplaceError
from .module_bus import ModuleBusError
from .settings import SettingsError
from .event_hub import EventGapError
from nyxion_heredity_sdk import ContractError, NetworkBindingClosed

MAX_BODY = 1024 * 1024
SESSION_SECONDS = 8 * 60 * 60
LOG_LEVELS = ["trace", "debug", "info", "success", "warn", "error"]
ACTION_NAME = re.compile(r"[A-Za-z0-9_]{1,128}\Z")
PLUGIN_ROUTE = re.compile(r"/api/heredity/plugins/([a-z][a-z0-9_.-]{0,127})/(start|stop|retry|restart|challenge)\Z")
PLUGIN_INSTANCES = re.compile(r"/api/heredity/plugins/([a-z][a-z0-9_.-]{0,127})/instances\Z")
PLUGIN_INSTANCE = re.compile(
    r"/api/heredity/plugins/([a-z][a-z0-9_.-]{0,127})/instances/"
    r"([a-z0-9][a-z0-9_-]{0,63})(?:/(start|stop|retry|restart|challenge))?\Z")
NETWORK_ROUTE = re.compile(
    r"/api/heredity/network-interfaces/([0-9a-f]{16})(?:/(start|stop|retry|regenerate-token|credentials))?\Z")
NETWORK_REQUEST_ROUTE = re.compile(
    r"/api/heredity/network-requests/([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})/"
    r"(assign|unassign|deny|auto-start|reapply)\Z")
MARKET_JOB_ROUTE = re.compile(r"/api/heredity/market/jobs/([0-9a-f]{32})\Z")
MARKET_MODULE_ROUTE = re.compile(
    r"/api/heredity/market/modules/([a-z][a-z0-9_.-]{0,127})/(start|stop|restart)\Z")


class ManagementError(Exception):
    def __init__(self, message, status=400, code="INVALID_REQUEST"):
        super().__init__(message)
        self.status, self.code = status, code


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _loopback_origin(value):
    try:
        parsed = parse.urlsplit(value)
        local = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
        if (parsed.scheme not in {"http", "https"} or not local or not parsed.port
                or parsed.username or parsed.password or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Gateway must be an explicit loopback HTTP(S) origin with a port.") from None
    return value.rstrip("/")


class GatewayClient:
    def __init__(self, origin, token="", execution_token=""):
        self.origin = _loopback_origin(origin)
        self.api_path = "/"
        self.token = token
        self.execution_token = execution_token
        self.opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
        self._lock = threading.Lock()
        self._snapshot_at = 0
        self._snapshot = None

    def action(self, action, params=None):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        encoded = json.dumps({"action": action, "params": params or {}}, allow_nan=False).encode()
        try:
            with self.opener.open(request.Request(
                    self.origin + self.api_path.rstrip("/") + "/", data=encoded, headers=headers),
                                  timeout=3) as response:
                raw = response.read(2 * MAX_BODY + 1)
            if len(raw) > 2 * MAX_BODY:
                raise ValueError
            result = _decode_json(raw.decode("utf-8"))
            if not isinstance(result, dict) or result.get("status") not in {"ok", "failed"}:
                raise ValueError
            return result
        except error.HTTPError as exc:
            if exc.code == 404:
                return {"status": "failed", "retcode": 1404, "data": None,
                        "message": "Gateway action is not supported."}
            raise ManagementError("Heredity gateway rejected the request.", 502, "GATEWAY_ERROR") from None
        except (OSError, HTTPException, ValueError, error.URLError, RecursionError):
            raise ManagementError("Heredity gateway is unavailable or returned invalid data.",
                                  503, "GATEWAY_UNAVAILABLE") from None

    def snapshot(self, force=False):
        with self._lock:
            if not force and self._snapshot is not None and time.monotonic() - self._snapshot_at < 1:
                return deepcopy(self._snapshot)
            status = self.action("get_status")
            if status.get("status") != "ok" or not isinstance(status.get("data"), dict):
                raise ManagementError("Gateway status could not be read.", 503, "GATEWAY_UNAVAILABLE")
            state = status["data"]
            identity = self.action("get_login_info") if state.get("self_id") else None
            account = identity.get("data") if isinstance(identity, dict) and identity.get("status") == "ok" else None
            self._snapshot = {"state": state, "account": account}
            self._snapshot_at = time.monotonic()
            return deepcopy(self._snapshot)

    def execution_info(self):
        data = self._execution_request("GET", "/_heredity/providers")
        if (not isinstance(data.get("levels"), list) or not isinstance(data.get("state"), dict)):
            raise ManagementError("执行提供方状态无效。", 502, "INVALID_RESPONSE")
        return {"levels": data["levels"], "state": data["state"],
                "canSwitch": bool(self.execution_token) and data.get("switch_enabled") is True}

    def switch_provider(self, payload):
        if not self.execution_token:
            raise ManagementError("未配置独立执行管理通道。", 503, "EXECUTION_DISABLED")
        if (not isinstance(payload.get("provider_id"), str)
                or not isinstance(payload.get("expected_epoch"), str)):
            raise ManagementError("provider_id 和 expected_epoch 不能为空。")
        outgoing = {"provider_id": payload["provider_id"], "expected_epoch": payload["expected_epoch"]}
        if "timeout" in payload:
            outgoing["timeout"] = payload["timeout"]
        try:
            return self._execution_request("POST", "/_heredity/providers/switch", outgoing)
        finally:
            with self._lock:
                self._snapshot = None
                self._snapshot_at = 0

    def _execution_request(self, method, path, payload=None):
        token = self.execution_token if method == "POST" else self.token
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        raw = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            raw = json.dumps(payload, allow_nan=False).encode()
        outgoing = request.Request(self.origin + self.api_path.rstrip("/") + path,
                                   data=raw, headers=headers, method=method)
        try:
            with self.opener.open(outgoing, timeout=10) as response:
                result = response.read(2 * MAX_BODY + 1)
        except error.HTTPError as exc:
            message = "执行管理请求失败。"
            try:
                body = _decode_json(exc.read(MAX_BODY).decode("utf-8"))
                if isinstance(body, dict) and isinstance(body.get("message"), str):
                    message = body["message"]
            except (ValueError, OSError, UnicodeError, RecursionError):
                pass
            raise ManagementError(message, exc.code,
                                  "EXECUTION_CONFLICT" if exc.code == 409 else "EXECUTION_ERROR") from None
        except (OSError, HTTPException, error.URLError):
            raise ManagementError("执行管理通道不可用。", 503, "EXECUTION_UNAVAILABLE") from None
        try:
            if len(result) > 2 * MAX_BODY:
                raise ValueError
            decoded = _decode_json(result.decode("utf-8"))
            if not isinstance(decoded, dict):
                raise ValueError
            return decoded
        except (ValueError, UnicodeError, RecursionError):
            raise ManagementError("执行管理响应无效。", 502, "INVALID_RESPONSE") from None


class LocalGatewayClient(GatewayClient):
    """Management access stays inside the host and owns no network listener."""

    def __init__(self, backend):
        self.backend = backend
        self.origin = None
        self.api_path = "/"
        self.token = self.execution_token = ""
        self._lock = threading.Lock()
        self._snapshot_at = 0
        self._snapshot = None

    def action(self, action, params=None):
        try:
            data = self.backend.status() if action == "get_status" else (
                {"app_name": "Heredity", "display_name": "NyXion Heredity",
                 "app_version": __version__, "protocol_version": "v11"}
                if action == "get_version_info" else self.backend.call(action, params or {}))
            return {"status": "ok", "retcode": 0, "data": data}
        except BackendError as failure:
            result = {"status": "failed", "retcode": failure.retcode,
                      "data": None, "message": str(failure)}
            if failure.details is not None:
                result["details"] = failure.details
            return result

    def execution_info(self):
        info = self.backend.execution_info()
        return {"levels": info["levels"], "state": info["state"], "canSwitch": False}

    def switch_provider(self, payload):
        raise ManagementError("执行方式由平台插件提供。", 501, "PLATFORM_PLUGIN_REQUIRED")


def default_ui_config():
    appearance = {
        "mode": "system", "accentMode": "preset", "accentPreset": "lavender",
        "accentCustom": "#8561bd", "accentScope": "global", "darkIntensity": "soft",
        "palette": "default", "sidebarStyle": "follow",
        "background": {"type": "none", "color": "#ffffff", "gradient": "aurora",
                       "imageOpacity": 0.85, "imageBlur": 0, "hasImage": False,
                       "imageMime": "", "imageVersion": 0},
        "fontSans": "default", "fontSansCustom": "", "fontMono": "default",
        "fontMonoCustom": "", "uiScale": 1, "radius": 0.75, "density": "cozy",
        "reduceMotion": False, "disableMotion": False, "customPointerSystem": False,
        "customContextMenu": True, "highContrast": False, "sidebarPinned": False,
        "timeFormat": "24h", "pollInterval": 3000, "customCss": "", "cssVars": {},
    }
    return {
        "version": 1, "appearance": appearance,
        "layout": {
            "overviewBlocks": [{"id": name, "visible": True}
                               for name in ("stats", "connections", "alerts", "host", "sessions")],
            "overviewMobile": [], "navItems": [{"id": name, "visible": True}
                                              for name in ("/", "/plugins", "/connections", "/logs",
                                                           "/debug", "/settings")],
            "topbarItems": [{"id": name, "visible": True} for name in ("status", "theme", "kiosk")],
        },
        "pages": {"defaultRoute": "/", "logs": {"visibleLevels": LOG_LEVELS, "maxLines": 1000,
                  "autoScroll": True, "wrap": True, "highlightRules": [], "preset": "custom"},
                  "configTab": ""},
    }


def _merge_ui(current, patch):
    if not isinstance(patch, dict) or any(key not in {"version", "appearance", "layout", "pages"}
                                          for key in patch):
        raise ManagementError("Only appearance, layout and page preferences can be saved.")
    result = deepcopy(current)
    for section in ("appearance", "layout", "pages"):
        if section in patch:
            if not isinstance(patch[section], dict):
                raise ManagementError("UI preference sections must be objects.")
            for key, value in patch[section].items():
                if key not in result[section]:
                    raise ManagementError("Unknown UI preference field.")
                if isinstance(result[section][key], dict) and isinstance(value, dict):
                    result[section][key] = {**result[section][key], **value}
                else:
                    result[section][key] = value
    appearance = result["appearance"]
    if appearance["mode"] not in {"light", "dark", "system"}:
        raise ManagementError("Unknown appearance mode.")
    for key in ("uiScale", "radius", "pollInterval"):
        if type(appearance[key]) not in {int, float}:
            raise ManagementError("Appearance numeric values must be numbers.")
    if not (0.9 <= appearance["uiScale"] <= 1.2 and 0 <= appearance["radius"] <= 2
            and 0 <= appearance["pollInterval"] <= 60000):
        raise ManagementError("Appearance numeric value is out of range.")
    if not isinstance(appearance["customCss"], str) or len(appearance["customCss"]) > 50000:
        raise ManagementError("Custom CSS exceeds the supported size.")
    if not isinstance(appearance["cssVars"], dict):
        raise ManagementError("CSS variables must be an object.")
    background = appearance["background"]
    if not isinstance(background, dict) or background.get("type") not in {"none", "solid", "gradient"}:
        raise ManagementError("Only built-in backgrounds are supported by this adapter.", 501, "UNSUPPORTED")
    background.update(hasImage=False, imageMime="", imageVersion=0)
    for key in ("overviewBlocks", "overviewMobile", "navItems", "topbarItems"):
        items = result["layout"][key]
        if not isinstance(items, list) or len(items) > 100:
            raise ManagementError("Invalid layout item list.")
        if any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
               or type(item.get("visible")) is not bool for item in items):
            raise ManagementError("Invalid layout item.")
    encoded = json.dumps(result, allow_nan=False, ensure_ascii=True)
    if len(encoded) > MAX_BODY:
        raise ManagementError("UI preferences are too large.")
    return result


def _action_doc(name, summary, params=(), read_only=True, category="Heredity"):
    return {"name": name, "aliases": [], "category": category, "summary": summary,
            "readOnly": read_only, "stream": False, "params": list(params)}


def action_catalog():
    def field(name, kind, desc, required=True):
        return {"name": name, "type": kind, "required": required, "desc": desc}
    target = field("user_id", "number", "用户 ID")
    group = field("group_id", "number", "群 ID")
    message = field("message", "message", "消息文本或消息段")
    mid = field("message_id", "number", "非零有符号 32 位消息 ID")
    return [
        _action_doc("get_login_info", "读取绑定账号"),
        _action_doc("get_status", "读取账号运行状态"),
        _action_doc("get_version_info", "读取 Heredity 版本"),
        _action_doc("get_runtime_info", "读取运行模式与能力"),
        _action_doc("get_execution_info", "读取执行等级与提供方目录"),
        _action_doc("get_friend_list", "读取后端好友列表"),
        _action_doc("get_group_list", "读取后端群列表"),
        _action_doc("get_msg", "优先读取本地消息", [mid]),
        _action_doc("send_private_msg", "发送私聊消息；DEMO 模式仅模拟",
                    [target, message], False),
        _action_doc("send_group_msg", "发送群消息；DEMO 模式仅模拟",
                    [group, message], False),
        _action_doc("delete_msg", "撤回消息；DEMO 模式仅修改演示状态", [mid], False),
        _action_doc("get_local_history", "查询本地持久历史",
                    [field("message_type", "string", "private 或 group"), target,
                     field("limit", "number", "最多返回条数", False)]),
        _action_doc("can_send_image", "查询后端图片发送能力"),
        _action_doc("can_send_record", "查询后端语音发送能力"),
    ]


class WebUIState:
    def __init__(self, password, gateway, state_dir, gateway_token="", execution_token="",
                 plugin_host=None, plugin_gateway=None, host_connections=None,
                 network_interfaces=None, network_allocator=None,
                 marketplace=None, module_bus=None):
        if not isinstance(password, str) or len(password) < 12:
            raise ValueError("WebUI password must contain at least 12 characters.")
        self.gateway = (LocalGatewayClient(plugin_gateway) if plugin_gateway is not None else
                        GatewayClient(gateway, gateway_token, execution_token))
        self._default_gateway = self.gateway
        self.plugin_host = plugin_host
        self.plugin_gateway = plugin_gateway
        self.host_connections = host_connections
        self.network_interfaces = network_interfaces
        self.network_allocator = network_allocator
        self.marketplace, self.module_bus = marketplace, module_bus
        self.settings = None
        self._connection_lock = threading.RLock()
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.connection_manager = ConnectionManager(
            self.state_dir, self.gateway.origin,
            forbidden_origins=("http://127.0.0.1:5099", "http://127.0.0.1:5178"))
        self._ui_path = self.state_dir / "preferences.json"
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._salt = secrets.token_bytes(16)
        self._password = self._password_digest(password)
        self._sessions = {}
        self._login_failures = deque(maxlen=12)
        self.logs = deque(maxlen=500)
        self.debug = deque(maxlen=256)
        self._log_sequence = self._debug_sequence = 0
        self.log_level = "info"
        self.closed = threading.Event()
        self._relay = None
        self.ui = default_ui_config()
        if self._ui_path.exists():
            saved = _decode_json(self._ui_path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                compatible = {
                    section: {key: value for key, value in values.items()
                              if key in self.ui[section]}
                    for section, values in saved.items()
                    if section in {"appearance", "layout", "pages"} and isinstance(values, dict)
                }
                self.ui = _merge_ui(self.ui, compatible)
        self.log("info", "WebUI", "NyXion Heredity management API started.")

    def plugin_catalog(self):
        if self.plugin_host is None:
            return {"api_version": "1.0", "host_running": False, "plugins": [], "sessions": []}
        return self.plugin_host.catalog()

    def plugin_operation(self, plugin_id, operation, config=None, *, instance_key=None):
        if self.plugin_host is None:
            raise ManagementError("插件宿主尚未运行。", 503, "PLUGIN_HOST_UNAVAILABLE")
        try:
            options = {} if instance_key is None else {"instance_key": instance_key}
            if operation == "start":
                return self.plugin_host.start(plugin_id, config, **options)
            if operation == "stop":
                return self.plugin_host.stop(plugin_id, **options)
            if operation == "retry":
                return self.plugin_host.retry(plugin_id, **options)
            if operation == "restart":
                return self.plugin_host.retry(plugin_id, **options)
            if operation == "list_instances":
                return {"instances": self.plugin_host.instances(plugin_id)}
            if operation == "create_instance":
                return self.plugin_host.create_instance(
                    plugin_id, config["instance_key"], config["display_name"])
            if operation == "remove_instance":
                return self.plugin_host.remove_instance(plugin_id, instance_key)
            challenge = self.plugin_host.challenge(plugin_id, **options)
        except ValueError as exc:
            status = getattr(exc, "status", 400)
            code = getattr(exc, "code", "PLUGIN_REQUEST_REJECTED")
            if type(status) is not int or status not in {400, 403, 404, 409, 422, 503, 504}:
                status = 400
            if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
                code = "PLUGIN_REQUEST_REJECTED"
            raise ManagementError("插件操作未完成，请检查配置和运行状态。", status, code) from None
        if challenge is None:
            return {"challenge": None}
        if not isinstance(challenge, dict):
            raise ManagementError("插件交互格式无效。", 502, "INVALID_PLUGIN_CHALLENGE")
        payload = challenge.get("payload", {})
        if not isinstance(payload, dict):
            raise ManagementError("插件交互格式无效。", 502, "INVALID_PLUGIN_CHALLENGE")
        # Only a decoded PNG is exposed as image content. Plugin-local paths,
        # login URLs and credentials do not belong in the management response.
        public_payload = {}
        if isinstance(payload.get("message"), str):
            public_payload["message"] = payload["message"][:8192]
        for key in ("status", "stage", "code"):
            if isinstance(payload.get(key), str):
                public_payload[key] = payload[key][:128]
        if "image_base64" in payload:
            encoded = payload["image_base64"]
            try:
                if (payload.get("mime") != "image/png" or not isinstance(encoded, str)
                        or len(encoded) > 3 * 1024 * 1024):
                    raise ValueError
                raw = base64.b64decode(encoded, validate=True)
                if not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw) > 2 * 1024 * 1024:
                    raise ValueError
            except (ValueError, TypeError):
                raise ManagementError("插件二维码格式无效。", 502, "INVALID_PLUGIN_CHALLENGE") from None
            public_payload.update(image_base64=encoded, mime="image/png")
        return {"challenge": {key: challenge.get(key) for key in
                              ("challenge_id", "kind", "title", "expires_at")}
                | {"payload": public_payload}}

    def account_selection(self, account=None, *, select=False):
        if self.plugin_gateway is None:
            raise ManagementError("插件账号网关尚未运行。", 503, "PLUGIN_GATEWAY_UNAVAILABLE")
        try:
            with self._connection_lock:
                if select:
                    result = self.plugin_gateway.select_account(account)
                    with self.gateway._lock:
                        self.gateway._snapshot = None
                        self.gateway._snapshot_at = 0
                    return result
                return self.plugin_gateway.selection()
        except ValueError:
            raise ManagementError("请选择当前在线的插件账号。", 400, "INVALID_ACCOUNT_SELECTION") from None

    def host_connection_settings(self, payload=None):
        if self.host_connections is None:
            raise ManagementError("当前服务未连接本体连接管理。", 503, "HOST_CONNECTIONS_UNAVAILABLE")
        try:
            with self._connection_lock:
                if payload is None:
                    return self.host_connections.snapshot()
                result = self.host_connections.update(payload)
                return result
        except ValueError:
            raise ManagementError("本体连接配置无效，请检查地址、令牌及开关。",
                                  400, "INVALID_HOST_CONNECTIONS") from None

    def network_operation(self, operation, identifier=None, payload=None):
        if self.network_interfaces is None:
            raise ManagementError("网络接口管理尚未运行。", 503, "NETWORK_INTERFACES_UNAVAILABLE")
        try:
            manager = self.network_interfaces
            if operation == "catalog":
                return manager.catalog()
            if operation == "get":
                return manager.get(identifier)
            if operation == "create":
                return manager.create(payload)
            if operation == "update":
                return manager.update(identifier, payload)
            if operation == "start":
                return manager.start(identifier)
            if operation == "stop":
                return manager.stop(identifier)
            if operation == "retry":
                return manager.retry(identifier)
            if operation == "credentials":
                return manager.credentials(identifier)
            if operation == "regenerate-token":
                return manager.regenerate_token(identifier)
            if operation == "delete":
                return manager.delete(identifier)
            raise ValueError("Unknown network operation")
        except NetworkInterfaceError as error:
            raise ManagementError(str(error), error.status, error.code) from None
        except ValueError:
            raise ManagementError("网络接口配置无效。", 400, "INVALID_NETWORK_INTERFACE") from None

    def network_request_operation(self, operation, identifier=None, payload=None, *,
                                  include_history=False):
        if self.network_allocator is None:
            raise ManagementError("模块接口分配服务尚未运行。", 503, "NETWORK_ALLOCATOR_UNAVAILABLE")
        try:
            if operation == "catalog":
                return self.network_allocator.catalog(include_history=include_history)
            if operation == "auto-start":
                if (type(payload) is not dict or set(payload) != {"enabled"}
                        or type(payload["enabled"]) is not bool):
                    raise ManagementError("自动启动需要 enabled 布尔值。")
                return self.network_allocator.set_auto_start(identifier, payload["enabled"])
            if operation == "reapply":
                if payload:
                    raise ManagementError("重新申请不能包含其他配置。")
                return self.network_allocator.reapply(identifier)
            if operation == "assign":
                if type(payload) is not dict:
                    raise ManagementError("接口分配需要接口 ID 或接口 ID 列表。")
                if set(payload) == {"interface_id"} and isinstance(payload["interface_id"], str):
                    return self.network_allocator.assign(identifier, payload["interface_id"])
                if (set(payload) == {"interface_ids"}
                        and type(payload["interface_ids"]) is list
                        and all(isinstance(value, str) for value in payload["interface_ids"])):
                    return self.network_allocator.assign_many(identifier, payload["interface_ids"])
                raise ManagementError("提交 interface_ids 列表，或单个 interface_id。")
            if operation == "unassign":
                if type(payload) is not dict or set(payload) - {"interface_id"}:
                    raise ManagementError("撤销分配只接受可选的 interface_id。")
                if "interface_id" in payload and not isinstance(payload["interface_id"], str):
                    raise ManagementError("撤销接口 ID 必须为字符串。")
                return self.network_allocator.unassign(identifier, payload.get("interface_id"))
            if operation == "deny":
                if payload:
                    raise ManagementError("拒绝分配不需要其他字段。")
                return self.network_allocator.deny(identifier)
            raise ManagementError("模块接口操作无效。")
        except NetworkInterfaceError as failure:
            raise ManagementError(str(failure), failure.status, failure.code) from None
        except NetworkAllocationError as failure:
            raise ManagementError(str(failure), failure.status, failure.code) from None
        except (ContractError, NetworkBindingClosed, ValueError):
            raise ManagementError("模块请求与所选接口不匹配或已撤销。",
                                  409, "NETWORK_ALLOCATION_REJECTED") from None
        except KeyError:
            raise ManagementError("模块请求不存在。", 404, "NETWORK_REQUEST_NOT_FOUND") from None

    def market_operation(self, operation, identifier=None, payload=None):
        if self.marketplace is None:
            raise ManagementError("模块商场尚未运行。", 503, "MARKET_UNAVAILABLE")
        try:
            if operation == "catalog":
                result = self.marketplace.snapshot()
                if self.module_bus is not None:
                    modules = self.module_bus.snapshot()
                    result["modules"] = modules["modules"]
                    result["jobs"].extend(modules["jobs"])
                return result
            if operation == "modules":
                return self.module_bus.snapshot() if self.module_bus is not None else {
                    "modules": [], "tree": {"namespace": "/", "children": []}, "jobs": []}
            if operation == "refresh":
                if payload:
                    raise ManagementError("刷新目录不能包含其他字段。")
                return self.marketplace.refresh()
            if operation == "install":
                required = {"plugin_id", "expected_version", "expected_sha256",
                            "confirmed_permissions", "force_warning"}
                if type(payload) is not dict or set(payload) != required:
                    raise ManagementError("安装需要完整的包身份、版本、摘要和权限确认。")
                if (not isinstance(payload["plugin_id"], str)
                        or not isinstance(payload["expected_version"], str)
                        or not isinstance(payload["expected_sha256"], str)
                        or type(payload["confirmed_permissions"]) is not list
                        or type(payload["force_warning"]) is not bool):
                    raise ManagementError("安装确认字段格式无效。")
                return self.marketplace.install(
                    payload["plugin_id"], expected_version=payload["expected_version"],
                    expected_sha256=payload["expected_sha256"],
                    confirmed_permissions=payload["confirmed_permissions"],
                    force_warning=payload["force_warning"])
            if operation == "job":
                try:
                    return self.marketplace.job(identifier)
                except MarketplaceError as failure:
                    if failure.status != 404 or self.module_bus is None:
                        raise
                    return self.module_bus.job(identifier)
            if operation in {"start", "stop", "restart"}:
                if payload:
                    raise ManagementError("模块运行操作不能包含其他配置。")
                if self.module_bus is None:
                    raise ManagementError("Aegis 模块总线尚未运行。", 503, "AEGIS_UNAVAILABLE")
                return self.module_bus.submit(operation, identifier)
            raise ManagementError("模块商场操作无效。")
        except (MarketplaceError, ModuleBusError) as failure:
            raise ManagementError(str(failure), failure.status, failure.code) from None

    def settings_operation(self, payload=None):
        if self.settings is None:
            raise ManagementError("启动配置管理尚未运行。", 503, "SETTINGS_UNAVAILABLE")
        try:
            return self.settings.snapshot() if payload is None else self.settings.update(payload)
        except SettingsError as failure:
            raise ManagementError(str(failure), failure.status, failure.code) from None

    def attention(self):
        items = {}

        def add(path, severity, count, message):
            if not count:
                return
            previous = items.get(path)
            items[path] = {
                "path": path, "severity": "error" if severity == "error"
                    or previous is not None and previous["severity"] == "error" else "warning",
                "count": count + (previous["count"] if previous else 0),
                "message": message,
            }

        if self.network_interfaces is not None:
            interfaces = self.network_interfaces.catalog()["interfaces"]
            failed = [item for item in interfaces if item["state"] == "error"]
            add("/connections", "error", len(failed),
                "网络服务启动或连接失败，请检查端口占用与接口配置。")
        if self.network_allocator is not None:
            requests = self.network_allocator.catalog()["requests"]
            pending = [item for item in requests if item["state"] in {"pending", "revoked", "expired"}
                       and item.get("module_active") is True and not item.get("restoring")]
            add("/connections", "warning", len(pending),
                "存在需要分配或重新申请的模块接口。")
        if self.plugin_host is not None:
            catalog = self.plugin_host.catalog()
            for plugin in catalog["plugins"]:
                failures = [instance for instance in plugin["instances"]
                            if instance["state"] == "failed"
                            or (instance.get("session") or {}).get("state") == "failed"]
                if failures:
                    pages = plugin.get("ui_pages", [])
                    path = pages[0]["path"] if pages else "/plugins"
                    add(path, "error", len(failures),
                        "插件或账号会话出现异常，请检查对应页面。")
        if self.marketplace is not None:
            market = self.marketplace.snapshot()
            issues = len(market.get("invalid_entries", [])) + len(market.get("installed_errors", []))
            if market.get("error") is not None:
                issues += 1
            add("/market", "warning", issues, "模块目录或安装记录需要注意，请查看模块商场。")
        if self.module_bus is not None:
            failures = sum(item["state"] == "failed" for item in self.module_bus.snapshot()["modules"])
            add("/market", "error", failures, "Aegis 已隔离异常模块，请查看模块商场。")
        if self.settings is not None:
            try:
                changed = self.settings.snapshot()["startup"]["restart_required"]
                add("/settings", "warning", int(changed), "启动端口已修改，下次启动生效。")
            except SettingsError:
                add("/settings", "error", 1, "启动配置文件无效，请检查本机 .env 配置。")
        return {"healthy": not items, "items": list(items.values())}

    def update_internal_gateway_token(self, token, *, api_path=None):
        with self._connection_lock:
            gateway = self._default_gateway
            with gateway._lock:
                gateway.token = token
                if api_path is not None:
                    gateway.api_path = api_path
                gateway._snapshot = None
                gateway._snapshot_at = 0

    def _password_digest(self, value):
        return hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"), self._salt, 100000)

    def login(self, password):
        with self._lock:
            now = time.monotonic()
            while self._login_failures and self._login_failures[0] < now - 60:
                self._login_failures.popleft()
            if len(self._login_failures) >= 10:
                raise ManagementError("登录尝试过多，请稍后重试。", 429, "RATE_LIMITED")
            valid = isinstance(password, str) and len(password) <= 1024 and hmac.compare_digest(
                self._password_digest(password), self._password)
            if not valid:
                self._login_failures.append(now)
                raise ManagementError("密码不正确。", 401, "UNAUTHORIZED")
            self._sessions = {token: expires for token, expires in self._sessions.items()
                              if expires > now}
            if len(self._sessions) >= 128:
                self._sessions.pop(next(iter(self._sessions)))
            token = secrets.token_urlsafe(32)
            self._sessions[token] = now + SESSION_SECONDS
            return {"success": True, "token": token, "mustChangePassword": False}

    def authorized(self, token):
        with self._lock:
            return self._sessions.get(token, 0) > time.monotonic()

    def logout(self, token):
        with self._lock:
            self._sessions.pop(token, None)

    def log(self, level, scope, message):
        with self._condition:
            self._log_sequence += 1
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            self.logs.append({"id": self._log_sequence, "time": stamp, "level": level,
                              "scope": scope, "message": message,
                              "line": f"{stamp} [{level}] [{scope}] {message}"})
            self._condition.notify_all()

    def publish_debug(self, value):
        with self._condition:
            self._debug_sequence += 1
            self.debug.append((self._debug_sequence, deepcopy(value)))
            self._condition.notify_all()

    def save_ui(self, patch):
        with self._lock:
            result = _merge_ui(self.ui, patch)
            temporary = self._ui_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(result, ensure_ascii=True, allow_nan=False),
                                 encoding="utf-8")
            os.replace(temporary, self._ui_path)
            self.ui = result
            return deepcopy(result)

    def _runtime_account(self, runtime):
        if runtime is None or self.plugin_host is None:
            return None
        for identity, candidate in self.plugin_host.account_runtimes():
            if candidate is runtime:
                return {
                    "account": f"{identity.platform}:{identity.self_id}",
                    "platform": identity.platform,
                    "self_id": identity.self_id,
                }
        return None

    def accounts(self):
        with self._connection_lock:
            if self.plugin_gateway is not None and isinstance(self.gateway, LocalGatewayClient):
                runtime = self.plugin_gateway.event_runtime()
                identity = self._runtime_account(runtime)
                if identity is None:
                    return []
                try:
                    login = runtime.call("get_login_info", {})
                except BackendError:
                    return []
                return [{**identity, "nickname": str(login.get("nickname", ""))}]
            account = self.gateway.snapshot()["account"]
            if not isinstance(account, dict):
                return []
            self_id = str(account["user_id"])
            return [{"account": f"onebot:{self_id}", "platform": "onebot",
                     "self_id": self_id, "nickname": str(account.get("nickname", ""))}]

    def connections(self):
        if self.network_interfaces is not None and self.plugin_host is not None:
            resources = self.network_interfaces.catalog()["interfaces"]
            return [{"account": f"{item['platform']}:{item['self_id']}",
                     "platform": item["platform"], "self_id": item["self_id"],
                     "nickname": item.get("display_name", ""),
                     "adapters": [
                         {"name": interface["name"], "kind": interface["type"],
                          "status": "ok" if interface["state"] == "running" else "down",
                          "detail": interface["endpoint"] or interface["state"]}
                         for interface in resources
                         if interface["account"] == f"{item['platform']}:{item['self_id']}"]}
                    for item in self.plugin_host.account_descriptors()]
        with self._connection_lock:
            snapshot = self.gateway.snapshot()
            account = snapshot["account"]
            if not account:
                return []
            state = snapshot["state"]
            detail = ("DEMO / simulated provider; no live platform connection" if state.get("mode") == "demo"
                      else "Heredity account runtime")
            self_id = str(account["user_id"])
            return [{"account": f"onebot:{self_id}", "platform": "onebot",
                     "self_id": self_id, "nickname": account.get("nickname", ""),
                     "adapters": [{"name": "Heredity HTTP", "kind": "httpServer",
                                   "status": "ok" if state.get("online") else "down",
                                   "detail": f"{self.gateway.origin} / {detail}"}]}]

    def connection_snapshot(self):
        with self._connection_lock:
            managed = self.connection_manager.snapshot()
            if managed["configured"]:
                return managed
            try:
                snapshot = self.gateway.snapshot(force=True)
                state, account = snapshot["state"], snapshot["account"]
                return {"configured": False, "active": {
                    "kind": "gateway", "origin": self.gateway.origin,
                    "gateway_origin": self.gateway.origin,
                    "self_id": account["user_id"] if isinstance(account, dict) else 0,
                    "nickname": account.get("nickname", "") if isinstance(account, dict) else "",
                    "online": state.get("online") is True,
                    "verified": state.get("identity_verified") is True,
                    "execution_level": state.get("execution_level"),
                    "provider_id": state.get("provider_id"), "source_kind": state.get("mode"),
                }}
            except ManagementError as exc:
                return {"configured": False, "active": None, "error": str(exc)}

    def probe_connection(self, payload):
        with self._connection_lock:
            return self.connection_manager.probe(payload)

    def connect(self, payload):
        probe_id = payload.get("probe_id")
        if not isinstance(probe_id, str) or not probe_id:
            raise ManagementError("需要先检测服务，再提交 probe_id。")
        with self._connection_lock:
            handle = self.connection_manager.connect(probe_id)
            self.gateway = GatewayClient(handle.gateway_origin, handle.gateway_token,
                                         handle.execution_token)
            self.log("info", "Connection", f"External OneBot account {handle.self_id} connected.")
            return self.connection_manager.snapshot()

    def disconnect(self):
        with self._connection_lock:
            self.gateway = self._default_gateway
            self.connection_manager.disconnect()
            self.log("info", "Connection", "External OneBot connection closed.")
            return self.connection_snapshot()

    def gateway_config(self):
        with self._connection_lock:
            if self.connection_manager.snapshot()["configured"]:
                return self.connection_manager.gateway_config()
            if isinstance(self.gateway, LocalGatewayClient):
                raise ManagementError(
                    "当前使用本地插件通道；请在网络接口中创建可访问的服务。",
                    409, "NO_NETWORK_GATEWAY")
            return {"origin": self.gateway.origin, "access_token": self.gateway.token}

    def event_config(self):
        with self._connection_lock:
            return self.connection_manager.event_config()

    def execution_info(self):
        with self._connection_lock:
            return self.gateway.execution_info()

    def switch_provider(self, payload):
        with self._connection_lock:
            result = self.gateway.switch_provider(payload)
            self.log("info", "Execution", "Provider transition request completed.")
            return result

    def config(self, account_key):
        accounts = self.accounts()
        if not any(account["account"] == account_key for account in accounts):
            raise ManagementError("账号不属于当前 Heredity 实例。", 404, "ACCOUNT_NOT_FOUND")
        if self.network_interfaces is not None:
            return {"network_interfaces": [
                item for item in self.network_interfaces.catalog()["interfaces"]
                if item["account"] == account_key]}
        origin = parse.urlsplit(self.gateway.origin)
        return {"networks": {"httpServers": [{
            "name": "Heredity HTTP", "enabled": True, "host": origin.hostname,
            "port": origin.port, "path": "/", "accessToken": "", "messageFormat": "array",
            "reportSelfMessage": False, "enableWebSocket": False, "enableSse": True}],
            "httpClients": [], "wsServers": [], "wsClients": []},
            "statusCommand": {"enabled": False, "swallow": False, "cooldownSeconds": 0,
                              "trigger": "status"},
            "historySync": {"enabled": False}, "notifications": {"channelIds": []}}

    def invoke(self, payload):
        if (not isinstance(payload, dict) or not isinstance(payload.get("action"), str)
                or not ACTION_NAME.fullmatch(payload["action"])
                or not isinstance(payload.get("params", {}), dict)):
            raise ManagementError("调试动作需要有效的 action 和 params 对象。")
        with self._connection_lock:
            action, params = payload["action"], payload.get("params", {})
            started = time.monotonic()
            if self.plugin_gateway is not None and isinstance(self.gateway, LocalGatewayClient):
                # Capture one account before executing. A concurrent selection
                # change must never redirect an already confirmed action.
                runtime = self.plugin_gateway.event_runtime()
                identity = self._runtime_account(runtime)
                if identity is None or identity["account"] != payload.get("account"):
                    raise ManagementError("账号不属于当前 OneBot 选择。", 403, "ACCOUNT_MISMATCH")
                try:
                    data = runtime.status() if action == "get_status" else (
                        {"app_name": "Heredity", "display_name": "NyXion Heredity",
                         "app_version": __version__, "protocol_version": "v11"}
                        if action == "get_version_info" else runtime.call(action, params))
                    result = {"status": "ok", "retcode": 0, "data": data}
                except BackendError as error:
                    result = {"status": "failed", "retcode": error.retcode,
                              "data": None, "message": str(error)}
                    if error.details is not None:
                        result["details"] = error.details
            else:
                if not any(account["account"] == payload.get("account") for account in self.accounts()):
                    raise ManagementError("账号不属于当前 Heredity 实例。", 403, "ACCOUNT_MISMATCH")
                result = self.gateway.action(action, params)
            self.publish_debug({"kind": "action", "account": payload["account"], "action": action,
                                "params": params, "response": result,
                                "ms": round((time.monotonic() - started) * 1000)})
            self.log("info" if result.get("status") == "ok" else "warn",
                     "Debug", f"Action {action}: {result.get('status', 'unknown')}")
            return result

    def start(self):
        if self._relay is None:
            self._relay = threading.Thread(target=self._relay_events, daemon=True,
                                           name="heredity-webui-events")
            self._relay.start()

    def _relay_events(self):
        cursors = {}
        while not self.closed.is_set():
            with self._connection_lock:
                gateway = self.gateway
            if isinstance(gateway, LocalGatewayClient):
                if self.plugin_host is not None:
                    self._relay_local_events(cursors)
                self.closed.wait(0.1)
                continue
            headers = {"Accept": "text/event-stream"}
            if gateway.token:
                headers["Authorization"] = "Bearer " + gateway.token
            try:
                outgoing = request.Request(gateway.origin + "/_heredity/events", headers=headers)
                with gateway.opener.open(outgoing, timeout=3) as response:
                    while not self.closed.is_set():
                        line = response.readline(2 * MAX_BODY + 1)
                        if not line or len(line) > 2 * MAX_BODY:
                            break
                        if line.startswith(b"data: "):
                            event = _decode_json(line[6:].decode("utf-8"))
                            if isinstance(event, dict) and "self_id" in event:
                                with self._connection_lock:
                                    if gateway is not self.gateway:
                                        break
                                    self.publish_debug({"kind": "event",
                                                        "account": f"onebot:{event['self_id']}",
                                                        "event": event})
            except (OSError, HTTPException, ValueError, error.URLError, RecursionError):
                pass
            self.closed.wait(2)

    def _relay_local_events(self, cursors):
        active = set()
        for identity, runtime in self.plugin_host.account_runtimes():
            key = (identity.platform, identity.self_id, identity.session_epoch)
            active.add(key)
            cursor = cursors.get(key, runtime.event_hub.latest_sequence)
            try:
                records = runtime.event_hub.read(cursor, timeout=0)
            except EventGapError:
                cursors[key] = runtime.event_hub.latest_sequence
                continue
            for record in records:
                with self._connection_lock:
                    if not isinstance(self.gateway, LocalGatewayClient):
                        return
                    self.publish_debug({"kind": "event",
                                        "account": f"{identity.platform}:{identity.self_id}",
                                        "event": record["event"]})
                cursor = record["sequence"]
            cursors[key] = cursor
        for key in set(cursors) - active:
            del cursors[key]

    def close(self):
        self.closed.set()
        with self._condition:
            self._condition.notify_all()
        if self._relay is not None:
            self._relay.join(timeout=4)
        with self._connection_lock:
            self.connection_manager.close()


class WebUIHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, state, static_dir=None):
        self.state = state
        self.static_dir = Path(static_dir).resolve() if static_dir else None
        super().__init__(address, _WebUIHandler)
        self.state.connection_manager.add_forbidden_origin(
            f"http://127.0.0.1:{self.server_port}")

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address

    def server_close(self):
        self.state.close()
        super().server_close()


class _WebUIHandler(BaseHTTPRequestHandler):
    server_version = f"NyXion-Heredity-WebUI/{__version__}"
    sys_version = ""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PUT(self):
        self._handle("PUT")

    def _json(self, status, value):
        data = json.dumps(value, ensure_ascii=True, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def _token(self):
        own = self.headers.get("X-Heredity-Token")
        if own is not None:
            return own
        scheme, _, value = self.headers.get("Authorization", "").partition(" ")
        return value if scheme.lower() == "bearer" else ""

    def _body(self):
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) > 1:
            raise ManagementError("Ambiguous request framing.")
        try:
            size = int(lengths[0]) if lengths else 0
            if not 0 <= size <= MAX_BODY:
                raise ValueError
            if self.headers.get_content_type() != "application/json" and size:
                raise ManagementError("JSON request required.", 415)
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError
            result = _decode_json(raw.decode("utf-8")) if raw else {}
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, UnicodeError, OSError, RecursionError):
            raise ManagementError("Invalid or oversized JSON request.") from None

    def _plugin_origin(self):
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise ManagementError("不允许跨站插件管理请求。", 403, "CROSS_ORIGIN_REJECTED")
        origin = self.headers.get("Origin")
        if origin is None:
            return
        own = {f"http://{self.headers.get('Host', '')}",
               "http://127.0.0.1:5178", "http://localhost:5178"}
        if origin not in own:
            raise ManagementError("不允许跨站插件管理请求。", 403, "CROSS_ORIGIN_REJECTED")

    def _sse(self, kind, token):
        state = self.server.state
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        cursor = 0 if kind == "logs" else state._debug_sequence
        previous = None
        try:
            self.wfile.write(b'data: {"kind":"ready"}\n\n' if kind != "logs" else b": ready\n\n")
            self.wfile.flush()
            while not state.closed.is_set() and state.authorized(token):
                frames = []
                if kind == "state":
                    if state.plugin_host is not None:
                        snapshot = [("plugins", state.plugin_catalog())]
                    else:
                        snapshot = [("accounts", state.accounts()), ("connections", state.connections())]
                    encoded = json.dumps(snapshot, sort_keys=True)
                    if encoded != previous:
                        frames = [{"resource": resource, "data": data} for resource, data in snapshot]
                        previous = encoded
                else:
                    with state._condition:
                        if kind == "logs":
                            records = [(item["id"], item) for item in state.logs]
                        else:
                            records = list(state.debug)
                        if records and cursor and cursor < records[0][0] - 1:
                            if kind == "debug":
                                frames.append({"kind": "dropped", "count": records[0][0] - cursor - 1})
                        for sequence, item in records:
                            if sequence > cursor:
                                frames.append(item)
                                cursor = sequence
                for frame in frames:
                    encoded = json.dumps(frame, ensure_ascii=True, allow_nan=False)
                    self.wfile.write(f"data: {encoded}\n\n".encode())
                if not frames:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                state.closed.wait(1)
        except (OSError, ValueError, ManagementError):
            pass

    def _static(self, path):
        root = self.server.static_dir
        if root is None:
            self._json(404, {"error": "Frontend is served by the local Vite UI on port 5178."})
            return
        relative = path.lstrip("/")
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            raise ManagementError("Path is outside the static root.", 403)
        if not target.is_file():
            if "." in Path(relative).name:
                raise ManagementError("Static file not found.", 404)
            target = root / "index.html"
        if not target.is_file():
            raise ManagementError("Frontend build is not available.", 404)
        raw = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except OSError:
            pass

    def _handle(self, method):
        state = self.server.state
        try:
            parsed = parse.urlsplit(self.path)
            path = parse.unquote(parsed.path).rstrip("/") or "/"
            query = parse.parse_qs(parsed.query, max_num_fields=100)
            if not path.startswith("/api/"):
                if method == "GET" and path.startswith("/avatar/"):
                    self._json(404, {"error": "No local avatar is available."})
                elif method == "GET":
                    self._static(path)
                else:
                    raise ManagementError("Unsupported management operation.", 501, "UNSUPPORTED")
                return
            if method == "POST" and path == "/api/login":
                self._json(200, state.login(self._body().get("password")))
                return
            if method == "GET" and path == "/api/ui/public":
                appearance = deepcopy(state.ui["appearance"])
                appearance["customCss"] = ""
                self._json(200, {"appearance": appearance})
                return
            token = self._token()
            if not state.authorized(token):
                raise ManagementError("登录已过期或尚未登录。", 401, "UNAUTHORIZED")
            if method == "GET" and path in {"/api/state/stream", "/api/logs/stream", "/api/debug/stream"}:
                self._sse(path.split("/")[2], token)
                return
            if method == "GET":
                value = self._get(path, query)
            elif method == "POST":
                value = self._post(path, self._body(), token)
            elif method == "DELETE":
                self._plugin_origin()
                network_route = NETWORK_ROUTE.fullmatch(path)
                if network_route and network_route[2] is None:
                    if self._body():
                        raise ManagementError("删除接口不能包含配置。")
                    self._json(200, state.network_operation("delete", network_route[1]))
                    return
                instance_route = PLUGIN_INSTANCE.fullmatch(path)
                if not instance_route or instance_route[3] is not None:
                    raise ManagementError("此删除操作尚未接入。", 501, "UNSUPPORTED")
                if self._body():
                    raise ManagementError("删除账号实例不能包含其他数据。")
                value = state.plugin_operation(
                    instance_route[1], "remove_instance", instance_key=instance_route[2])
            elif method == "PUT":
                self._plugin_origin()
                if path == "/api/heredity/settings":
                    self._json(200, state.settings_operation(self._body()))
                    return
                network_route = NETWORK_ROUTE.fullmatch(path)
                if not network_route or network_route[2] is not None:
                    raise ManagementError("接口更新路径无效。", 404, "NOT_FOUND")
                value = state.network_operation("update", network_route[1], self._body())
            else:
                raise ManagementError("此管理功能尚未接入 Heredity。", 501, "UNSUPPORTED")
            self._json(200, value)
        except (ManagementError, ConnectionManagementError) as exc:
            self._json(exc.status, {"success": False, "error": str(exc),
                                   "message": str(exc), "code": exc.code})
        except Exception:
            state.log("error", "WebUI", "Management request failed.")
            self._json(500, {"success": False, "error": "Management request failed.",
                             "code": "INTERNAL_ERROR"})

    def _get(self, path, query):
        state = self.server.state
        if path == "/api/status":
            return {"success": True, "app": "Heredity", "display_name": "NyXion Heredity",
                    "version": __version__, "version_label": __version_label__,
                    "plugin_host": state.plugin_host is not None}
        if path == "/api/auth/state":
            return {"mustChangePassword": False}
        if path == "/api/auth/totp":
            return {"enabled": False}
        if path == "/api/agreements":
            return {"version": "heredity-1", "consentRequired": False, "documents": [],
                    "notice": "Heredity local management interface."}
        if path == "/api/ui":
            return {"config": deepcopy(state.ui)}
        if path == "/api/accounts":
            return {"list": state.accounts()}
        if path == "/api/connections":
            return {"list": state.connections()}
        if path == "/api/system":
            return system_info()
        if path == "/api/debug/actions":
            actions = action_catalog()
            return {"actions": actions, "categories": [{"category": "Heredity", "count": len(actions)}]}
        if path.startswith("/api/config/"):
            return {"config": state.config(path.removeprefix("/api/config/"))}
        if path == "/api/logs":
            try:
                limit = min(500, max(1, int(query.get("limit", ["500"])[0])))
            except ValueError:
                raise ManagementError("Invalid log limit.") from None
            with state._lock:
                return {"list": list(state.logs)[-limit:]}
        if path == "/api/logs/level":
            return {"level": state.log_level, "levels": LOG_LEVELS}
        if path == "/api/update/check":
            return {"current": __version__, "latest": None, "hasUpdate": False, "htmlUrl": None,
                    "notes": None, "publishedAt": None, "checkedAt": int(time.time() * 1000),
                    "error": "No automatic update channel is configured."}
        if path == "/api/update/status":
            return {"current": __version__, "phase": "unavailable",
                    "message": "Update installation is not supported by the local test adapter."}
        if path == "/api/notifications/config":
            return {"config": {"channels": [], "debounceMs": 5000}}
        if path == "/api/notifications/recent":
            return {"recent": []}
        if path == "/api/heredity/status":
            return {"app": "Heredity", "version": __version__,
                    "version_label": __version_label__, "ui": "Heredity",
                    "gateway": state.gateway.origin, "state": state.gateway.snapshot()["state"]}
        if path == "/api/heredity/execution":
            return state.execution_info()
        if path == "/api/heredity/plugins":
            return state.plugin_catalog()
        if path == "/api/heredity/account-selection":
            return state.account_selection()
        if path == "/api/heredity/host-connections":
            return state.host_connection_settings()
        if path == "/api/heredity/network-interfaces":
            return state.network_operation("catalog")
        if path == "/api/heredity/network-requests":
            history = query.get("include_history", ["false"])
            if len(history) != 1 or history[0] not in {"true", "false"}:
                raise ManagementError("include_history 必须为 true 或 false。")
            return state.network_request_operation("catalog", include_history=history[0] == "true")
        if path == "/api/heredity/market":
            return state.market_operation("catalog")
        if path == "/api/heredity/settings":
            return state.settings_operation()
        if path == "/api/heredity/attention":
            return state.attention()
        if path == "/api/heredity/market/modules":
            return state.market_operation("modules")
        market_job = MARKET_JOB_ROUTE.fullmatch(path)
        if market_job:
            return state.market_operation("job", market_job[1])
        network_route = NETWORK_ROUTE.fullmatch(path)
        if network_route and network_route[2] == "credentials":
            return state.network_operation("credentials", network_route[1])
        if network_route and network_route[2] is None:
            return state.network_operation("get", network_route[1])
        instances_route = PLUGIN_INSTANCES.fullmatch(path)
        if instances_route:
            return state.plugin_operation(instances_route[1], "list_instances")
        instance_route = PLUGIN_INSTANCE.fullmatch(path)
        if instance_route and instance_route[3] == "challenge":
            return state.plugin_operation(
                instance_route[1], "challenge", instance_key=instance_route[2])
        plugin_route = PLUGIN_ROUTE.fullmatch(path)
        if plugin_route and plugin_route[2] == "challenge":
            return state.plugin_operation(plugin_route[1], "challenge")
        if path == "/api/heredity/connection":
            return state.connection_snapshot()
        if path == "/api/heredity/connection/gateway":
            return state.gateway_config()
        if path == "/api/heredity/connection/events/config":
            return state.event_config()
        raise ManagementError("此管理功能尚未接入 Heredity。", 501, "UNSUPPORTED")

    def _post(self, path, payload, token):
        state = self.server.state
        if path in {"/api/heredity/market/refresh", "/api/heredity/market/install"}:
            self._plugin_origin()
            return state.market_operation(path.rsplit("/", 1)[1], payload=payload)
        market_module = MARKET_MODULE_ROUTE.fullmatch(path)
        if market_module:
            self._plugin_origin()
            return state.market_operation(market_module[2], market_module[1], payload)
        if path == "/api/heredity/network-interfaces":
            self._plugin_origin()
            return state.network_operation("create", payload=payload)
        network_route = NETWORK_ROUTE.fullmatch(path)
        if network_route and network_route[2] in {"start", "stop", "retry", "regenerate-token"}:
            self._plugin_origin()
            if payload:
                raise ManagementError("接口启停请求不能包含配置。")
            return state.network_operation(network_route[2], network_route[1])
        allocation_route = NETWORK_REQUEST_ROUTE.fullmatch(path)
        if allocation_route:
            self._plugin_origin()
            return state.network_request_operation(
                allocation_route[2], allocation_route[1], payload)
        if path == "/api/heredity/host-connections":
            self._plugin_origin()
            return state.host_connection_settings(payload)
        if path == "/api/heredity/account-selection":
            self._plugin_origin()
            if set(payload) != {"account"}:
                raise ManagementError("请选择账号或自动选择。")
            return state.account_selection(payload["account"], select=True)
        instances_route = PLUGIN_INSTANCES.fullmatch(path)
        if instances_route:
            self._plugin_origin()
            if (set(payload) != {"instance_key", "display_name"}
                    or not isinstance(payload["instance_key"], str)
                    or not isinstance(payload["display_name"], str)):
                raise ManagementError("账号实例需要 instance_key 和 display_name。")
            return state.plugin_operation(instances_route[1], "create_instance", payload)
        instance_route = PLUGIN_INSTANCE.fullmatch(path)
        if instance_route and instance_route[3] in {"start", "stop", "retry", "restart"}:
            self._plugin_origin()
            if instance_route[3] == "start":
                if set(payload) != {"config"} or not isinstance(payload["config"], dict):
                    raise ManagementError("账号实例启动需要 config 对象。")
                return state.plugin_operation(instance_route[1], "start", payload["config"],
                                              instance_key=instance_route[2])
            if payload:
                raise ManagementError("停止请求不能包含配置。")
            return state.plugin_operation(
                instance_route[1], instance_route[3], instance_key=instance_route[2])
        plugin_route = PLUGIN_ROUTE.fullmatch(path)
        if plugin_route and plugin_route[2] in {"start", "stop", "retry", "restart"}:
            self._plugin_origin()
            operation = plugin_route[2]
            if operation == "start":
                if set(payload) != {"config"} or not isinstance(payload["config"], dict):
                    raise ManagementError("插件启动需要 config 对象。")
                return state.plugin_operation(plugin_route[1], "start", payload["config"])
            if payload:
                raise ManagementError("插件停止请求不能包含配置。")
            return state.plugin_operation(plugin_route[1], operation)
        if path == "/api/logout":
            state.logout(token)
            return {"success": True}
        if path == "/api/ui":
            return {"config": state.save_ui(payload)}
        if path == "/api/debug/invoke":
            return state.invoke(payload)
        if path == "/api/heredity/execution/switch":
            return state.switch_provider(payload)
        if path == "/api/heredity/connection/probe":
            return state.probe_connection(payload)
        if path == "/api/heredity/connection/connect":
            return state.connect(payload)
        if path == "/api/heredity/connection/disconnect":
            return state.disconnect()
        if path == "/api/logs/level":
            level = payload.get("level")
            if level not in LOG_LEVELS:
                raise ManagementError("Unknown log level.")
            state.log_level = level
            return {"level": level, "levels": LOG_LEVELS}
        if path == "/api/agreements/record-consent":
            raise ManagementError("此测试接入不能记录或授予上游复用授权。", 501, "UNSUPPORTED")
        if path.startswith("/api/config/"):
            raise ManagementError("运行连接配置需通过 Heredity CLI 修改；此面板暂为只读。",
                                  501, "UNSUPPORTED")
        raise ManagementError("此管理功能尚未接入 Heredity。", 501, "UNSUPPORTED")


def create_webui_server(host, port, password, gateway="http://127.0.0.1:5700",
                        gateway_token="", state_dir=Path(".heredity/webui"), static_dir=None,
                        execution_token="", plugin_host=None, plugin_gateway=None,
                        host_connections=None, network_interfaces=None, network_allocator=None,
                        marketplace=None, module_bus=None):
    try:
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if not local:
        raise ValueError("The Heredity WebUI can only bind a loopback address.")
    state = WebUIState(password, gateway, state_dir, gateway_token, execution_token,
                       plugin_host, plugin_gateway, host_connections, network_interfaces,
                       network_allocator, marketplace, module_bus)
    try:
        return WebUIHTTPServer((host, port), state, static_dir)
    except BaseException:
        state.close()
        raise
