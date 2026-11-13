"""Portable package metadata for the separate Aegis WebAssembly runtime."""

from dataclasses import dataclass
import re

from .errors import ContractError


_IDENTIFIER = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?\Z")
_PERMISSION = re.compile(r"[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+\Z")
_SUPPORTED = frozenset({
    "fs.read", "fs.write", "net.connect", "bus.call", "bus.publish", "bus.subscribe",
})


@dataclass(frozen=True)
class SandboxModuleManifest:
    """A package declaration is an upper bound, never a host authorization."""

    id: str
    name: str
    version: str
    min_heredity_version: str
    permissions: tuple[str, ...] = ()
    module: str = "module.wasm"
    schema_version: int = 1
    api_major: int = 1
    api_minor: int = 0

    def __post_init__(self):
        if (not isinstance(self.id, str) or len(self.id) > 128
                or not _IDENTIFIER.fullmatch(self.id)):
            raise ContractError("Sandbox module ID is invalid")
        if (not isinstance(self.name, str) or not 1 <= len(self.name) <= 128
                or any(ord(char) < 32 or ord(char) == 127 for char in self.name)):
            raise ContractError("Sandbox module name is invalid")
        try:
            self.name.encode("utf-8")
        except UnicodeError:
            raise ContractError("Sandbox module name is invalid Unicode") from None
        for value in (self.version, self.min_heredity_version):
            if not isinstance(value, str) or len(value) > 64 or not _VERSION.fullmatch(value):
                raise ContractError("Sandbox module version is invalid")
        if (type(self.schema_version) is not int or self.schema_version != 1
                or type(self.api_major) is not int or self.api_major != 1
                or type(self.api_minor) is not int or self.api_minor != 0):
            raise ContractError("Sandbox package or ABI version is unsupported")
        if self.module != "module.wasm":
            raise ContractError("Sandbox packages must contain module.wasm")
        if type(self.permissions) not in (tuple, list) or len(self.permissions) > 64:
            raise ContractError("Sandbox permissions must be a bounded list")
        if (any(not isinstance(value, str) or len(value) > 128
                or not _PERMISSION.fullmatch(value) for value in self.permissions)
                or len(set(self.permissions)) != len(self.permissions)
                or set(self.permissions) - _SUPPORTED):
            raise ContractError("Sandbox permissions are invalid or duplicated")
        object.__setattr__(self, "permissions", tuple(self.permissions))

    @classmethod
    def from_dict(cls, value):
        required = {"schema_version", "id", "name", "version", "sdk_api",
                    "min_heredity_version", "permissions", "module"}
        if type(value) is not dict or set(value) != required:
            raise ContractError("Sandbox manifest fields are invalid")
        api = value["sdk_api"]
        if type(api) is not dict or set(api) != {"major", "minor"}:
            raise ContractError("Sandbox manifest SDK API is invalid")
        return cls(
            value["id"], value["name"], value["version"], value["min_heredity_version"],
            value["permissions"], value["module"], value["schema_version"],
            api["major"], api["minor"])

    def to_dict(self):
        return {"schema_version": self.schema_version, "id": self.id, "name": self.name,
                "version": self.version, "sdk_api": {"major": self.api_major, "minor": self.api_minor},
                "min_heredity_version": self.min_heredity_version,
                "permissions": list(self.permissions), "module": self.module}
