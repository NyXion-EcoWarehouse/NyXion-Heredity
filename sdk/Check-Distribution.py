"""Verify a built SDK wheel and source archive without importing the monorepo."""

import argparse
from email.parser import BytesParser
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile


SDK_VERSION = tomllib.loads(
    Path(__file__).with_name("pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
DIST_STEM = "nyxion_heredity_sdk-" + SDK_VERSION


def inspect_wheel(wheel):
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if not names or len(names) != len(set(names)):
            raise ValueError("SDK wheel contains no entries or duplicate names")
        if any(not (name.startswith("nyxion_heredity_sdk/")
                    or name.startswith(DIST_STEM + ".dist-info/"))
               or "\\" in name or ".." in PurePosixPath(name).parts for name in names):
            raise ValueError("SDK wheel contains foreign packages or unsafe names")
        for required in ("__init__.py", "onebot.py", "networks.py", "sandbox.py", "py.typed",
                         "schemas/plugin-manifest.v1.schema.json",
                         "schemas/contracts.v1.schema.json",
                         "schemas/sandbox-module.v1.schema.json"):
            if "nyxion_heredity_sdk/" + required not in names:
                raise ValueError("SDK wheel omitted a required public asset")
        metadata = BytesParser().parsebytes(
            archive.read(DIST_STEM + ".dist-info/METADATA"))
        if (metadata.get("Name") != "nyxion-heredity-sdk"
                or metadata.get("Version") != SDK_VERSION or metadata.get_all("Requires-Dist")):
            raise ValueError("SDK wheel metadata has an unexpected version or runtime dependency")
    return names


def check(wheel, sdist, work_directory):
    wheel, sdist = Path(wheel).resolve(strict=True), Path(sdist).resolve(strict=True)
    inspect_wheel(wheel)
    work = Path(work_directory).absolute()
    if work.exists():
        raise ValueError("SDK verification directory must be new")
    work.mkdir(parents=True)
    extracted = work / "extracted"
    extracted.mkdir()
    with tarfile.open(sdist, "r:gz") as archive:
        members = archive.getmembers()
        if not members or len(members) > 1000:
            raise ValueError("SDK source archive has an invalid entry count")
        total = 0
        for member in members:
            path = PurePosixPath(member.name)
            total += member.size
            if (path.is_absolute() or ".." in path.parts or "\\" in member.name
                    or not path.parts or path.parts[0] != DIST_STEM
                    or not (member.isfile() or member.isdir()) or total > 16 * 1024 * 1024):
                raise ValueError("SDK source archive contains unsafe or unexpected entries")
        archive.extractall(extracted, filter="data")
    project = extracted / DIST_STEM
    if not (project / "AEGIS.md").is_file():
        raise ValueError("SDK source archive omitted the Aegis ABI documentation")
    rebuilt = work / "rebuilt"
    rebuilt.mkdir()
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys;from setuptools import build_meta;build_meta.build_wheel(sys.argv[1])",
         str(rebuilt)], cwd=project, check=False)
    if result.returncode:
        raise RuntimeError("SDK source distribution could not rebuild independently")
    rebuilt_wheel = rebuilt / wheel.name
    if not rebuilt_wheel.is_file():
        raise RuntimeError("SDK source rebuild did not produce the expected wheel")
    inspect_wheel(rebuilt_wheel)
    target = work / "isolated-install"
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--no-index",
         "--target", str(target), str(rebuilt_wheel)], cwd=work, check=False)
    if result.returncode:
        raise RuntimeError("SDK wheel could not install without runtime dependencies")
    code = (
        "import importlib.abc,importlib.resources,sys;"
        "baseline=set(sys.modules);"
        "sys.path.insert(0,sys.argv[1]);"
        "import nyxion_heredity_sdk;"
        f"assert nyxion_heredity_sdk.__version__=={SDK_VERSION!r};"
        "assert nyxion_heredity_sdk.API_MAJOR==1 and nyxion_heredity_sdk.API_MINOR==0;"
        "from nyxion_heredity_sdk import NetworkInterfaceRequirement,"
        "NetworkInterfaceGrant,NetworkInterfaceAllocator,NetworkInterfaceDeclarations,"
        "NetworkInterfaceMultiAllocator,NetworkInterfaceLifecycle,SandboxModuleManifest;"
        "requirement=NetworkInterfaceRequirement('distribution.check',('http_server',));"
        "assert requirement.to_dict()['directions']==['call','receive'];"
        "assert requirement.to_dict()['max_interfaces']==16;"
        "assert hasattr(NetworkInterfaceMultiAllocator,'request_interfaces');"
        "assert hasattr(NetworkInterfaceMultiAllocator,'release_binding');"
        "assert hasattr(NetworkInterfaceLifecycle,'reapply_interface');"
        "grant=NetworkInterfaceGrant('distribution-request','distribution.check',"
        "'1234567890abcdef',1,'http_server',grant_id='distribution-grant');"
        "assert grant.to_dict()['grant_id']=='distribution-grant';"
        "sandbox=SandboxModuleManifest('distribution.check','Distribution check',"
        "'1.0.0','0.3.0',('bus.call',));"
        "assert SandboxModuleManifest.from_dict(sandbox.to_dict())==sandbox;"
        "from nyxion_heredity_sdk.conformance import run_conformance;"
        "from nyxion_heredity_sdk.example import OfflineExamplePlugin;"
        "result=run_conformance(OfflineExamplePlugin);"
        "assert result['passed'] and len(result['checks'])==13;"
        "assert all(n.split('.')[0] in sys.stdlib_module_names or "
        "n.split('.')[0]=='nyxion_heredity_sdk' for n in set(sys.modules)-baseline);"
        "root=importlib.resources.files('nyxion_heredity_sdk');"
        "assert root.joinpath('py.typed').is_file();"
        "assert root.joinpath('schemas/plugin-manifest.v1.schema.json').is_file();"
        "assert root.joinpath('schemas/sandbox-module.v1.schema.json').is_file();"
        "import json;print(json.dumps(result))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(target)], cwd=work,
        capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("SDK isolated installed conformance failed")
    report = json.loads(result.stdout)
    return {"wheel": str(wheel), "sdist": str(sdist), "sdist_rebuilt": True,
            "runtime_dependencies": [], "isolated_conformance_checks": len(report["checks"]),
            "installed_target": str(target), "passed": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--sdist", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args(argv)
    parent = Path(__file__).resolve().parents[1] / "build" / "sdk-distribution"
    parent.mkdir(parents=True, exist_ok=True)
    work = args.work_dir
    if work is None:
        parent = Path(tempfile.mkdtemp(prefix="sdk-check-", dir=parent))
        work = parent / "verification"
    print(json.dumps(check(args.wheel, args.sdist, work), ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, tarfile.TarError) as error:
        print(f"SDK distribution verification failed: {type(error).__name__}.", file=sys.stderr)
        raise SystemExit(1) from None
