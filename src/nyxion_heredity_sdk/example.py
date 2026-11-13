"""An offline fixture plugin illustrating the public SDK, with no host imports."""

from collections.abc import Mapping
import threading

from . import (
    ActionRequest, ActionResult, ContractError, Heartbeat, HostHello, PluginContext,
    PluginHello, PluginManifest, Pong, SessionIdentity, SessionUpdate,
    require_session, validate_config,
)


class OfflineExamplePlugin:
    """A synthetic account for conformance and tutorials; never a real platform."""

    manifest = PluginManifest(
        plugin_id="example.offline", name="Offline SDK example", version="1.0.0",
        platform="example", actions=("get_login_info", "echo"),
        capabilities=("synthetic",),
        config_schema={
            "type": "object",
            "properties": {"self_id": {"type": "string", "minLength": 1, "maxLength": 32}},
            "required": ["self_id"], "additionalProperties": False,
        })

    def __init__(self):
        self._lock = threading.RLock()
        self._hello = None
        self._context = None
        self._session = None
        self._started = False
        self._stopped = False
        self._last_ping = 0

    def handshake(self, hello: HostHello) -> PluginHello:
        with self._lock:
            if self._hello is not None or self._stopped:
                raise ContractError("one plugin instance accepts one handshake")
            if not self.manifest.supports_host(hello.api_major, hello.api_minor):
                raise ContractError("host API is incompatible")
            self._hello = hello
            return PluginHello(hello.instance_id, hello.nonce, self.manifest.plugin_id)

    def start(self, context: PluginContext, config: Mapping) -> None:
        with self._lock:
            if (self._hello is None or self._started or self._stopped
                    or context.instance_id != self._hello.instance_id):
                raise ContractError("start requires this instance's completed handshake")
            config = validate_config(self.manifest, config)
            self._context = context
            self._session = SessionIdentity(
                self.manifest.platform, config["self_id"], context.instance_id)
            self._started = True
        context.update_session(SessionUpdate(self._session, "online"))

    def ping(self, heartbeat: Heartbeat) -> Pong:
        with self._lock:
            if (not self._started or self._stopped
                    or heartbeat.instance_id != self._hello.instance_id
                    or heartbeat.sequence <= self._last_ping):
                raise ContractError("heartbeat is stale or belongs to another instance")
            self._last_ping = heartbeat.sequence
            return Pong(heartbeat.instance_id, heartbeat.nonce, heartbeat.sequence)

    def call(self, request: ActionRequest) -> ActionResult:
        request.require_live()
        with self._lock:
            if not self._started or self._stopped:
                return ActionResult(
                    request.request_id, "not_started", error_code=1503,
                    error_message="example session is stopped")
            require_session(request.session, self._session)
            if request.action == "get_login_info":
                if not self._session.self_id.isdecimal():
                    return ActionResult(
                        request.request_id, "not_started", error_code=1400,
                        error_message="this example requires a numeric OneBot v11 identity")
                return ActionResult(request.request_id, "completed", {
                    "user_id": int(self._session.self_id), "nickname": "Offline SDK example"})
            if request.action == "echo":
                return ActionResult(request.request_id, "completed", request.params)
            return ActionResult(
                request.request_id, "not_started", error_code=1404,
                error_message="action is not implemented by this example")

    def stop(self, reason: str) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            context, session = self._context, self._session
        if context is not None:
            context.update_session(SessionUpdate(session, "stopped"))


def create_plugin():
    return OfflineExamplePlugin()
