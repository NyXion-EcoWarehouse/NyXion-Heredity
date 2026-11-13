"""Bounded real host network-interface UI fixture; no platform networking."""

import argparse
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from heredity.application import Application
from nyxion_heredity_sdk import NetworkInterfaceRequirement
from nyxion_heredity_sdk.example import OfflineExamplePlugin
from offline_host_fixture import offline_marketplace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    os.environ["NYXION_NETWORK_UI_PASSWORD"] = "network-interfaces-fixture-2026"
    options = SimpleNamespace(
        state_dir=args.state_dir, password_env="NYXION_NETWORK_UI_PASSWORD",
        plugin_registry=None, account=None, host="127.0.0.1", port=0, ui_port=0,
        token_env="NYXION_NETWORK_UI_TOKEN", event_url=[], sse=True,
        event_secret_env="NYXION_NETWORK_UI_SECRET", static_dir=ROOT / "webui" / "dist")
    with offline_marketplace():
        application = Application(options)
    module = None
    scopes = {}
    current = {}
    try:
        application.host.register(OfflineExamplePlugin.manifest, factory=OfflineExamplePlugin)
        application.host.start("example.offline", {"self_id": "12345678"})
        deadline = time.monotonic() + 3
        while not application.host.account_runtimes() and time.monotonic() < deadline:
            time.sleep(0.005)
        interface = application.network_interfaces.create({
            "name": "离线 SDK 接口", "type": "http_server",
            "account": "example:12345678", "settings": {}})
        application.start()
        application.network_interfaces.start(interface["interface_id"])
        module = application.network_interfaces.bind_module(
            "example.network-observer", interface["interface_id"])
        requirements = {
            "qa.interface-request": NetworkInterfaceRequirement(
                "qa.interface-request", ("http_server", "websocket_server"),
                ("call", "receive"), account="example:12345678", purpose="离线模块接口分配验收"),
            "qa.released-request": NetworkInterfaceRequirement(
                "qa.released-request", ("http_server",), ("receive",),
                account="example:12345678", purpose="已释放申请的重新申请验收"),
            "qa.inactive-request": NetworkInterfaceRequirement(
                "qa.inactive-request", ("http_server",), ("receive",),
                account="example:12345678", purpose="未运行模块不能恢复旧申请"),
            "qa.history-request": NetworkInterfaceRequirement(
                "qa.history-request", ("http_server",), ("receive",),
                account="example:12345678", purpose="申请历史去重验收"),
        }
        for name, requirement in requirements.items():
            scopes[name] = application.network_allocator.for_module(name)
            current[name] = scopes[name].declare_interface(requirement)
        released = current["qa.released-request"]
        application.network_allocator.release_interface(released["request_id"])
        application.network_allocator.release_module("qa.inactive-request")
        scopes.pop("qa.inactive-request")
        application.network_allocator.release_interface(current["qa.history-request"]["request_id"])
        current["qa.history-request"] = scopes["qa.history-request"].declare_interface(
            requirements["qa.history-request"])
        print(json.dumps({
            "ui": list(application.ui.server_address),
            "scope": "real network-interface host and offline SDK account only",
        }), flush=True)
        stop = threading.Event()
        def shutdown_on_stdin():
            for line in sys.stdin:
                if not line.strip():
                    break
                command = json.loads(line)
                name = command.get("module_id", "qa.interface-request")
                if name not in requirements:
                    raise ValueError("Unknown offline fixture module")
                operation = command["operation"]
                if operation == "stop":
                    application.network_allocator.release_module(name)
                    scopes.pop(name, None)
                    result = None
                elif operation == "start":
                    scopes[name] = application.network_allocator.for_module(name)
                    result = scopes[name].declare_interface(requirements[name])
                    current[name] = result
                elif operation == "release":
                    records = application.network_allocator.catalog()["requests"]
                    latest = next(item for item in reversed(records) if item["module_id"] == name)
                    application.network_allocator.release_interface(latest["request_id"])
                    result = None
                else:
                    raise ValueError("Unknown offline fixture operation")
                print(json.dumps({"command_id": command["command_id"], "snapshot": result}), flush=True)
            stop.set()
        threading.Thread(target=shutdown_on_stdin, daemon=True).start()
        stop.wait(180)
    finally:
        if module is not None:
            module.close()
        application.close()


if __name__ == "__main__":
    main()
