# Build the patched VCMI (MSVC + Ninja + Conan deps).  Session 14 (2026-10-21).
#
# Why a script:
#  * MSVC env must come from vcvars64.bat (PowerShell does not inherit it)
#  * Conan runtime env (Qt tools + dependency DLLs) comes from conanrun.ps1
#  * cmake.exe here is MSYS2's -> it rewrites POSIX-looking args; disable that
#  * source patches live in platform/vcmi-patch/apply.py (single source of truth)
#
# Usage:
#   pwsh -NoProfile -File tools/vcmi_build.ps1                 # client + server
#   pwsh -NoProfile -File tools/vcmi_build.ps1 -Targets VCMI_client.exe -Jobs 16
#   pwsh -NoProfile -File tools/vcmi_build.ps1 -Configure       # (re)configure only
param(
    [string[]]$Targets = @('VCMI_client.exe', 'VCMI_server.exe'),
    [int]$Jobs = 32,
    [switch]$Configure,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$src = Join-Path $root 'vendor\vcmi-src'
$vs = 'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools'
$vcvars = Join-Path $vs 'VC\Auxiliary\Build\vcvars64.bat'
$ninja = 'C:\msys64\mingw64\bin\ninja.exe'
$log = Join-Path $root 'var\logs\vcmi-build.log'

if (-not (Test-Path $vcvars)) { throw "vcvars64.bat not found: $vcvars" }
if ($Clean) { Remove-Item (Join-Path $src 'build') -Recurse -Force -ErrorAction SilentlyContinue }

# ---- source patches (single source of truth: platform/vcmi-patch/apply.py) ----
$applyPy = Join-Path $root 'platform\vcmi-patch\apply.py'
$py = Join-Path $root '.venv\Scripts\python.exe'
if (Test-Path $applyPy) {
    & $py $applyPy
    if ($LASTEXITCODE -ne 0) { throw "apply.py failed: $LASTEXITCODE" }
}

# ---- environment -----------------------------------------------------------
$envs = cmd /c "`"$vcvars`" >nul 2>&1 && set"
foreach ($line in $envs) {
    if ($line -match '^([^=]+)=(.*)$') {
        Set-Item -Path ("env:" + $matches[1]) -Value $matches[2] -ErrorAction SilentlyContinue
    }
}
$env:VSLANG = '1033'               # English MSVC messages (avoid GBK mojibake)
$env:MSYS2_ARG_CONV_EXCL = '*'
if (-not (Get-Command cl -ErrorAction SilentlyContinue)) { throw 'cl.exe not on PATH after vcvars' }

Push-Location $src
try {
    if (Test-Path 'conan-msvc\conanrun.ps1') { . .\conan-msvc\conanrun.ps1 }

    & 'C:\msys64\mingw64\bin\cmake.exe' -S . -B build -G Ninja `
        -DCMAKE_MAKE_PROGRAM=C:/msys64/mingw64/bin/ninja.exe `
        -DCMAKE_C_COMPILER=cl -DCMAKE_CXX_COMPILER=cl `
        --toolchain conan-msvc/conan_toolchain.cmake `
        -DCMAKE_BUILD_TYPE=RelWithDebInfo 2>&1 | Select-Object -Last 4
    if ($LASTEXITCODE -ne 0) { throw "cmake configure failed: $LASTEXITCODE" }
    if ($Configure) { Write-Host 'configure-only done'; exit 0 }

    & $ninja -C build -j $Jobs @Targets 2>&1 | Tee-Object -FilePath $log | Select-Object -Last 25
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
Write-Host "build exit code: $code"
exit $code
