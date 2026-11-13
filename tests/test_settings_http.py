"""Dotenv ports and attention markers never expose local secrets or rebind live UI."""

import http.client
import json
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from heredity.application import Application
from heredity.settings import SettingsError, SettingsService, load_environment


PASSWORD = "settings-local-fixture-password"


class SettingsFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="Heredity dotenv settings ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / ".env"
        self.interfaces = SimpleNamespace(catalog=lambda: {"interfaces": []})

    def test_plain_values_are_loaded_without_shell_evaluation_and_process_values_win(self):
        self.path.write_text(
            "HEREDITY_UI_PORT=5109\nPRIVATE_TOKEN='literal$(do-not-execute)'\n", encoding="utf-8")
        environment = {"HEREDITY_UI_PORT": "5110"}
        load_environment(self.path, environ=environment)
        self.assertEqual(environment["HEREDITY_UI_PORT"], "5110")
        self.assertEqual(environment["PRIVATE_TOKEN"], "literal$(do-not-execute)")

    def test_update_preserves_secrets_and_comments_but_projects_only_public_settings(self):
        original = "# private configuration\nHEREDITY_UI_PORT=5109\nPRIVATE_TOKEN=keep-this-private\n"
        self.path.write_text(original, encoding="utf-8")
        service = SettingsService(self.path, 5109, self.interfaces)
        updated = service.update({"ui_port": 5200})
        self.assertEqual(updated["startup"]["configured_ui_port"], 5200)
        self.assertEqual(updated["startup"]["ui_port"], 5109)
        self.assertTrue(updated["startup"]["restart_required"])
        self.assertNotIn("keep-this-private", json.dumps(updated))
        self.assertIn("PRIVATE_TOKEN=keep-this-private", self.path.read_text())
        self.assertIn("# private configuration", self.path.read_text())
        for invalid in ({"ui_port": 0}, {"ui_port": True}, {"ui_port": 65536},
                        {"ui_port": 5200, "PRIVATE_TOKEN": "replace"}):
            before = self.path.read_bytes()
            with self.subTest(payload=invalid), self.assertRaises(SettingsError):
                service.update(invalid)
            self.assertEqual(self.path.read_bytes(), before)

    def test_duplicate_malformed_oversize_and_linked_configuration_is_rejected(self):
        for content in (
                "HEREDITY_UI_PORT=5109\nHEREDITY_UI_PORT=5110\n",
                "HEREDITY_UI_PORT=invalid\n", "MALFORMED\n",
                "PRIVATE_TOKEN='unterminated\n", "PRIVATE_TOKEN=" + "x" * 65536):
            self.path.write_text(content, encoding="utf-8")
            with self.subTest(kind=content[:24]), self.assertRaises(SettingsError):
                load_environment(self.path, environ={})
        self.path.write_text("HEREDITY_UI_PORT=5109\n")
        with patch.object(Path, "is_symlink", return_value=True):
            with self.assertRaises(SettingsError):
                load_environment(self.path, environ={})


class SettingsManagementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="Heredity settings API ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env_file = self.root / "startup.env"
        self.env_file.write_text("HEREDITY_UI_PORT=5099\nPRIVATE_TOKEN=never-render\n")
        with patch.dict(os.environ, {"SETTINGS_FIXTURE_PASSWORD": PASSWORD}):
            self.app = Application(SimpleNamespace(
                state_dir=self.root / "state", password_env="SETTINGS_FIXTURE_PASSWORD",
                plugin_registry=None, account=None, ui_port=0, env_file=self.env_file,
                static_dir=None, market_refresh_on_start=False))
        self.addCleanup(self.app.close)
        self.app.start()
        _, response = self.request("POST", "/api/login", {"password": PASSWORD}, authenticated=False)
        self.token = response["token"]

    def request(self, method, path, payload=None, *, authenticated=True, headers=None):
        outgoing = {"Content-Type": "application/json", **(headers or {})}
        if authenticated:
            outgoing["Authorization"] = "Bearer " + self.token
        connection = http.client.HTTPConnection(*self.app.ui.server_address, timeout=4)
        try:
            connection.request(method, path, json.dumps(payload) if payload is not None else None,
                               outgoing)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_authentication_origin_and_actual_port_are_preserved_when_saving_startup_port(self):
        for method, payload in (("GET", None), ("PUT", {"ui_port": 5200})):
            self.assertEqual(self.request(
                method, "/api/heredity/settings", payload, authenticated=False)[0], 401)
        self.assertEqual(self.request(
            "PUT", "/api/heredity/settings", {"ui_port": 5200},
            headers={"Origin": "https://untrusted.invalid"})[0], 403)
        running = self.app.ui.server_address[1]
        status, result = self.request("PUT", "/api/heredity/settings", {"ui_port": 5200})
        self.assertEqual(status, 200, result)
        self.assertEqual(self.app.ui.server_address[1], running)
        self.assertEqual(result["startup"]["ui_port"], running)
        self.assertEqual(result["startup"]["configured_ui_port"], 5200)
        self.assertTrue(self.app._ui_worker.is_alive())
        self.assertNotIn("never-render", json.dumps(result))
        self.assertEqual(result["services"], [])

    def test_registered_service_port_conflict_is_visible_in_settings_and_sidebar_attention(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        self.addCleanup(blocker.close)
        port = blocker.getsockname()[1]
        item = self.app.network_interfaces.create({
            "name": "Port conflict fixture", "type": "http_server",
            "settings": {"host": "127.0.0.1", "port": port}})
        value = self.app.network_interfaces.start(item["interface_id"])
        self.assertEqual(value["state"], "error")
        status, settings = self.request("GET", "/api/heredity/settings")
        self.assertEqual(status, 200)
        self.assertEqual(settings["services"][0]["port"], port)
        self.assertEqual(settings["services"][0]["state"], "error")
        status, attention = self.request("GET", "/api/heredity/attention")
        self.assertEqual(status, 200)
        badge = next(item for item in attention["items"] if item["path"] == "/connections")
        self.assertEqual(badge["severity"], "error")
        self.assertNotIn("never-render", json.dumps(attention))
        blocker.close()
        fixed = self.app.network_interfaces.retry(item["interface_id"])
        self.assertEqual(fixed["state"], "running")
        self.assertEqual(fixed["actual_port"], port)
        self.assertFalse(any(item["path"] == "/connections"
                             for item in self.request("GET", "/api/heredity/attention")[1]["items"]))

    def test_conflicting_management_port_refuses_startup_without_changing_the_port(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        self.addCleanup(blocker.close)
        port = blocker.getsockname()[1]
        with patch.dict(os.environ, {"SETTINGS_FIXTURE_PASSWORD": PASSWORD}):
            with self.assertRaises(SettingsError) as failure:
                Application(SimpleNamespace(
                    state_dir=self.root / "conflicting-startup",
                    password_env="SETTINGS_FIXTURE_PASSWORD", plugin_registry=None,
                    account=None, ui_port=port, env_file=self.env_file,
                    static_dir=None, market_refresh_on_start=False))
        self.assertIn(str(port), str(failure.exception))
        self.assertIn("端口", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
