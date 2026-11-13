"""A bounded local host and offline SDK identity for external bridge UI checks."""

import argparse
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from heredity.application import Application
from nyxion_heredity_sdk.example import OfflineExamplePlugin
from offline_host_fixture import offline_marketplace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    os.environ["NYXION_CONNECTIONS_QA_PASSWORD"] = "host-connections-fixture-2026"
    options = SimpleNamespace(
        state_dir=args.state_dir, password_env="NYXION_CONNECTIONS_QA_PASSWORD",
        plugin_registry=None, account=None, host="127.0.0.1", port=0, ui_port=0,
        token_env="NYXION_CONNECTIONS_QA_TOKEN", event_url=[], sse=True,
        event_secret_env="NYXION_CONNECTIONS_QA_SECRET", static_dir=ROOT / "webui" / "dist")
    with offline_marketplace():
        app = Application(options)
    try:
        app.host.register(OfflineExamplePlugin.manifest, factory=OfflineExamplePlugin)
        app.host.start("example.offline", {"self_id": "12345678"})
        app.start()
        print(json.dumps({
            "ui": list(app.ui.server_address),
            "scope": "real host with offline SDK account only",
        }), flush=True)
        stop = threading.Event()
        def read_shutdown():
            sys.stdin.readline()
            stop.set()
        threading.Thread(target=read_shutdown, daemon=True).start()
        stop.wait(90)
    finally:
        app.close()


if __name__ == "__main__":
    main()
