"""Non-secret, host-owned interface restart intent keyed by module and account."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
from uuid import uuid4

from nyxion_heredity_sdk import NetworkInterfaceRequirement
from nyxion_heredity_sdk.networks import validate_interface_id


@dataclass(frozen=True)
class NetworkRestartPolicy:
    auto_start: bool = True
    interface_ids: tuple[str, ...] = ()


class NetworkRestartPolicies:
    def __init__(self, state_dir, *, limit=1024):
        self.limit = limit
        self.path = (Path(state_dir).resolve() / "network-module-policies.json"
                     if state_dir is not None else None)
        self.values = {}
        if self.path is not None and self.path.exists():
            self._read()

    @staticmethod
    def key(requirement):
        return requirement.module_id, requirement.account

    def get(self, requirement):
        return self.values.get(self.key(requirement), NetworkRestartPolicy())

    def _check_path(self):
        if self.path is None:
            return
        for path in (self.path, *self.path.parents):
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise ValueError("Network module policy path cannot traverse links.")

    def _read(self):
        self._check_path()
        if not self.path.is_file() or self.path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("Network module policy file has an invalid type or size.")

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Network module policy contains duplicate fields.")
                result[key] = value
            return result

        data = json.loads(self.path.read_bytes(), object_pairs_hook=unique)
        if (type(data) is not dict or set(data) != {"schema_version", "policies"}
                or type(data["schema_version"]) is not int or data["schema_version"] != 1
                or type(data["policies"]) is not list or len(data["policies"]) > self.limit):
            raise ValueError("Network module policy format is invalid.")
        for record in data["policies"]:
            if (type(record) is not dict
                    or set(record) != {"module_id", "account", "auto_start", "interface_ids"}
                    or type(record["auto_start"]) is not bool
                    or type(record["interface_ids"]) is not list
                    or len(record["interface_ids"]) > 64):
                raise ValueError("Network module policy entry is invalid.")
            requirement = NetworkInterfaceRequirement(
                record["module_id"], ("http_server",), account=record["account"])
            identifiers = tuple(validate_interface_id(value) for value in record["interface_ids"])
            key = self.key(requirement)
            if key in self.values or len(identifiers) != len(set(identifiers)):
                raise ValueError("Network module policy identities conflict.")
            self.values[key] = NetworkRestartPolicy(record["auto_start"], identifiers)

    def set(self, requirement, *, auto_start=None, interface_ids=None):
        key = self.key(requirement)
        previous = self.values.get(key)
        old = previous or NetworkRestartPolicy()
        value = NetworkRestartPolicy(
            old.auto_start if auto_start is None else auto_start,
            old.interface_ids if interface_ids is None else tuple(interface_ids))
        if value == old and previous is not None:
            return old
        if previous is None and len(self.values) >= self.limit:
            raise ValueError("The network module restart policy limit was reached.")
        self.values[key] = value
        try:
            self._persist()
        except BaseException:
            if previous is None:
                self.values.pop(key)
            else:
                self.values[key] = previous
            raise
        return value

    def _persist(self):
        if self.path is None:
            return
        self._check_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "policies": [
            {"module_id": module_id, "account": account,
             "auto_start": policy.auto_start, "interface_ids": list(policy.interface_ids)}
            for (module_id, account), policy in self.values.items()]}
        temporary = self.path.with_name(".network-module-policies-" + uuid4().hex + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=True, allow_nan=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            self._check_path()
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
