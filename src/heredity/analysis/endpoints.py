"""Extract bounded loopback origins from ordinary byte strings."""

import ipaddress
import re
from urllib.parse import urlsplit


_URL = re.compile(
    rb"(?:https?|wss?)://(?:localhost|127(?:\.[0-9]{1,3}){3}|\[::1\])"
    rb":[0-9]{1,5}(?=[/\s\x00\"'?#:]|$)",
    re.IGNORECASE,
)


def endpoint_candidates(data: bytes) -> list[str]:
    """Return local origins without paths, query strings or credentials."""
    matches = set()
    variants = (
        data,
        data[0::2] if data[1::2].count(0) > len(data) // 4 else b"",
        data[1::2] if data[0::2].count(0) > len(data) // 4 else b"",
    )
    for variant in variants:
        for match in _URL.finditer(variant):
            origin = match.group().decode("ascii").lower()
            try:
                parsed = urlsplit(origin)
                if parsed.port is None or not 1 <= parsed.port <= 65535:
                    continue
                if parsed.hostname != "localhost":
                    if not ipaddress.ip_address(parsed.hostname).is_loopback:
                        continue
            except ValueError:
                continue
            matches.add(origin)
    return sorted(matches)
