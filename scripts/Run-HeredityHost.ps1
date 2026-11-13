#requires -Version 5.1
<#
.SYNOPSIS
Start NyXion Heredity's UI, configured interfaces and SDK plugin host.
.DESCRIPTION
The default runs local Python. -UseWsl runs the host in an explicitly selected
existing Linux distribution.
Only saved profiles with automatic login enabled start automatically.
OneBot listeners are created by the user in the network interfaces page.
#>
[CmdletBinding()]
param(
    [switch]$UseWsl,
    [string]$Distribution = 'Ubuntu',
    [ValidateRange(0, 65535)][int]$Port = 5700,
    [ValidateRange(0, 65535)][int]$UiPort,
    [string]$StateDir = '.heredity/host',
    [string]$PluginRegistry,
    [string]$Account,
    [switch]$Sse
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$arguments = @('run.py', 'app', '--port', "$Port")
if ($PSBoundParameters.ContainsKey('UiPort')) { $arguments += @('--ui-port', "$UiPort") }
if ($Account) { $arguments += @('--account', $Account) }
if ($Sse) { $arguments += '--sse' }

function Resolve-ProjectPath([string]$Path) {
    if ([IO.Path]::IsPathRooted($Path)) { return [IO.Path]::GetFullPath($Path) }
    return [IO.Path]::GetFullPath((Join-Path $projectRoot $Path))
}

Push-Location -LiteralPath $projectRoot
try {
    if (-not $UseWsl) {
        $python = Join-Path $projectRoot '.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
            $python = (Get-Command python -CommandType Application -ErrorAction Stop |
                Select-Object -First 1).Source
        }
        $arguments += @('--state-dir', (Resolve-ProjectPath $StateDir))
        if ($PluginRegistry) {
            $arguments += @('--plugin-registry', (Resolve-ProjectPath $PluginRegistry))
        }
        & $python @arguments
        exit $LASTEXITCODE
    }
    $wsl = (Get-Command wsl.exe -CommandType Application -ErrorAction Stop |
        Select-Object -First 1).Source
    function Convert-HostPath([string]$Path) {
        $absolute = Resolve-ProjectPath $Path
        $previousNativeEncoding = [Console]::OutputEncoding
        try {
            # wslpath emits UTF-8 even when a background PowerShell inherits GBK.
            [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
            $converted = @(& $wsl --distribution $Distribution --exec wslpath -a -u $absolute)
            $conversionExit = $LASTEXITCODE
        } finally {
            [Console]::OutputEncoding = $previousNativeEncoding
        }
        if ($conversionExit -ne 0 -or $converted.Count -ne 1 -or
                -not ([string]$converted[0]).StartsWith('/')) {
            throw 'WSL path conversion failed.'
        }
        return ([string]$converted[0]).TrimEnd("`r", "`n")
    }
    $rootLinux = Convert-HostPath $projectRoot
    $arguments += @('--state-dir', (Convert-HostPath $StateDir))
    if ($PluginRegistry) {
        $arguments += @('--plugin-registry', (Convert-HostPath $PluginRegistry))
    }
    $previousWslEnv = [Environment]::GetEnvironmentVariable('WSLENV', 'Process')
    $entries = @()
    if ($previousWslEnv) { $entries = @($previousWslEnv.Split(':')) }
    foreach ($name in @('HEREDITY_WEBUI_PASSWORD', 'HEREDITY_ACCESS_TOKEN', 'HEREDITY_EVENT_SECRET')) {
        if ([Environment]::GetEnvironmentVariable($name, 'Process')) {
            $entries = @($entries | Where-Object { (($_ -split '/', 2)[0]) -ine $name })
            $entries += "$name/u"
        }
    }
    try {
        [Environment]::SetEnvironmentVariable('WSLENV', ($entries -join ':'), 'Process')
        $workspaceDependencies = Join-Path $projectRoot '.heredity\python-deps'
        if (Test-Path -LiteralPath $workspaceDependencies -PathType Container) {
            $linuxDependencies = Convert-HostPath $workspaceDependencies
            & $wsl --distribution $Distribution --cd $rootLinux --exec env `
                "PYTHONPATH=$linuxDependencies" python3 @arguments
        } else {
            & $wsl --distribution $Distribution --cd $rootLinux --exec python3 @arguments
        }
        $runExit = $LASTEXITCODE
    } finally {
        [Environment]::SetEnvironmentVariable('WSLENV', $previousWslEnv, 'Process')
    }
    exit $runExit
} finally {
    Pop-Location
}
