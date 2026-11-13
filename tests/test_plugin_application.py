"""The real host/UI/OneBot boundary, using the SDK's offline example only."""

import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from heredity.application import Application
from nyxion_heredity_sdk.example import OfflineExamplePlugin


ROOT = Path(__file__).resolve().parents[1]
PASSWORD_ENV = "NYXION_TEST_UI_PASSWORD"


def wait_for(check, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("local application state did not converge")


def request(server, path, method="GET", payload=None, token=None):
    if type(server) is dict:
        address = urlsplit(server["endpoint"])
        connection = http.client.HTTPConnection(address.hostname, address.port, timeout=3)
    else:
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    headers = {}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    try:
        connection.request(method, path, json.dumps(payload) if payload is not None else None, headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


class PluginApplicationTests(unittest.TestCase):
    def onebot(self, app, **settings):
        resource = app.network_interfaces.create({
            "name": "Explicit test interface", "type": "http_server", "enabled": True,
            "settings": {"host": "127.0.0.1", "port": 0, "sse_enabled": True, **settings}})
        token = app.network_interfaces.credentials(resource["interface_id"])["access_token"]
        return resource, token

    def application(self, directory, **overrides):
        options = dict(
            state_dir=Path(directory), password_env=PASSWORD_ENV, plugin_registry=None,
            market_refresh_on_start=False,
            account=None, host="127.0.0.1", port=0, ui_port=0,
            token_env="NYXION_TEST_ACCESS_TOKEN", event_url=[], sse=True,
            event_secret_env="NYXION_TEST_EVENT_SECRET", static_dir=None)
        with patch.dict(os.environ, {PASSWORD_ENV: "local-fixture-password-2026"}):
            application = Application(SimpleNamespace(**(options | overrides)))
        self.addCleanup(application.close)
        return application

    def test_metadata_only_host_starts_without_adapters_or_crypto_imports(self):
        code = (
            "import sys,tempfile;sys.path.insert(0,sys.argv[1]);"
            "from heredity.application import discover_plugins;"
            "from heredity.plugins.host import PluginHost;"
            "root=tempfile.TemporaryDirectory();host=PluginHost(root.name);"
            "discover_plugins(host);"
            "assert host.catalog()['plugins']==[];"
            "assert not any(n.startswith(('heredity.protocol',"
            "'cryptography')) for n in sys.modules);"
            "assert not any(n.startswith('nyxion_heredity_') and "
            "not n.startswith('nyxion_heredity_sdk') for n in sys.modules);"
            "host.close();root.cleanup();print('host-only-import-ok')"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", code, str(ROOT / "src")],
            cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("host-only-import-ok", result.stdout)

    def test_onebot_listener_survives_plugin_login_and_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.application(directory)
            app.host.register(OfflineExamplePlugin.manifest, factory=OfflineExamplePlugin)
            app.start()
            resource, token = self.onebot(app)
            status, payload = request(resource, "/get_status", token=token)
            self.assertEqual(status, 200)
            self.assertEqual(payload["data"]["state"], "waiting_for_plugin")
            app.host.start("example.offline", {"self_id": "12345678"})
            wait_for(lambda: app.gateway_backend.online)
            status, identity = request(resource, "/get_login_info", token=token)
            self.assertEqual(status, 200)
            self.assertEqual(identity["data"]["user_id"], 12345678)
            app.host.stop("example.offline")
            wait_for(lambda: app.host.catalog()["plugins"][-1]["state"] == "stopped")
            status, payload = request(resource, "/get_status", token=token)
            self.assertEqual(status, 200)
            self.assertFalse(payload["data"]["online"])
            self.assertTrue(app._ui_worker.is_alive())
            app.close()
            self.assertFalse(app._ui_worker.is_alive())

    def test_authenticated_ui_can_start_and_stop_an_sdk_plugin(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.application(directory)
            app.host.register(OfflineExamplePlugin.manifest, factory=OfflineExamplePlugin)
            app.start()
            _, login = request(app.ui, "/api/login", "POST",
                               {"password": "local-fixture-password-2026"})
            token = login["token"]
            status, _ = request(app.ui, "/api/heredity/plugins")
            self.assertEqual(status, 401)
            status, result = request(
                app.ui, "/api/heredity/plugins/example.offline/start", "POST",
                {"config": {"self_id": "12345678"}}, token=token)
            self.assertEqual(status, 200, result)
            wait_for(lambda: app.gateway_backend.online)
            status, catalog = request(app.ui, "/api/heredity/plugins", token=token)
            self.assertEqual(status, 200)
            self.assertTrue(catalog["host_running"])
            active = next(item for item in catalog["plugins"] if item["id"] == "example.offline")
            self.assertTrue(active["handshake"]["verified"])
            self.assertEqual(active["session"]["state"], "online")
            status, _ = request(
                app.ui, "/api/heredity/plugins/example.offline/stop", "POST", {}, token=token)
            self.assertEqual(status, 200)
            wait_for(lambda: not app.gateway_backend.online)
            app.close()

    def test_multiple_online_accounts_require_explicit_gateway_selection(self):
        from dataclasses import replace
        class Second(OfflineExamplePlugin):
            manifest = replace(OfflineExamplePlugin.manifest, plugin_id="example.second",
                               platform="another")
        with tempfile.TemporaryDirectory() as directory:
            app = self.application(directory)
            app.host.register(OfflineExamplePlugin.manifest, factory=OfflineExamplePlugin)
            app.host.register(Second.manifest, factory=Second)
            app.start()
            app.host.start("example.offline", {"self_id": "123"})
            app.host.start("example.second", {"self_id": "456"})
            wait_for(lambda: len(app.host.account_runtimes()) == 2)
            resource, token = self.onebot(app)
            _, state = request(resource, "/get_status", token=token)
            self.assertEqual(state["data"]["state"], "account_selection_required")
            _, result = request(resource, "/get_login_info", token=token)
            self.assertEqual(result["status"], "failed")
            resource = app.network_interfaces.update(
                resource["interface_id"], {"account": "another:456"})
            _, result = request(resource, "/get_login_info", token=token)
            self.assertEqual(result["data"]["user_id"], 456)
            app.close()

    def test_generated_ui_password_is_stable_and_not_part_of_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {PASSWORD_ENV: ""}):
                first = self.application_without_environment(directory)
            secret = first.password_file.read_text().strip()
            self.assertGreaterEqual(len(secret), 12)
            self.assertNotIn(secret, json.dumps(first.host.catalog()))
            first.close()
            with patch.dict(os.environ, {PASSWORD_ENV: ""}):
                second = self.application_without_environment(directory)
            self.assertEqual(second.password_file.read_text().strip(), secret)
            second.close()

    def test_ui_is_independent_of_network_interfaces_and_fresh_host_has_none(self):
        with tempfile.TemporaryDirectory() as directory:
            first = self.application(directory)
            first.start()
            self.assertEqual(first.network_interfaces.catalog()["interfaces"], [])
            with patch("socket.create_connection", side_effect=AssertionError("No internal HTTP")):
                self.assertEqual(first.ui.state.gateway.snapshot(force=True)["state"]["state"],
                                 "waiting_for_plugin")
                self.assertEqual(first.ui.state.accounts(), [])
            resource, token = self.onebot(first, path="/onebot")
            identifier = resource["interface_id"]
            self.assertEqual(request(resource, "/get_status", token=token)[0], 404)
            self.assertEqual(request(resource, "/onebot/get_status", token=token)[0], 200)
            first.network_interfaces.stop(identifier)
            first.network_interfaces.delete(identifier)
            self.assertTrue(first._ui_worker.is_alive())
            first.close()

            restored = self.application(directory)
            restored.start()
            self.assertEqual(restored.network_interfaces.catalog()["interfaces"], [])
            self.assertEqual(restored.ui.state.gateway.snapshot(force=True)["state"]["state"],
                             "waiting_for_plugin")
            restored.close()

    def application_without_environment(self, directory):
        options = SimpleNamespace(
            state_dir=Path(directory), password_env=PASSWORD_ENV, plugin_registry=None,
            market_refresh_on_start=False,
            account=None, host="127.0.0.1", port=0, ui_port=0,
            token_env="NYXION_TEST_ACCESS_TOKEN", event_url=[], sse=False,
            event_secret_env="NYXION_TEST_EVENT_SECRET", static_dir=None)
        value = Application(options)
        self.addCleanup(value.close)
        return value


if __name__ == "__main__":
    unittest.main()
