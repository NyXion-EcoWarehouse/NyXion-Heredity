"""Catalog confirmation and package checks never execute downloaded Python."""

import hashlib
import ipaddress
import io
import json
from pathlib import Path
import stat
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib import error
from urllib.request import Request
import zipfile

from heredity.marketplace import (
    HASH_FAILURE, INDEX_URL, MAX_PACKAGE_BYTES, Marketplace, MarketplaceError,
    _PublicHTTPSHandler, _url, validate_catalog,
)


WASM = b"\x00asm\x01\x00\x00\x00\x00\x01\x00"


def manifest(**changes):
    return {
        "schema_version": 1, "id": "example.wasm", "name": "Local WASM fixture",
        "version": "1.0.0", "sdk_api": {"major": 1, "minor": 0},
        "min_heredity_version": "0.3.0", "permissions": ["bus.call"],
        "module": "module.wasm",
    } | changes


def package(*, declaration=None, module=WASM, extra=()):
    value = io.BytesIO()
    with zipfile.ZipFile(value, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(declaration or manifest()))
        archive.writestr("module.wasm", module)
        for name, data in extra:
            archive.writestr(name, data)
    return value.getvalue()


def entry(raw=None, **changes):
    raw = package() if raw is None else raw
    return {
        "id": "example.wasm", "name": "Local WASM fixture", "description": "Offline package",
        "version": "1.0.0", "author": {"name": "Local fixture"},
        "permissions": ["bus.call"], "download_url": "https://packages.example/plugin.zip",
        "hash_sha256": hashlib.sha256(raw).hexdigest(), "status": "active",
        "security_advisory": "", "min_heredity_version": "0.3.0",
    } | changes


class Response(io.BytesIO):
    def __init__(self, raw, *, length=None):
        super().__init__(raw)
        self.headers = {"Content-Length": str(len(raw) if length is None else length)}


class LocalTransport:
    def __init__(self):
        self.values, self.calls = {}, []

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        value = self.values[url]
        if callable(value):
            value = value()
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, Response) else Response(value)


class MarketplaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="NyXion verified market ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.transport = LocalTransport()
        self.market = Marketplace(self.root, "0.3.0", auto_refresh=False,
                                  _open_url=self.transport, timeout=1)
        self.addCleanup(self.market.close)

    def terminal(self, job):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = self.market.job(job["job_id"])
            if current["state"] not in {"queued", "running"}:
                return current
            threading.Event().wait(0.005)
        self.fail("The offline marketplace job did not complete.")

    def refresh(self, entries):
        self.transport.values[INDEX_URL] = json.dumps({"plugins": entries}).encode()
        result = self.terminal(self.market.refresh())
        self.assertEqual(result["state"], "completed", result)
        return result

    def prepare(self, raw=None, **changes):
        raw = package() if raw is None else raw
        source = entry(raw, **changes)
        self.transport.values[source["download_url"]] = raw
        self.refresh([source])
        return source

    def install(self, source, **changes):
        return self.market.install(source["id"], **{
            "expected_version": source["version"], "expected_sha256": source["hash_sha256"],
            "confirmed_permissions": source["permissions"], "force_warning": False,
        } | changes)

    def test_real_index_shape_keeps_invalid_placeholder_entry_visible_without_fabricating_an_id(self):
        source = entry()
        source.pop("id")
        source.update(download_url="https://github.com/.../plugin.wasm",
                      license="MIT", repo_url="https://github.com/example/plugin",
                      tags=["official", "example"])
        result = self.refresh([source])
        snapshot = self.market.snapshot()
        self.assertEqual(result["result"], {
            "accepted": 0, "rejected": 1, "fetched_at": snapshot["last_success_at"]})
        self.assertEqual(snapshot["plugins"], [])
        self.assertEqual(snapshot["invalid_entries"][0]["position"], 1)
        self.assertEqual(snapshot["invalid_entries"][0]["name"], source["name"])
        self.assertEqual(snapshot["error"]["code"], "INDEX_ENTRY_INVALID")
        self.assertFalse(snapshot["stale"])

    def test_catalog_rejects_duplicate_ids_unknown_permissions_invalid_json_and_unhashable_status(self):
        source = entry()
        accepted, rejected = validate_catalog(json.dumps({"plugins": [
            source, source, entry(status=[]), entry(permissions=["os.exec"])]}).encode())
        self.assertEqual(len(accepted), 0)
        self.assertEqual(len(rejected), 4)
        with self.assertRaises(MarketplaceError):
            validate_catalog(b'{"plugins":[],"plugins":[]}')
        with self.assertRaises(MarketplaceError):
            validate_catalog(b'{"plugins":[],"registry_version":[]}')

    def test_refresh_is_async_coalesced_and_network_failure_keeps_last_good_cache(self):
        source = self.prepare()
        entered, release = threading.Event(), threading.Event()
        def delayed():
            entered.set()
            if not release.wait(2):
                raise AssertionError("The local fetch barrier was not released.")
            raise OSError("private url query and credentials must not appear")
        self.transport.values[INDEX_URL] = delayed
        try:
            started = time.monotonic()
            first = self.market.refresh()
            self.assertLess(time.monotonic() - started, 0.1)
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.market.refresh()["job_id"], first["job_id"])
            self.assertEqual(self.market.snapshot()["plugins"][0]["id"], source["id"])
        finally:
            release.set()
        self.assertEqual(self.terminal(first)["state"], "failed")
        snapshot = self.market.snapshot()
        self.assertTrue(snapshot["stale"])
        self.assertEqual(snapshot["plugins"][0]["id"], source["id"])
        self.assertNotIn("private url", json.dumps(snapshot))
        self.market.close()
        restored = Marketplace(self.root, "0.3.0", auto_refresh=False, _open_url=self.transport)
        self.addCleanup(restored.close)
        self.assertTrue(restored.snapshot()["stale"])
        self.assertEqual(restored.snapshot()["plugins"][0]["id"], source["id"])
        with self.assertRaises(MarketplaceError) as failure:
            restored.install(source["id"], expected_version=source["version"],
                             expected_sha256=source["hash_sha256"],
                             confirmed_permissions=source["permissions"])
        self.assertEqual(failure.exception.code, "MARKET_CATALOG_STALE")

    def test_install_requires_exact_current_version_hash_and_permission_confirmation(self):
        source = self.prepare()
        for change in ({"expected_version": "2.0.0"},
                       {"expected_sha256": "0" * 64},
                       {"confirmed_permissions": []},
                       {"confirmed_permissions": ["fs.read"]},
                       {"confirmed_permissions": ["bus.call", "bus.call"]}):
            with self.subTest(change=change), self.assertRaises(MarketplaceError):
                self.install(source, **change)
        self.assertEqual([url for url, _ in self.transport.calls], [INDEX_URL])

    def test_banned_never_downloads_warning_requires_explicit_force_and_host_version_is_checked(self):
        banned = self.prepare(status="banned")
        with self.assertRaises(MarketplaceError) as failure:
            self.install(banned, force_warning=True)
        self.assertEqual(failure.exception.code, "MARKET_PLUGIN_BANNED")
        warning = self.prepare(status="warning", security_advisory="Review before use")
        with self.assertRaises(MarketplaceError) as failure:
            self.install(warning)
        self.assertEqual(failure.exception.code, "MARKET_WARNING_CONFIRMATION")
        result = self.terminal(self.install(warning, force_warning=True))
        self.assertEqual(result["state"], "completed", result)
        newer = self.prepare(min_heredity_version="1.0.0")
        with self.assertRaises(MarketplaceError) as failure:
            self.install(newer)
        self.assertEqual(failure.exception.code, "HOST_VERSION_TOO_OLD")

    def test_verified_zip_installs_exact_files_and_passes_only_local_verified_record_to_callback(self):
        raw = package()
        source = self.prepare(raw)
        received = []
        self.market.on_install = lambda record: (received.append(record) or {"started": True})
        result = self.terminal(self.install(source))
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(result["result"]["state"], "installed")
        self.assertTrue(result["result"]["started"])
        self.assertEqual(len(received), 1)
        record = received[0]
        self.assertEqual(record["approved_permissions"], ["bus.call"])
        self.assertEqual(Path(record["module_path"]).read_bytes(), WASM)
        self.assertEqual(Path(record["package_path"]).read_bytes(), raw)
        self.assertEqual(record["manifest"], manifest())
        self.assertEqual(self.market.installed_records()[0]["sha256"], source["hash_sha256"])
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertNotIn(str(self.root), json.dumps(self.market.snapshot()))
        self.assertEqual(self.market.snapshot()["plugins"][0]["installed"]["version"], "1.0.0")

    def test_raw_wasm_uses_confirmed_catalog_manifest_without_claiming_embedded_identity(self):
        source = self.prepare(WASM, download_url="https://packages.example/plugin.wasm")
        result = self.terminal(self.install(source))
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(self.market.installed_records()[0]["manifest"], manifest())

    def test_hash_mismatch_uses_required_warning_deletes_all_temporary_files_and_never_loads(self):
        source = self.prepare()
        self.transport.values[source["download_url"]] = b"not the approved artifact"
        self.market.on_install = lambda _: self.fail("A mismatched artifact was loaded.")
        result = self.terminal(self.install(source))
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"], {"code": "PLUGIN_HASH_MISMATCH", "message": HASH_FAILURE})
        self.assertEqual(self.market.installed_records(), ())
        self.assertEqual(list(self.market.root.glob(".download-*")), [])
        self.assertEqual(list(self.market.plugins_dir.glob(".install-*")), [])

    def test_malicious_zip_paths_duplicate_entries_symlinks_bombs_and_manifest_mismatch_fail_closed(self):
        examples = [
            package(extra=[("../outside.py", b"bad")]),
            package(extra=[("module.wasm", WASM)]),
            package(declaration=manifest(id="other.module")),
            package(declaration=manifest(permissions=["fs.read"])),
            package(declaration=manifest(sdk_api={"major": 2, "minor": 0})),
            package(module=b"not wasm"),
            package(module=b"\0asm\1\0\0\0" + b"a" * (1024 * 1024)),
        ]
        linked = io.BytesIO()
        with zipfile.ZipFile(linked, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest()))
            info = zipfile.ZipInfo("module.wasm")
            info.create_system, info.external_attr = 3, (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "outside")
        examples.append(linked.getvalue())
        for raw in examples:
            with self.subTest(digest=hashlib.sha256(raw).hexdigest()):
                source = self.prepare(raw)
                result = self.terminal(self.install(source))
                self.assertEqual(result["state"], "failed", result)
                self.assertEqual(self.market.installed_records(), ())
                self.assertFalse((self.root / "outside.py").exists())

    def test_start_failure_is_installed_not_started_and_can_be_reloaded_from_verified_registry(self):
        source = self.prepare()
        def failing(_):
            raise RuntimeError("private exception detail")
        self.market.on_install = failing
        result = self.terminal(self.install(source))
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["result"]["state"], "installed_not_started")
        self.assertFalse(result["result"]["started"])
        self.assertNotIn("private exception", json.dumps(result))
        self.market.close()
        restored = Marketplace(self.root, "0.3.0", auto_refresh=False, _open_url=self.transport)
        self.addCleanup(restored.close)
        self.assertEqual(restored.installed_records()[0]["plugin_id"], source["id"])

    def test_reload_rehashes_both_package_and_module_and_banned_catalog_prevents_loading(self):
        source = self.prepare()
        self.terminal(self.install(source))
        record = self.market.installed_records()[0]
        module = Path(record["module_path"])
        module.write_bytes(WASM + b"tampered")
        self.assertEqual(self.market.installed_records(), ())
        self.assertEqual(self.market.snapshot()["installed_errors"][0]["code"], "PLUGIN_HASH_MISMATCH")
        module.write_bytes(WASM)
        self.refresh([source | {"status": "banned"}])
        self.assertEqual(self.market.installed_records(), ())
        self.assertEqual(self.market.snapshot()["installed_errors"][0]["code"], "MARKET_PLUGIN_BANNED")

    def test_private_destinations_credentials_and_https_downgrades_are_rejected(self):
        for url in (
                "http://example.com/plugin.wasm", "https://127.0.0.1/plugin.wasm",
                "https://[::1]/plugin.wasm", "https://169.254.169.254/latest/meta-data",
                "https://10.0.0.1/plugin.zip", "https://localhost/plugin.zip",
                "https://user:pass@example.com/plugin.zip",
                "https://github.com/.../plugin.wasm"):
            with self.subTest(url=url), self.assertRaises(MarketplaceError):
                _url(url)
        handler = _PublicHTTPSHandler()
        request = __import__("urllib.request", fromlist=["Request"]).Request(
            "https://public.example/plugin.zip")
        answer = [(2, 1, 6, "", ("127.0.0.1", 443))]
        with patch("socket.getaddrinfo", return_value=answer), \
                patch("socket.create_connection", side_effect=AssertionError("private connection")):
            with self.assertRaises(MarketplaceError) as failure:
                handler.https_open(request)
        self.assertEqual(failure.exception.code, "MARKET_DESTINATION_REJECTED")

    def test_only_the_exact_fixed_index_transport_accepts_benchmark_fake_dns_with_tls_validation(self):
        handler = _PublicHTTPSHandler(context=ssl.create_default_context(), allow_fixed_index=True)
        fake_dns = [(2, 1, 6, "", ("198.18.0.6", 443))]
        observed = []

        def connect(factory, req, **options):
            connection = factory("raw.githubusercontent.com", timeout=1, **options)
            self.assertEqual(connection.host, "raw.githubusercontent.com")
            self.assertEqual(connection._addresses, ("198.18.0.6",))
            self.assertTrue(connection._context.check_hostname)
            self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
            observed.append(req.full_url)
            return Response(b'{"plugins":[]}')

        with patch("socket.getaddrinfo", return_value=fake_dns), \
                patch.object(handler, "do_open", side_effect=connect):
            handler.https_open(Request(INDEX_URL))
            for other in (
                    INDEX_URL + "?token=anything", INDEX_URL + "/other",
                    INDEX_URL.replace("/main/plugins.json", "/other/plugins.json"),
                    INDEX_URL.replace("raw.githubusercontent.com", "other.example")):
                with self.subTest(other=other), self.assertRaises(MarketplaceError):
                    handler.https_open(Request(other))
            with self.assertRaises(MarketplaceError):
                _PublicHTTPSHandler().https_open(Request(INDEX_URL))
        self.assertEqual(observed, [INDEX_URL])
        for private in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"):
            family = 10 if ipaddress.ip_address(private).version == 6 else 2
            with patch("socket.getaddrinfo", return_value=[(family, 1, 6, "", (private, 443))]):
                with self.subTest(private=private), self.assertRaises(MarketplaceError):
                    handler.https_open(Request(INDEX_URL))

    def test_fixed_index_rejects_redirects_and_cannot_be_used_for_arbitrary_package_downloads(self):
        self.transport.values[INDEX_URL] = error.HTTPError(
            INDEX_URL, 302, "redirect", {"Location": "https://public.example/index"}, None)
        result = self.terminal(self.market.refresh())
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"]["code"], "MARKET_INDEX_REDIRECT_REJECTED")
        destination = self.market.root / "not-downloaded.tmp"
        with self.assertRaises(MarketplaceError):
            self.market._download(
                "https://other.example/index", destination, 1024, fixed_index=True)
        self.assertFalse(destination.exists())

    def test_redirects_are_revalidated_signed_query_is_not_exposed_and_download_size_is_bounded(self):
        signed = "https://packages.example/plugin.zip?signature=private-signed-value"
        source = self.prepare(download_url=signed)
        self.assertNotIn("private-signed-value", json.dumps(self.market.snapshot()))
        self.transport.values[signed] = error.HTTPError(
            signed, 302, "redirect", {"Location": "https://127.0.0.1/private"}, None)
        result = self.terminal(self.install(source))
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"]["code"], "INVALID_MARKET_URL")
        self.transport.values[signed] = Response(b"", length=MAX_PACKAGE_BYTES + 1)
        result = self.terminal(self.install(source))
        self.assertEqual(result["error"]["code"], "MARKET_FILE_TOO_LARGE")
        self.assertEqual(list(self.market.root.glob(".download-*")), [])

    def test_forged_local_registry_cannot_escape_the_market_plugin_directory(self):
        self.market.close()
        registry = self.market.root / "installed.json"
        registry.write_text(json.dumps({"schema_version": 1, "plugins": {
            "../outside": {"plugin_id": "../outside"}}}), encoding="utf-8")
        restored = Marketplace(self.root, "0.3.0", auto_refresh=False)
        self.addCleanup(restored.close)
        self.assertEqual(restored.installed_records(), ())
        self.assertEqual(restored.snapshot()["installed_errors"][0]["code"], "MARKET_APPROVAL_INVALID")

    def test_forged_registry_permissions_or_module_hash_cannot_expand_approved_capabilities(self):
        source = self.prepare()
        self.terminal(self.install(source))
        self.market.close()
        registry = self.market.root / "installed.json"
        persisted = json.loads(registry.read_bytes())
        record = persisted["plugins"][source["id"]]
        record["approved_permissions"] = ["fs.read"]
        record["manifest"]["permissions"] = ["fs.read"]
        registry.write_text(json.dumps(persisted), encoding="utf-8")
        restored = Marketplace(self.root, "0.3.0", auto_refresh=False, _open_url=self.transport)
        self.addCleanup(restored.close)
        self.assertEqual(restored.installed_records(), ())
        self.assertEqual(restored.snapshot()["installed_errors"][0]["code"], "MARKET_APPROVAL_INVALID")
        self.assertNotIn(self.market._approval_key.hex(), json.dumps(restored.snapshot()))

    def test_original_package_binds_the_extracted_module_and_manifest_even_with_a_valid_receipt(self):
        source = self.prepare()
        self.terminal(self.install(source))
        record = self.market._installed[source["id"]]
        directory = self.market.plugins_dir / record["relative_path"]
        altered_module = WASM + b"altered"
        (directory / "module.wasm").write_bytes(altered_module)
        record["module_sha256"] = hashlib.sha256(altered_module).hexdigest()
        record["approval_mac"] = self.market._approval_mac(record)
        self.assertEqual(self.market.installed_records(), ())
        self.assertEqual(self.market.snapshot()["installed_errors"][0]["code"], "PLUGIN_HASH_MISMATCH")

    def test_duplicate_banned_identity_stays_blocked_for_existing_install_after_cache_restart(self):
        source = self.prepare()
        self.terminal(self.install(source))
        self.refresh([source, source | {"status": "banned"}])
        self.assertEqual(self.market.installed_records(), ())
        self.assertEqual(self.market.snapshot()["installed_errors"][0]["code"], "MARKET_PLUGIN_BANNED")
        self.market.close()
        restored = Marketplace(self.root, "0.3.0", auto_refresh=False, _open_url=self.transport)
        self.addCleanup(restored.close)
        self.assertEqual(restored.installed_records(), ())
        self.assertEqual(len(restored.snapshot()["invalid_entries"]), 2)
        self.assertEqual(restored.snapshot()["installed_errors"][0]["code"], "MARKET_PLUGIN_BANNED")

    def test_one_bad_installed_record_does_not_prevent_loading_another_verified_plugin(self):
        first = self.prepare()
        self.terminal(self.install(first))
        raw = package(declaration=manifest(id="second.wasm"))
        second = self.prepare(raw, id="second.wasm")
        self.terminal(self.install(second))
        self.market.close()
        registry = self.market.root / "installed.json"
        data = json.loads(registry.read_bytes())
        data["plugins"][first["id"]]["approved_permissions"] = ["fs.write"]
        registry.write_text(json.dumps(data), encoding="utf-8")
        restored = Marketplace(self.root, "0.3.0", auto_refresh=False, _open_url=self.transport)
        self.addCleanup(restored.close)
        records = restored.installed_records()
        self.assertEqual([record["plugin_id"] for record in records], [second["id"]])
        self.assertEqual(restored.installed_record(second["id"])["plugin_id"], second["id"])
        self.assertEqual(restored.snapshot()["installed_errors"][0]["plugin_id"], first["id"])
        with self.assertRaises(MarketplaceError):
            restored.installed_record(first["id"])

    def test_refresh_callback_runs_outside_lock_and_single_record_restart_checks_current_ban(self):
        source = self.prepare()
        self.terminal(self.install(source))
        checked = []
        def enforce():
            checked.append(self.market.snapshot()["stale"])
            with self.assertRaises(MarketplaceError) as failure:
                self.market.installed_record(source["id"])
            self.assertEqual(failure.exception.code, "MARKET_PLUGIN_BANNED")
        self.market.on_refresh = enforce
        self.refresh([source | {"status": "banned"}])
        self.assertEqual(checked, [False])
        self.market.on_refresh = lambda: (_ for _ in ()).throw(RuntimeError("private"))
        job = self.refresh([source | {"status": "banned"}])
        self.assertEqual(job["state"], "completed")
        self.assertFalse(self.market.snapshot()["stale"])
        self.assertEqual(self.market.snapshot()["error"]["code"], "MARKET_ENFORCEMENT_FAILED")

    def test_job_queue_is_bounded_and_close_cancels_pending_jobs(self):
        entered, release = threading.Event(), threading.Event()
        def delayed():
            entered.set()
            release.wait(1)
            return b'{"plugins":[]}'
        self.transport.values[INDEX_URL] = delayed
        first = self.market.refresh()
        self.assertTrue(entered.wait(1))
        for number in range(32):
            self.market._submit("install", "queued." + str(number), {})
        with self.assertRaises(MarketplaceError) as failure:
            self.market._submit("install", "queue.full", {})
        self.assertEqual(failure.exception.code, "MARKETPLACE_QUEUE_FULL")
        release.set()
        self.market.close()
        self.assertFalse(self.market._worker.is_alive())
        self.assertEqual(self.market.job(first["job_id"])["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
