# Build the private test-only a3 obs-outputs module.
# Requires: OBS headers in vendor/obs-studio, platform/obs-recorder/build-media/sdk/obs.lib,
# and the staged private SDL2 build.  The output is loaded only when
# AUTO_HEROS3_OUTPUTS_DLL points at it.
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$src = Join-Path $root 'platform\obs-recorder\custom-output'
$out = Join-Path $root 'platform\obs-recorder\bin'
$work = Join-Path $root 'platform\obs-recorder\build-media\a3outputs'
New-Item -ItemType Directory -Force -Path $out,$work | Out-Null
$obs = Join-Path $root 'platform\obs-recorder\build-media\sdk\obs.lib'
if (-not (Test-Path -LiteralPath $obs)) { throw "missing $obs" }
$vcvars = 'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat'
if (-not (Test-Path -LiteralPath $vcvars)) {
  $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
  $install = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
  $vcvars = Join-Path $install 'VC\Auxiliary\Build\vcvars64.bat'
}
$def = Join-Path $work 'w32p.def'
@"
LIBRARY w32-pthreads.dll
EXPORTS
pthread_mutex_init
pthread_mutex_destroy
pthread_mutex_lock
pthread_mutex_unlock
pthread_once
pthread_create
pthread_join
pthread_detach
"@ | Set-Content -Encoding ASCII $def
$lib = Join-Path $work 'w32-pthreads.lib'
cmd /d /c "call `"$vcvars`" >nul && lib /nologo /def:`"$def`" /machine:x64 /out:`"$lib`" >nul" 
if ($LASTEXITCODE -ne 0) { throw 'w32-pthreads import lib failed' }
$sources = @(
  (Join-Path $src 'plugin.c'), (Join-Path $src 'mp4-output.c'), (Join-Path $src 'mp4-mux.c'),
  (Join-Path $root 'vendor\obs-studio\plugins\obs-outputs\rtmp-hevc.c'),
  (Join-Path $root 'vendor\obs-studio\plugins\obs-outputs\rtmp-av1.c'),
  (Join-Path $root 'vendor\obs-studio\shared\bpm\bpm.c'),
  (Join-Path $root 'vendor\obs-studio\shared\opts-parser\opts-parser.c')
)
foreach ($f in 'caption.c','cea708.c','mpeg.c','eia608.c','eia608_from_utf8.c','eia608_charmap.c','utf8.c','xds.c') {
  $sources += (Join-Path $root "vendor\obs-studio\deps\libcaption\src\$f")
}
$incs = @(
  (Join-Path $root 'vendor\obs-studio\libobs'),
  (Join-Path $root 'platform\obs-recorder\build-media\sdk'),
  (Join-Path $root 'vendor\obs-studio\plugins\obs-outputs'),
  (Join-Path $root 'vendor\obs-studio\shared\bpm'),
  (Join-Path $root 'vendor\obs-studio\shared\opts-parser'),
  (Join-Path $root 'vendor\obs-studio\deps\w32-pthreads'),
  (Join-Path $root 'vendor\obs-studio\deps\libcaption'),
  (Join-Path $root 'vendor\obs-studio\deps\libcaption\caption'),
  (Join-Path $root 'vendor\obs-studio\deps\libcaption\src')
) | ForEach-Object { '/I"' + $_ + '"' }
$args = ($incs -join ' ') + ' ' + (($sources | ForEach-Object { '"' + $_ + '"' }) -join ' ') +
        ' "' + $obs + '" "' + $lib + '" /Fo:"' + $work + '\\" /Fe:"' + (Join-Path $out 'a3-obs-outputs.dll') + '" /link'
cmd /d /c "call `"$vcvars`" >nul && cl /nologo /LD /MD /W3 $args"
if ($LASTEXITCODE -ne 0) { throw 'private outputs build failed' }
Write-Host ('PASS ' + (Join-Path $out 'a3-obs-outputs.dll'))

$dll = Join-Path $out 'a3-obs-outputs.dll'
$manifest = [ordered]@{
    built_at_utc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    dll = $dll
    sha256 = (Get-FileHash -LiteralPath $dll -Algorithm SHA256).Hash.ToLowerInvariant()
    bytes = (Get-Item -LiteralPath $dll).Length
    obs_dll = $obs
    obs_dll_sha256 = (Get-FileHash -LiteralPath $obs -Algorithm SHA256).Hash.ToLowerInvariant()
    compiler = $vcvars
    sources = $sources
    source_root = $root
    source_dirty = [bool]((& git -C $root status --porcelain 2>$null | Out-String).Trim())
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $out 'a3-obs-outputs.build.json') -Encoding UTF8
