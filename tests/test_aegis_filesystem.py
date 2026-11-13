"""The capability broker confines file operations to regular private state."""

import base64
import os
from pathlib import Path
import tempfile
import unittest

from heredity.aegis import CapabilityBroker, PermissionViolation


class AegisFilesystemTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="NyXion Aegis filesystem ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.broker = CapabilityBroker(("fs.read", "fs.write"), self.root / "state")
        self.addCleanup(self.broker.close)

    def read(self, path):
        return self.broker.dispatch("fs.read", "fs.read", {"path": path})

    def write(self, path, raw=b"inside"):
        return self.broker.dispatch("fs.write", "fs.write", {
            "path": path, "data_base64": base64.b64encode(raw).decode()})

    def test_allowed_relative_read_and_write_round_trip(self):
        self.assertEqual(self.write("nested/file.txt"), {"bytes": 6})
        self.assertEqual(base64.b64decode(self.read("nested/file.txt")["data_base64"]), b"inside")

    def test_ambiguous_absolute_traversal_and_stream_paths_are_refused(self):
        for path in ("../outside", "/outside", "C:/outside", "file:secret",
                     "a\\b", "a//b", "./a", "a/../b", "a/./b", "", "a\0b"):
            with self.subTest(path=path), self.assertRaises(PermissionViolation):
                self.write(path)
        self.assertEqual(list((self.root / "state").iterdir()), [])

    def test_hard_link_cannot_be_read_or_truncated_even_with_capabilities(self):
        outside = self.root / "outside.txt"
        outside.write_bytes(b"keep outside")
        os.link(outside, self.root / "state" / "linked.txt")
        for operation in (lambda: self.read("linked.txt"), lambda: self.write("linked.txt")):
            with self.assertRaises(PermissionViolation):
                operation()
        self.assertEqual(outside.read_bytes(), b"keep outside")

    def test_directory_is_not_opened_as_a_regular_file(self):
        (self.root / "state" / "directory").mkdir()
        for operation in (lambda: self.read("directory"), lambda: self.write("directory")):
            with self.assertRaises((PermissionViolation, OSError)):
                operation()

    def test_symlink_and_linked_parent_are_refused_when_supported(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"outside")
        link = self.root / "state" / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest("Local OS does not grant symlink creation: " + type(error).__name__)
        for operation in (lambda: self.read("linked/secret.txt"),
                          lambda: self.write("linked/new.txt")):
            with self.assertRaises(PermissionViolation):
                operation()
        self.assertFalse((outside / "new.txt").exists())

    def test_oversized_file_and_write_payload_are_refused(self):
        (self.root / "state" / "large.bin").write_bytes(b"a" * 65537)
        for operation in (lambda: self.read("large.bin"),
                          lambda: self.write("new.bin", b"b" * 65537)):
            with self.assertRaises(PermissionViolation):
                operation()
        self.assertFalse((self.root / "state" / "new.bin").exists())

    def test_undeclared_operation_and_after_close_access_are_refused(self):
        for capability, operation, params in (
                ("fs.write", "fs.read", {"path": "file"}),
                ("fs.read", "fs.read", {"path": "file", "extra": True}),
                ("net.connect", "tcp.connect", {"host": "127.0.0.1", "port": 80})):
            with self.subTest(capability=capability), self.assertRaises(PermissionViolation):
                self.broker.dispatch(capability, operation, params)
        self.broker.close()
        with self.assertRaises(PermissionViolation):
            self.write("after-close")


if __name__ == "__main__":
    unittest.main()
