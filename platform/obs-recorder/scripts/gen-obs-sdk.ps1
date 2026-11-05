# Generate a minimal OBS SDK from the *installed* OBS runtime (no SDK download).
#
# OBS Studio's Windows installer ships obs.dll but no import library or headers.
# For the S1b media self-test we only need:
#   * an import library for the installed obs.dll  -> obs.lib
#   * the generated obsconfig.h consumed by libobs/obs-config.h
#   * the source headers from the matching OBS tag  -> vendor/obs-studio/libobs
#
# This script produces obs.lib + obsconfig.h in an output directory (default:
# platform/obs-recorder/build-media/sdk, git-ignored).  It is deterministic for
# a given obs.dll: the .def is generated from `dumpbin /exports`, and the
# summary records hashes of every artifact.
#
# Usage:
#   pwsh -NoProfile -ExecutionPolicy Bypass -File platform\obs-recorder\scripts\gen-obs-sdk.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File ... -ObsDir D:\obs -OutDir D:\sdk
[CmdletBinding()]
param(
    [string]$ObsDir = 'C:\Program Files\obs-studio',
    [string]$OutDir = '',
    [string]$RepoRoot = ''
)

$ErrorActionPreference = 'Stop'

function Fail([string]$Message) {
    Write-Error $Message
    exit 1
}

function Find-Tool([string[]]$Candidates) {
    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return ''
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = (Resolve-Path (Join-Path $scriptDir '..')).Path
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $projectDir '..\..')).Path }
if (-not $OutDir) { $OutDir = Join-Path (Join-Path $projectDir 'build-media') 'sdk' }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path

$obsDll = Join-Path $ObsDir 'bin\64bit\obs.dll'
$d3d11 = Join-Path $ObsDir 'bin\64bit\libobs-d3d11.dll'
$libobsData = Join-Path $ObsDir 'data\libobs'
if (-not (Test-Path -LiteralPath $obsDll)) { Fail "obs.dll not found: $obsDll" }
if (-not (Test-Path -LiteralPath $d3d11)) { Fail "libobs-d3d11.dll not found: $d3d11" }
if (-not (Test-Path -LiteralPath $libobsData)) { Fail "libobs data dir not found: $libobsData" }

# --- toolchain: MSVC (dumpbin/lib) + CMake/Ninja -----------------------------
$vcvars = ''
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (Test-Path -LiteralPath $vswhere) {
    $install = & $vswhere -latest -products * `
        -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
        -property installationPath 2>$null
    if ($install) {
        $candidate = Join-Path $install 'VC\Auxiliary\Build\vcvars64.bat'
        if (Test-Path -LiteralPath $candidate) { $vcvars = $candidate }
    }
}
if (-not $vcvars) {
    $vcvars = Find-Tool @(
        'C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat',
        'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat',
        'C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat',
        'C:\Program Files\Microsoft Visual Studio\2022\Professional\VC\Auxiliary\Build\vcvars64.bat',
        'C:\Program Files\Microsoft Visual Studio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat'
    )
}
if (-not $vcvars) { Fail 'vcvars64.bat not found (Visual Studio C++ workload missing?)' }

$envBat = Join-Path $OutDir '_vcvars_env.bat'
Set-Content -LiteralPath $envBat -Encoding ASCII -Value @(
    '@echo off',
    'setlocal',
    ('call "' + $vcvars + '" >nul 2>&1'),
    'if errorlevel 1 exit /b 101',
    'set'
)
$envOutput = & cmd.exe /d /c ('"' + $envBat + '"') 2>&1
$toolchainExit = $LASTEXITCODE
$envLines = @($envOutput | ForEach-Object { "$_" })
if ($toolchainExit -ne 0) { Fail ("vcvars64.bat failed with exit {0}" -f $toolchainExit) }
foreach ($line in $envLines) {
    $index = $line.IndexOf('=')
    if ($index -le 0) { continue }
    $name = $line.Substring(0, $index)
    $value = $line.Substring($index + 1)
    if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_()]*$') { continue }
    try { [Environment]::SetEnvironmentVariable($name, $value, 'Process') } catch { }
}
$dumpbin = (Get-Command dumpbin.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
$libExe = (Get-Command lib.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $dumpbin) { Fail 'dumpbin.exe not on PATH after vcvars64.bat' }
if (-not $libExe) { Fail 'lib.exe not on PATH after vcvars64.bat' }
$clPath = (Get-Command cl.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
$vcvarsVerBat = Join-Path $OutDir '_dumpbin_ver.bat'
Set-Content -LiteralPath $vcvarsVerBat -Encoding ASCII -Value @(
    '@echo off',
    ('"' + $dumpbin + '" /? >nul 2>&1'),
    'echo done'
)

# --- 1. obs.def from dumpbin /exports ---------------------------------------
$defPath = Join-Path $OutDir 'obs.def'
$dumpText = (& $dumpbin /exports $obsDll 2>&1 | Out-String)
$dumpExit = $LASTEXITCODE
if ($dumpExit -ne 0) { Fail ("dumpbin /exports failed with exit {0}: {1}" -f $dumpExit, $dumpText) }

$names = New-Object System.Collections.Generic.List[string]
$seen = New-Object 'System.Collections.Generic.HashSet[string]'
foreach ($line in ($dumpText -split "`r?`n")) {
    # dumpbin export rows: "   ordinal  hint  RVA  name [= alias]"
    if ($line -match '^\s+\d+\s+[0-9A-Fa-f]+\s+[0-9A-Fa-f]+\s+(\S+)') {
        $name = $Matches[1]
        if ($name -match '^[A-Za-z_][A-Za-z0-9_]*$' -and -not $seen.Contains($name)) {
            $null = $seen.Add($name)
            $names.Add($name)
        }
    }
}
if ($names.Count -lt 1500) { Fail ("unexpectedly few exports parsed from obs.dll: {0}" -f $names.Count) }

$defLines = New-Object System.Collections.Generic.List[string]
$defLines.Add('; auto-generated by platform/obs-recorder/scripts/gen-obs-sdk.ps1')
$defLines.Add('; from: ' + $obsDll)
$defLines.Add('LIBRARY obs.dll')
$defLines.Add('EXPORTS')
foreach ($name in $names) { $defLines.Add('  ' + $name) }
Set-Content -LiteralPath $defPath -Value $defLines -Encoding ASCII

# --- 2. obs.lib from obs.def ------------------------------------------------
$libPath = Join-Path $OutDir 'obs.lib'
$libOut = (& $libExe "/def:$defPath" '/machine:x64' "/out:$libPath" 2>&1 | Out-String)
$libExit = $LASTEXITCODE
if ($libExit -ne 0 -or -not (Test-Path -LiteralPath $libPath)) {
    Fail ("lib.exe failed with exit {0}: {1}" -f $libExit, $libOut)
}

# --- 3. minimal obsconfig.h -------------------------------------------------
$obsConfigPath = Join-Path $OutDir 'obsconfig.h'
$obsConfigText = @(
    '#pragma once',
    '#define OBS_DATA_PATH "data/obs-studio"',
    '#define OBS_PLUGIN_PATH "obs-plugins/64bit"',
    '#define OBS_PLUGIN_DESTINATION "obs-plugins"',
    '#define OBS_RELEASE_CANDIDATE 0',
    '#define OBS_BETA 0',
    ''
) -join "`r`n"
Set-Content -LiteralPath $obsConfigPath -Value $obsConfigText -Encoding ASCII

# --- 4. summary -------------------------------------------------------------
$obsDllHash = (Get-FileHash -LiteralPath $obsDll -Algorithm SHA256).Hash.ToLowerInvariant()
$summary = [ordered]@{
    timestamp_utc            = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    obs_dir                  = $ObsDir
    obs_dll                  = $obsDll
    obs_dll_size_bytes       = (Get-Item -LiteralPath $obsDll).Length
    obs_dll_sha256           = $obsDllHash
    obs_dll_exports_parsed   = $names.Count
    dumpbin_exit             = $dumpExit
    def_path                 = $defPath
    def_sha256               = (Get-FileHash -LiteralPath $defPath -Algorithm SHA256).Hash.ToLowerInvariant()
    lib_path                 = $libPath
    lib_exit                 = $libExit
    lib_size_bytes           = (Get-Item -LiteralPath $libPath).Length
    lib_sha256               = (Get-FileHash -LiteralPath $libPath -Algorithm SHA256).Hash.ToLowerInvariant()
    obsconfig_path           = $obsConfigPath
    obsconfig_sha256         = (Get-FileHash -LiteralPath $obsConfigPath -Algorithm SHA256).Hash.ToLowerInvariant()
    vcvars64                 = $vcvars
    cl                       = $clPath
    dumpbin                  = $dumpbin
    lib                      = $libExe
}
$summaryPath = Join-Path $OutDir 'sdk_summary.json'
$summary | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $summaryPath -Encoding UTF8

Write-Host ("SDK obs.lib:   {0} ({1} bytes) exports={2}" -f $libPath, $summary.lib_size_bytes, $names.Count)
Write-Host ("SDK obsconfig: {0}" -f $obsConfigPath)
Write-Host ("SDK summary:   {0}" -f $summaryPath)
exit 0
