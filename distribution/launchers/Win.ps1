$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$AppArguments = @($args)
$Root = $PSScriptRoot
$Venv = Join-Path $Root ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
$Wheels = Join-Path $Root "wheels"
$VersionCheck = "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"

function Test-Python {
    param([string]$Executable, [string[]]$Prefix = @())
    try {
        & $Executable @Prefix -c $VersionCheck 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

try {
    Set-Location -LiteralPath $Root
    if (-not (Test-Path -LiteralPath (Join-Path $Root "run.py") -PathType Leaf)) {
        throw "run.py is missing. Keep the launcher in the extracted release directory."
    }
    if (-not (Test-Path -LiteralPath $Wheels -PathType Container)) {
        throw "The wheels directory is missing. Extract the complete release first."
    }
    $SdkWheels = @(Get-ChildItem -LiteralPath $Wheels -Filter "nyxion_heredity_sdk-*.whl" -File)
    $HostWheels = @(Get-ChildItem -LiteralPath $Wheels -Filter "nyxion_heredity-*.whl" -File)
    if ($SdkWheels.Count -ne 1 -or $HostWheels.Count -ne 1) {
        throw "wheels must contain exactly one NyXion Heredity wheel and one SDK wheel."
    }

    if (-not (Test-Path -LiteralPath $Venv)) {
        $Python = $null
        $PythonPrefix = @()
        if ($env:HEREDITY_PYTHON) {
            if (-not (Test-Python $env:HEREDITY_PYTHON)) {
                throw "HEREDITY_PYTHON must point to a working Python 3.11+ executable."
            }
            $Python = $env:HEREDITY_PYTHON
        } else {
            foreach ($Name in @("py", "python", "python3")) {
                $Command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue |
                    Select-Object -First 1
                if ($null -eq $Command) { continue }
                $Prefix = @()
                if ($Name -eq "py") { $Prefix = @("-3") }
                if (Test-Python -Executable $Command.Source -Prefix $Prefix) {
                    $Python = $Command.Source
                    $PythonPrefix = $Prefix
                    break
                }
            }
        }
        if ($null -eq $Python) {
            throw "Install Python 3.11+ with venv support, then run this launcher again."
        }
        Write-Host "[1/3] Creating .venv ..."
        & $Python @PythonPrefix -m venv $Venv
        if ($LASTEXITCODE -ne 0) {
            throw "Could not create .venv. Check Python's venv/ensurepip support."
        }
    } else {
        Write-Host "[1/3] Reusing .venv ..."
    }
    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf) -or
        -not (Test-Python $VenvPython)) {
        throw ".venv is unusable or belongs to another OS/Python installation. Move it aside and rerun; it will not be deleted automatically."
    }
    & $VenvPython -c "import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix else 1)"
    if ($LASTEXITCODE -ne 0) {
        throw ".venv does not contain an isolated Python virtual environment."
    }

    Write-Host "[2/3] Installing/checking release dependencies ..."
    & $VenvPython -m pip --version
    if ($LASTEXITCODE -ne 0) {
        & $VenvPython -m ensurepip --upgrade
        if ($LASTEXITCODE -ne 0) { throw "Could not install pip inside .venv." }
    }
    & $VenvPython -m pip install --disable-pip-version-check --no-input `
        --find-links $Wheels $SdkWheels[0].FullName $HostWheels[0].FullName
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed. Check the pip error above and network access. Keep the release's pinned dependency versions."
    }
    & $VenvPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw "The virtual environment has incompatible or missing dependencies." }
    & $VenvPython -c "import heredity, nyxion_heredity_sdk, websockets, wasmtime"
    if ($LASTEXITCODE -ne 0) { throw "A required runtime dependency could not be imported." }

    Write-Host "[3/3] Starting NyXion Heredity ..."
    Write-Host "The app reports its UI address below. Stop with Ctrl+C."
    Write-Host "Default password file: .heredity/host/ui-password.txt (unless a password is configured)."
    & $VenvPython -u (Join-Path $Root "run.py") app @AppArguments
    $AppExitCode = $LASTEXITCODE
    if ($AppExitCode -ne 0) {
        [Console]::Error.WriteLine("NyXion Heredity exited with code $AppExitCode.")
    }
    exit $AppExitCode
} catch {
    [Console]::Error.WriteLine("Launcher error: " + $_.Exception.Message)
    exit 1
}
