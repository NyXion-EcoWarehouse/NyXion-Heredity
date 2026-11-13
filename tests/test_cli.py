from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import unittest
from unittest.mock import patch

from heredity.cli import build_parser, main


class CLITests(unittest.TestCase):
    def test_missing_profile_is_user_error(self):
        error = StringIO()
        with redirect_stderr(error):
            code = main(["serve", "--profile", "does-not-exist-test-fixture.json"])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", error.getvalue())

    def test_deobfuscation_command_routes_to_static_analyzer(self):
        output = StringIO()
        with patch("heredity.analysis.deobfuscate.analyze_file",
                   return_value={"static_only": True, "findings": []}) as analyze:
            with redirect_stdout(output):
                code = main(["deobfuscate", "sample.js"])
        self.assertEqual(code, 0)
        self.assertEqual(str(analyze.call_args.args[0]), "sample.js")
        self.assertTrue(json.loads(output.getvalue())["static_only"])

    def test_core_parser_has_only_generic_commands(self):
        parser = build_parser()
        for command in ("app", "serve", "webui", "plugins", "providers", "deobfuscate"):
            with self.subTest(command=command):
                self.assertIn(command, parser.format_help())
        for command in ("protocol", "discover", "scan"):
            with self.subTest(command=command), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as failure:
                    parser.parse_args([command])
                self.assertEqual(failure.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
