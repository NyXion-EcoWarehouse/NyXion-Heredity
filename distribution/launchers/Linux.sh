#!/usr/bin/env bash
set -euo pipefail
export PYTHONUTF8=1

fail() {
    printf 'Launcher error: %s\n' "$*" >&2
    exit 1
}

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd -- "$root"
venv="$root/.venv"
venv_python="$venv/bin/python"
wheels="$root/wheels"
version_check='import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'

[[ -f "$root/run.py" ]] ||
    fail "run.py is missing. Keep the launcher in the extracted release directory."
[[ -d "$wheels" ]] ||
    fail "The wheels directory is missing. Extract the complete release first."
shopt -s nullglob
sdk_wheels=("$wheels"/nyxion_heredity_sdk-*.whl)
host_wheels=("$wheels"/nyxion_heredity-*.whl)
[[ ${#sdk_wheels[@]} -eq 1 && ${#host_wheels[@]} -eq 1 ]] ||
    fail "wheels must contain exactly one NyXion Heredity wheel and one SDK wheel."

if [[ ! -e "$venv" ]]; then
    python=""
    if [[ -n "${HEREDITY_PYTHON:-}" ]]; then
        "$HEREDITY_PYTHON" -c "$version_check" 2>/dev/null ||
            fail "HEREDITY_PYTHON must point to a working Python 3.11+ executable."
        python="$HEREDITY_PYTHON"
    else
        for candidate in python3 python; do
            if command -v "$candidate" >/dev/null 2>&1 &&
                "$candidate" -c "$version_check" 2>/dev/null; then
                python="$candidate"
                break
            fi
        done
    fi
    [[ -n "$python" ]] ||
        fail "Install Python 3.11+ with venv support, then run this launcher again."
    printf '[1/3] Creating .venv ...\n'
    "$python" -m venv "$venv" ||
        fail "Could not create .venv. Install your distribution's python3-venv/ensurepip package if needed."
else
    printf '[1/3] Reusing .venv ...\n'
fi
[[ -x "$venv_python" ]] && "$venv_python" -c "$version_check" 2>/dev/null ||
    fail ".venv is unusable or belongs to another OS/Python installation. Move it aside and rerun; it will not be deleted automatically."
"$venv_python" -c 'import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)' ||
    fail ".venv does not contain an isolated Python virtual environment."

printf '[2/3] Installing/checking release dependencies ...\n'
if ! "$venv_python" -m pip --version; then
    "$venv_python" -m ensurepip --upgrade ||
        fail "Could not install pip inside .venv."
fi
"$venv_python" -m pip install --disable-pip-version-check --no-input \
    --find-links "$wheels" "${sdk_wheels[0]}" "${host_wheels[0]}" ||
    fail "Dependency installation failed. Check the pip error above and network access. Keep the release's pinned dependency versions."
"$venv_python" -m pip check ||
    fail "The virtual environment has incompatible or missing dependencies."
"$venv_python" -c 'import heredity, nyxion_heredity_sdk, websockets, wasmtime' ||
    fail "A required runtime dependency could not be imported."

printf '[3/3] Starting NyXion Heredity ...\n'
printf 'The app reports its UI address below. Stop with Ctrl+C.\n'
printf 'Default password file: .heredity/host/ui-password.txt (unless a password is configured).\n'
exec "$venv_python" -u "$root/run.py" app "$@"
