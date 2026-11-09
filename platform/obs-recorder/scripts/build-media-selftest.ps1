# Build obs-media-selftest.exe: the S1b libobs media self-test host.
#
# This generates a minimal OBS SDK (obs.lib + obsconfig.h) from the installed
# OBS runtime (scripts/gen-obs-sdk.ps1), configures CMake with the OBS 32.1.0
# source headers (vendor/obs-studio/libobs), builds only the
# obs-media-selftest target, and stages the OBS runtime DLLs next to the exe.
#
# The S0/S1a scaffold build (scripts/build.ps1, dynamic-loading obs-recorder.exe)
# is untouched; this uses its own git-ignored build-media/ directory.
#
# Requires: Visual Studio (vcvars64.bat), CMake/Ninja (C:\msys64\mingw64\bin),
#           installed OBS 32.1.0, and vendor/obs-studio @ tag 32.1.0.
#
# Usage:
#   pwsh -NoProfile -ExecutionPolicy Bypass -File platform\obs-recorder\scripts\build-media-selftest.ps1
[CmdletBinding()]
param(
    [string]$RepoRoot = '',
    [string]$BuildDir = '',
    [string]$Config = 'Release',
    [string]$ObsDir = 'C:\Program Files\obs-studio',
    [string]$ExpectedCommit = '',
    [switch]$RequireClean
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

function Write-Log([string]$Path, [string]$Text) {
    $Text | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Replace-Bytes([byte[]]$Bytes, [byte[]]$Find, [byte[]]$Replacement) {
    $result = New-Object System.Collections.Generic.List[byte]
    $i = 0
    while ($i -lt $Bytes.Length) {
        if ($i + $Find.Length -le $Bytes.Length) {
            $matched = $true
            for ($k = 0; $k -lt $Find.Length; $k++) {
                if ($Bytes[$i + $k] -ne $Find[$k]) { $matched = $false; break }
            }
            if ($matched) {
                $result.AddRange($Replacement)
                $i += $Find.Length
                continue
            }
        }
        $result.Add($Bytes[$i])
        $i++
    }
    return ,$result.ToArray()
}

function Repair-NinjaShowIncludesPrefix([string]$Directory, [string]$Prefix) {
    $latin1 = [System.Text.Encoding]::GetEncoding(28591)
    $marker = [System.Text.Encoding]::ASCII.GetBytes('@@@PREFIX@@@')
    $replacement = [System.Text.Encoding]::UTF8.GetBytes($Prefix)
    $patched = 0
    foreach ($relative in @('CMakeFiles\rules.ninja', 'build.ninja')) {
        $path = Join-Path $Directory $relative
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $text = $latin1.GetString([System.IO.File]::ReadAllBytes($path))
        if ($text -notmatch '(?m)^msvc_deps_prefix = ') { continue }
        $rewritten = [regex]::Replace(
            $text,
            '(?m)^msvc_deps_prefix = [^\r\n]*',
            'msvc_deps_prefix = @@@PREFIX@@@')
        $bytes = $latin1.GetBytes($rewritten)
        $bytes = Replace-Bytes $bytes $marker $replacement
        [System.IO.File]::WriteAllBytes($path, $bytes)
        $patched++
    }
    return $patched
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir 'build-provenance.ps1')
$projectDir = (Resolve-Path (Join-Path $scriptDir '..')).Path
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $projectDir '..\..')).Path }
if (-not $BuildDir) { $BuildDir = Join-Path $projectDir 'build-media' }
$BuildDir = [System.IO.Path]::GetFullPath($BuildDir)
$logsDir = Join-Path $BuildDir 'logs'
$sdkDir = Join-Path $BuildDir 'sdk'
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $ObsDir 'bin\64bit\obs.dll'))) {
    Fail "installed OBS not found at '$ObsDir' (expected bin\64bit\obs.dll)"
}
$libobsHeaders = Join-Path $RepoRoot 'vendor\obs-studio\libobs'
if (-not (Test-Path -LiteralPath (Join-Path $libobsHeaders 'obs.h'))) {
    Fail ("OBS source headers missing at '{0}'. Expected OBS 32.1.0 source clone in vendor/obs-studio." -f $libobsHeaders)
}

# --- toolchain discovery -----------------------------------------------------
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
        'C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat'
    )
}
if (-not $vcvars) { Fail 'vcvars64.bat not found' }
$cmake = Find-Tool @('C:\msys64\mingw64\bin\cmake.exe', (Get-Command cmake.exe -ErrorAction SilentlyContinue | ForEach-Object { $_.Source }))
$ninja = Find-Tool @('C:\msys64\mingw64\bin\ninja.exe', (Get-Command ninja.exe -ErrorAction SilentlyContinue | ForEach-Object { $_.Source }))
if (-not $cmake) { Fail 'cmake.exe not found' }
if (-not $ninja) { Fail 'ninja.exe not found' }

$envBat = Join-Path $BuildDir 'step_vcvars_env.bat'
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
Write-Log (Join-Path $logsDir 'toolchain.log') (($envLines | Select-Object -First 25) -join [Environment]::NewLine)
if ($toolchainExit -ne 0) { Fail ("vcvars64.bat failed with exit {0}" -f $toolchainExit) }
foreach ($line in $envLines) {
    $index = $line.IndexOf('=')
    if ($index -le 0) { continue }
    $name = $line.Substring(0, $index)
    $value = $line.Substring($index + 1)
    if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_()]*$') { continue }
    try { [Environment]::SetEnvironmentVariable($name, $value, 'Process') } catch { }
}
$clPath = (Get-Command cl.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $clPath) { Fail 'cl.exe not on PATH after vcvars64.bat' }
$cmakeVersion = (& $cmake --version | Select-Object -First 1)
$ninjaVersion = (& $ninja --version)

# --- generate the OBS SDK from the installed runtime -------------------------
Write-Host '=== generating OBS SDK (obs.lib + obsconfig.h) from installed runtime ==='
$genSdk = Join-Path $scriptDir 'gen-obs-sdk.ps1'
if (-not (Test-Path -LiteralPath $genSdk)) { Fail "missing generator script: $genSdk" }
& $genSdk -ObsDir $ObsDir -OutDir $sdkDir -RepoRoot $RepoRoot
$sdkExit = $LASTEXITCODE
if ($sdkExit -ne 0) { Fail ("gen-obs-sdk.ps1 failed with exit {0}" -f $sdkExit) }
$sdkSummaryPath = Join-Path $sdkDir 'sdk_summary.json'
if (-not (Test-Path -LiteralPath $sdkSummaryPath)) { Fail "SDK summary missing: $sdkSummaryPath" }
$sdkSummary = Get-Content -LiteralPath $sdkSummaryPath -Raw | ConvertFrom-Json

$inputPaths = @(Get-A3TrackedInputs $RepoRoot @('platform/obs-recorder', 'platform/audio-protocol')) + @(
    (Join-Path $projectDir 'src'), (Join-Path $projectDir 'tests'),
    $libobsHeaders, (Join-Path $sdkDir 'obs.lib'), (Join-Path $sdkDir 'obsconfig.h'),
    (Join-Path $ObsDir 'obs-plugins/64bit/obs-ffmpeg.dll'),
    (Join-Path $ObsDir 'obs-plugins/64bit/obs-x264.dll'),
    (Join-Path $ObsDir 'obs-plugins/64bit/win-capture.dll')) +
    @(Get-ChildItem -LiteralPath (Join-Path $ObsDir 'bin/64bit') -Filter '*.dll' | Select-Object -ExpandProperty FullName)
$provenance = Start-A3BuildProvenance -Root $RepoRoot -Component 'native-recorder' `
    -RecordDir (Join-Path $BuildDir 'provenance') -Inputs $inputPaths `
    -VendorRoots @((Join-Path $RepoRoot 'vendor/obs-studio')) -Tools @($vcvars, $clPath, $cmake, $ninja) `
    -ExpectedCommit $ExpectedCommit -RequireClean:$RequireClean `
    -Configuration @{ config = $Config; generator = 'Ninja'; obs_version = '32.1.0'; clean_first = [bool]$RequireClean }

# --- detect the localized /showIncludes prefix --------------------------------
$probeSrc = Join-Path $BuildDir '_deps_probe.cpp'
$probeObj = Join-Path $BuildDir '_deps_probe.obj'
$probeOut = Join-Path $BuildDir '_deps_probe_showincludes.txt'
@('#include <cstdio>', 'int main() { return 0; }') | Set-Content -LiteralPath $probeSrc -Encoding ASCII
$probeCmd = ('cl.exe /nologo /showIncludes /c /Fo"' + $probeObj + '" "' + $probeSrc + '" > "' + $probeOut + '" 2>&1')
& cmd.exe /d /c $probeCmd | Out-Null
$includePrefix = ''
try {
    $bytes = [System.IO.File]::ReadAllBytes($probeOut)
    $utf8 = New-Object System.Text.UTF8Encoding($false, $true)
    try { $text = $utf8.GetString($bytes) } catch { $text = [System.Text.Encoding]::Default.GetString($bytes) }
    foreach ($line in ($text -split "`r?`n")) {
        if ($line -match '^(.*?)\s+[A-Za-z]:[\\/]') {
            $candidate = $Matches[1].Trim()
            if ($candidate.Length -gt 3 -and $candidate.EndsWith(':')) {
                $includePrefix = $candidate
                break
            }
        }
    }
} catch { }
if (-not $includePrefix) {
    $includePrefix = 'Note: including file:'
    Write-Host 'WARN: localized /showIncludes prefix not detected; using English fallback.'
}

$prefixCacheFile = Join-Path $BuildDir 'showincludes_prefix.cmake'
$prefixCacheText = 'set(CMAKE_CL_SHOWINCLUDES_PREFIX "' + `
    $includePrefix.Replace('\', '\\').Replace('"', '\"') + `
    '" CACHE STRING "MSVC /showIncludes prefix" FORCE)' + "`r`n"
$ansiCodePage = [System.Globalization.CultureInfo]::CurrentCulture.TextInfo.ANSICodePage
$ansiEncoding = [System.Text.Encoding]::GetEncoding($ansiCodePage)
[System.IO.File]::WriteAllText($prefixCacheFile, $prefixCacheText, $ansiEncoding)

# --- configure + build --------------------------------------------------------
$configureArgs = @(
    '-S', $projectDir,
    '-B', $BuildDir,
    '-G', 'Ninja',
    ("-DCMAKE_BUILD_TYPE=" + $Config),
    ("-DCMAKE_MAKE_PROGRAM=" + $ninja),
    '-C', $prefixCacheFile,
    '-DOBS_RECORDER_BUILD_MEDIA_SELFTEST=ON',
    ("-DOBS_SDK_LIB=" + (Join-Path $sdkDir 'obs.lib')),
    ("-DOBS_SDK_GENERATED_DIR=" + $sdkDir),
    ("-DOBS_SOURCE_LIBOBS_DIR=" + $libobsHeaders),
    ("-DOBS_RUNTIME_BIN_DIR=" + (Join-Path $ObsDir 'bin\64bit'))
)
$configureOutput = (& $cmake @configureArgs 2>&1 | Out-String)
$configureExit = $LASTEXITCODE
Write-Log (Join-Path $logsDir 'configure.log') $configureOutput
Write-Host $configureOutput

$buildOutput = ''
$buildExit = 0
$showIncludesPatched = 0
if ($configureExit -eq 0) {
    $showIncludesPatched = Repair-NinjaShowIncludesPrefix $BuildDir $includePrefix
    Write-Host ("MSVC /showIncludes prefix patched in {0} Ninja file(s)" -f $showIncludesPatched)
    $buildArgs = @('--build', $BuildDir, '--target', 'obs-media-selftest',
        'audio-core-tests', 'framed-core-tests', 'framed-source-smoke',
        'stop-policy-tests', 'split-policy-tests', '--config', $Config)
    if ($RequireClean) { $buildArgs += '--clean-first' }
    $buildOutput = (& $cmake @buildArgs 2>&1 | Out-String)
    $buildExit = $LASTEXITCODE
    Write-Log (Join-Path $logsDir 'build.log') $buildOutput
    Write-Host (($buildOutput -split "`r?`n" | Where-Object { $_ -notmatch '包含文件|including file' }) -join [Environment]::NewLine)
}

$exePath = Join-Path (Join-Path $BuildDir 'bin') 'obs-media-selftest.exe'
$exeExists = Test-Path -LiteralPath $exePath
$sourceCommit = $provenance.Record.source.commit
$sourceTree = $provenance.Record.source.tree
$sourceDirty = $provenance.Record.source.dirty
$summary = [ordered]@{
    timestamp_utc        = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    repo_root            = $RepoRoot
    source_commit        = $sourceCommit
    source_tree_hash     = $sourceTree
    source_dirty         = $sourceDirty
    provenance_start     = $provenance.StartPath
    project_dir          = $projectDir
    build_dir            = $BuildDir
    obs_dir              = $ObsDir
    sdk_dir              = $sdkDir
    sdk_obs_lib          = $sdkSummary.lib_path
    sdk_obs_lib_sha256   = $sdkSummary.lib_sha256
    sdk_obs_dll_sha256   = $sdkSummary.obs_dll_sha256
    sdk_export_count     = $sdkSummary.obs_dll_exports_parsed
    generator            = 'Ninja'
    config               = $Config
    vcvars64             = $vcvars
    cl                   = $clPath
    cmake                = $cmake
    cmake_version        = $cmakeVersion
    ninja                = $ninja
    ninja_version        = $ninjaVersion
    include_prefix       = $includePrefix
    ninja_files_patched  = $showIncludesPatched
    toolchain_exit       = $toolchainExit
    sdk_gen_exit         = $sdkExit
    configure_exit       = $configureExit
    build_exit           = $buildExit
    exe_path             = $exePath
    exe_exists           = $exeExists
    exe_sha256           = $null
    exe_size_bytes       = $null
    staged_runtime_dlls  = @(Get-ChildItem -LiteralPath (Join-Path $BuildDir 'bin') -Filter *.dll -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name)
}
if ($exeExists) {
    $summary.exe_sha256 = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $summary.exe_size_bytes = (Get-Item -LiteralPath $exePath).Length
}
$summaryPath = Join-Path $BuildDir 'build_media_summary.json'
$summary | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $summaryPath -Encoding UTF8

Write-Host ''
Write-Host ("toolchain_exit={0} sdk_gen_exit={1} configure_exit={2} build_exit={3}" -f $toolchainExit, $sdkExit, $configureExit, $buildExit)
Write-Host ("exe={0} exists={1}" -f $exePath, $exeExists)
Write-Host ("summary={0}" -f $summaryPath)

if ($toolchainExit -ne 0 -or $sdkExit -ne 0 -or $configureExit -ne 0 -or $buildExit -ne 0 -or -not $exeExists) {
    exit 1
}
$artifacts = @(Get-ChildItem -LiteralPath (Join-Path $BuildDir 'bin') -File |
    Where-Object { $_.Extension -in '.exe', '.dll' } | Select-Object -ExpandProperty FullName)
$completed = Complete-A3BuildProvenance $provenance $artifacts
$summary['provenance'] = $completed.Path
$summary['provenance_sha256'] = $completed.Sha256
$summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $summaryPath -Encoding UTF8
exit 0
