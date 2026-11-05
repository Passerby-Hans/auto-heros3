# S0/S1a evidence run for obs-recorder: build, --capabilities, IPC self-test and
# a real two-process named-pipe test.  No OBS GUI, no recording, no audio.
#
# Usage:
#   pwsh -NoProfile -ExecutionPolicy Bypass -File platform\obs-recorder\scripts\s0-smoke.ps1
#   ... -SkipBuild          reuse the existing build
#   ... -EvidenceDir <dir>  explicit output directory
[CmdletBinding()]
param(
    [string]$RepoRoot = '',
    [string]$EvidenceDir = '',
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'

function Write-JsonFile([string]$Path, $Object) {
    $Object | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Get-ObsRadioactiveCheck {
    $processes = @(Get-Process obs64,obs -ErrorAction SilentlyContinue | Select-Object -ExpandProperty ProcessName -Unique)
    return $processes
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = (Resolve-Path (Join-Path $scriptDir '..')).Path
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $projectDir '..\..')).Path }
$timestamp = Get-Date -Format 'yyyyMMdd-HHmm'
if (-not $EvidenceDir) {
    $EvidenceDir = Join-Path $RepoRoot ('var\runs\obs-native-s0-' + $timestamp)
}
$EvidenceDir = [System.IO.Path]::GetFullPath($EvidenceDir)
$logsDir = Join-Path $EvidenceDir 'logs'
New-Item -ItemType Directory -Force -Path $EvidenceDir | Out-Null
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

$buildDir = Join-Path $projectDir 'build'
$exe = Join-Path $buildDir 'bin\obs-recorder.exe'
$checks = New-Object System.Collections.Generic.List[object]
$commands = New-Object System.Collections.Generic.List[string]
$failures = 0

function Add-Check([string]$Name, [bool]$Ok, [string]$Detail) {
    $script:checks.Add([ordered]@{ name = $Name; ok = $Ok; detail = $Detail })
    if (-not $Ok) { $script:failures++ }
    Write-Host ("[{0}] {1}: {2}" -f $(if ($Ok) { 'PASS' } else { 'FAIL' }), $Name, $Detail)
}

Write-Host ("evidence_dir={0}" -f $EvidenceDir)

# --- build -------------------------------------------------------------------
$buildExit = $null
if ($SkipBuild) {
    Write-Host 'build: skipped (-SkipBuild)'
    Add-Check 'build_skipped' (Test-Path -LiteralPath $exe) 'reusing existing executable'
} else {
    $buildScript = Join-Path $projectDir 'scripts\build.ps1'
    $pwshPath = (Get-Command pwsh.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    if (-not $pwshPath) {
        $pwshPath = (Get-Command powershell.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    }
    if (-not $pwshPath) { throw 'no pwsh.exe/powershell.exe found to run build.ps1' }
    & $pwshPath -NoProfile -ExecutionPolicy Bypass -File $buildScript -RepoRoot $RepoRoot -BuildDir $buildDir
    $buildExit = $LASTEXITCODE
    Add-Check 'build' ($buildExit -eq 0) ("exit={0}" -f $buildExit)
}
$commands.Add('build: pwsh -NoProfile -ExecutionPolicy Bypass -File ' + (Join-Path $projectDir 'scripts\build.ps1'))
if (-not (Test-Path -LiteralPath $exe)) {
    throw ("executable missing: {0}" -f $exe)
}
foreach ($name in @('build.log', 'configure.log', 'toolchain.log')) {
    $source = Join-Path $buildDir ('logs\' + $name)
    if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination (Join-Path $logsDir $name) -Force }
}
$buildSummarySource = Join-Path $buildDir 'build_summary.json'
if (Test-Path -LiteralPath $buildSummarySource) {
    Copy-Item -LiteralPath $buildSummarySource -Destination (Join-Path $EvidenceDir 'build_summary.json') -Force
}
$buildSummary = $null
if (Test-Path -LiteralPath (Join-Path $EvidenceDir 'build_summary.json')) {
    $buildSummary = Get-Content -LiteralPath (Join-Path $EvidenceDir 'build_summary.json') -Raw | ConvertFrom-Json
}

# --- capabilities ------------------------------------------------------------
$capStdout = Join-Path $logsDir 'capabilities.stdout.log'
$capStderr = Join-Path $logsDir 'capabilities.stderr.log'
$capJson = Join-Path $EvidenceDir 'capabilities.json'
$commands.Add(('capabilities: "{0}" --capabilities --json-out "{1}"' -f $exe, $capJson))
& $exe --capabilities --json-out $capJson > $capStdout 2> $capStderr
$capExit = $LASTEXITCODE
Add-Check 'capabilities_exit_code' ($capExit -eq 0) ("exit={0}" -f $capExit)
$caps = $null
if (Test-Path -LiteralPath $capJson) {
    try {
        $caps = Get-Content -LiteralPath $capJson -Raw | ConvertFrom-Json
        Add-Check 'capabilities_json_valid' $true $capJson
    } catch {
        Add-Check 'capabilities_json_valid' $false ("parse error: {0}" -f $_)
    }
}
if ($caps) {
    Add-Check 'obs_dll_loaded' ([bool]$caps.obs.dynamic_load.loaded) ("dll={0}" -f $caps.obs.dll_path)
    Add-Check 'obs_file_version' ($caps.obs.file_version.string -eq '32.1.0') ("file_version={0}" -f $caps.obs.file_version.string)
    Add-Check 'obs_version_call' ([bool]($caps.obs.version_accessors.get_version_string -eq '32.1.0')) ("obs_get_version_string={0}" -f $caps.obs.version_accessors.get_version_string)
    Add-Check 'sdk_headers_absent' (-not [bool]$caps.obs.sdk.sdk_headers) ("sdk_headers={0}; sdk_import_lib={1}" -f $caps.obs.sdk.sdk_headers, $caps.obs.sdk.sdk_import_lib)
    Add-Check 'obs_startup_not_called' (-not [bool]$caps.obs.startup.obs_startup_called) 'obs_startup_called=false'
    Add-Check 'media_not_started' (-not [bool]$caps.capability_matrix.media_active) 'media_active=false'
}

# --- IPC in-process self-test ------------------------------------------------
$selfTestLog = Join-Path $EvidenceDir 'ipc_selftest.jsonl'
$selfTestErr = Join-Path $logsDir 'ipc_selftest.stderr.log'
$commands.Add(('self-test: "{0}" --self-test' -f $exe))
& $exe --self-test > $selfTestLog 2> $selfTestErr
$selfTestExit = $LASTEXITCODE
Add-Check 'ipc_selftest_exit_code' ($selfTestExit -eq 0) ("exit={0}" -f $selfTestExit)
$selfTestLines = @(Get-Content -LiteralPath $selfTestLog -ErrorAction SilentlyContinue)
$selfTestResult = $null
foreach ($line in $selfTestLines) {
    if ($line -match '"event":"selftest_result"') {
        $selfTestResult = $line | ConvertFrom-Json
    }
}
if ($selfTestResult) {
    Add-Check 'ipc_selftest_result' ([bool]$selfTestResult.ok) ("pipe={0}" -f $selfTestResult.pipe)
} else {
    Add-Check 'ipc_selftest_result' $false 'selftest_result line missing'
}
$rxCount = @($selfTestLines | Where-Object { $_ -match '"event":"rx"' }).Count
Add-Check 'ipc_selftest_responses' ($rxCount -eq 3) ("hello/status/shutdown responses={0}" -f $rxCount)

# --- IPC two-process test ----------------------------------------------------
$pipeName = '\\.\pipe\auto-heros3-obs-recorder-s0-' + $timestamp
$serverOut = Join-Path $logsDir 'ipc_server.stdout.jsonl'
$serverErr = Join-Path $logsDir 'ipc_server.stderr.log'
$probeLog = Join-Path $EvidenceDir 'ipc_client.probe.jsonl'
$commands.Add(('serve: "{0}" --serve --pipe "{1}" --idle-timeout-ms 30000 --read-timeout-ms 15000' -f $exe, $pipeName))
$commands.Add(('ipc-probe: "{0}" --ipc-probe --pipe "{1}"' -f $exe, $pipeName))
$server = Start-Process -FilePath $exe `
    -ArgumentList @('--serve', '--pipe', $pipeName, '--idle-timeout-ms', '30000', '--read-timeout-ms', '15000') `
    -RedirectStandardOutput $serverOut -RedirectStandardError $serverErr -PassThru -NoNewWindow
Start-Sleep -Milliseconds 400
& $exe --ipc-probe --pipe $pipeName > $probeLog 2> (Join-Path $logsDir 'ipc_probe.stderr.log')
$probeExit = $LASTEXITCODE
$serverExited = $server.WaitForExit(30000)
if (-not $serverExited) { Stop-Process -Id $server.Id -Force }
$serverExit = if ($serverExited) { $server.ExitCode } else { -1 }
Add-Check 'ipc_server_exit_code' ($serverExit -eq 0) ("exit={0}" -f $serverExit)
Add-Check 'ipc_probe_exit_code' ($probeExit -eq 0) ("exit={0}" -f $probeExit)
$probeLines = @(Get-Content -LiteralPath $probeLog -ErrorAction SilentlyContinue)
$probeRx = @($probeLines | Where-Object { $_ -match '"event":"rx"' }).Count
$probeSummary = $null
foreach ($line in $probeLines) {
    if ($line -match '"event":"probe_summary"') { $probeSummary = $line | ConvertFrom-Json }
}
Add-Check 'ipc_probe_responses' ($probeRx -eq 3) ("responses={0}" -f $probeRx)
if ($probeSummary) {
    Add-Check 'ipc_probe_summary_ok' ([bool]$probeSummary.ok) ("responses_ok={0} failures={1}" -f $probeSummary.responses_ok, $probeSummary.failures)
}
$serverLines = @(Get-Content -LiteralPath $serverOut -ErrorAction SilentlyContinue)
$serverStopped = @($serverLines | Where-Object { $_ -match '"event":"stopping"' }).Count
Add-Check 'ipc_server_clean_stop' ($serverStopped -eq 1) ("stopping_events={0}" -f $serverStopped)
Add-Check 'ipc_server_loaded_obs' ((@($serverLines | Where-Object { $_ -match '"obs_dll_loaded":true' }).Count) -ge 1) 'server reported dynamic obs.dll load'

# --- no OBS GUI / media side effects ----------------------------------------
$obsProcesses = @(Get-ObsRadioactiveCheck)
Add-Check 'no_obs_gui_process' ($obsProcesses.Count -eq 0) ("obs64/obs processes={0}" -f ($obsProcesses -join ','))

# --- environment + summary ---------------------------------------------------
$gitHead = (& git -C $RepoRoot rev-parse HEAD 2>$null)
$gitBranch = (& git -C $RepoRoot branch --show-current 2>$null)
$gitStatus = @(& git -C $RepoRoot status --porcelain 2>$null)
$obsDll = 'C:\Program Files\obs-studio\bin\64bit\obs.dll'
$obsVersion = $null
if (Test-Path -LiteralPath $obsDll) { $obsVersion = (Get-Item -LiteralPath $obsDll).VersionInfo.FileVersion }
$environment = [ordered]@{
    timestamp_utc  = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    host           = [ordered]@{
        os_product_name = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').ProductName
        os_display_version = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').DisplayVersion
        os_build = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').CurrentBuildNumber
    }
    repo           = [ordered]@{
        root = $RepoRoot
        head = "$gitHead"
        branch = "$gitBranch"
        dirty_entries = $gitStatus.Count
    }
    obs_install    = [ordered]@{
        install_dir = 'C:\Program Files\obs-studio'
        dll_path = $obsDll
        dll_file_version = "$obsVersion"
        sdk_headers = $false
    }
    obs_recorder   = [ordered]@{
        exe_path = $exe
        exe_sha256 = $(if (Test-Path -LiteralPath $exe) { (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant() } else { $null })
        exe_size_bytes = $(if (Test-Path -LiteralPath $exe) { (Get-Item -LiteralPath $exe).Length } else { $null })
    }
}
Write-JsonFile (Join-Path $EvidenceDir 'environment.json') $environment
$commands | Set-Content -LiteralPath (Join-Path $EvidenceDir 'commands.txt') -Encoding UTF8

$summary = [ordered]@{
    schema     = 'auto-heros3.obs-native-s0.evidence/v1'
    timestamp_utc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    ok         = ($failures -eq 0)
    failures   = $failures
    build      = [ordered]@{ exit = $buildExit; summary = $buildSummary }
    capabilities = [ordered]@{ exit = $capExit }
    ipc        = [ordered]@{
        selftest_exit = $selfTestExit
        selftest_pipe = $selfTestResult.pipe
        probe_exit = $probeExit
        server_exit = $serverExit
        pipe = $pipeName
        responses = $probeRx
    }
    checks     = $checks
}
if ($caps) {
    $summary.capabilities = [ordered]@{
        exit = $capExit
        file_version = $caps.obs.file_version.string
        dynamic_load_loaded = $caps.obs.dynamic_load.loaded
        obs_get_version_string = $caps.obs.version_accessors.get_version_string
        obs_get_version_uint = $caps.obs.version_accessors.get_version_uint_hex
        sdk_headers = $caps.obs.sdk.sdk_headers
        sdk_import_lib = $caps.obs.sdk.sdk_import_lib
        obs_startup_called = $caps.obs.startup.obs_startup_called
        media_active = $caps.capability_matrix.media_active
        not_probed = $caps.capability_matrix.not_probed
    }
}
Write-JsonFile (Join-Path $EvidenceDir 'summary.json') $summary

Write-Host ''
Write-Host ("checks: {0} total, {1} failed" -f $checks.Count, $failures)
Write-Host ("evidence: {0}" -f $EvidenceDir)
if ($failures -eq 0) { exit 0 }
exit 1
