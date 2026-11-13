"""Build the small, deterministic Aegis example module package."""

from __future__ import annotations

import hashlib
import argparse
import json
from pathlib import Path
import sys
import tempfile
import zipfile

import wasmtime


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "examples" / "market-module"
FILENAME = "nyxion-example-module-1.0.0.zip"
DOWNLOAD_URL = (
    "https://github.com/NyXion-EcoWarehouse/Nyxion-Example-Plugin/"
    "releases/download/v1.0.0/" + FILENAME)


def build(output, download_url=DOWNLOAD_URL):
    sys.path.insert(0, str(ROOT / "src"))
    from heredity.aegis import SandboxedModule
    from heredity.marketplace import validate_catalog
    from nyxion_heredity_sdk import SandboxModuleManifest

    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    package = output / FILENAME
    if package.exists():
        raise ValueError("Example package already exists; choose a new output directory.")
    manifest = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    SandboxModuleManifest.from_dict(manifest)
    module = wasmtime.wat2wasm((SOURCE / "module.wat").read_text(encoding="utf-8"))
    wasm_path = output / "module.wasm"
    wasm_path.write_bytes(module)
    with tempfile.TemporaryDirectory(prefix="aegis-example-") as state:
        worker = SandboxedModule(manifest, wasm_path, [], Path(state))
        try:
            worker.start()
            status = worker.call("get_status")
            if (status.get("status") != "ok" or status.get("example") is not True
                    or status.get("module_id") != manifest["id"]):
                raise RuntimeError("The example did not complete the actual Aegis handshake.")
        finally:
            worker.stop()
    temporary = package.with_suffix(".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in (
                ("manifest.json", (json.dumps(manifest, ensure_ascii=True, indent=2) + "\n").encode()),
                ("module.wasm", module)):
            info = zipfile.ZipInfo(name, date_time=(2026, 11, 13, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, raw)
    temporary.replace(package)
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    entry = {
        "id": manifest["id"], "name": manifest["name"],
        "description": "Minimal Aegis placeholder. No network, file or platform access.",
        "version": manifest["version"], "author": {
            "name": "NyXion", "github": "https://github.com/NyXion-EcoWarehouse"},
        "permissions": [], "download_url": download_url, "hash_sha256": digest,
        "status": "active", "security_advisory": "",
        "min_heredity_version": manifest["min_heredity_version"],
        "license": "MIT", "tags": ["official", "example"],
    }
    catalog = {"registry_version": "1.0.0", "plugins": [entry]}
    accepted, invalid = validate_catalog(json.dumps(catalog).encode())
    if len(accepted) != 1 or invalid:
        raise RuntimeError("Generated example catalog is invalid.")
    for name, raw in (
            ("manifest.json", json.dumps(manifest, ensure_ascii=True, indent=2) + "\n"),
            ("plugins.json", json.dumps(catalog, ensure_ascii=True, indent=2) + "\n"),
            ("SHA256SUMS.txt", f"{digest}  {FILENAME}\n"),
            ("README.md", (SOURCE / "README.md").read_text(encoding="utf-8"))):
        (output / name).write_text(raw, encoding="utf-8")
    report = {
        "package": str(package), "sha256": digest,
        "module_sha256": hashlib.sha256(module).hexdigest(),
        "permissions": [], "aegis_verified": True,
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "example-module")
    parser.add_argument("--download-url", default=DOWNLOAD_URL)
    args = parser.parse_args()
    print(json.dumps(build(args.output, args.download_url), ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
