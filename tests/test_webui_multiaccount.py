"""Authenticated multi-account UI APIs over a real offline SDK host."""

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
from nyxion_heredity_sdk import Challenge
import test_plugin_multiaccount as fixtures


PASSWORD = "local-multi-account-ui-password"
PASSWORD_ENV = "NYXION_MULTI_UI_TEST_PASSWORD"
PLUGIN_ID = fixtures.PLUGIN_ID
BASE = f"/api/heredity/plugins/{PLUGIN_ID}/instances"


class MultiaccountManagementHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="NyXion account UI ")
        self.addCleanup(self.temporary.cleanup)
        self.created = []
        options = SimpleNamespace(
            state_dir=Path(self.temporary.name), password_env=PASSWORD_ENV,
            market_refresh_on_start=False,
            plugin_registry=None, account=None, host="127.0.0.1", port=0, ui_port=0,
            token_env="NYXION_MULTI_TEST_ACCESS_TOKEN", event_url=[], sse=True,
            event_secret_env="NYXION_MULTI_TEST_EVENT_SECRET", static_dir=None)
        with patch.dict(os.environ, {PASSWORD_ENV: PASSWORD}):
            self.app = Application(options)
        self.addCleanup(self.close_application)
        def factory():
            plugin = fixtures.AccountPlugin()
            self.created.append(plugin)
            return plugin
        self.app.host.register(fixtures.AccountPlugin.manifest, factory=factory)
        self.app.start()
        self.network = self.app.network_interfaces.create({
            "name": "Explicit multi-account test", "type": "http_server", "enabled": True,
            "settings": {"host": "127.0.0.1", "port": 0}})
        self.network_token = self.app.network_interfaces.credentials(
            self.network["interface_id"])["access_token"]
        status, login = self.request("POST", "/api/login", {"password": PASSWORD}, authenticated=False)
        self.assertEqual(status, 200)
        self.token = login["token"]

    def close_application(self):
        for plugin in self.created:
            plugin.stop_release.set()
        self.app.close()
        self.assertFalse(self.app._ui_worker.is_alive())
        self.assertFalse(self.app.host._monitor.is_alive())

    def request(self, method, path, payload=None, *, authenticated=True, headers=None,
                server=None):
        selected = server or self.app.ui
        outgoing = dict(headers or {})
        if authenticated and selected is self.app.ui:
            outgoing["Authorization"] = "Bearer " + self.token
        elif selected is self.network:
            outgoing["Authorization"] = "Bearer " + self.network_token
        data = None
        if payload is not None:
            data = json.dumps(payload)
            outgoing["Content-Type"] = "application/json"
        if type(selected) is dict:
            parsed = urlsplit(selected["endpoint"])
            address = (parsed.hostname, parsed.port)
        else:
            address = selected.server_address
        connection = http.client.HTTPConnection(*address, timeout=3)
        try:
            connection.request(method, path, data, outgoing)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def wait(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.005)
        self.fail("local multi-account HTTP state did not converge")

    def snapshot(self, key):
        return next(item for item in self.app.host.instances(PLUGIN_ID)
                    if item["instance_key"] == key)

    def create(self, key, label=None):
        status, result = self.request(
            "POST", BASE, {"instance_key": key, "display_name": label or key})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["instance_key"], key)
        self.assertEqual(result["state"], "stopped")
        return result

    def start(self, key, account):
        before = len(self.created)
        status, result = self.request(
            "POST", f"{BASE}/{key}/start", {"config": {"self_id": account}})
        self.assertEqual(status, 200, result)
        self.wait(lambda: len(self.created) > before)
        self.wait(lambda: self.snapshot(key)["state"] == "running")
        self.wait(lambda: self.snapshot(key)["session"] is not None
                  and self.snapshot(key)["session"]["state"] == "online")
        return self.created[-1]

    def stop(self, key):
        status, result = self.request("POST", f"{BASE}/{key}/stop", {})
        self.assertEqual(status, 200, result)
        self.wait(lambda: self.snapshot(key)["state"] == "stopped")

    def test_catalog_registers_plugin_sidebar_page_and_gets_never_construct_platform(self):
        status, result = self.request("GET", "/api/heredity/plugins")
        self.assertEqual(status, 200)
        navigation = next(item for item in result["navigation"] if item["plugin_id"] == PLUGIN_ID)
        self.assertEqual(navigation["path"], f"/plugins/{PLUGIN_ID}/accounts")
        self.assertEqual(navigation["title"], "平台账号")
        status, profiles = self.request("GET", BASE)
        self.assertEqual(status, 200)
        self.assertEqual(profiles["instances"], [])
        self.assertEqual(self.created, [])
        self.create("work", "Work account")
        for path in (BASE, f"{BASE}/work/challenge", "/api/heredity/plugins"):
            status, _ = self.request("GET", path)
            self.assertEqual(status, 200)
        self.assertEqual(self.created, [])

    def test_create_two_start_independently_then_select_onebot_account(self):
        self.create("home", "Home account")
        self.create("work", "Work account")
        home = self.start("home", "10001")
        work = self.start("work", "10002")
        status, profiles = self.request("GET", BASE)
        self.assertEqual(status, 200)
        online = {item["instance_key"]: item for item in profiles["instances"]
                  if item["session"] is not None and item["session"]["state"] == "online"}
        self.assertEqual(set(online), {"home", "work"})
        self.assertEqual(online["home"]["session"]["self_id"], "10001")
        self.assertEqual(online["work"]["session"]["self_id"], "10002")
        self.assertNotEqual(online["home"]["instance_id"], online["work"]["instance_id"])
        self.assertNotEqual(home.context.state_dir, work.context.state_dir)
        _, gateway = self.request("GET", "/get_status", server=self.network)
        self.assertEqual(gateway["data"]["state"], "account_selection_required")
        status, selection = self.request(
            "POST", "/api/heredity/account-selection", {"account": "example:10002"})
        self.assertEqual(status, 200, selection)
        self.assertEqual(selection["account"], "example:10002")
        self.assertEqual({item["self_id"] for item in selection["accounts"]}, {"10001", "10002"})
        self.network = self.app.network_interfaces.update(
            self.network["interface_id"], {"account": "example:10002"})
        status, identity = self.request("GET", "/get_login_info", server=self.network)
        self.assertEqual(status, 200)
        self.assertEqual(identity["data"]["user_id"], 10002)
        status, selection = self.request("GET", "/api/heredity/account-selection")
        self.assertEqual(status, 200)
        self.assertEqual(selection["account"], "example:10002")

    def test_notice_challenge_and_diagnostics_stay_scoped_to_the_selected_instance(self):
        self.create("home")
        self.create("work")
        home = self.start("home", "10001")
        work = self.start("work", "10002")
        home.context.publish_challenge(Challenge(
            "home-notice", "notice", "Home login",
            {"message": "Home failed", "code": "GENERIC_FRAME", "stage": "ticket_exchange",
             "status": "failed", "token": "must-never-reach-ui", "path": "/private/local/key"}))
        work.context.publish_challenge(Challenge(
            "work-notice", "notice", "Work login", {"message": "Work ready"}))
        status, result = self.request("GET", f"{BASE}/home/challenge")
        self.assertEqual(status, 200)
        self.assertEqual(result["challenge"]["kind"], "notice")
        self.assertEqual(result["challenge"]["challenge_id"], "home-notice")
        self.assertEqual(result["challenge"]["payload"], {
            "message": "Home failed", "code": "GENERIC_FRAME",
            "stage": "ticket_exchange", "status": "failed",
        })
        status, result = self.request("GET", f"{BASE}/work/challenge")
        self.assertEqual(status, 200)
        self.assertEqual(result["challenge"]["challenge_id"], "work-notice")
        self.assertNotIn("Home", json.dumps(result))
        self.assertNotIn("must-never-reach-ui", json.dumps(result))

    def test_stop_one_account_preserves_other_and_the_onebot_listener(self):
        self.create("home")
        self.create("work")
        self.start("home", "10001")
        work = self.start("work", "10002")
        self.request("POST", "/api/heredity/account-selection", {"account": "example:10002"})
        self.stop("home")
        self.assertEqual(work.stop_calls, 0)
        self.assertEqual(self.snapshot("work")["session"]["state"], "online")
        status, identity = self.request("GET", "/get_login_info", server=self.network)
        self.assertEqual(status, 200)
        self.assertEqual(identity["data"]["user_id"], 10002)
        self.assertEqual(self.app.network_interfaces.get(self.network["interface_id"])["state"],
                         "running")
        self.assertTrue(self.app._ui_worker.is_alive())

    def test_delete_requires_clean_stop_and_there_is_no_implicit_default_slot(self):
        self.create("home")
        plugin = self.start("home", "10001")
        history = plugin.context.state_dir / "retained-test-history"
        history.write_text("keep", encoding="utf-8")
        status, result = self.request("DELETE", f"{BASE}/home", {})
        self.assertEqual(status, 409, result)
        status, result = self.request("DELETE", f"{BASE}/default", {})
        self.assertEqual(status, 404, result)
        self.stop("home")
        status, result = self.request("DELETE", f"{BASE}/home", {})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["removed"])
        self.assertEqual(history.read_text(encoding="utf-8"), "keep")
        _, profiles = self.request("GET", BASE)
        self.assertEqual(profiles["instances"], [])

    def test_new_management_routes_all_require_authentication(self):
        requests = (
            ("GET", BASE, None),
            ("GET", f"{BASE}/default/challenge", None),
            ("GET", "/api/heredity/account-selection", None),
            ("POST", BASE, {"instance_key": "work", "display_name": "Work"}),
            ("POST", f"{BASE}/default/start", {"config": {"self_id": "10001"}}),
            ("POST", f"{BASE}/default/stop", {}),
            ("POST", "/api/heredity/account-selection", {"account": None}),
            ("DELETE", f"{BASE}/default", {}),
        )
        for method, path, payload in requests:
            with self.subTest(method=method, path=path):
                status, _ = self.request(method, path, payload, authenticated=False)
                self.assertEqual(status, 401)
        self.assertEqual(self.created, [])
        self.assertEqual(self.app.host.instances(PLUGIN_ID), [])

    def test_cross_site_post_and_delete_are_rejected_before_mutating_profiles(self):
        self.create("work")
        requests = (
            ("POST", BASE, {"instance_key": "another", "display_name": "Another"}),
            ("POST", f"{BASE}/work/start", {"config": {"self_id": "10001"}}),
            ("POST", f"{BASE}/work/stop", {}),
            ("POST", "/api/heredity/account-selection", {"account": None}),
            ("DELETE", f"{BASE}/work", {}),
        )
        for headers in ({"Origin": "https://unrelated.invalid"}, {"Sec-Fetch-Site": "cross-site"}):
            for method, path, payload in requests:
                with self.subTest(headers=headers, method=method, path=path):
                    status, _ = self.request(method, path, payload, headers=headers)
                    self.assertEqual(status, 403)
        self.assertEqual([item["instance_key"] for item in self.app.host.instances(PLUGIN_ID)],
                         ["work"])
        self.assertEqual(self.created, [])

    def test_malformed_management_payloads_return_client_errors_without_loading_platform(self):
        self.create("work")
        requests = (
            ("POST", BASE, {}),
            ("POST", BASE, {"instance_key": "bad", "display_name": 1}),
            ("POST", BASE, {"instance_key": "../bad", "display_name": "Bad"}),
            ("POST", BASE, {"instance_key": "bad", "display_name": "Bad", "extra": True}),
            ("POST", f"{BASE}/work/start", {}),
            ("POST", f"{BASE}/work/start", {"config": []}),
            ("POST", f"{BASE}/work/start", {"config": {}}),
            ("POST", f"{BASE}/work/stop", {"extra": True}),
            ("POST", "/api/heredity/account-selection", {}),
            ("POST", "/api/heredity/account-selection", {"account": "example:99999"}),
            ("POST", "/api/heredity/account-selection", {"account": 10001}),
            ("DELETE", f"{BASE}/work", {"config": {}}),
        )
        for method, path, payload in requests:
            with self.subTest(method=method, path=path, payload=payload):
                status, result = self.request(method, path, payload)
                self.assertEqual(status, 400, result)
        self.assertEqual(self.created, [])

    def test_invalid_account_selection_does_not_change_existing_route(self):
        self.create("home")
        self.start("home", "10001")
        status, _ = self.request(
            "POST", "/api/heredity/account-selection", {"account": "example:10001"})
        self.assertEqual(status, 200)
        status, _ = self.request(
            "POST", "/api/heredity/account-selection", {"account": "other:10001"})
        self.assertEqual(status, 400)
        _, selection = self.request("GET", "/api/heredity/account-selection")
        self.assertEqual(selection["account"], "example:10001")
        _, identity = self.request("GET", "/get_login_info", server=self.network)
        self.assertEqual(identity["data"]["user_id"], 10001)


if __name__ == "__main__":
    unittest.main()
