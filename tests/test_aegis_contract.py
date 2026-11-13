"""The standalone SDK package declaration grants no host authority."""

import unittest

from nyxion_heredity_sdk import ContractError
from nyxion_heredity_sdk.sandbox import SandboxModuleManifest


class AegisContractTests(unittest.TestCase):
    def manifest(self):
        return {"schema_version": 1, "id": "example.adapter", "name": "Example adapter",
                "version": "1.0.0", "sdk_api": {"major": 1, "minor": 0},
                "min_heredity_version": "0.3.0", "permissions": ["fs.read"],
                "module": "module.wasm"}

    def test_manifest_round_trip_copies_permissions(self):
        raw = self.manifest()
        parsed = SandboxModuleManifest.from_dict(raw)
        raw["permissions"].append("fs.write")
        self.assertEqual(parsed.permissions, ("fs.read",))
        returned = parsed.to_dict()
        returned["permissions"].append("net.connect")
        self.assertEqual(parsed.permissions, ("fs.read",))

    def test_package_declaration_rejects_ambiguous_names_and_native_entrypoints(self):
        for changes in (
                {"id": "../module"}, {"id": "Uppercase"}, {"name": "\ud800"},
                {"version": "latest"}, {"version": 1}, {"schema_version": True},
                {"sdk_api": {"major": 2, "minor": 0}},
                {"sdk_api": {"major": True, "minor": 0}},
                {"module": "plugin.py"}, {"module": "../module.wasm"},
                {"permissions": ["fs.read", "fs.read"]}, {"permissions": "fs.read"},
                {"permissions": ["filesystem"]}, {"permissions": ["fs read"]},
                {"permissions": ["fs.execute"]},
                {"extra": True}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                SandboxModuleManifest.from_dict(self.manifest() | changes)


if __name__ == "__main__":
    unittest.main()
