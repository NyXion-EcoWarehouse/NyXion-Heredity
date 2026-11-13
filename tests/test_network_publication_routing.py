"""In-flight publications retain the account and destination captured at entry."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from heredity.network_interfaces import NetworkInterfaceManager


ACCOUNT_A, ACCOUNT_B = 12345678, 22334455


class EmptyAccounts:
    def account_descriptors(self):
        return []

    def account_runtimes(self):
        return []


class GatedBackend:
    def __init__(self, self_id):
        self.self_id = self_id
        self.entered = threading.Event()
        self.release = threading.Event()

    def event_runtime(self):
        self.entered.set()
        if not self.release.wait(3):
            raise AssertionError("Publication account selection was not released")
        return SimpleNamespace(self_id=self.self_id)


class LocalTransport:
    def __init__(self, *, url, **kwargs):
        self.url = url
        self.events = []
        self.stopped = False

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def snapshot(self):
        return {"endpoint": self.url, "state": "running"}

    def publish(self, event):
        self.events.append(event)


class LocalServer:
    def __init__(self, host, port, *args, **kwargs):
        self.server_address = (host, port or 19000)
        self.events = []
        self.stopped = threading.Event()

    def serve_forever(self):
        self.stopped.wait(5)

    def start_events(self):
        pass

    def shutdown(self):
        self.stopped.set()

    def server_close(self):
        self.stopped.set()

    def _deliver_event(self, opener, event, *, observe=True):
        self.events.append(event)


class PublicationRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="NyXion publication routing ")
        self.addCleanup(temporary.cleanup)
        self.manager = NetworkInterfaceManager(Path(temporary.name), EmptyAccounts())
        self.addCleanup(self.manager.close)
        self.clients = []
        self.servers = []

        def client(**kwargs):
            value = LocalTransport(**kwargs)
            self.clients.append(value)
            return value

        def server(*args, **kwargs):
            value = LocalServer(*args, **kwargs)
            self.servers.append(value)
            return value

        for replacement in (
                patch("heredity.network_interfaces.HTTPClientTransport", side_effect=client),
                patch("heredity.network_interfaces.create_server", side_effect=server),
                patch.object(self.manager, "_pump")):
            replacement.start()
            self.addCleanup(replacement.stop)
        # Stop worker fixtures while the transport patches remain in place.
        self.addCleanup(self.manager.close)

    def create(self, kind, *, account=ACCOUNT_A):
        value = self.manager.create({
            "name": "Local publication routing",
            "type": kind, "enabled": True,
            "account": None if account is None else f"example:{account}",
            "settings": {"url": "http://127.0.0.1:19001/original",
                         "heartbeat_seconds": 3600},
        })
        self.assertEqual(value["state"], "running", value)
        identifier = value["interface_id"]
        resource = self.manager._resources[identifier]
        backend = GatedBackend(ACCOUNT_A)
        resource.backend = backend
        return identifier, resource, backend

    @staticmethod
    def event(account):
        return {"time": 17, "self_id": account, "post_type": "meta_event",
                "meta_event_type": "lifecycle", "sub_type": "enable"}

    def switch_during_publication(self, kind, account, *, configured=ACCOUNT_A):
        identifier, resource, backend = self.create(kind, account=configured)
        original = resource.server if kind == "http_server" else resource.transport
        binding = self.manager.bind_module("test.publication-routing", identifier)
        replacement_kind = "http_client" if kind == "http_server" else "http_server"
        with ThreadPoolExecutor(max_workers=1) as pool:
            publication = pool.submit(binding.publish_event, self.event(account))
            try:
                self.assertTrue(backend.entered.wait(1))
                changed = self.manager.update(identifier, {
                    "type": replacement_kind,
                    "account": f"example:{ACCOUNT_B}",
                    "settings": {"url": "http://127.0.0.1:19002/replacement"},
                })
                self.assertEqual(changed["state"], "running", changed)
                replacement = resource.server if replacement_kind == "http_server" else resource.transport
                self.assertIsNot(original, replacement)
            finally:
                backend.release.set()
            return identifier, original, replacement, publication, binding

    def test_client_publication_keeps_original_account_and_transport_after_reassignment(self):
        identifier, original, replacement, result, binding = self.switch_during_publication(
            "http_client", ACCOUNT_A)
        self.assertTrue(result.result(timeout=1)["published"])
        self.assertEqual(original.events, [self.event(ACCOUNT_A)])
        self.assertEqual(replacement.events, [])
        self.assertTrue(binding.closed)
        self.assertEqual(self.manager.get(identifier)["account"], f"example:{ACCOUNT_B}")

    def test_server_publication_keeps_original_account_and_server_after_reassignment(self):
        _, original, replacement, result, _ = self.switch_during_publication(
            "http_server", ACCOUNT_A)
        self.assertTrue(result.result(timeout=1)["published"])
        self.assertEqual(original.events, [self.event(ACCOUNT_A)])
        self.assertEqual(replacement.events, [])

    def test_reassigned_account_cannot_publish_using_the_original_route(self):
        for kind in ("http_client", "http_server"):
            with self.subTest(kind=kind):
                _, original, replacement, result, _ = self.switch_during_publication(kind, ACCOUNT_B)
                with self.assertRaisesRegex(ValueError, "account does not match"):
                    result.result(timeout=1)
                self.assertEqual(original.events, [])
                self.assertEqual(replacement.events, [])

    def test_automatic_account_selection_is_also_captured_before_reassignment(self):
        _, original, replacement, result, _ = self.switch_during_publication(
            "http_client", ACCOUNT_A, configured=None)
        self.assertTrue(result.result(timeout=1)["published"])
        self.assertEqual(original.events, [self.event(ACCOUNT_A)])
        self.assertEqual(replacement.events, [])

    def test_new_publication_after_reassignment_uses_the_replacement_route(self):
        identifier, original, replacement, result, _ = self.switch_during_publication(
            "http_client", ACCOUNT_A)
        self.assertTrue(result.result(timeout=1)["published"])
        self.manager.publish_event(identifier, self.event(ACCOUNT_B))
        self.assertEqual(original.events, [self.event(ACCOUNT_A)])
        self.assertEqual(replacement.events, [self.event(ACCOUNT_B)])


if __name__ == "__main__":
    unittest.main()
