import base64
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from heredity.webui import create_webui_server


PASSWORD = "plugin-ui-local-password"
PLUGIN_ID = "example.adapter"


class HostError(ValueError):
    def __init__(self, status, code):
        super().__init__("private-token-must-not-be-returned")
        self.status, self.code = status, code


class LocalPluginHost:
    """A management-only double with no platform or network access."""

    def __init__(self):
        self.calls = []
        self.failure = None
        self.current_challenge = None
        self.plugin = {
            "id": PLUGIN_ID, "name": "Example", "version": "0.1.0", "platform": "example",
            "execution": "embedded", "state": "discovered",
            "handshake": {"api_major": 1, "api_minor": 0, "verified": False},
            "heartbeat": {"sequence": 0, "last_seen_at": None, "lease_seconds": 30, "healthy": False},
            "session": None, "actions": ["get_login_info"], "capabilities": ["text"],
            "config_schema": {"type": "object", "properties": {
                "expected_self_id": {"type": "integer", "minimum": 1}}, "required": ["expected_self_id"]},
            "error": None,
        }

    def catalog(self):
        self.calls.append(("catalog",))
        return {"api_version": "1.0", "host_running": True, "plugins": [self.plugin], "sessions": []}

    def start(self, plugin_id, config):
        self.calls.append(("start", plugin_id, config))
        if self.failure:
            raise self.failure
        self.plugin = self.plugin | {"state": "starting"}
        return self.plugin

    def stop(self, plugin_id):
        self.calls.append(("stop", plugin_id))
        if self.failure:
            raise self.failure
        self.plugin = self.plugin | {"state": "stopped"}
        return self.plugin

    def challenge(self, plugin_id):
        self.calls.append(("challenge", plugin_id))
        if self.failure:
            raise self.failure
        return self.current_challenge


class PluginManagementHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.host = LocalPluginHost()
        self.ui = self.make_server(self.host, "managed")
        status, login = self.request("POST", "/api/login", {"password": PASSWORD})
        self.assertEqual(status, 200)
        self.token = login["token"]

    def make_server(self, host, folder):
        server = create_webui_server(
            "127.0.0.1", 0, PASSWORD, state_dir=Path(self.directory.name) / folder, plugin_host=host)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(self.stop_server, server, worker)
        return server

    def stop_server(self, server, worker):
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())

    def request(self, method, path, payload=None, *, token=None, headers=None, server=None):
        selected = server or self.ui
        connection = http.client.HTTPConnection(*selected.server_address, timeout=3)
        outgoing = dict(headers or {})
        if token is not None:
            outgoing["X-Heredity-Token"] = token
        data = None
        if payload is not None:
            data = json.dumps(payload)
            outgoing["Content-Type"] = "application/json"
        try:
            connection.request(method, path, data, outgoing)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_catalog_and_challenge_require_authentication_and_have_no_start_side_effect(self):
        for path in ("/api/heredity/plugins", f"/api/heredity/plugins/{PLUGIN_ID}/challenge"):
            status, _ = self.request("GET", path)
            self.assertEqual(status, 401)
        self.assertEqual(self.host.calls, [])
        status, value = self.request("GET", "/api/heredity/plugins", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(value["plugins"][0]["state"], "discovered")
        self.assertEqual(self.host.calls, [("catalog",)])
        status, value = self.request("GET", f"/api/heredity/plugins/{PLUGIN_ID}/challenge", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(value, {"challenge": None})
        self.assertFalse(any(call[0] == "start" for call in self.host.calls))

    def test_start_and_stop_are_explicit_authenticated_posts(self):
        path = f"/api/heredity/plugins/{PLUGIN_ID}"
        status, _ = self.request("POST", path + "/start", {"config": {"expected_self_id": 123}})
        self.assertEqual(status, 401)
        self.assertEqual(self.host.calls, [])
        status, value = self.request(
            "POST", path + "/start", {"config": {"expected_self_id": 123}}, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(value["state"], "starting")
        self.assertEqual(self.host.calls, [("start", PLUGIN_ID, {"expected_self_id": 123})])
        status, value = self.request("POST", path + "/stop", {}, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(value["state"], "stopped")
        self.assertEqual(self.host.calls[-1], ("stop", PLUGIN_ID))

    def test_bad_start_and_stop_shapes_never_reach_host(self):
        for payload in ({}, {"config": []}, {"config": {}, "extra": 1}):
            status, _ = self.request(
                "POST", f"/api/heredity/plugins/{PLUGIN_ID}/start", payload, token=self.token)
            self.assertEqual(status, 400)
        status, _ = self.request(
            "POST", f"/api/heredity/plugins/{PLUGIN_ID}/stop", {"config": {}}, token=self.token)
        self.assertEqual(status, 400)
        self.assertEqual(self.host.calls, [])

    def test_get_start_never_starts_plugin(self):
        status, _ = self.request("GET", f"/api/heredity/plugins/{PLUGIN_ID}/start", token=self.token)
        self.assertEqual(status, 501)
        self.assertEqual(self.host.calls, [])

    def test_unknown_or_escaped_routes_never_invoke_host(self):
        for path in ("/api/heredity/plugins/../example.adapter/start",
                     "/api/heredity/plugins/%2Fsecret/start", "/api/heredity/plugins/a%20b/start"):
            status, _ = self.request("POST", path, {"config": {}}, token=self.token)
            self.assertEqual(status, 501)
        self.assertEqual(self.host.calls, [])

    def test_cross_origin_mutations_are_rejected_even_with_valid_token(self):
        path = f"/api/heredity/plugins/{PLUGIN_ID}/start"
        for headers in ({"Origin": "https://untrusted.example"},
                        {"Origin": "http://127.0.0.1:9999"},
                        {"Sec-Fetch-Site": "cross-site"}):
            status, result = self.request("POST", path, {"config": {}}, token=self.token, headers=headers)
            self.assertEqual(status, 403)
            self.assertEqual(result["code"], "CROSS_ORIGIN_REJECTED")
        self.assertEqual(self.host.calls, [])

    def test_same_origin_and_existing_vite_frontend_are_allowed(self):
        for origin in (f"http://127.0.0.1:{self.ui.server_port}", "http://127.0.0.1:5178"):
            status, _ = self.request(
                "POST", f"/api/heredity/plugins/{PLUGIN_ID}/start", {"config": {}},
                token=self.token, headers={"Origin": origin, "Sec-Fetch-Site": "same-origin"})
            self.assertEqual(status, 200)

    def test_host_failure_is_sanitized_and_preserves_safe_status_code(self):
        self.host.failure = HostError(409, "PLUGIN_ALREADY_RUNNING")
        status, value = self.request(
            "POST", f"/api/heredity/plugins/{PLUGIN_ID}/start", {"config": {"secret": "private-secret"}},
            token=self.token)
        self.assertEqual(status, 409)
        self.assertEqual(value["code"], "PLUGIN_ALREADY_RUNNING")
        self.assertNotIn("private-", json.dumps(value))
        self.assertNotIn("private-", json.dumps(list(self.ui.state.logs)))

    def test_missing_host_stays_unconfigured_without_loading_plugins(self):
        ui = self.make_server(None, "standalone")
        status, login = self.request("POST", "/api/login", {"password": PASSWORD}, server=ui)
        self.assertEqual(status, 200)
        status, catalog = self.request("GET", "/api/heredity/plugins", token=login["token"], server=ui)
        self.assertEqual(status, 200)
        self.assertEqual(catalog, {
            "api_version": "1.0", "host_running": False, "plugins": [], "sessions": []})
        status, value = self.request(
            "POST", f"/api/heredity/plugins/{PLUGIN_ID}/start", {"config": {}},
            token=login["token"], server=ui)
        self.assertEqual(status, 503)
        self.assertEqual(value["code"], "PLUGIN_HOST_UNAVAILABLE")

    def test_qr_material_is_authenticated_and_filters_paths_urls_and_tokens(self):
        encoded = base64.b64encode(b"\x89PNG\r\n\x1a\nlocal-fixture").decode()
        self.host.current_challenge = {
            "challenge_id": "challenge-1", "kind": "qr", "title": "Confirm login",
            "expires_at": None,
            "payload": {"mime": "image/png", "image_base64": encoded, "message": "Waiting",
                        "path": "private-path", "url": "https://private.example", "token": "private-token"}}
        status, value = self.request(
            "GET", f"/api/heredity/plugins/{PLUGIN_ID}/challenge", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(value["challenge"]["payload"], {
            "mime": "image/png", "image_base64": encoded, "message": "Waiting"})
        self.assertNotIn("private-", json.dumps(value))

    def test_non_png_or_invalid_base64_is_rejected(self):
        for payload in ({"mime": "image/svg+xml", "image_base64": "PHN2Zz4="},
                        {"mime": "image/png", "image_base64": "not-base64"},
                        {"mime": "image/png", "image_base64": base64.b64encode(b"not-png").decode()}):
            self.host.current_challenge = {
                "challenge_id": "fixture", "kind": "qr", "title": "fixture",
                "expires_at": None, "payload": payload}
            status, value = self.request(
                "GET", f"/api/heredity/plugins/{PLUGIN_ID}/challenge", token=self.token)
            self.assertEqual(status, 502)
            self.assertEqual(value["code"], "INVALID_PLUGIN_CHALLENGE")

    def test_status_retains_compatibility_identifier_and_exposes_formal_brand(self):
        status, value = self.request("GET", "/api/status", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(value["app"], "Heredity")
        self.assertEqual(value["display_name"], "NyXion Heredity")
        self.assertTrue(value["plugin_host"])

    def test_plugin_host_rejects_legacy_process_inspection_without_loading_it(self):
        paths = (("GET", "/api/processes", None),
                 ("GET", "/api/heredity/program", None),
                 ("POST", "/api/heredity/program/bind", {"pid": 123, "start_token": "fixture"}),
                 ("POST", "/api/heredity/program/inspect", {"pid": 123, "start_token": "fixture"}),
                 ("POST", "/api/heredity/program/unbind", {}))
        for method, path, payload in paths:
            with self.subTest(path=path):
                status, value = self.request(method, path, payload, token=self.token)
                self.assertEqual(status, 501)
                self.assertEqual(value["code"], "UNSUPPORTED")
        self.assertFalse(hasattr(self.ui.state, "program"))
        self.assertFalse(hasattr(self.ui.state, "_program"))


class CleanPluginUIImportsTests(unittest.TestCase):
    def test_application_and_plugin_ui_do_not_import_adapters_or_process_discovery(self):
        source = Path(__file__).resolve().parents[1] / "src"
        script = f"""
import sys, tempfile
sys.path.insert(0, {str(source)!r})
import heredity.application
from heredity.webui import create_webui_server
class LocalHost:
    def catalog(self):
        return {{'api_version':'1.0','host_running':True,'plugins':[],'sessions':[]}}
with tempfile.TemporaryDirectory() as directory:
    server=create_webui_server('127.0.0.1',0,'isolated-plugin-ui-password',
        state_dir=directory,plugin_host=LocalHost())
    try:
        assert server.state.plugin_catalog()['host_running']
        assert not hasattr(server.state, 'program')
    finally:
        server.server_close()
for name in sys.modules:
    assert name != 'heredity.program', name
    assert not name.startswith('heredity.discovery'), name
    assert not name.startswith('heredity.protocol'), name
    assert not (name.startswith('nyxion_heredity_') and not name.startswith('nyxion_heredity_sdk')), name
print('clean-plugin-ui')
"""
        result = subprocess.run(
            [sys.executable, "-I", "-c", script], capture_output=True, timeout=15,
            env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"})
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertIn(b"clean-plugin-ui", result.stdout)

    def test_running_application_status_and_state_stream_stay_platform_independent(self):
        source = Path(__file__).resolve().parents[1] / "src"
        script = f"""
import http.client, json, os, sys, tempfile, time
sys.path.insert(0, {str(source)!r})
from heredity.application import Application
from heredity.cli import build_parser
os.environ['NYXION_TEST_UI_PASSWORD']='isolated-plugin-ui-password'
def request(server,method,path,payload=None,token=None):
    connection=http.client.HTTPConnection(*server.server_address,timeout=3)
    headers={{}}
    if token: headers['X-Heredity-Token']=token
    if payload is not None: headers['Content-Type']='application/json'
    try:
        connection.request(method,path,json.dumps(payload) if payload is not None else None,headers)
        response=connection.getresponse()
        return response.status,json.loads(response.read())
    finally: connection.close()
with tempfile.TemporaryDirectory() as directory:
    args=build_parser().parse_args(['app','--port','0','--ui-port','0',
        '--state-dir',directory,'--password-env','NYXION_TEST_UI_PASSWORD'])
    app=Application(args)
    try:
        app.start()
        assert app.ui.state._relay.is_alive()
        status,login=request(app.ui,'POST','/api/login',{{'password':os.environ['NYXION_TEST_UI_PASSWORD']}})
        assert status==200
        token=login['token']
        for path in ('/api/status','/api/heredity/status','/api/heredity/plugins'):
            status,value=request(app.ui,'GET',path,token=token)
            assert status==200,(path,value)
        status,value=request(app.ui,'GET','/api/heredity/program',token=token)
        assert status==501
        connection=http.client.HTTPConnection(*app.ui.server_address,timeout=3)
        try:
            connection.request('GET','/api/state/stream',headers={{'X-Heredity-Token':token}})
            response=connection.getresponse()
            assert response.status==200
            seen=False
            for _ in range(10):
                line=response.readline()
                if b'"resource": "plugins"' in line:
                    seen=True
                    break
            assert seen,'plugin state frame missing'
        finally: connection.close()
        assert not hasattr(app.ui.state, 'program')
    finally:
        app.close()
    assert not app.ui.state._relay.is_alive()
for name in sys.modules:
    assert name != 'heredity.program', name
    assert not name.startswith('heredity.discovery'), name
    assert not name.startswith('heredity.protocol'), name
    assert not (name.startswith('nyxion_heredity_') and not name.startswith('nyxion_heredity_sdk')), name
print('clean-running-application')
"""
        result = subprocess.run(
            [sys.executable, "-I", "-c", script], capture_output=True, timeout=30,
            env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"})
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertIn(b"clean-running-application", result.stdout)
