"""Network permissions resolve once and connect only to checked destinations."""

from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from heredity.aegis import CapabilityBroker, PermissionViolation
from heredity.aegis.broker import _PinnedHTTPSConnection


def address(ip="8.8.8.8", port=443):
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))


class AegisNetworkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="NyXion Aegis network ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def broker(self, allowlist=()):
        broker = CapabilityBroker(
            ("net.connect",), self.root / "state", network_allowlist=allowlist)
        self.addCleanup(broker.close)
        return broker

    def test_permission_without_a_destination_grant_does_not_resolve_or_connect(self):
        broker = self.broker()
        with patch("heredity.aegis.broker.socket.getaddrinfo") as dns:
            with self.assertRaises(PermissionViolation):
                broker.dispatch("net.connect", "tcp.connect", {"host": "example.org", "port": 443})
        dns.assert_not_called()

    def test_public_policy_rejects_private_mixed_and_metadata_addresses_before_connecting(self):
        broker = self.broker(("*public*",))
        for ip in ("127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1",
                   "169.254.169.254", "0.0.0.0", "224.0.0.1"):
            records = [address("8.8.8.8"), address(ip)]
            with self.subTest(ip=ip), patch(
                    "heredity.aegis.broker.socket.getaddrinfo", return_value=records):
                with patch("heredity.aegis.broker._connect_checked") as connect:
                    with self.assertRaises(PermissionViolation):
                        broker.dispatch("net.connect", "tcp.connect",
                                        {"host": "example.org", "port": 443})
                connect.assert_not_called()

    def test_tcp_connect_uses_the_checked_numeric_address_without_a_second_dns_lookup(self):
        broker = self.broker(("*public*",))
        connection = Mock()
        with patch("heredity.aegis.broker.socket.getaddrinfo",
                   return_value=[address()]) as dns:
            with patch("heredity.aegis.broker.socket.socket", return_value=connection):
                result = broker.dispatch(
                    "net.connect", "tcp.connect", {"host": "example.org", "port": 443})
        dns.assert_called_once_with("example.org", 443, type=socket.SOCK_STREAM)
        connection.connect.assert_called_once_with(("8.8.8.8", 443))
        self.assertIn(result["handle"], broker._sockets)

    def test_explicit_origin_can_permit_a_private_service_but_not_another_port(self):
        broker = self.broker(("tcp://127.0.0.1:61000",))
        with patch("heredity.aegis.broker.socket.getaddrinfo",
                   return_value=[address("127.0.0.1", 61000)]):
            with patch("heredity.aegis.broker._connect_checked", return_value=Mock()) as connect:
                broker.dispatch("net.connect", "tcp.connect", {"host": "127.0.0.1", "port": 61000})
        connect.assert_called_once()
        with self.assertRaises(PermissionViolation):
            broker.dispatch("net.connect", "tcp.connect", {"host": "127.0.0.1", "port": 61001})

    def test_https_pin_retains_the_original_server_name_for_tls(self):
        records = [address()]
        connection = _PinnedHTTPSConnection("example.org", 443, addresses=records)
        raw_socket, tls_socket = Mock(), Mock()
        context = Mock()
        context.wrap_socket.return_value = tls_socket
        connection._context = context
        with patch("heredity.aegis.broker._connect_checked", return_value=raw_socket) as connect:
            connection.connect()
        connect.assert_called_once_with(records)
        context.wrap_socket.assert_called_once_with(raw_socket, server_hostname="example.org")
        self.assertIs(connection.sock, tls_socket)

    def test_https_redirect_is_returned_and_never_followed(self):
        broker = self.broker(("*public*",))
        response = Mock(status=302)
        response.read.return_value = b"redirect response"
        connection = Mock()
        connection.getresponse.return_value = response
        with patch("heredity.aegis.broker.socket.getaddrinfo", return_value=[address()]):
            with patch("heredity.aegis.broker._PinnedHTTPSConnection",
                       return_value=connection) as factory:
                result = broker.dispatch("net.connect", "https.request",
                                         {"url": "https://example.org/start"})
        self.assertEqual(result["status"], 302)
        factory.assert_called_once_with("example.org", 443, addresses=[address()])
        connection.request.assert_called_once_with("GET", "/start", None)
        connection.close.assert_called_once()

    def test_foreign_socket_handle_never_becomes_an_authority(self):
        broker = self.broker(("*public*",))
        with self.assertRaises(PermissionViolation):
            broker.dispatch("net.connect", "tcp.read", {"handle": "other-module", "max_bytes": 10})


if __name__ == "__main__":
    unittest.main()
