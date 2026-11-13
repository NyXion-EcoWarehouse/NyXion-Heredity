import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ProviderCLITests(unittest.TestCase):
    def test_catalog_is_utf8_and_does_not_activate_a_provider(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "run.py"), "providers", "list",
             "--registry", str(ROOT / "examples/providers.demo.json")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["inspection_only"])
        self.assertIsNone(payload["state"]["active_provider_id"])
        self.assertEqual(payload["levels"][0]["label"], "界面驱动")
        demos = [item for item in payload["state"]["providers"] if item["source_kind"] == "demo"]
        self.assertEqual(len(demos), 2)
        self.assertTrue(all(item["execution_level"] is None for item in demos))

    def test_unavailable_initial_native_provider_fails_without_server_start(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "run.py"), "serve", "--demo", "--memory-state",
             "--port", "0", "--provider", "l1-attached"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=8)
        self.assertEqual(result.returncode, 1)
        self.assertIn("unavailable", result.stderr.lower())
        self.assertNotIn("HTTP on", result.stdout)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
