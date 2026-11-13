"""UI action ownership remains bound while the current account changes."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from heredity.plugins.gateway import PluginGatewayBackend
from heredity.plugins.host import PluginHost
from heredity.webui import ManagementError, WebUIState
from nyxion_heredity_sdk import ActionResult, require_session
from nyxion_heredity_sdk.example import OfflineExamplePlugin


PLUGIN_ID = "test.account-routing"


class RoutingPlugin(OfflineExamplePlugin):
    manifest = replace(
        OfflineExamplePlugin.manifest, plugin_id=PLUGIN_ID, platform="example",
        actions=("get_login_info", "echo", "synthetic_write"))

    def __init__(self):
        super().__init__()
        self.action_calls = []

    def call(self, request):
        if request.action in {"echo", "synthetic_write"}:
            request.require_live()
            require_session(request.session, self._session)
            self.action_calls.append((request.action, request.params))
            return ActionResult(request.request_id, "completed", {
                "user_id": int(self._session.self_id),
                "action": request.action,
            })
        return super().call(request)


class PluginAccountRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="NyXion routing ownership ")
        self.addCleanup(self.temporary.cleanup)
        self.host = PluginHost(
            Path(self.temporary.name) / "host", heartbeat_interval=0.02,
            lease_seconds=0.5, control_timeout=0.2)
        self.addCleanup(self.host.close)
        self.plugins = []
        def factory():
            plugin = RoutingPlugin()
            self.plugins.append(plugin)
            return plugin
        self.host.register(RoutingPlugin.manifest, factory=factory)
        for key, account in (("first", "10001"), ("second", "10002")):
            self.host.create_instance(PLUGIN_ID, key, key)
            self.host.start(PLUGIN_ID, {"self_id": account}, instance_key=key)
            self.wait(lambda: any(identity.self_id == account
                                  for identity, _ in self.host.account_runtimes()))
        self.runtimes = {identity.self_id: runtime
                         for identity, runtime in self.host.account_runtimes()}
        self.gateway = PluginGatewayBackend(self.host)
        self.state = WebUIState(
            "local-account-routing-password", "http://127.0.0.1:5700",
            Path(self.temporary.name) / "ui", plugin_host=self.host,
            plugin_gateway=self.gateway)
        self.addCleanup(self.state.close)
        self.network = patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def wait(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail("local routing state did not converge")

    def payload(self, account="10001", action="echo"):
        return {"account": f"example:{account}", "action": action,
                "params": {"marker": "local-only"}}

    def test_action_keeps_captured_runtime_when_gateway_selector_changes_during_call(self):
        self.gateway.select_account("example:10001")
        entered, release = threading.Event(), threading.Event()
        first = self.runtimes["10001"]
        original_call = first.call
        def gated(action, params):
            entered.set()
            if not release.wait(2):
                raise AssertionError("test gate did not release")
            return original_call(action, params)
        with patch.object(first, "call", side_effect=gated), \
             patch.object(self.state.gateway, "action",
                          side_effect=AssertionError("must not reselect through HTTP")), \
             ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.state.invoke, self.payload())
            try:
                self.assertTrue(entered.wait(1))
                self.gateway.select_account("example:10002")
            finally:
                release.set()
            result = pending.result(timeout=2)
        self.assertEqual(result["data"]["user_id"], 10001)
        self.assertEqual(self.gateway.selection()["account"], "example:10002")
        self.assertEqual(len(self.plugins[0].action_calls), 1)
        self.assertEqual(self.plugins[1].action_calls, [])

    def test_ui_selection_waits_for_existing_action_then_uses_new_account(self):
        self.state.account_selection("example:10001", select=True)
        entered, release, select_entered = threading.Event(), threading.Event(), threading.Event()
        original_call = self.runtimes["10001"].call
        def gated(action, params):
            entered.set()
            if not release.wait(2):
                raise AssertionError("test gate did not release")
            return original_call(action, params)
        def select():
            select_entered.set()
            return self.state.account_selection("example:10002", select=True)
        with patch.object(self.runtimes["10001"], "call", side_effect=gated), \
             ThreadPoolExecutor(max_workers=2) as pool:
            action = pool.submit(self.state.invoke, self.payload(action="synthetic_write"))
            self.assertTrue(entered.wait(1))
            selection = pool.submit(select)
            try:
                self.assertTrue(select_entered.wait(1))
                self.assertFalse(selection.done())
                self.assertEqual(self.gateway.selection()["account"], "example:10001")
            finally:
                release.set()
            self.assertEqual(action.result(timeout=2)["data"]["user_id"], 10001)
            self.assertEqual(selection.result(timeout=2)["account"], "example:10002")
        self.assertEqual(
            self.state.invoke(self.payload("10002", "synthetic_write"))["data"]["user_id"], 10002)
        self.assertEqual(len(self.plugins[0].action_calls), 1)
        self.assertEqual(len(self.plugins[1].action_calls), 1)

    def test_selection_immediately_invalidates_cached_gateway_identity(self):
        self.state.account_selection("example:10001", select=True)
        self.state.gateway._snapshot = {
            "state": {"self_id": 10001, "online": True},
            "account": {"user_id": 10001, "nickname": "stale"},
        }
        self.state.gateway._snapshot_at = time.monotonic()
        self.state.account_selection("example:10002", select=True)
        self.assertIsNone(self.state.gateway._snapshot)
        self.assertEqual(self.state.gateway._snapshot_at, 0)
        def local_action(action, params=None):
            data = (self.gateway.status() if action == "get_status"
                    else self.gateway.call(action, params or {}))
            return {"status": "ok", "retcode": 0, "data": data}
        with patch.object(self.state.gateway, "action", side_effect=local_action):
            fresh = self.state.gateway.snapshot()
        self.assertEqual(fresh["account"]["user_id"], 10002)
        self.assertEqual(fresh["state"]["self_id"], 10002)

    def test_wrong_account_and_invalid_payload_never_execute_either_plugin(self):
        self.state.account_selection("example:10002", select=True)
        for action in ("echo", "synthetic_write", "get_status"):
            with self.subTest(action=action), self.assertRaises(ManagementError) as error:
                self.state.invoke(self.payload("10001", action))
            self.assertEqual(error.exception.status, 403)
        for invalid in (
                {}, {"account": "example:10002", "action": "bad/name", "params": {}},
                {"account": "example:10002", "action": "echo", "params": []},
                {"account": "example:10002", "action": 1, "params": {}}):
            with self.subTest(invalid=invalid), self.assertRaises(ManagementError) as error:
                self.state.invoke(invalid)
            self.assertEqual(error.exception.status, 400)
        self.assertEqual(self.plugins[0].action_calls, [])
        self.assertEqual(self.plugins[1].action_calls, [])

    def test_unselected_multiple_accounts_reject_both_reads_and_writes(self):
        self.assertEqual(self.gateway.status()["state"], "account_selection_required")
        for action in ("echo", "synthetic_write", "get_status", "get_login_info"):
            with self.subTest(action=action), self.assertRaises(ManagementError) as error:
                self.state.invoke(self.payload("10001", action))
            self.assertEqual(error.exception.status, 403)
        self.assertEqual(self.plugins[0].action_calls, [])
        self.assertEqual(self.plugins[1].action_calls, [])


if __name__ == "__main__":
    unittest.main()
