"""Core boundaries stay independent of installed platform implementations."""

from dataclasses import replace
from io import BytesIO
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

from heredity import __version__
from heredity.marketplace import Marketplace, _version
from heredity.plugins.gateway import PluginGatewayBackend
from heredity.plugins.host import PluginHost
from heredity.webui import ManagementError, WebUIState
from nyxion_heredity_sdk.example import OfflineExamplePlugin


ROOT = Path(__file__).resolve().parents[1]


class CoreBoundaryTests(unittest.TestCase):
    def test_core_imports_only_declared_runtime_dependencies(self):
        code = (
            "import sys; sys.path.insert(0,sys.argv[1]); baseline=set(sys.modules);"
            "import heredity.cli,heredity.application;"
            "allowed=set(sys.stdlib_module_names)|"
            "{'heredity','nyxion_heredity_sdk','websockets','wasmtime'};"
            "unexpected={name.split('.')[0] for name in set(sys.modules)-baseline"
            " if name.split('.')[0] not in allowed};"
            "assert not unexpected, sorted(unexpected)"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code, str(ROOT / "src")],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_host_post_revision_does_not_relax_module_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            market = Marketplace(temporary, __version__, auto_refresh=False)
            try:
                self.assertEqual(market.host_version, __version__)
                with self.assertRaises(ValueError):
                    _version("1.0.0.post1")
            finally:
                market.close()

    def test_same_numeric_identity_on_two_platforms_never_crosses_accounts(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = PluginHost(Path(temporary) / "host")
            state = None
            try:
                for platform in ("alpha", "beta"):
                    manifest = replace(
                        OfflineExamplePlugin.manifest,
                        plugin_id=f"test.{platform}", platform=platform,
                    )
                    factory = type(
                        f"Adapter_{platform}", (OfflineExamplePlugin,), {"manifest": manifest})
                    host.register(manifest, factory=factory)
                    host.start(manifest.plugin_id, {"self_id": "10001"})
                deadline = time.monotonic() + 3
                while len(host.account_runtimes()) != 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(len(host.account_runtimes()), 2)
                gateway = PluginGatewayBackend(host)
                gateway.select_account("alpha:10001")
                state = WebUIState(
                    "local-boundary-test-password", None, Path(temporary) / "ui",
                    plugin_host=host, plugin_gateway=gateway)
                self.assertEqual(state.accounts()[0]["account"], "alpha:10001")
                self.assertIsNone(state.connection_snapshot()["active"]["origin"])
                with self.assertRaises(ManagementError) as local_only:
                    state.gateway_config()
                self.assertEqual(
                    (local_only.exception.status, local_only.exception.code),
                    (409, "NO_NETWORK_GATEWAY"))
                with self.assertRaises(ManagementError) as rejected:
                    state.invoke({"account": "beta:10001", "action": "echo", "params": {}})
                self.assertEqual(rejected.exception.code, "ACCOUNT_MISMATCH")
                result = state.invoke({
                    "account": "alpha:10001", "action": "echo", "params": {"value": "local"}})
                self.assertEqual(result["status"], "ok")
                gateway.select_account("beta:10001")
                self.assertEqual(state.accounts()[0]["account"], "beta:10001")
            finally:
                if state is not None:
                    state.close()
                host.close()

    def test_local_host_can_relay_a_selected_external_gateway(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = PluginHost(Path(temporary) / "host")
            gateway = PluginGatewayBackend(host)
            state = WebUIState(
                "local-boundary-test-password", None, Path(temporary) / "ui",
                plugin_host=host, plugin_gateway=gateway)
            try:
                state.start()
                external = SimpleNamespace(
                    origin="http://127.0.0.1:1", token="",
                    opener=SimpleNamespace(open=lambda *args, **kwargs: BytesIO(
                        b'data: {"self_id":10001,"post_type":"notice"}\n\n')),
                )
                with state._connection_lock:
                    state.gateway = external
                deadline = time.monotonic() + 3
                while not state.debug and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(state.debug, "External gateway event was not relayed.")
                self.assertEqual(state.debug[-1][1]["account"], "onebot:10001")
            finally:
                state.close()
                host.close()


if __name__ == "__main__":
    unittest.main()
