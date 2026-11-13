"""Build independent core and SDK distributions from explicit source inputs."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
NATIVE_SUFFIXES = {".node", ".dll", ".so", ".dylib", ".exe"}


def _build(project, output):
    subprocess.run(
        [sys.executable, "-c",
         "import sys; from setuptools import build_meta; build_meta.build_wheel(sys.argv[1])",
         str(output)],
        cwd=project, check=True,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Distribution output must be empty.")
    ui = ROOT / "webui" / "dist"
    if not (ui / "index.html").is_file():
        raise ValueError("Build the WebUI before building the core.")
    subprocess.run(
        [sys.executable, str(ROOT / "sdk" / "Build-Sdk.py"), "--output", str(output)],
        cwd=ROOT, check=True,
    )
    stage_parent = ROOT / "build" / "distributions"
    stage_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="core-", dir=stage_parent) as directory:
        stage = Path(directory)
        shutil.copyfile(ROOT / "pyproject.toml", stage / "pyproject.toml")
        shutil.copyfile(ROOT / "distribution" / "host-README.md", stage / "README.md")
        shutil.copyfile(ROOT / "LICENSE", stage / "LICENSE")
        package = stage / "src" / "heredity"
        shutil.copytree(
            ROOT / "src" / "heredity", package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "ui"),
        )
        shutil.copytree(ui, package / "ui")
        _build(stage, output)
    versions = {
        "nyxion_heredity": tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"],
        "nyxion_heredity_sdk": tomllib.loads(
            (ROOT / "sdk" / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"],
    }
    reports = []
    for prefix, version in versions.items():
        path = output / f"{prefix}-{version}-py3-none-any.whl"
        if not path.is_file():
            raise RuntimeError("A required distribution was not produced.")
        expected = "heredity" if prefix == "nyxion_heredity" else "nyxion_heredity_sdk"
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            package_roots = {
                name.split("/", 1)[0] for name in names
                if "/" in name and not name.split("/", 1)[0].endswith(".dist-info")
            }
            if package_roots != {expected}:
                raise RuntimeError("A distribution contains an unexpected package root.")
            if any(Path(name).suffix.lower() in NATIVE_SUFFIXES for name in names):
                raise RuntimeError("A source distribution contains a native executable.")
            if expected == "heredity" and "heredity/ui/index.html" not in names:
                raise RuntimeError("The core distribution is missing its UI.")
            reports.append({"wheel": str(path), "files": len(names)})
    print(json.dumps({"built": reports, "adapters_bundled": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
