"""Host SDK declarations assigned through the authenticated management API."""

from dataclasses import replace
import http.client
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from heredity.application import Application
from nyxion_heredity_sdk import (
    NetworkBindingClosed, NetworkInterfaceGrant, NetworkInterfaceRequirement,
)
from nyxion_heredity_sdk.example import OfflineExamplePlugin


PASSWORD = "local-network-request-http-password"
PLUGIN_ID = "example.declared"
ROUTE = "/api/heredity/network-requests"


class DeclaringPlugin(OfflineExamplePlugin):
    manifest = replace(
        OfflineExamplePlugin.manifest, plugin_id=PLUGIN_ID,
        ui_pages=({"id": "accounts", "title": "Accounts", "view": "accounts", "icon": "users"},))

    def start(self, context, config):
        self.networks = context.networks
        self.requirement = NetworkInterfaceRequirement(
            module_id=context.networks.module_id, transport_types=("http_server",),
            account="example:" + config["self_id"], directions=("call", "receive"),
            purpose="Offline SDK declaration")
        self.declaration = self.networks.declare_interface(self.requirement)
        super().start(context, config)


class NetworkRequestHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="Heredity network request API ")
        self.addCleanup(self.temporary.cleanup)
        with patch.dict(os.environ, {"NYXION_NETWORK_REQUEST_PASSWORD": PASSWORD}):
            self.app = Application(SimpleNamespace(
                state_dir=Path(self.temporary.name), password_env="NYXION_NETWORK_REQUEST_PASSWORD",
                market_refresh_on_start=False,
                plugin_registry=None, account=None, ui_port=0, static_dir=None))
        self.addCleanup(self.app.close)
        self.created = []
        def factory():
            plugin = DeclaringPlugin()
            self.created.append(plugin)
            return plugin
        self.app.host.register(DeclaringPlugin.manifest, factory=factory)
        self.app.start()
        _, login = self.request("POST", "/api/login", {"password": PASSWORD}, authorized=False)
        self.token = login["token"]

    def request(self, method, path, payload=None, *, authorized=True, headers=None):
        outgoing = {"Content-Type": "application/json", **(headers or {})}
        if authorized:
            outgoing["Authorization"] = "Bearer " + self.token
        connection = http.client.HTTPConnection(*self.app.ui.server_address, timeout=3)
        try:
            connection.request(method, path, json.dumps(payload) if payload is not None else None,
                               outgoing)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def declare(self):
        self.app.host.create_instance(PLUGIN_ID, "personal", "Personal test")
        self.app.host.start(PLUGIN_ID, {"self_id": "12345678"}, instance_key="personal")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.created and hasattr(self.created[0], "declaration"):
                return self.created[0]
            time.sleep(0.005)
        self.fail("SDK plugin did not declare its interface requirement")

    def network_action(self, interface, action):
        parsed = urlsplit(interface["endpoint"])
        token = self.app.network_interfaces.credentials(interface["interface_id"])["access_token"]
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        try:
            connection.request("POST", parsed.path or "/",
                               json.dumps({"action": action, "params": {}}),
                               {"Content-Type": "application/json",
                                "Authorization": "Bearer " + token})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            return json.loads(response.read())
        finally:
            connection.close()

    def wait_for(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.005)
        self.fail("local network request state did not converge")

    def test_module_declares_then_operator_assigns_existing_interface_without_plugin_network_config(self):
        plugin = self.declare()
        status, catalog = self.request("GET", ROUTE)
        self.assertEqual(status, 200)
        self.assertEqual(len(catalog["requests"]), 1)
        item = catalog["requests"][0]
        self.assertEqual(item["state"], "pending")
        self.assertEqual(item["module_id"], plugin.networks.module_id)
        self.assertEqual(self.app.network_interfaces.catalog()["interfaces"], [])
        interface = self.app.network_interfaces.create({
            "name": "User-created", "type": "http_server", "settings": {"port": 0}})
        status, assigned = self.request("POST", ROUTE + "/" + item["request_id"] + "/assign",
                                        {"interface_id": interface["interface_id"]})
        self.assertEqual(status, 200, assigned)
        self.assertEqual(assigned["state"], "granted")
        self.assertEqual(self.app.network_interfaces.get(interface["interface_id"])["account"],
                         "example:12345678")
        grant = plugin.networks.request_interface(plugin.requirement, timeout=1)
        binding = plugin.networks.bind_grant(plugin.networks.module_id, grant)
        self.app.network_interfaces.start(interface["interface_id"])
        self.assertEqual(binding.call("get_login_info", {})["data"]["user_id"], 12345678)
        # A management identity check must not release the plugin's network service.
        self.assertEqual(self.request("GET", ROUTE)[1]["requests"][0]["state"], "granted")
        self.app.network_interfaces.retry(interface["interface_id"])
        self.assertTrue(binding.closed)
        self.assertEqual(self.request("GET", ROUTE)[1]["requests"][0]["state"], "revoked")

    def test_management_grants_require_login_origin_validation_and_matching_transport(self):
        plugin = self.declare()
        request_id = plugin.declaration["request_id"]
        interface = self.app.network_interfaces.create({
            "name": "Different transport", "type": "websocket_server"})
        path = ROUTE + "/" + request_id
        for method, suffix, body in (
                ("GET", "", None), ("POST", "/assign", {"interface_id": interface["interface_id"]}),
                ("POST", "/assign", {"interface_ids": [interface["interface_id"]]}),
                ("POST", "/unassign", {}), ("POST", "/unassign", {"interface_id": interface["interface_id"]}),
                ("POST", "/deny", {})):
            target = ROUTE if method == "GET" else path + suffix
            self.assertEqual(self.request(method, target, body, authorized=False)[0], 401)
        self.assertEqual(self.request("POST", path + "/deny", {},
                                      headers={"Origin": "https://untrusted.invalid"})[0], 403)
        self.assertEqual(self.request("POST", path + "/assign",
                                      {"interface_id": interface["interface_id"]})[0], 409)
        status, denied = self.request("POST", path + "/deny", {})
        self.assertEqual(status, 200)
        self.assertEqual(denied["state"], "denied")

    def test_multiple_assignments_can_be_changed_and_revoked_while_account_and_interfaces_stay_online(self):
        plugin = self.declare()
        deadline = time.monotonic() + 3
        while not self.app.host.account_runtimes() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(self.app.host.account_runtimes())
        original_session, original_runtime = self.app.host.account_runtimes()[0]
        interfaces = [self.app.network_interfaces.create({
            "name": f"User interface {index}", "type": "http_server", "enabled": True,
            "account": "example:12345678", "settings": {"port": 0},
        }) for index in range(3)]
        identifiers = [item["interface_id"] for item in interfaces]
        path = ROUTE + "/" + plugin.declaration["request_id"]
        status, assigned = self.request("POST", path + "/assign", {"interface_ids": identifiers[:2]})
        self.assertEqual(status, 200, assigned)
        self.assertEqual([item["interface_id"] for item in assigned["grants"]], identifiers[:2])
        grants = plugin.networks.request_interfaces(plugin.requirement, timeout=1)
        self.assertEqual(len(grants), 2)
        first, second = [plugin.networks.bind_grant(plugin.networks.module_id, grant)
                         for grant in grants]
        self.assertEqual(first.call("get_login_info", {})["data"]["user_id"], 12345678)
        self.assertEqual(second.call("get_login_info", {})["data"]["user_id"], 12345678)

        # Keeping an assigned endpoint preserves its actual handle and grant identity.
        status, changed = self.request("POST", path + "/assign", {"interface_ids": identifiers[1:]})
        self.assertEqual(status, 200, changed)
        self.assertEqual(changed["grants"][0]["grant_id"], assigned["grants"][1]["grant_id"])
        self.assertTrue(first.closed)
        self.assertFalse(second.closed)
        self.assertEqual(second.call("get_login_info", {})["data"]["user_id"], 12345678)
        self.assertEqual(self.network_action(interfaces[0], "get_login_info")["status"], "failed")
        self.assertEqual(self.network_action(interfaces[1], "get_login_info")["status"], "ok")
        status, removed = self.request("POST", path + "/unassign", {"interface_id": identifiers[1]})
        self.assertEqual(status, 200, removed)
        self.assertEqual([item["interface_id"] for item in removed["grants"]], [identifiers[2]])
        self.assertTrue(second.closed)
        self.assertEqual(self.network_action(interfaces[1], "get_login_info")["status"], "failed")
        self.assertEqual(self.network_action(interfaces[2], "get_login_info")["status"], "ok")
        status, empty = self.request("POST", path + "/unassign", {})
        self.assertEqual(status, 200, empty)
        self.assertEqual(empty["state"], "pending")
        self.assertEqual(empty["grants"], [])
        status = self.network_action(interfaces[2], "get_status")
        self.assertEqual(status["data"]["state"], "network_interface_unassigned")
        status, reassigned = self.request("POST", path + "/assign", {"interface_ids": [identifiers[0]]})
        self.assertEqual(status, 200, reassigned)
        self.assertNotEqual(reassigned["grants"][0]["grant_id"], assigned["grants"][0]["grant_id"])
        self.assertEqual(self.network_action(interfaces[0], "get_login_info")["status"], "ok")
        self.assertTrue(all(self.app.network_interfaces.get(identifier)["state"] == "running"
                            for identifier in identifiers))
        self.assertEqual(self.app.host.account_runtimes()[0][0], original_session)
        self.assertIs(self.app.host.account_runtimes()[0][1], original_runtime)
        self.assertEqual(len(self.created), 1)

    def test_invalid_assignment_and_cross_site_revocation_leave_existing_grants_unchanged(self):
        plugin = self.declare()
        interface = self.app.network_interfaces.create({
            "name": "User interface", "type": "http_server", "settings": {"port": 0}})
        path = ROUTE + "/" + plugin.declaration["request_id"]
        _, assigned = self.request("POST", path + "/assign",
                                    {"interface_ids": [interface["interface_id"]]})
        for suffix, body, expected in (
                ("/assign", {"interface_ids": "invalid"}, 400),
                ("/assign", {"interface_ids": [None]}, 400),
                ("/assign", {"interface_id": interface["interface_id"], "interface_ids": []}, 400),
                ("/unassign", {"interface_id": None}, 400),
                ("/unassign", {"interface_ids": []}, 400),
                ("/assign", {"interface_ids": [interface["interface_id"], "f" * 16]}, 404)):
            with self.subTest(suffix=suffix, payload=body):
                status, _ = self.request("POST", path + suffix, body)
                self.assertEqual(status, expected)
        self.assertEqual(self.request(
            "POST", path + "/unassign", {},
            headers={"Origin": "https://untrusted.invalid"})[0], 403)
        current = self.request("GET", ROUTE)[1]["requests"][0]
        self.assertEqual(current["grants"], assigned["grants"])

    def test_shared_interface_routes_until_its_last_grant_is_removed(self):
        plugin = self.declare()
        deadline = time.monotonic() + 3
        while not self.app.host.account_runtimes() and time.monotonic() < deadline:
            time.sleep(0.005)
        interface = self.app.network_interfaces.create({
            "name": "Shared running endpoint", "type": "http_server", "enabled": True,
            "account": "example:12345678", "settings": {"port": 0}})
        independent = self.app.network_interfaces.create({
            "name": "Independent endpoint", "type": "http_server", "enabled": True,
            "account": "example:12345678", "settings": {"port": 0}})
        other = self.app.network_allocator.declare_interface(NetworkInterfaceRequirement(
            "example.other-module", ("http_server",), account="example:12345678"))
        first_path = ROUTE + "/" + plugin.declaration["request_id"]
        second_path = ROUTE + "/" + other["request_id"]
        for path in (first_path, second_path):
            self.assertEqual(self.request("POST", path + "/assign",
                                          {"interface_ids": [interface["interface_id"]]})[0], 200)
        self.assertEqual(self.request("POST", first_path + "/unassign", {})[0], 200)
        self.assertEqual(self.network_action(interface, "get_login_info")["status"], "ok")
        self.assertEqual(self.request("POST", second_path + "/unassign", {})[0], 200)
        self.assertEqual(self.network_action(interface, "get_login_info")["status"], "failed")
        self.assertEqual(self.network_action(independent, "get_login_info")["status"], "ok")
        self.assertEqual(self.app.network_interfaces.get(interface["interface_id"])["state"], "running")

    def test_stopped_plugin_releases_declarations_and_cannot_redeclare_from_stale_context(self):
        plugin = self.declare()
        self.app.host.stop(PLUGIN_ID, instance_key="personal")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.app.host.instances(PLUGIN_ID)[0]["state"] == "stopped":
                break
            time.sleep(0.005)
        self.assertEqual(self.request("GET", ROUTE)[1]["requests"][0]["state"], "released")
        with self.assertRaises(NetworkBindingClosed):
            plugin.networks.declare_interface(plugin.requirement)

    def test_restart_automatically_requests_saved_interfaces_and_starts_them(self):
        plugin = self.declare()
        self.wait_for(lambda: self.app.host.account_runtimes())
        original = self.app.host.account_runtimes()[0][0]
        item = self.app.network_interfaces.create({
            "name": "Restore after module restart", "type": "http_server",
            "settings": {"port": 0}})
        old_request = plugin.declaration["request_id"]
        status, assigned = self.request("POST", ROUTE + "/" + old_request + "/assign",
                                        {"interface_ids": [item["interface_id"]]})
        self.assertEqual(status, 200, assigned)
        self.assertTrue(assigned["auto_start"])
        old_grant = NetworkInterfaceGrant(**assigned["grants"][0])
        self.app.host.retry(PLUGIN_ID, instance_key="personal")
        self.wait_for(lambda: len(self.created) == 2 and hasattr(self.created[1], "declaration"))
        self.wait_for(lambda: self.app.host.account_runtimes()
                      and self.app.host.account_runtimes()[0][0] != original)
        fresh = self.created[1]
        fresh_id = fresh.declaration["request_id"]
        self.assertNotEqual(fresh_id, old_request)
        self.wait_for(lambda: self.app.network_interfaces.get(item["interface_id"])["state"] == "running")
        current = self.request("GET", ROUTE)[1]["requests"]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["request_id"], fresh_id)
        self.assertEqual(current[0]["state"], "granted")
        self.assertEqual(current[0]["grants"][0]["interface_id"], item["interface_id"])
        self.assertNotEqual(current[0]["grants"][0]["grant_id"], old_grant.grant_id)
        self.assertEqual(self.network_action(
            self.app.network_interfaces.get(item["interface_id"]), "get_login_info")["status"], "ok")
        with self.assertRaises(NetworkBindingClosed):
            plugin.networks.bind_grant(plugin.networks.module_id, old_grant)
        history = self.request("GET", ROUTE + "?include_history=true")[1]["requests"]
        self.assertEqual(len(history), 2)

    def test_released_request_can_reapply_with_active_module_and_old_history_does_not_duplicate_current(self):
        plugin = self.declare()
        self.wait_for(lambda: self.app.host.account_runtimes())
        original = plugin.declaration["request_id"]
        plugin.networks.release_interface(original)
        status, released = self.request("GET", ROUTE)
        self.assertEqual(status, 200)
        self.assertTrue(released["requests"][0]["can_reapply"])
        path = ROUTE + "/" + original + "/reapply"
        status, current = self.request("POST", path, {})
        self.assertEqual(status, 200, current)
        self.assertNotEqual(current["request_id"], original)
        self.assertEqual(current["state"], "pending")
        self.assertEqual(self.request("POST", path, {})[1]["request_id"], current["request_id"])
        self.assertEqual(len(self.request("GET", ROUTE)[1]["requests"]), 1)
        self.assertEqual(len(self.request("GET", ROUTE + "?include_history=true")[1]["requests"]), 2)
        self.app.host.stop(PLUGIN_ID, instance_key="personal")
        self.wait_for(lambda: self.app.host.instances(PLUGIN_ID)[0]["state"] == "stopped")
        status, _ = self.request("POST", ROUTE + "/" + current["request_id"] + "/reapply", {})
        self.assertEqual(status, 409)
        self.assertEqual(len(self.created), 1)

    def test_auto_start_can_be_disabled_and_lifecycle_management_is_authenticated(self):
        plugin = self.declare()
        identifier = plugin.declaration["request_id"]
        path = ROUTE + "/" + identifier
        for suffix, payload in (("/auto-start", {"enabled": False}), ("/reapply", {})):
            self.assertEqual(self.request(
                "POST", path + suffix, payload, authorized=False)[0], 401)
            self.assertEqual(self.request(
                "POST", path + suffix, payload,
                headers={"Origin": "https://untrusted.invalid"})[0], 403)
        for payload in ({}, {"enabled": 1}, {"enabled": False, "extra": True}):
            self.assertEqual(self.request("POST", path + "/auto-start", payload)[0], 400)
        for query in ("?include_history=invalid", "?include_history=true&include_history=false"):
            self.assertEqual(self.request("GET", ROUTE + query)[0], 400)
        self.assertEqual(self.request("POST", path + "/reapply", {"extra": True})[0], 400)
        item = self.app.network_interfaces.create({
            "name": "Manual after opt-out", "type": "http_server", "settings": {"port": 0}})
        self.request("POST", path + "/assign", {"interface_ids": [item["interface_id"]]})
        status, disabled = self.request("POST", path + "/auto-start", {"enabled": False})
        self.assertEqual(status, 200, disabled)
        self.assertFalse(disabled["auto_start"])
        self.app.host.retry(PLUGIN_ID, instance_key="personal")
        self.wait_for(lambda: len(self.created) == 2 and hasattr(self.created[1], "declaration"))
        current = self.request("GET", ROUTE)[1]["requests"][0]
        self.assertFalse(current["auto_start"])
        self.assertEqual(current["state"], "pending")
        self.assertEqual(current["saved_interface_ids"], [item["interface_id"]])
        self.assertEqual(self.app.network_interfaces.get(item["interface_id"])["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
