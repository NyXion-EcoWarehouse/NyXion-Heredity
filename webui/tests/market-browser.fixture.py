"""Offline market UI fixture with real package checks and real WASM workers."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from heredity.application import Application
from heredity.marketplace import INDEX_URL, Marketplace
import test_aegis_runtime


class Response(io.BytesIO):
    def __init__(self, raw):
        super().__init__(raw)
        self.headers = {"Content-Length": str(len(raw))}

    def read(self, length=-1):
        time.sleep(0.015)
        return super().read(length)


def package(identifier):
    with patch.object(test_aegis_runtime, "MODULE_ID", identifier):
        return test_aegis_runtime.fixture()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    entries, files = [], {}
    for identifier, name, status, minimum, permissions in [
        ("qa.market-active", "离线事件模块", "active", "0.3.0", ["net.connect", "fs.read"]),
        ("qa.market-warning", "兼容性警告模块", "warning", "0.3.0", ["bus.subscribe"]),
        ("qa.market-banned", "已封禁模块", "banned", "0.3.0", []),
        ("qa.market-future", "新版宿主模块", "active", "1.0.0", []),
        ("qa.market-corrupt", "校验失败测试模块", "active", "0.3.0", []),
    ]:
        raw = package(identifier)
        url = f"https://packages.example.test/{identifier}.wasm?signature=fixture-private-query"
        files[url] = raw
        entries.append({
            "id": identifier, "name": name, "description": "离线目录与真实宿主安装流程验收模块。",
            "version": "1.0.0", "author": {"name": "NyXion QA", "github": "https://github.com/example"},
            "permissions": permissions, "download_url": url,
            "hash_sha256": "f" * 64 if identifier.endswith("corrupt") else hashlib.sha256(raw).hexdigest(),
            "status": status,
            "security_advisory": "测试安全提示：该版本存在兼容性限制。" if status == "warning"
                else "此模块已被禁止安装和启动。" if status == "banned" else "",
            "min_heredity_version": minimum,
        })
    entries.append({"name": "不完整目录示例", "version": "1.0.0"})
    index = json.dumps({"plugins": entries}).encode("utf-8")
    opens = []
    fail_refresh = threading.Event()

    def open_url(url, timeout):
        opens.append(url)
        if url == INDEX_URL:
            if fail_refresh.is_set():
                raise OSError("Offline fixture refresh failure")
            return Response(index)
        if url not in files:
            raise AssertionError("Unconfigured fixture destination")
        return Response(files[url])

    def marketplace(*values, **options):
        options.update(_open_url=open_url, auto_refresh=True, timeout=2)
        return Marketplace(*values, **options)

    os.environ["NYXION_MARKET_UI_PASSWORD"] = "market-browser-fixture-2026"
    options = SimpleNamespace(
        state_dir=args.state_dir, password_env="NYXION_MARKET_UI_PASSWORD",
        plugin_registry=None, account=None, host="127.0.0.1", port=0, ui_port=0,
        token_env="NYXION_MARKET_UI_TOKEN", event_url=[], sse=True,
        event_secret_env="NYXION_MARKET_UI_SECRET", static_dir=ROOT / "webui" / "dist")
    application = None
    try:
        with patch("heredity.application.Marketplace", side_effect=marketplace):
            application = Application(options)
        application.start()
        deadline = time.monotonic() + 5
        while application.marketplace.snapshot()["refreshing"] and time.monotonic() < deadline:
            time.sleep(0.01)
        print(json.dumps({
            "ui": list(application.ui.server_address),
            "scope": "offline catalog/package bytes, real host APIs and WASM workers",
        }), flush=True)
        stop = threading.Event()

        def commands():
            for line in sys.stdin:
                if not line.strip():
                    break
                command = json.loads(line)
                if command["operation"] == "fail-refresh":
                    fail_refresh.set()
                elif command["operation"] == "restore-refresh":
                    fail_refresh.clear()
                else:
                    raise ValueError("Unknown fixture command")
                print(json.dumps({"command_id": command["command_id"], "ready": True}), flush=True)
            stop.set()

        threading.Thread(target=commands, daemon=True).start()
        stop.wait(150)
    finally:
        if application is not None:
            application.close()


if __name__ == "__main__":
    main()
