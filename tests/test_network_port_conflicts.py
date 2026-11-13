"""Actual localhost bind failures retain explicit ports and sanitized errors."""

import errno
import http.client
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from websockets.sync.client import connect

from heredity.network_errors import listener_failure
from heredity.network_interfaces import NetworkInterfaceManager
from heredity.network_websocket import WebSocketServerTransport, WebSocketTransportError


class EmptyAccounts:
    def account_descriptors(self):
        return []

    def account_runtimes(self):
        return []


def occupied_listener():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server


class NetworkPortConflictTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="NyXion listener ports ")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.manager = NetworkInterfaceManager(self.directory, EmptyAccounts())
        self.addCleanup(self.manager.close)

    def create(self, kind, port, *, enabled=True):
        return self.manager.create({
            "name": "User configured listener", "type": kind, "enabled": enabled,
            "settings": {"host": "127.0.0.1", "port": port,
                         "path": "/onebot", "heartbeat_seconds": 3600}})

    def request(self, snapshot):
        token = self.manager.credentials(snapshot["interface_id"])["access_token"]
        if snapshot["type"] == "http_server":
            parsed = urlsplit(snapshot["endpoint"])
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
            try:
                connection.request("POST", parsed.path, json.dumps({
                    "action": "get_version_info", "params": {}}),
                    {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                return json.loads(response.read())
            finally:
                connection.close()
        with connect(snapshot["endpoint"], additional_headers={
                "Authorization": "Bearer " + token}, proxy=None) as connection:
            connection.send(json.dumps({"action": "get_version_info", "echo": "port-test"}))
            response = json.loads(connection.recv(timeout=2))
            self.assertEqual(response["echo"], "port-test")
            return response

    def test_http_and_websocket_conflicts_are_explicit_and_retry_uses_the_same_requested_port(self):
        for kind in ("http_server", "websocket_server"):
            with self.subTest(kind=kind):
                held = occupied_listener()
                self.addCleanup(held.close)
                port = held.getsockname()[1]
                failed = self.create(kind, port)
                self.assertEqual(failed["state"], "error")
                self.assertEqual(failed["error_code"], "PORT_IN_USE")
                self.assertIn(str(port), failed["error"])
                self.assertIn("已被占用", failed["error"])
                self.assertFalse(failed["enabled"])
                self.assertIsNone(failed["actual_port"])
                self.assertIsNone(failed["endpoint"])
                self.assertEqual(failed["settings"]["port"], port)
                identifier = failed["interface_id"]
                token = self.manager.credentials(identifier)["access_token"]
                self.assertNotIn(token, failed["error"])
                held.close()
                restarted = self.manager.retry(identifier)
                self.assertEqual(restarted["interface_id"], identifier)
                self.assertEqual(restarted["state"], "running", restarted)
                self.assertEqual(restarted["actual_port"], port)
                self.assertEqual(restarted["settings"]["port"], port)
                self.assertIsNone(restarted["error"])
                self.assertIsNone(restarted["error_code"])
                self.assertEqual(self.request(restarted)["status"], "ok")
                self.manager.stop(identifier)

    def test_direct_websocket_transport_preserves_structured_bind_failure_without_native_details(self):
        held = occupied_listener()
        self.addCleanup(held.close)
        port = held.getsockname()[1]
        transport = WebSocketServerTransport(host="127.0.0.1", port=port,
                                             token="private-endpoint-token")
        self.addCleanup(transport.stop)
        with self.assertRaises(WebSocketTransportError) as failure:
            transport.start()
        self.assertEqual(failure.exception.code, "PORT_IN_USE")
        snapshot = transport.snapshot()
        self.assertEqual(snapshot["state"], "error")
        self.assertEqual(snapshot["error_code"], "PORT_IN_USE")
        self.assertIsNone(snapshot["actual_port"])
        self.assertIn(str(port), str(failure.exception))
        self.assertNotIn("private-endpoint-token", str(failure.exception))

    def test_two_distinct_explicit_ports_run_independently_and_never_change_during_other_failures(self):
        first_port_socket, second_port_socket = occupied_listener(), occupied_listener()
        self.addCleanup(first_port_socket.close)
        self.addCleanup(second_port_socket.close)
        first_port = first_port_socket.getsockname()[1]
        second_port = second_port_socket.getsockname()[1]
        self.assertNotEqual(first_port, second_port)
        first_port_socket.close()
        second_port_socket.close()
        first = self.create("http_server", first_port)
        second = self.create("websocket_server", second_port)
        self.assertEqual((first["state"], second["state"]), ("running", "running"))
        self.assertEqual((first["actual_port"], second["actual_port"]), (first_port, second_port))
        conflict = self.create("websocket_server", first_port)
        self.assertEqual(conflict["error_code"], "PORT_IN_USE")
        self.assertEqual(self.request(first)["status"], "ok")
        self.assertEqual(self.request(second)["status"], "ok")
        self.manager.stop(first["interface_id"])
        self.assertEqual(self.manager.get(second["interface_id"])["state"], "running")
        self.assertEqual(self.request(second)["status"], "ok")

    def test_common_custom_ports_are_preserved_and_generic_bind_failures_are_sanitized(self):
        for port in (2000, 9999):
            for kind in ("http_server", "websocket_server"):
                with self.subTest(port=port, kind=kind):
                    value = self.create(kind, port, enabled=False)
                    self.assertEqual(value["settings"]["port"], port)
                    self.assertEqual(value["state"], "stopped")
                    self.assertIsNone(value["actual_port"])
                    self.assertIsNone(value["error_code"])
        private_error = OSError(errno.EACCES, "private/token/C:/secret-path")
        for kind, target in (("http_server", "heredity.network_interfaces.create_server"),
                             ("websocket_server", "heredity.network_websocket.serve")):
            with self.subTest(kind=kind):
                with patch(target, side_effect=private_error):
                    failed = self.create(kind, 9999)
                self.assertEqual(failed["state"], "error")
                self.assertEqual(failed["error_code"], "NETWORK_BIND_FAILED")
                self.assertIn("9999", failed["error"])
                self.assertNotIn("secret", failed["error"])
                self.assertNotIn("private", failed["error"])
                self.assertNotIn("EACCES", failed["error"])

    def test_os_error_codes_have_the_same_public_shape_on_windows_linux_and_macos(self):
        for code in (errno.EADDRINUSE, 48, 98, 10048):
            with self.subTest(code=code):
                public = listener_failure(OSError(code, "native detail"), 2000)
                self.assertEqual(public[0], "PORT_IN_USE")
                self.assertIn("2000", public[1])
        native = OSError("native Windows detail")
        native.winerror = 10048
        self.assertEqual(listener_failure(native, 9999)[0], "PORT_IN_USE")

    def test_temporary_bind_error_is_not_persisted_and_does_not_start_on_restore(self):
        held = occupied_listener()
        self.addCleanup(held.close)
        port = held.getsockname()[1]
        failed = self.create("http_server", port)
        self.assertEqual(failed["error_code"], "PORT_IN_USE")
        self.manager.close()
        raw = json.loads((self.directory / "network-interfaces.json").read_bytes())
        self.assertNotIn("error_code", raw["interfaces"][0])
        self.assertNotIn("error", raw["interfaces"][0])
        restored = NetworkInterfaceManager(self.directory, EmptyAccounts())
        self.addCleanup(restored.close)
        restored.start_all()
        current = restored.get(failed["interface_id"])
        self.assertEqual(current["state"], "stopped")
        self.assertFalse(current["enabled"])
        self.assertEqual(current["settings"]["port"], port)
        self.assertIsNone(current["actual_port"])
        self.assertIsNone(current["error_code"])
        self.assertIsNone(current["error"])


if __name__ == "__main__":
    unittest.main()
