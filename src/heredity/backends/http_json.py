"""Map explicitly documented HTTP JSON endpoints to OneBot action data.

This is a transport adapter, not a platform protocol implementation. Profiles must
define endpoints and result shapes for a confirmed local control service.
"""

import ipaddress
import json
import os
from pathlib import Path
import threading
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .base import BackendError

MAX_RESPONSE_BYTES = 1024 * 1024
RESERVED_ACTIONS = frozenset({"get_status", "get_version_info"})


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _local_base(value):
    if not isinstance(value, str):
        raise ValueError("profile.base_url must be an explicit loopback HTTP(S) URL.")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("profile.base_url must use HTTP or HTTPS.")
    try:
        is_local = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
        valid_port = parsed.port is not None and 1 <= parsed.port <= 65535
    except ValueError:
        raise ValueError("profile.base_url requires a loopback address and explicit port.") from None
    if not is_local or not valid_port or parsed.username or parsed.password:
        raise ValueError("profile.base_url requires a loopback address, explicit port and no credentials.")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("profile.base_url must be an origin without a path, query or fragment.")
    return value.rstrip("/")


class HTTPJSONBackend:
    def __init__(self, profile: dict):
        if not isinstance(profile, dict):
            raise ValueError("Profile must be a JSON object.")
        self.base_url = _local_base(profile.get("base_url"))
        self.self_id = profile.get("self_id")
        if type(self.self_id) is not int or self.self_id <= 0:
            raise ValueError("profile.self_id must be a positive integer.")
        self.actions = profile.get("actions")
        if not isinstance(self.actions, dict) or not self.actions:
            raise ValueError("profile.actions must be a nonempty object.")
        self.actions = json.loads(json.dumps(self.actions))
        for action, spec in self.actions.items():
            if (not isinstance(action, str) or not action or action in RESERVED_ACTIONS
                    or action.endswith("_async") or not isinstance(spec, dict)):
                raise ValueError("Invalid or reserved profile action.")
            route = spec.get("path")
            if (not isinstance(route, str) or not route.startswith("/")
                    or route.startswith("//") or "\\" in route
                    or urlsplit(route).fragment or urlsplit(route).query
                    or any(ord(char) < 33 or ord(char) > 126 for char in route)):
                raise ValueError(f"Action {action} requires a plain absolute URL path.")
            if spec.get("method", "POST") not in {"GET", "POST"}:
                raise ValueError(f"Action {action} method must be GET or POST.")
            response_path = spec.get("response_path", [])
            if not isinstance(response_path, list) or any(
                    type(item) not in {str, int} for item in response_path):
                raise ValueError(f"Action {action} response_path must be an array of keys/indices.")
        token_env = profile.get("access_token_env")
        if token_env is not None and (not isinstance(token_env, str) or not token_env):
            raise ValueError("access_token_env must name an environment variable.")
        self._token = os.environ.get(token_env, "") if token_env else ""
        if token_env and not self._token:
            raise ValueError("Configured backend token environment variable is empty.")
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        self._lock = threading.Lock()
        self.online = False

    @classmethod
    def from_file(cls, path):
        with Path(path).open(encoding="utf-8") as stream:
            return cls(json.load(stream))

    def call(self, action, params):
        spec = self.actions.get(action)
        if spec is None:
            raise BackendError(f"Action is absent from the explicit backend profile: {action}", 1404)
        method = spec.get("method", "POST")
        url = self.base_url + spec["path"]
        headers = {"Accept": "application/json"}
        data = None
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        if method == "GET":
            query = {}
            for key, value in params.items():
                query[key] = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list, bool)) else value
            if query:
                url += "?" + urlencode(query)
        else:
            headers["Content-Type"] = "application/json"
            data = json.dumps(params, ensure_ascii=False, allow_nan=False).encode("utf-8")
        try:
            with self._lock:
                with self._opener.open(Request(url, data=data, headers=headers, method=method),
                                       timeout=3) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError("oversized response")
                def reject_nonfinite(value):
                    raise ValueError("nonfinite backend JSON")
                result = json.loads(raw, parse_constant=reject_nonfinite)
                if isinstance(result, dict) and result.get("status") == "failed":
                    raise ValueError("backend reported failure")
                for key in spec.get("response_path", []):
                    result = result[key]
                # A profile cannot silently turn a OneBot-shaped failure into success.
                if isinstance(result, dict) and result.get("status") == "failed":
                    raise ValueError("backend reported failure")
                json.dumps(result, allow_nan=False)
                self.online = True
                return result
        except (OSError, HTTPException, HTTPError, URLError, ValueError, TypeError,
                KeyError, IndexError, RecursionError):
            self.online = False
            raise BackendError("Configured backend request failed or returned an invalid response.", 1502) from None

    def close(self):
        self.online = False
