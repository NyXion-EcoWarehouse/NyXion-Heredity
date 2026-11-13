import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "sdk" / "Build-Sdk.py"
SPEC = importlib.util.spec_from_file_location("nyxion_sdk_build", SCRIPT)
PACKAGING = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGING)
CHECK_SPEC = importlib.util.spec_from_file_location(
    "nyxion_sdk_distribution_check", ROOT / "sdk" / "Check-Distribution.py")
CHECKER = importlib.util.module_from_spec(CHECK_SPEC)
CHECK_SPEC.loader.exec_module(CHECKER)


class SDKDistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="NyXion SDK distribution ")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_staged_source_contains_only_sdk_and_is_independent_of_monorepo_paths(self):
        stage = PACKAGING.stage_source(self.directory / "source")
        package = stage / "src" / "nyxion_heredity_sdk"
        self.assertTrue((package / "__init__.py").is_file())
        self.assertTrue((package / "onebot.py").is_file())
        self.assertTrue((package / "networks.py").is_file())
        self.assertTrue((package / "sandbox.py").is_file())
        self.assertTrue((package / "py.typed").is_file())
        self.assertTrue((package / "schemas" / "plugin-manifest.v1.schema.json").is_file())
        self.assertTrue((package / "schemas" / "sandbox-module.v1.schema.json").is_file())
        self.assertEqual([item.name for item in (stage / "src").iterdir()],
                         ["nyxion_heredity_sdk"])
        self.assertFalse((package / "__pycache__").exists())
        for name in ("README.md", "NETWORKS.md", "AEGIS.md", "LICENSE", "MANIFEST.in", "pyproject.toml"):
            self.assertTrue((stage / name).is_file())
        self.assertEqual((stage / "NETWORKS.md").read_bytes(), (ROOT / "sdk/NETWORKS.md").read_bytes())
        self.assertEqual((stage / "AEGIS.md").read_bytes(), (ROOT / "sdk/AEGIS.md").read_bytes())
        metadata = (stage / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('name = "nyxion-heredity-sdk"', metadata)
        staged_version = tomllib.loads(metadata)["project"]["version"]
        self.assertEqual(staged_version, CHECKER.SDK_VERSION)
        self.assertEqual(staged_version, "1.5.0")
        self.assertIn("dependencies = []", metadata)
        self.assertNotIn("../src", metadata)

    def test_staged_readme_relative_links_and_network_document_are_packaged(self):
        stage = PACKAGING.stage_source(self.directory / "source")
        readme = (stage / "README.md").read_text(encoding="utf-8")
        destinations = re.findall(r"\[[^\]]*\]\(([^)]+)\)", readme)
        relative = [value for value in destinations if "://" not in value and not value.startswith("#")]
        self.assertIn("NETWORKS.md", relative)
        self.assertIn("AEGIS.md", relative)
        for destination in relative:
            with self.subTest(destination=destination):
                path = destination.split("#", 1)[0]
                self.assertTrue((stage / path).resolve().is_relative_to(stage.resolve()))
                self.assertTrue((stage / path).exists())
        manifest = (stage / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertIn("NETWORKS.md", manifest.split())
        self.assertIn("AEGIS.md", manifest.split())

    def test_staging_refuses_existing_destination_without_modification(self):
        stage = self.directory / "source"
        stage.mkdir()
        marker = stage / "existing"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaises(ValueError):
            PACKAGING.stage_source(stage)
        self.assertEqual(marker.read_text(), "keep")

    def test_stage_only_does_not_start_build_or_install_dependencies(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--stage-only", "--stage",
             str(self.directory / "source")],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["self_contained"])
        self.assertFalse(report["built"])
        self.assertEqual(report["artifacts"], [])

    def test_staged_sdk_imports_and_runs_conformance_in_isolated_python(self):
        stage = PACKAGING.stage_source(self.directory / "source")
        code = (
            "import runpy,sys;"
            "baseline=set(sys.modules);"
            f"sys.path.insert(0,{str(stage / 'src')!r});"
            "from nyxion_heredity_sdk import NetworkInterfaceRequirement,"
            "NetworkInterfaceGrant,NetworkInterfaceDeclarations,NetworkInterfaceAllocator,"
            "NetworkInterfaceMultiAllocator,NetworkInterfaceLifecycle,SandboxModuleManifest,"
            "__version__,API_MAJOR,API_MINOR;"
            f"assert __version__=={CHECKER.SDK_VERSION!r};"
            "assert (API_MAJOR,API_MINOR)==(1,0);"
            "requirement=NetworkInterfaceRequirement('distribution.check',('http_server',));"
            "assert requirement.to_dict()['transport_types']==['http_server'];"
            "assert requirement.max_interfaces==16;"
            "assert hasattr(NetworkInterfaceMultiAllocator,'request_interfaces');"
            "assert hasattr(NetworkInterfaceMultiAllocator,'release_binding');"
            "assert hasattr(NetworkInterfaceLifecycle,'reapply_interface');"
            "sandbox=SandboxModuleManifest('distribution.check','Distribution check',"
            "'1.0.0','0.3.0',('bus.call',));"
            "assert SandboxModuleManifest.from_dict(sandbox.to_dict())==sandbox;"
            "assert all(name.split('.')[0] in sys.stdlib_module_names or "
            "name.split('.')[0]=='nyxion_heredity_sdk' "
            "for name in set(sys.modules)-baseline);"
            "sys.argv=['nyxion-sdk-check'];"
            "runpy.run_module('nyxion_heredity_sdk.conformance',run_name='__main__')"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", code], cwd=self.directory,
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["passed"])

    def make_wheel(self, *, extra=(), metadata=None):
        path = self.directory / "sdk.whl"
        with zipfile.ZipFile(path, "w") as wheel:
            for name in ("__init__.py", "onebot.py", "networks.py", "sandbox.py", "py.typed",
                         "schemas/plugin-manifest.v1.schema.json",
                         "schemas/contracts.v1.schema.json",
                         "schemas/sandbox-module.v1.schema.json"):
                wheel.writestr("nyxion_heredity_sdk/" + name, "")
            wheel.writestr(CHECKER.DIST_STEM + ".dist-info/METADATA",
                           metadata or f"Name: nyxion-heredity-sdk\nVersion: {CHECKER.SDK_VERSION}\n")
            for name in extra:
                wheel.writestr(name, "")
        return path

    def test_wheel_inspection_accepts_only_independent_sdk_assets(self):
        names = CHECKER.inspect_wheel(self.make_wheel())
        self.assertIn("nyxion_heredity_sdk/py.typed", names)
        self.assertIn("nyxion_heredity_sdk/networks.py", names)
        self.assertIn("nyxion_heredity_sdk/sandbox.py", names)

    def test_wheel_inspection_rejects_sdk_without_network_contracts(self):
        original = self.make_wheel()
        missing = self.directory / "sdk-without-networks.whl"
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(missing, "w") as target:
            for item in source.infolist():
                if item.filename != "nyxion_heredity_sdk/networks.py":
                    target.writestr(item, source.read(item))
        with self.assertRaises(ValueError):
            CHECKER.inspect_wheel(missing)

    def test_wheel_inspection_rejects_foreign_packages_or_runtime_dependencies(self):
        for extra in (("heredity/__init__.py",), ("vendor_adapter/__init__.py",),
                      ("nyxion_heredity_sdk/../host.py",)):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                CHECKER.inspect_wheel(self.make_wheel(extra=extra))
        with self.assertRaises(ValueError):
            CHECKER.inspect_wheel(self.make_wheel(
                metadata=f"Name: nyxion-heredity-sdk\nVersion: {CHECKER.SDK_VERSION}\nRequires-Dist: heredity\n"))

    def test_wheel_inspection_rejects_mismatched_version_metadata(self):
        with self.assertRaises(ValueError):
            CHECKER.inspect_wheel(self.make_wheel(
                metadata="Name: nyxion-heredity-sdk\nVersion: 0.0.0\n"))

    def test_host_requires_the_platform_neutral_sdk_release(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("nyxion-heredity-sdk>=1.5,<2",
                      metadata["project"]["dependencies"])


if __name__ == "__main__":
    unittest.main()
