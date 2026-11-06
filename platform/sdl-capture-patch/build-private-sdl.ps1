[CmdletBinding()]
param(
    [string]$RepoRoot = '',
    [string]$SdlDir = '',
    [string]$BuildDir = '',
    [string]$ObsDir = ''
)
$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $scriptDir '..\..')).Path }
if (-not $SdlDir) { $SdlDir = Join-Path $RepoRoot 'vendor\sdl-2.32.10' }
if (-not $BuildDir) { $BuildDir = Join-Path $scriptDir 'build' }
$SdlDir = (Resolve-Path $SdlDir).Path
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null

function Find-Tool([string[]]$Candidates) {
    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return ''
}

$vcvars = ''
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (Test-Path -LiteralPath $vswhere) {
    $install = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
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
if (-not $vcvars) { throw 'vcvars64.bat not found' }
$cmake = Find-Tool @('C:\msys64\mingw64\bin\cmake.exe', (Get-Command cmake.exe -ErrorAction SilentlyContinue | ForEach-Object { $_.Source }))
$ninja = Find-Tool @('C:\msys64\mingw64\bin\ninja.exe', (Get-Command ninja.exe -ErrorAction SilentlyContinue | ForEach-Object { $_.Source }))
if (-not $cmake) { throw 'cmake.exe not found' }
if (-not $ninja) { throw 'ninja.exe not found' }

# Apply the tracked backend patch to the ignored SDL source tree.
$python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = 'python.exe' }
& $python (Join-Path $scriptDir 'apply.py')
if ($LASTEXITCODE -ne 0) { throw 'SDL backend patch failed' }

$envBat = Join-Path $BuildDir 'vcvars_env.bat'
Set-Content -LiteralPath $envBat -Encoding ASCII -Value @(
    '@echo off', 'setlocal', ('call "' + $vcvars + '" >nul 2>&1'),
    'if errorlevel 1 exit /b 101', 'set')
$envOutput = & cmd.exe /d /c ('"' + $envBat + '"') 2>&1
if ($LASTEXITCODE -ne 0) { throw 'vcvars64.bat failed' }
foreach ($line in $envOutput) {
    $idx = $line.IndexOf('=')
    if ($idx -le 0) { continue }
    $name = $line.Substring(0, $idx)
    if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_()]*$') { continue }
    try { [Environment]::SetEnvironmentVariable($name, $line.Substring($idx + 1), 'Process') } catch { }
}

# CMake passes the localized /showIncludes prefix to rc.exe on Windows.  Set
# the same cache contract used by the native OBS build so the version resource
# is not invoked with the prefix as an extra argument.
$probeSrc = Join-Path $BuildDir '_show_probe.cpp'
$probeObj = Join-Path $BuildDir '_show_probe.obj'
$probeOut = Join-Path $BuildDir '_show_probe.txt'
@('#include <cstdio>', 'int main() { return 0; }') | Set-Content -LiteralPath $probeSrc -Encoding ASCII
& cmd.exe /d /c ('cl.exe /nologo /showIncludes /c /Fo"' + $probeObj + '" "' + $probeSrc + '" > "' + $probeOut + '" 2>&1') | Out-Null
$includePrefix = 'Note: including file:'
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
Write-Host ("showIncludes prefix: '{0}'" -f $includePrefix)

$configure = @(
    '-S', $SdlDir, '-B', $BuildDir, '-G', 'Ninja',
    '-DCMAKE_BUILD_TYPE=Release',
    ('-DCMAKE_MAKE_PROGRAM=' + $ninja),
    ('-DCMAKE_CL_SHOWINCLUDES_PREFIX=' + $includePrefix),
    '-DSDL_SHARED=ON', '-DSDL_STATIC=OFF',
    '-DSDL_TEST=OFF', '-DSDL_TESTS=OFF', '-DSDL_INSTALL=OFF',
    '-DSDL_AUDIO=ON', '-DSDL_DISKAUDIO=OFF',
    '-DSDL_A3CAPTUREAUDIO=ON',
    '-DSDL_WERROR=OFF'
)
& $cmake @configure
if ($LASTEXITCODE -ne 0) { throw 'SDL configure failed' }
& $cmake --build $BuildDir --target SDL2 --config Release
if ($LASTEXITCODE -ne 0) { throw 'SDL build failed' }

$dll = Get-ChildItem -Path $BuildDir -Filter 'SDL2.dll' -Recurse | Select-Object -First 1
if (-not $dll) { throw 'SDL2.dll not found in build output' }
$stage = Join-Path $scriptDir 'stage'
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Copy-Item -LiteralPath $dll.FullName -Destination (Join-Path $stage 'SDL2.dll') -Force
$hash = (Get-FileHash -LiteralPath (Join-Path $stage 'SDL2.dll') -Algorithm SHA256).Hash.ToLowerInvariant()
$info = [ordered]@{
    built_at_utc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    source_dir = $SdlDir
    source_tag = 'release-2.32.10'
    output = (Join-Path $stage 'SDL2.dll')
    sha256 = $hash
    bytes = (Get-Item -LiteralPath (Join-Path $stage 'SDL2.dll')).Length
    build_dir = $BuildDir
    backend = 'a3capture'
}
$info | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $stage 'build.json') -Encoding UTF8
Write-Host ('SDL2.dll -> {0} sha256={1}' -f (Join-Path $stage 'SDL2.dll'), $hash)

# A2 smoke: explicitly load the private DLL, select a3capture, open one second
# of s16/44100/stereo and verify the backend produced PCM without a device.
$cl = (Get-Command cl.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $cl) { throw 'cl.exe not available for a3capture smoke' }
$smoke = Join-Path $stage 'a3capture-smoke.exe'
& cl.exe /nologo /W3 /O2 /Fe:"$smoke" (Join-Path $scriptDir 'tests\a3capture_smoke.c')
if ($LASTEXITCODE -ne 0) { throw 'a3capture smoke compile failed' }
$raw = Join-Path $stage 'a3capture-smoke.raw'
$smokeLog = Join-Path $stage 'a3capture-smoke.log'
& $smoke (Join-Path $stage 'SDL2.dll') $raw *> $smokeLog
$smokeExit = $LASTEXITCODE
Get-Content -LiteralPath $smokeLog
$info['smoke_exit'] = $smokeExit
$info['smoke_log'] = $smokeLog
$info['smoke_raw'] = $raw
$info['smoke_raw_bytes'] = if (Test-Path -LiteralPath $raw) { (Get-Item -LiteralPath $raw).Length } else { 0 }
$info | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $stage 'build.json') -Encoding UTF8
if ($smokeExit -ne 0) { throw "a3capture smoke failed (exit $smokeExit)" }
Write-Host 'A2 a3capture smoke PASS'
