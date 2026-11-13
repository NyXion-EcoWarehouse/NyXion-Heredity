"""Stage the monorepo SDK as an independent source tree and build local artifacts."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib


def _safe_file(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("SDK source inputs must be regular files, not links")
    return path


def stage_source(destination):
    """Copy exact SDK inputs only; the staged project is fully self-contained."""
    source_root = Path(__file__).resolve().parents[1]
    sdk_root = source_root / "sdk"
    package = source_root / "src" / "nyxion_heredity_sdk"
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("SDK staging destination already exists")
    for path in (package, *package.parents):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("SDK source path must not contain link indirection")
    if not package.is_dir():
        raise ValueError("SDK source package is missing")
    files = [(_safe_file(sdk_root / name), Path(name))
             for name in ("pyproject.toml", "MANIFEST.in", "README.md", "NETWORKS.md", "AEGIS.md")]
    files.append((_safe_file(source_root / "LICENSE"), Path("LICENSE")))
    for source in package.rglob("*"):
        if source.is_symlink() or getattr(source, "is_junction", lambda: False)():
            raise ValueError("SDK package must not contain links")
        if not source.is_file():
            continue
        if source.suffix == ".py" or source.name == "py.typed":
            files.append((_safe_file(source), Path("src/nyxion_heredity_sdk")
                          / source.relative_to(package)))
    schemas = sdk_root / "schemas"
    for source in schemas.glob("*.json"):
        files.append((_safe_file(source), Path("src/nyxion_heredity_sdk/schemas") / source.name))
    if not any(target.name == "py.typed" for _, target in files):
        raise ValueError("SDK typing marker is missing")
    destination.mkdir(parents=True)
    for source, relative in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return destination


def build(stage_root, output):
    output = Path(output).absolute()
    output.mkdir(parents=True, exist_ok=True)
    metadata = tomllib.loads((Path(stage_root) / "pyproject.toml").read_text(encoding="utf-8"))
    stem = "nyxion_heredity_sdk-" + metadata["project"]["version"]
    expected = (output / (stem + "-py3-none-any.whl"), output / (stem + ".tar.gz"))
    if any(path.exists() for path in expected):
        raise ValueError("SDK artifacts already exist; use another output directory")
    # Build isolation is deliberately disabled: this script never downloads or
    # modifies the build environment. Install the declared tooling explicitly.
    code = (
        "import sys;from setuptools import build_meta;"
        "output=sys.argv[1];"
        "build_meta.build_wheel(output);build_meta.build_sdist(output)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(output)], cwd=stage_root, check=False)
    if result.returncode:
        raise RuntimeError("SDK wheel/source build failed")
    if not all(path.is_file() for path in expected):
        raise RuntimeError("SDK build did not produce both expected artifacts")
    return expected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-only", action="store_true",
                        help="Prepare a self-contained source project without building.")
    parser.add_argument("--stage", type=Path,
                        help="New explicit staging directory; existing directories are rejected.")
    parser.add_argument("--output", type=Path,
                        help="Artifact directory; existing same-version artifacts are rejected.")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    parent = root / "build" / "sdk-distribution"
    parent.mkdir(parents=True, exist_ok=True)
    if not args.stage_only:
        for module in ("setuptools", "wheel"):
            if importlib.util.find_spec(module) is None:
                raise RuntimeError("SDK build tooling is not installed")
    if args.stage is None:
        run_root = Path(tempfile.mkdtemp(prefix="sdk-", dir=parent))
        stage = run_root / "source"
    else:
        stage = args.stage
    staged = stage_source(stage)
    output = args.output or staged.parent / "artifacts"
    artifacts = () if args.stage_only else build(staged, output)
    report = {"staged_source": str(staged), "self_contained": True,
              "built": not args.stage_only, "artifacts": [
                  {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                  for path in artifacts]}
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(f"SDK packaging failed: {type(error).__name__}.", file=sys.stderr)
        raise SystemExit(1) from None
