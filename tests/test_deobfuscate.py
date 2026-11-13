import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from heredity.analysis import analyze_bytes, analyze_file
from heredity.analysis import deobfuscate


class DeobfuscationTests(unittest.TestCase):
    def origins(self, report):
        return {finding["origin"] for finding in report["findings"]}

    def test_plain_origins_only_and_source_offset(self):
        payload = b'const url = "http://127.0.0.1:5700/private?token=never-print";'
        result = analyze_bytes(payload, r"C:\private\artifact.js?token=never-print")
        self.assertIn("http://127.0.0.1:5700", self.origins(result))
        raw = next(
            finding for finding in result["findings"]
            if finding["decode_chain"] == ["raw_ascii"]
        )
        self.assertEqual(raw["byte_offset"], payload.index(b"http"))
        self.assertEqual(result["source"], "artifact.js")
        self.assertEqual(result["source_sha256"], hashlib.sha256(payload).hexdigest())
        self.assertNotIn("never-print", json.dumps(result))
        self.assertNotIn("/private", json.dumps(result))
        self.assertFalse(result["control_api_verified"])
        self.assertTrue(result["static_only"])

    def test_javascript_and_json_literal_escapes(self):
        payload = (
            rb'const a="http\x3a\x2f\x2f127.0.0.1\x3a5701/path";'
            rb'const b="ws\u003a\u002f\u002flocalhost\u003a5702/path";'
            rb'const c="https\u{3a}\u{2f}\u{2f}[::1]\u{3a}5703/path";'
            rb'const d="http:\/\/127.0.0.1:5704/path";'
        )
        result = analyze_bytes(payload)
        self.assertEqual(self.origins(result), {
            "http://127.0.0.1:5701", "ws://localhost:5702",
            "https://[::1]:5703", "http://127.0.0.1:5704",
        })
        self.assertTrue(all(
            "literal_escapes" in finding["decode_chain"]
            for finding in result["findings"]
        ))

    def test_percent_encoded_strings(self):
        result = analyze_bytes(
            b'"http%3A%2F%2F127.0.0.1%3A5705%2Fpath%3Ftoken%3Dprivate"'
        )
        self.assertIn("http://127.0.0.1:5705", self.origins(result))
        self.assertTrue(any(
            "percent" in finding["decode_chain"] for finding in result["findings"]
        ))
        self.assertNotIn("private", json.dumps(result))

    def test_json_surrogate_pair_and_invalid_escape(self):
        result = analyze_bytes(
            rb'"\uD83D\uDE00 http\u003a\u002f\u002flocalhost\u003a5713/path"'
        )
        self.assertIn("http://localhost:5713", self.origins(result))
        invalid = analyze_bytes(
            rb'"\uD83D http\u003a\u002f\u002flocalhost\u003a5713/path"'
        )
        self.assertEqual(invalid["findings"], [])

    def test_strict_whole_base64_and_hex_literals(self):
        base = base64.b64encode(b"http://localhost:5706/path?token=never")
        hexadecimal = b"ws://127.0.0.1:5707/path".hex().encode("ascii")
        result = analyze_bytes(b'"' + base + b'"; "' + hexadecimal + b'";')
        self.assertEqual(self.origins(result), {
            "http://localhost:5706", "ws://127.0.0.1:5707",
        })
        self.assertTrue(any(
            "base64" in finding["decode_chain"] for finding in result["findings"]
        ))
        self.assertTrue(any(
            "hex" in finding["decode_chain"] for finding in result["findings"]
        ))
        self.assertNotIn("never", json.dumps(result))
        invalid = analyze_bytes(b'"prefix-' + base + b'-suffix"')
        self.assertEqual(invalid["findings"], [])

    def test_utf16_little_and_big_endian(self):
        for encoding in ("utf-16le", "utf-16be"):
            with self.subTest(encoding=encoding):
                data = "http://127.0.0.1:5708/private".encode(encoding)
                result = analyze_bytes(data)
                self.assertIn("http://127.0.0.1:5708", self.origins(result))
                self.assertTrue(any(
                    encoding in "/".join(finding["decode_chain"])
                    for finding in result["findings"]
                ))
        sparse = b"\xfe\xba" * 1000 + "ws://localhost:5709/path".encode("utf-16le")
        self.assertIn("ws://localhost:5709", self.origins(analyze_bytes(sparse)))

    def test_two_layers_and_depth_bound(self):
        source = b"http%3A%2F%2Flocalhost%3A5710%2Fprivate"
        encoded = base64.b64encode(source)
        report = analyze_bytes(b'"' + encoded + b'"')
        self.assertIn("http://localhost:5710", self.origins(report))
        self.assertTrue(any(
            finding["decode_chain"][-2:] == ["base64", "percent"]
            for finding in report["findings"]
        ))
        for _ in range(2):
            encoded = base64.b64encode(encoded)
        limited = analyze_bytes(b'"' + encoded + b'"')
        self.assertEqual(limited["findings"], [])
        self.assertTrue(limited["truncated"])
        self.assertIn("depth", limited["limits_hit"])

    def test_raw_chunk_boundary_cannot_truncate_port(self):
        prefix = b"http://127.0.0.1:5700"
        data = b" " * (deobfuscate.MAX_STRING_BYTES - len(prefix))
        data += prefix + b"1/private"
        report = analyze_bytes(data)
        self.assertEqual(self.origins(report), {"http://127.0.0.1:57001"})

    def test_raw_offset_does_not_point_to_longer_port_prefix(self):
        data = b"http://127.0.0.1:57001/one http://127.0.0.1:5700/two"
        report = analyze_bytes(data)
        finding = next(
            item for item in report["findings"]
            if item["origin"] == "http://127.0.0.1:5700"
            and item["decode_chain"] == ["raw_ascii"]
        )
        self.assertEqual(finding["byte_offset"], data.rindex(b"http"))

    def test_artifact_magic_and_unknown_binary(self):
        self.assertEqual(analyze_bytes(b"\x7fELF" + b"\0" * 64)["artifact_kind"], "elf")
        self.assertEqual(analyze_bytes(b"dex\n035\0" + b"\0" * 64)["artifact_kind"], "dex")
        pe = bytearray(132)
        pe[:2] = b"MZ"
        struct.pack_into("<I", pe, 60, 128)
        pe[128:132] = b"PE\0\0"
        self.assertEqual(analyze_bytes(bytes(pe))["artifact_kind"], "pe")
        self.assertEqual(
            analyze_bytes(b"\0\xfe\xab\x12", source_name="not_really.js")["artifact_kind"],
            "unknown_binary",
        )
        self.assertNotEqual(analyze_bytes(b"MZ" + b"\0" * 64)["artifact_kind"], "pe")

    def test_validated_asar_header_not_filename(self):
        header = b'{"files":{}}'
        payload_size = 4 + len(header)
        header_size = ((payload_size + 3) // 4) * 4 + 4
        data = struct.pack("<IIII", 4, header_size, payload_size, len(header)) + header
        data += b"\0" * (8 + header_size - len(data))
        self.assertEqual(analyze_bytes(data)["artifact_kind"], "asar")
        self.assertNotEqual(
            analyze_bytes(b"not an archive", "fake.asar")["artifact_kind"], "asar"
        )

    def test_arbitrary_code_is_never_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "must-not-exist"
            payload = (
                f'__import__("pathlib").Path({str(marker)!r}).write_text("executed");'
                'const x = "http\\x3a\\x2f\\x2flocalhost\\x3a5711/path";'
            ).encode("utf-8")
            report = analyze_bytes(payload)
            self.assertFalse(marker.exists())
            self.assertIn("http://localhost:5711", self.origins(report))

    def test_file_input_and_size_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.js"
            path.write_bytes(b'"http%3A%2F%2Flocalhost%3A5712%2Fpath"')
            result = analyze_file(path)
            self.assertEqual(result["source"], "fixture.js")
            self.assertIn("http://localhost:5712", self.origins(result))
            with self.assertRaises(ValueError):
                analyze_file(Path(directory))
            with patch.object(deobfuscate, "MAX_FILE_BYTES", 8):
                with self.assertRaises(ValueError):
                    analyze_file(path)
                with self.assertRaises(ValueError):
                    analyze_bytes(b"012345678")

    def test_work_candidate_and_string_budgets(self):
        with patch.object(deobfuscate, "MAX_WORK_UNITS", 3):
            report = analyze_bytes(b'"aHR0cDovL2xvY2FsaG9zdDo1NzAwLw=="' * 20)
            self.assertTrue(report["truncated"])
            self.assertLessEqual(report["work_units"], 3)
        with patch.object(deobfuscate, "MAX_CANDIDATES", 2):
            report = analyze_bytes(b'"nothing";' * 20)
            self.assertTrue(report["truncated"])
            self.assertLessEqual(report["candidates_analyzed"], 2)
        report = analyze_bytes(b'"' + b"A" * (deobfuscate.MAX_STRING_BYTES + 1) + b'"')
        self.assertTrue(report["truncated"])
        self.assertIn("string_bytes", report["limits_hit"])

    @unittest.skipIf(os.name == "nt", "POSIX special-file test")
    def test_named_pipe_is_rejected_without_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pipe"
            os.mkfifo(path)
            with self.assertRaises(ValueError):
                analyze_file(path)


if __name__ == "__main__":
    unittest.main()
