"""Create allowlisted core, SDK and generic example release artifacts."""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DATE = (2026, 11, 13, 0, 0, 0)
PRIVATE_PARTS = {".env", ".heredity", ".venv", ".git", "__pycache__", "node_modules"}
PRIVATE_SUFFIXES = {
    ".pyc", ".key", ".sqlite", ".sqlite3", ".db", ".log", ".node",
    ".dll", ".so", ".dylib", ".exe", ".dmp",
}


def source_files(directory, prefix):
    files = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if "__pycache__" in relative.parts:
            continue
        if path.is_file() and (path.suffix == ".py" or path.name == "py.typed"):
            files[prefix + "/" + relative.as_posix()] = path
    return files


def archive(path, files):
    with zipfile.ZipFile(path, "x") as output:
        for name, source in sorted(files.items()):
            if set(Path(name).parts) & PRIVATE_PARTS or Path(name).suffix.lower() in PRIVATE_SUFFIXES:
                raise ValueError("A private or runtime file reached release staging: " + name)
            if isinstance(source, Path):
                if source.is_symlink() or not source.is_file():
                    raise ValueError("Release inputs must be regular files.")
                raw = source.read_bytes()
            else:
                raw = source
            info = zipfile.ZipInfo(name, DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            output.writestr(info, raw)


def main(argv=None):
    host = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    sdk = tomllib.loads((ROOT / "sdk" / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / ("core-" + host["version"]))
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output.exists():
        raise ValueError("Release directory already exists; choose a new directory.")
    output.mkdir(parents=True)
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "Build-Distributions.py"),
         "--output", str(output / "wheels")],
        cwd=ROOT, check=True,
    )
    host_wheel = output / "wheels" / f"nyxion_heredity-{host['version']}-py3-none-any.whl"
    sdk_wheel = output / "wheels" / f"nyxion_heredity_sdk-{sdk['version']}-py3-none-any.whl"
    files = source_files(ROOT / "src" / "heredity", "src/heredity")
    for path in sorted((ROOT / "webui" / "dist").rglob("*")):
        if path.is_file():
            files["src/heredity/ui/" + path.relative_to(ROOT / "webui" / "dist").as_posix()] = path
    files.update({
        "README.md": ROOT / "distribution" / "host-README.md",
        "LICENSE": ROOT / "LICENSE",
        "pyproject.toml": ROOT / "pyproject.toml",
        "run.py": ROOT / "run.py",
        ".env.example": ROOT / ".env.example",
        "wheels/" + host_wheel.name: host_wheel,
        "wheels/" + sdk_wheel.name: sdk_wheel,
    })
    for name in ("marketplace", "settings", "network-interfaces", "plugin-architecture"):
        files[f"docs/{name}.md"] = ROOT / "docs" / f"{name}.md"
    for name in ("NETWORKS", "AEGIS"):
        files[f"sdk/{name}.md"] = ROOT / "sdk" / f"{name}.md"
    launchers = ROOT / "distribution" / "launchers"
    if launchers.is_dir():
        for path in sorted(launchers.iterdir()):
            if path.is_file():
                files[path.name] = path
    for dependency in ("react", "react-dom", "scheduler", "lucide-react"):
        license_file = ROOT / "webui" / "node_modules" / dependency / "LICENSE"
        if license_file.is_file():
            files[f"licenses/{dependency}.txt"] = license_file
    host_zip = output / f"NyXion-Heredity-{host['version']}.zip"
    archive(host_zip, files)
    spec = importlib.util.spec_from_file_location(
        "build_example", ROOT / "scripts" / "Build-ExampleModule.py")
    example_builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example_builder)
    example = example_builder.build(output / "example")
    example_sources = {
        "examples/market-module/" + path.name: path
        for path in (ROOT / "examples" / "market-module").iterdir()
        if path.is_file() and path.suffix in {".wat", ".json", ".md"}
    }
    example_sources["scripts/Build-ExampleModule.py"] = ROOT / "scripts" / "Build-ExampleModule.py"
    example_sources["LICENSE"] = ROOT / "LICENSE"
    archive(output / "example" / "nyxion-example-module-source.zip", example_sources)
    artifacts = []
    for path in sorted(output.rglob("*")):
        if path.is_file():
            artifacts.append({
                "file": path.relative_to(output).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
    report = {
        "host": {"version": host["version"], "archive": host_zip.name},
        "sdk_version": sdk["version"],
        "adapters_bundled": False,
        "runtime_state_included": False,
        "example_aegis_verified": example["aegis_verified"],
        "example_uploaded": False,
        "artifacts": artifacts,
    }
    (output / "release.json").write_text(
        json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    (output / "SHA256SUMS.txt").write_text(
        "".join(f"{item['sha256']}  {item['file']}\n" for item in artifacts), encoding="utf-8")
    print(json.dumps({"release_dir": str(output), "files": len(artifacts),
                      "adapters_bundled": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
