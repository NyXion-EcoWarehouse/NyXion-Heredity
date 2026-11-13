"""Host-owned OneBot transport configuration; distinct from upstream adapters."""

import hmac
import threading

from .onebot import _is_loopback, _validate_event_url


class HostConnectionSettings:
    """Update live transport settings without replacing platform sessions."""

    def __init__(self, server, gateway_backend, *, on_change=None):
        self.server = server
        self.gateway_backend = gateway_backend
        self.on_change = on_change
        self._lock = threading.RLock()

    @staticmethod
    def _secret(value, name):
        if (not isinstance(value, str) or len(value) > 4096
                or any(ord(char) < 33 or ord(char) > 126 for char in value)):
            raise ValueError(f"{name} must be an ASCII token without whitespace.")
        return value

    @staticmethod
    def _urls(values):
        if type(values) is not list or len(values) > 100:
            raise ValueError("Event receivers require a list of at most 100 URLs.")
        seen, urls = set(), []
        for value in values:
            if (not isinstance(value, str) or not 1 <= len(value) <= 2048
                    or "\\" in value or any(ord(char) < 33 or ord(char) > 126 for char in value)):
                raise ValueError("Event receivers require bounded HTTP(S) URLs.")
            _validate_event_url(value)
            if value in seen:
                raise ValueError("Event receivers must be distinct.")
            seen.add(value)
            urls.append(value)
        return tuple(urls)

    def origin(self):
        address, port = self.server.server_address[:2]
        address = "127.0.0.1" if address == "0.0.0.0" else "::1" if address == "::" else address
        authority = f"[{address}]" if ":" in address else address
        return f"http://{authority}:{port}"

    def snapshot(self):
        with self._lock, self.server._settings_lock:
            server = self.server
            origin = self.origin()
            return {
                "schema_version": 1,
                "http": {"enabled": not server._closing.is_set(),
                         "origin": origin, "host": server.server_address[0],
                         "port": server.server_address[1],
                         "authentication": {"configured": bool(server.token)}},
                "sse": {"enabled": server.enable_sse, "path": "/_heredity/events",
                        "url": origin + "/_heredity/events"},
                "event_delivery": {
                    "urls": list(server.event_urls),
                    "heartbeat_seconds": server.heartbeat_interval,
                    "signing_configured": bool(server.event_secret)},
                "account_selection": self.gateway_backend.selection(),
                "supported_transports": ["http", "http_post", "sse"],
                "unsupported_transports": ["websocket", "reverse_websocket"],
                "restart_required": False,
            }

    def update(self, payload):
        allowed = {"event_urls", "event_secret", "access_token", "sse_enabled"}
        if type(payload) is not dict or set(payload) - allowed:
            raise ValueError("Unknown host connection configuration fields.")
        with self._lock:
            with self.server._settings_lock:
                server = self.server
                if server._closing.is_set():
                    raise ValueError("The host listener is closing.")
                token, secret = server.token, server.event_secret
                urls, sse = server.event_urls, server.enable_sse
                if "event_urls" in payload:
                    urls = self._urls(payload["event_urls"])
                if payload.get("access_token") is not None:
                    token = self._secret(payload["access_token"], "Access token")
                if payload.get("event_secret") is not None:
                    secret = self._secret(payload["event_secret"], "Event signing secret")
                if "sse_enabled" in payload:
                    if type(payload["sse_enabled"]) is not bool:
                        raise ValueError("SSE enabled must be boolean.")
                    sse = payload["sse_enabled"]
                if not _is_loopback(server.server_address[0]) and not token:
                    raise ValueError("A non-loopback listener requires an access token.")
                for privileged in (server.provider_token, server.execution_token):
                    if token and privileged and hmac.compare_digest(token, privileged):
                        raise ValueError("Consumer and administrative tokens must differ.")
                # Validate every field before changing any running listener.
                server.token, server.event_secret = token, secret
                server.event_urls, server.enable_sse = urls, sse
            if self.on_change is not None:
                self.on_change(token)
            return self.snapshot()
