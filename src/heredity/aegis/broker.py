"""Host-side capabilities; guest code never receives host files or sockets."""

import base64
import binascii
import http.client
import ipaddress
import os
from pathlib import Path, PurePosixPath
import socket
import ssl
import stat
import threading
from urllib.parse import urlsplit
from uuid import uuid4


MAX_BYTES = 64 * 1024


class PermissionViolation(ValueError):
    """A forbidden guest request requires termination of its worker."""

    code = "AEGIS_PERMISSION_DENIED"


def _bytes(value):
    if not isinstance(value, str) or len(value) > 4 * ((MAX_BYTES + 2) // 3):
        raise PermissionViolation("Capability data exceeds its limit")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise PermissionViolation("Capability data must be base64") from None
    if len(raw) > MAX_BYTES:
        raise PermissionViolation("Capability data exceeds its limit")
    return raw


def _no_links(path):
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
            raise PermissionViolation("Capability paths cannot traverse links")


def _resolved_addresses(host, port, *, public_only):
    records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not records or len(records) > 32:
        raise PermissionViolation("Network destination could not be verified")
    addresses = [ipaddress.ip_address(record[4][0]) for record in records]
    if public_only and any(not address.is_global or address.is_multicast
                           or address.is_unspecified or address.is_loopback
                           or address.is_link_local for address in addresses):
        raise PermissionViolation("Network capability does not grant private destinations")
    return records


def _connect_checked(records):
    failure = None
    for family, kind, protocol, _, address in records:
        connection = socket.socket(family, kind, protocol)
        try:
            connection.settimeout(2)
            connection.connect(address)
            return connection
        except OSError as error:
            failure = error
            connection.close()
    raise OSError("The verified destination could not be reached") from failure


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, *, addresses):
        self._addresses = addresses
        super().__init__(host, port, timeout=2, context=ssl.create_default_context())

    def connect(self):
        connection = _connect_checked(self._addresses)
        try:
            self.sock = self._context.wrap_socket(connection, server_hostname=self.host)
        except BaseException:
            connection.close()
            raise


class CapabilityBroker:
    """Resolve only approved capabilities inside one private module namespace."""

    def __init__(self, permissions, state_dir, *, network_allowlist=(), host_handler=None):
        self.permissions = frozenset(permissions)
        self.state_dir = Path(state_dir).absolute()
        _no_links(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir = self.state_dir.resolve()
        self.host_handler = host_handler
        self._sockets = {}
        self._lock = threading.Lock()
        self._closed = False
        self.public_network = "*public*" in network_allowlist
        self.destinations = frozenset(self._destination(value) for value in network_allowlist
                                      if value != "*public*")

    @staticmethod
    def _destination(value):
        try:
            parsed = urlsplit(value)
            if (not isinstance(value, str) or len(value) > 512
                    or parsed.scheme not in {"tcp", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
                raise ValueError
            port = parsed.port or (443 if parsed.scheme == "https" else None)
            if port is None or not 1 <= port <= 65535:
                raise ValueError
            return parsed.scheme, parsed.hostname.lower(), port
        except (ValueError, TypeError):
            raise ValueError("Network allowlist entries must be exact TCP or HTTPS origins") from None

    def _path(self, value):
        if (not isinstance(value, str) or not 1 <= len(value) <= 1024 or "\\" in value
                or ":" in value or "\0" in value or value.startswith("/")
                or any(part in {"", ".", ".."} for part in value.split("/"))):
            raise PermissionViolation("Filesystem capability requires a plain relative path")
        relative = PurePosixPath(value)
        target = self.state_dir.joinpath(*relative.parts)
        _no_links(target)
        resolved = target.resolve()
        if not resolved.is_relative_to(self.state_dir):
            raise PermissionViolation("Filesystem capability escaped module state")
        return target

    def _file(self, capability, params):
        expected = {"path"} if capability == "fs.read" else {"path", "data_base64"}
        if set(params) != expected:
            raise PermissionViolation("Filesystem capability fields are invalid")
        target = self._path(params["path"])
        flags = getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if capability == "fs.read":
            with os.fdopen(os.open(target, os.O_RDONLY | flags), "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES
                        or info.st_nlink != 1):
                    raise PermissionViolation("Filesystem capability requires a small regular file")
                raw = stream.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise PermissionViolation("Filesystem capability read exceeds its limit")
            return {"data_base64": base64.b64encode(raw).decode(), "bytes": len(raw)}
        raw = _bytes(params["data_base64"])
        # Parent creation is host work; each existing component is rechecked.
        target.parent.mkdir(parents=True, exist_ok=True)
        _no_links(target)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PermissionViolation("Filesystem capability requires a regular file")
            stream.truncate(0)
            stream.write(raw)
        return {"bytes": len(raw)}

    def _authorize_origin(self, scheme, host, port):
        if (not isinstance(host, str) or not 1 <= len(host) <= 253
                or type(port) is not int or not 1 <= port <= 65535):
            raise PermissionViolation("Network destination is invalid")
        if any(character.isspace() for character in host) or any(
                character in host for character in ("/", "\\", "@", "\0")):
            raise PermissionViolation("Network destination is invalid")
        exact = (scheme, host.lower(), port) in self.destinations
        if not exact and not self.public_network:
            raise PermissionViolation("Network destination was not granted")
        return _resolved_addresses(host, port, public_only=not exact)

    def _network(self, operation, params):
        if operation == "tcp.connect":
            if set(params) != {"host", "port"}:
                raise PermissionViolation("TCP destination fields are invalid")
            host, port = params["host"], params["port"]
            addresses = self._authorize_origin("tcp", host, port)
            with self._lock:
                if self._closed or len(self._sockets) >= 4:
                    raise PermissionViolation("TCP handle limit was reached")
            connection = _connect_checked(addresses)
            with self._lock:
                if self._closed or len(self._sockets) >= 4:
                    connection.close()
                    raise PermissionViolation("TCP handle limit was reached")
                handle = uuid4().hex
                self._sockets[handle] = connection
            return {"handle": handle}
        if operation in {"tcp.write", "tcp.read", "tcp.close"}:
            allowed = {"handle", "data_base64"} if operation == "tcp.write" else (
                {"handle", "max_bytes"} if operation == "tcp.read" else {"handle"})
            if set(params) != allowed or not isinstance(params.get("handle"), str):
                raise PermissionViolation("TCP operation fields are invalid")
            with self._lock:
                connection = self._sockets.get(params["handle"])
                if connection is None:
                    raise PermissionViolation("TCP handle does not belong to this module")
                if operation == "tcp.close":
                    self._sockets.pop(params["handle"])
            if operation == "tcp.close":
                connection.close()
                return {"closed": True}
            if operation == "tcp.write":
                raw = _bytes(params["data_base64"])
                connection.sendall(raw)
                return {"bytes": len(raw)}
            count = params["max_bytes"]
            if type(count) is not int or not 1 <= count <= MAX_BYTES:
                raise PermissionViolation("TCP read limit is invalid")
            raw = connection.recv(count)
            return {"data_base64": base64.b64encode(raw).decode(), "bytes": len(raw)}
        if operation != "https.request" or set(params) - {"url", "method", "data_base64"}:
            raise PermissionViolation("Network operation is unsupported")
        value = params.get("url")
        if not isinstance(value, str) or len(value) > 2048 or "\\" in value:
            raise PermissionViolation("HTTPS URL is invalid")
        try:
            parsed = urlsplit(value)
            port = parsed.port or 443
        except ValueError:
            raise PermissionViolation("HTTPS URL is invalid") from None
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment):
            raise PermissionViolation("HTTPS URL must use a granted origin")
        addresses = self._authorize_origin("https", parsed.hostname, port)
        method = params.get("method", "GET")
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
            raise PermissionViolation("HTTPS method is unsupported")
        payload = _bytes(params["data_base64"]) if "data_base64" in params else None
        connection = _PinnedHTTPSConnection(parsed.hostname, port, addresses=addresses)
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection.request(method, path, payload)
            response = connection.getresponse()
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise PermissionViolation("HTTPS response exceeds its limit")
            # No redirect is followed, and response headers expose no host credentials.
            return {"status": response.status, "data_base64": base64.b64encode(raw).decode()}
        finally:
            connection.close()

    def dispatch(self, capability, operation, params):
        if (capability not in self.permissions or not isinstance(operation, str)
                or type(params) is not dict or self._closed):
            raise PermissionViolation("The module was not granted this capability")
        if capability in {"fs.read", "fs.write"}:
            if operation != capability:
                raise PermissionViolation("Filesystem operation does not match its capability")
            return self._file(capability, params)
        if capability == "net.connect":
            return self._network(operation, params)
        if self.host_handler is not None:
            return self.host_handler(capability, operation, params)
        raise PermissionViolation("The host does not provide this capability")

    def close(self):
        with self._lock:
            self._closed = True
            sockets, self._sockets = self._sockets, {}
        for connection in sockets.values():
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
