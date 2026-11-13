"""The platform-independent NyXion Heredity application."""

import importlib.metadata
import json
import os
from pathlib import Path
import secrets
import threading

from nyxion_heredity_sdk import PluginManifest

from .plugins.gateway import PluginGatewayBackend
from .plugins.host import PluginHost, PluginHostError
from .webui import create_webui_server
from .network_interfaces import NetworkInterfaceManager
from .network_allocator import NetworkInterfaceAllocator
from .marketplace import Marketplace
from .module_bus import SandboxModuleBus
from .settings import SettingsError, SettingsService, ui_port
from .network_errors import listener_failure
from . import __version__, __version_label__


def default_registry_path():
    candidate = Path(__file__).resolve().parents[2] / "plugins" / "registry.json"
    return candidate if candidate.is_file() else None


def discover_plugins(host, registry=None):
    """Read source or wheel metadata without importing plugin modules."""
    source = Path(registry) if registry is not None else default_registry_path()
    if source is not None:
        host.load_registry(source)
        return
    for entry in importlib.metadata.entry_points(group="nyxion_heredity.plugins"):
        distribution = entry.dist
        if distribution is None:
            continue
        candidates = [item for item in distribution.files or ()
                      if str(item).replace("\\", "/").endswith("/plugin.json")]
        if len(candidates) != 1:
            raise PluginHostError("Installed plugin requires one packaged SDK manifest.")
        raw = distribution.locate_file(candidates[0]).read_bytes()
        if len(raw) > 128 * 1024:
            raise PluginHostError("Installed plugin manifest exceeds its size limit.")
        host.register(PluginManifest.from_dict(json.loads(raw)), entrypoint=entry.value)


def _ui_password(state_dir, environment_name):
    configured = os.environ.get(environment_name, "")
    if configured:
        if len(configured) < 12:
            raise ValueError("UI password must contain at least 12 characters.")
        return configured, None
    path = Path(state_dir) / "ui-password.txt"
    if path.exists():
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("UI password file must not be a link.")
        value = path.read_text(encoding="utf-8").strip()
        if not 12 <= len(value) <= 1024:
            raise ValueError("Saved UI password has an invalid length.")
        return value, path
    value = secrets.token_urlsafe(24)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value + "\n")
    return value, path


class Application:
    def __init__(self, args):
        self.host = self.gateway = self.ui = None
        self.connection_settings = None
        self.network_interfaces = None
        self.network_allocator = None
        self.module_bus = None
        self.marketplace = None
        self.settings = None
        self._gateway_worker = None
        self._ui_worker = None
        self._halt = threading.Event()
        self._closed = False
        self.password_file = None
        self._started = False
        self._market_refresh_on_start = getattr(args, "market_refresh_on_start", True)
        state = Path(args.state_dir).resolve()
        state.mkdir(parents=True, exist_ok=True)
        password, self.password_file = _ui_password(state, args.password_env)
        try:
            self.host = PluginHost(state)
            discover_plugins(self.host, args.plugin_registry)
            self.gateway_backend = PluginGatewayBackend(self.host, account=args.account)
            self.network_interfaces = NetworkInterfaceManager(state, self.host)
            self.network_allocator = NetworkInterfaceAllocator(self.network_interfaces)
            self.host.network_allocator = self.network_allocator
            self.module_bus = SandboxModuleBus(state, self.network_allocator, __version__)
            self.marketplace = Marketplace(
                state, __version__,
                on_install=lambda record: self.module_bus.register(record, autostart=True),
                sandbox_info=self.module_bus.sandbox_info, auto_refresh=False,
                on_refresh=self.module_bus.enforce_catalog)
            self.module_bus.record_provider = self.marketplace.installed_record
            for record in self.marketplace.installed_records():
                self.module_bus.register(record)
            static = args.static_dir
            if static is None:
                source_static = Path(__file__).resolve().parents[2] / "webui" / "dist"
                installed_static = Path(__file__).parent / "ui"
                static = source_static if source_static.is_dir() else installed_static
            management_port = ui_port(args.ui_port, allow_ephemeral=True)
            try:
                self.ui = create_webui_server(
                    "127.0.0.1", management_port, password,
                    gateway=None,
                    state_dir=state / "webui", static_dir=static, plugin_host=self.host,
                    plugin_gateway=self.gateway_backend,
                    network_interfaces=self.network_interfaces,
                    network_allocator=self.network_allocator,
                    marketplace=self.marketplace, module_bus=self.module_bus)
            except OSError as failure:
                _, message = listener_failure(failure, management_port, host="127.0.0.1")
                raise SettingsError(message) from None
            self.settings = SettingsService(
                getattr(args, "env_file", state / ".env"),
                self.ui.server_address[1], self.network_interfaces)
            self.ui.state.settings = self.settings
        except BaseException:
            self.close()
            raise

    def start(self):
        if self._closed or self._started:
            raise ValueError("Application cannot be started twice or after shutdown.")
        self._started = True
        self.network_interfaces.start_all()
        self.host.start_enabled()
        if self._market_refresh_on_start:
            self.marketplace.refresh()
        self.ui.state.start()
        self._ui_worker = threading.Thread(
            target=self.ui.serve_forever, name="nyxion-ui", daemon=True)
        self._ui_worker.start()

    def serve_forever(self):
        self.start()
        while not self._halt.wait(0.5):
            if not self._ui_worker.is_alive():
                raise RuntimeError("An application listener stopped unexpectedly.")

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._halt.set()
        try:
            if self.ui is not None:
                if self._ui_worker is not None:
                    self.ui.shutdown()
                    self._ui_worker.join(timeout=3)
                self.ui.server_close()
        finally:
            try:
                if self.marketplace is not None:
                    self.marketplace.close()
                if self.module_bus is not None:
                    self.module_bus.close()
                if self.network_allocator is not None:
                    self.network_allocator.close()
                if self.network_interfaces is not None:
                    self.network_interfaces.close()
            finally:
                if self.host is not None:
                    self.host.close()


def run_application(args, *, report):
    application = Application(args)
    try:
        report({"application": "NyXion Heredity",
                "version": __version__, "version_label": __version_label__,
                "ui": list(application.ui.server_address),
                "network_interfaces": application.network_interfaces.catalog()["interfaces"],
                "plugins": application.host.catalog(),
                "password_file": (str(application.password_file)
                                  if application.password_file is not None else None),
                "automatic_plugin_start": "enabled_profiles_only"})
        application.serve_forever()
        return 0
    finally:
        application.close()
