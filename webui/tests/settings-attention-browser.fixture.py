"""Bounded startup settings and real interface/plugin failure UI fixture."""

import argparse
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from heredity.application import Application
from heredity.marketplace import Marketplace
from nyxion_heredity_sdk import NetworkInterfaceRequirement, PluginUIPage, SessionUpdate
from nyxion_heredity_sdk.example import OfflineExamplePlugin


class SettingsFixturePlugin(OfflineExamplePlugin):
    manifest = replace(
        OfflineExamplePlugin.manifest, plugin_id="qa.offline", name="离线诊断插件",
        ui_pages=(PluginUIPage("accounts", "离线诊断"),))

    def start(self, context, config):
        super().start(context, config)
        context.update_session(SessionUpdate(None, "failed", "Offline fixture account failure"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    args.state_dir.mkdir(parents=True, exist_ok=True)
    env_file = args.state_dir / ".env"
    env_file.write_text("# fixture settings\nOTHER_VALUE=preserved\n", encoding="utf-8")
    os.environ["NYXION_SETTINGS_UI_PASSWORD"] = "settings-browser-fixture-2026"
    options = SimpleNamespace(
        state_dir=args.state_dir, password_env="NYXION_SETTINGS_UI_PASSWORD",
        plugin_registry=None, account=None, host="127.0.0.1", port=0, ui_port=0,
        token_env="NYXION_SETTINGS_UI_TOKEN", event_url=[], sse=True,
        env_file=env_file, event_secret_env="NYXION_SETTINGS_UI_SECRET", static_dir=ROOT / "webui" / "dist")

    class Response(io.BytesIO):
        def __init__(self):
            raw = json.dumps({"plugins": [{"name": "无效测试目录条目"}]}).encode()
            super().__init__(raw)
            self.headers = {"Content-Length": str(len(raw))}

    def marketplace(*values, **kwargs):
        kwargs.update(auto_refresh=False, _open_url=lambda *args: Response())
        return Marketplace(*values, **kwargs)

    app = None
    reserved = socket.socket()
    reserved.bind(("127.0.0.1", 0))
    reserved.listen()
    scopes = []
    try:
        with patch("heredity.application.Marketplace", side_effect=marketplace):
            app = Application(options)
        env_file.write_text(
            f"# fixture settings\nOTHER_VALUE=preserved\nHEREDITY_UI_PORT={app.ui.server_address[1]}\n",
            encoding="utf-8")
        app.host.register(SettingsFixturePlugin.manifest, factory=SettingsFixturePlugin)
        app.host.start("qa.offline", {"self_id": "12345678"})
        good = app.network_interfaces.create({
            "name": "离线服务", "type": "http_server", "settings": {}})
        conflict = app.network_interfaces.create({
            "name": "端口冲突服务", "type": "http_server",
            "settings": {"port": reserved.getsockname()[1]}})
        configured = app.network_interfaces.create({
            "name": "独立 WS 服务", "type": "websocket_server", "settings": {"port": 9999}})
        app.network_interfaces.start(good["interface_id"])
        app.network_interfaces.start(conflict["interface_id"])
        scope = app.network_allocator.for_module("qa.settings-request")
        scopes.append(scope)
        scope.declare_interface(NetworkInterfaceRequirement(
            "qa.settings-request", ("http_server",), ("receive",), purpose="待分配接口提醒验收"))
        app.start()
        app.marketplace.refresh()
        deadline = time.monotonic() + 3
        while app.marketplace.snapshot()["refreshing"] and time.monotonic() < deadline:
            time.sleep(0.01)
        print(json.dumps({
            "ui": list(app.ui.server_address), "env_file": str(env_file),
            "good_interface_id": good["interface_id"], "conflict_interface_id": conflict["interface_id"],
            "scope": "isolated dotenv and offline local service/plugin errors",
        }), flush=True)
        stop = threading.Event()

        def commands():
            for line in sys.stdin:
                if not line.strip():
                    break
                command = json.loads(line)
                if command["operation"] == "read-env":
                    result = {"text": env_file.read_text(encoding="utf-8")}
                elif command["operation"] == "repair-network":
                    reserved.close()
                    result = app.network_interfaces.retry(conflict["interface_id"])
                else:
                    raise ValueError("Unknown fixture command")
                print(json.dumps({"command_id": command["command_id"], "result": result}), flush=True)
            stop.set()

        threading.Thread(target=commands, daemon=True).start()
        stop.wait(120)
    finally:
        reserved.close()
        if app is not None:
            app.close()


if __name__ == "__main__":
    main()
