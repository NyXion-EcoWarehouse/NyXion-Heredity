"""Validate host release revisions independently of stable module versions."""

import re


_HOST_VERSION = re.compile(
    r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})"
    r"(?:\.post(0|[1-9][0-9]{0,7}))?\Z"
)


def host_version(value):
    if not isinstance(value, str):
        raise ValueError("Host version must be a release version string.")
    match = _HOST_VERSION.fullmatch(value)
    if match is None:
        raise ValueError("Host version must contain three numeric parts and an optional post revision.")
    return tuple(int(part or 0) for part in match.groups())
