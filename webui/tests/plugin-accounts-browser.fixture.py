"""Bounded local UI fixture using an offline SDK plugin only."""

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from heredity.application import Application
from nyxion_heredity_sdk import (
    Challenge, PluginUIPage, SessionIdentity, SessionUpdate, validate_config,
)
from nyxion_heredity_sdk.example import OfflineExamplePlugin
from offline_host_fixture import offline_marketplace


class BrowserFixturePlugin(OfflineExamplePlugin):
    manifest = replace(
        OfflineExamplePlugin.manifest, plugin_id="qa.offline", name="离线多账号验收",
        config_schema={
            "type": "object", "additionalProperties": False,
            "required": ["expected_self_id"], "properties": {
                "expected_self_id": {"type": "integer", "minimum": 1, "maximum": 4294967295,
                                     "title": "预期测试账号"},
                "profile_version": {"type": "integer", "enum": [1, 2],
                                    "default": 1, "title": "配置版本"},
                "auto_login": {"type": "boolean", "default": False, "title": "自动登录"},
            }},
        ui_pages=(PluginUIPage("accounts", "离线多账号", "accounts", "users"),
                  PluginUIPage("secondary", "离线备用入口", "accounts", "users")))

    def start(self, context, config):
        checked = validate_config(self.manifest, config)
        with self._lock:
            self._context = context
            self._session = SessionIdentity(
                self.manifest.platform, str(checked["expected_self_id"]), context.instance_id)
            self._started = True
        if checked["expected_self_id"] == 12345678:
            context.update_session(SessionUpdate(
                None, "failed", "账号认证失败 [FIXTURE_AUTH_FAILURE]：离线诊断示例"))
            context.publish_challenge(Challenge(
                "fixture-notice", "notice", "登录状态",
                {"message": "账号认证失败 [FIXTURE_AUTH_FAILURE]：离线诊断示例",
                 "status": "failed"}))
        else:
            context.update_session(SessionUpdate(self._session, "online"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=90)
    args = parser.parse_args()
    os.environ["NYXION_BROWSER_TEST_PASSWORD"] = "offline-browser-fixture-2026"
    options = SimpleNamespace(
        state_dir=args.state_dir, password_env="NYXION_BROWSER_TEST_PASSWORD", plugin_registry=None,
        account=None, host="127.0.0.1", port=0, ui_port=0,
        token_env="NYXION_BROWSER_TEST_TOKEN", event_url=[], sse=True,
        event_secret_env="NYXION_BROWSER_TEST_SECRET", static_dir=ROOT / "webui" / "dist")
    with offline_marketplace():
        app = Application(options)
    try:
        app.host.register(BrowserFixturePlugin.manifest, factory=BrowserFixturePlugin)
        app.host.create_instance("qa.offline", "first", "测试账号 A")
        app.host.create_instance("qa.offline", "second", "测试账号 B")
        app.host.start("qa.offline", {"expected_self_id": 12345678})
        app.host.start("qa.offline", {"expected_self_id": 12345679}, instance_key="first")
        app.host.start("qa.offline", {"expected_self_id": 12345680}, instance_key="second")
        app.start()
        print(json.dumps({"ui": list(app.ui.server_address),
                          "scope": "offline SDK fixtures, no platform login"}, ensure_ascii=False),
              flush=True)
        stop = threading.Event()
        def read_shutdown():
            sys.stdin.readline()
            stop.set()
        threading.Thread(target=read_shutdown, daemon=True).start()
        stop.wait(args.seconds)
    finally:
        app.close()


if __name__ == "__main__":
    main()
