# S3a evidence runner: named-pipe IPC control of the libobs media pipeline.
#
# Starts `obs-media-selftest.exe --serve --pipe <name>` (libobs initialised once),
# connects a byte-mode named-pipe JSON-lines client, and drives:
#   hello / capabilities / invalid-json / unknown-command
#   start-before-prepare (explicit error) / stop-before-prepare (idempotent)
#   prepare with a bad window (error) / prepare with a missing raw file (error)
#   prepare (synthetic Tk window + growing raw tone) -> start -> start (idempotent)
#   status while recording -> auto-stop after duration_ms -> status -> stop (idempotent)
#   shutdown -> process exit
# A second short server/session tests an explicit stop() (duration_ms=0).
#
# Validates for each produced out_dir: selftest.json pass, manifest.json files,
# split MP4s (h264 1920x1080@60 + AAC 44100/2, decode clean, non-black/changing
# video, non-silent tone), no MMDevAPI/audioses/wdmaud/win-wasapi in the live
# server process, and audio endpoint before/after identical.
#
# No obs64/UI/WebSocket, no game, no playback, no ffplay.
[CmdletBinding()]
param(
    [string]$RepoRoot = '',
    [string]$BuildDir = '',
    [string]$ObsDir = 'C:\Program Files\obs-studio',
    [string]$RunRoot = '',
    [string]$PythonExe = 'C:\Python315\python.exe',
    [string]$PythonwExe = 'C:\Python315\pythonw.exe',
    [string]$VenvPython = '',
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()
function Fail([string]$m) { Write-Host ("FATAL: " + $m); exit 1 }

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = (Resolve-Path (Join-Path $scriptDir '..')).Path
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $projectDir '..\..')).Path }
if (-not $BuildDir) { $BuildDir = Join-Path $projectDir 'build-media' }
$BuildDir = [System.IO.Path]::GetFullPath($BuildDir)
if (-not $RunRoot) { $RunRoot = Join-Path (Join-Path $RepoRoot 'var\runs') '' }
if (-not $VenvPython) { $VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe' }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$runDir = Join-Path $RunRoot ("obs-ipc-s3-" + $stamp)
$logsDir = Join-Path $runDir 'logs'
$captureA = Join-Path $runDir 'capture-auto-stop'
$captureB = Join-Path $runDir 'capture-manual-stop'
$tmpDir = Join-Path $runDir 'tmp'
foreach ($d in @($runDir, $logsDir, $captureA, $captureB, $tmpDir)) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
$runDir = (Resolve-Path -LiteralPath $runDir).Path
$captureA = Join-Path $runDir 'capture-auto-stop'
$captureB = Join-Path $runDir 'capture-manual-stop'

$checks = New-Object System.Collections.Generic.List[object]
function Add-Check([string]$name, [bool]$ok, [string]$detail) {
    $script:checks.Add([ordered]@{ name = $name; ok = $ok; detail = $detail })
    Write-Host ("CHECK {0,-46} {1}  {2}" -f $name, $(if ($ok) { 'PASS' } else { 'FAIL' }), $detail)
}
function All-Checks { foreach ($c in $script:checks) { if (-not $c.ok) { return $false } }; return $true }

# --- prerequisites / build ---------------------------------------------------
$ffprobe = (Get-Command ffprobe.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
$ffmpeg = (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $ffprobe) { Fail 'ffprobe.exe not found on PATH' }
if (-not $ffmpeg) { Fail 'ffmpeg.exe not found on PATH' }
foreach ($py in @($PythonExe, $PythonwExe, $VenvPython)) { if (-not (Test-Path -LiteralPath $py)) { Fail ("python missing: " + $py) } }

if (-not $SkipBuild) {
    Write-Host '=== building obs-media-selftest.exe ==='
    & (Join-Path $scriptDir 'build-media-selftest.ps1') -RepoRoot $RepoRoot -BuildDir $BuildDir -ObsDir $ObsDir
    if ($LASTEXITCODE -ne 0) { Fail ("build failed: " + $LASTEXITCODE) }
} else { Write-Host '=== reusing existing build ===' }
$exe = Join-Path (Join-Path $BuildDir 'bin') 'obs-media-selftest.exe'
if (-not (Test-Path -LiteralPath $exe)) { Fail ("exe missing: " + $exe) }
Add-Check 'build_exe_exists' $true $exe
$exeHash = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
if (Test-Path (Join-Path $BuildDir 'build_media_summary.json')) {
    Copy-Item (Join-Path $BuildDir 'build_media_summary.json') $logsDir
}

# --- endpoint helper ---------------------------------------------------------
$epCode = 'import json,sys;from pycaw.pycaw import AudioUtilities;s=AudioUtilities.GetSpeakers();e=s.EndpointVolume;open(sys.argv[1],"w",encoding="utf-8").write(json.dumps({"device":str(getattr(s,"FriendlyName","") or ""),"mute":bool(e.GetMute()),"volume":float(e.GetMasterVolumeLevelScalar())},ensure_ascii=False))'
function Get-Endpoint {
    $tmp = Join-Path $runDir ('endpoint-' + [guid]::NewGuid().ToString('N') + '.json')
    & $VenvPython -c $epCode $tmp 2>&1 | Out-Null
    $text = Get-Content -LiteralPath $tmp -Raw -Encoding UTF8
    Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue
    return $text.Trim()
}
$endpointBefore = Get-Endpoint
Add-Check 'endpoint_snapshot_before' ($endpointBefore -match 'device') $endpointBefore

# --- synthetic Tk window + growing raw tone ---------------------------------
$readyFile = Join-Path $tmpDir 'window-info.json'
Remove-Item -LiteralPath $readyFile -ErrorAction SilentlyContinue
$tk = Start-Process -FilePath $PythonwExe -ArgumentList ('"' + (Join-Path $scriptDir 'wgc-synthetic-window.py') + '" --ready-file "' + $readyFile + '"') -PassThru `
    -RedirectStandardOutput (Join-Path $logsDir 'tk.stdout.log') -RedirectStandardError (Join-Path $logsDir 'tk.stderr.log')
$deadline = (Get-Date).AddSeconds(15)
while (-not (Test-Path -LiteralPath $readyFile) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 200 }
if (-not (Test-Path -LiteralPath $readyFile)) { Fail 'Tk window did not become ready' }
$windowInfo = Get-Content -LiteralPath $readyFile -Raw | ConvertFrom-Json
Add-Check 'synthetic_window_ready' $true ("hwnd=" + $windowInfo.hwnd_hex + " pid=" + $windowInfo.pid)
$growingRaw = Join-Path $tmpDir 'growing.raw'
Remove-Item -LiteralPath $growingRaw -ErrorAction SilentlyContinue
$appender = Start-Process -FilePath $PythonExe -ArgumentList ('"' + (Join-Path $scriptDir 'gen-test-raw-tone.py') + '" --out "' + $growingRaw + '" --seconds 40 --stream') -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logsDir 'raw-stream.stdout.log') -RedirectStandardError (Join-Path $logsDir 'raw-stream.stderr.log')
$sd = (Get-Date).AddSeconds(10)
while (-not (Test-Path -LiteralPath $growingRaw) -and (Get-Date) -lt $sd) { Start-Sleep -Milliseconds 100 }
Start-Sleep -Milliseconds 1200
$rawSizeAtPrepare = if (Test-Path $growingRaw) { (Get-Item $growingRaw).Length } else { 0 }
Add-Check 'growing_raw_ready' ($rawSizeAtPrepare -gt 0) ("bytes=" + $rawSizeAtPrepare)

# --- IPC client plumbing -----------------------------------------------------
$script:pipeSeq = 0
$script:reqSeq = 0
$script:events = New-Object System.Collections.Generic.List[object]
$script:reader = $null
$script:writer = $null
$script:pipe = $null

function Connect-Ipc([string]$pipeName) {
    $script:pipe = New-Object System.IO.Pipes.NamedPipeClientStream('.', $pipeName, [System.IO.Pipes.PipeDirection]::InOut)
    $script:pipe.Connect(15000)
    $script:writer = New-Object System.IO.StreamWriter($script:pipe)
    $script:writer.AutoFlush = $true
    $script:reader = New-Object System.IO.StreamReader($script:pipe)
    $script:reqSeq = 0
}
function Read-IpcLine([int]$timeoutMs = 20000) {
    $task = $script:reader.ReadLineAsync()
    if (-not $task.Wait($timeoutMs)) { throw ('IPC read timeout after ' + $timeoutMs + ' ms') }
    return $task.Result
}
function Invoke-Ipc([string]$cmd, [hashtable]$fields = @{}) {
    $script:reqSeq++
    $req = [ordered]@{ request_id = ('req-' + $script:reqSeq); cmd = $cmd }
    foreach ($k in $fields.Keys) { $req[$k] = $fields[$k] }
    $json = $req | ConvertTo-Json -Compress -Depth 8
    $script:writer.WriteLine($json)
    $deadline = (Get-Date).AddSeconds(25)
    while ((Get-Date) -lt $deadline) {
        $line = Read-IpcLine 20000
        $obj = $line | ConvertFrom-Json
        if ($obj.PSObject.Properties.Name -contains 'event') { $script:events.Add($obj); continue }
        if ($obj.request_id -ne $req.request_id) { throw ("unexpected response id " + $obj.request_id + " expected " + $req.request_id) }
        return $obj
    }
    throw 'IPC response timeout'
}
function Invoke-Raw([string]$rawLine) {
    $script:writer.WriteLine($rawLine)
    $line = Read-IpcLine 20000
    return ($line | ConvertFrom-Json)
}
function Wait-IpcEvent([string]$name, [int]$timeoutMs = 20000) {
    $deadline = (Get-Date).AddMilliseconds($timeoutMs)
    while ((Get-Date) -lt $deadline) {
        $line = Read-IpcLine $timeoutMs
        $obj = $line | ConvertFrom-Json
        if ($obj.PSObject.Properties.Name -contains 'event') {
            $script:events.Add($obj)
            if ($obj.event -eq $name) { return $obj }
        }
    }
    throw ('event timeout: ' + $name)
}

function Start-Server([string]$pipeLeaf, [string]$tag) {
    $stdout = Join-Path $logsDir ($tag + '-server.stdout.log')
    $stderr = Join-Path $logsDir ($tag + '-server.stderr.log')
    $proc = Start-Process -FilePath $exe -ArgumentList ('--serve --pipe ' + $pipeLeaf) -PassThru -NoNewWindow `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if ((Test-Path $stdout) -and (Select-String -Path $stdout -Pattern 'SERVE_READY' -Quiet)) { break }
        Start-Sleep -Milliseconds 200
    }
    if (-not (Select-String -Path $stdout -Pattern 'SERVE_READY' -Quiet)) { throw ('server did not become ready: ' + $tag) }
    return [ordered]@{ proc = $proc; stdout = $stdout; stderr = $stderr; pipe = $pipeLeaf }
}
function Stop-Server([hashtable]$srv) {
    try { if ($script:writer) { $script:writer.Close() } } catch { }
    try { if ($script:reader) { $script:reader.Close() } } catch { }
    try { if ($script:pipe) { $script:pipe.Close() } } catch { }
    if (-not $srv.proc.HasExited) {
        if (-not $srv.proc.WaitForExit(15000)) { Stop-Process -Id $srv.proc.Id -Force -ErrorAction SilentlyContinue }
    }
    return $srv.proc.ExitCode
}

# --- media validation --------------------------------------------------------
function Get-Probe([string]$file) {
    $json = & $ffprobe -v error -print_format json -show_format -show_streams $file 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($json | ConvertFrom-Json)
}
function Get-MaxVolume([string]$file) {
    $out = & $ffmpeg -hide_banner -v info -nostdin -i $file -map 0:a:0 -af volumedetect -f null - 2>&1 | Out-String
    if ($out -match 'max_volume:\s*(-?inf|-?[0-9.]+)\s*dB') {
        return $(if ($Matches[1] -in @('-inf', 'inf')) { [double]::NegativeInfinity } else { [double]$Matches[1] })
    }
    return $null
}
function Get-Metrics([string]$file) {
    $res = [ordered]@{ yavg = $null; ymax = $null; unique = 0; frames = 0; maxvol = $null; decode = ''; decodeRc = 0 }
    foreach ($key in @('YAVG', 'YMAX')) {
        $out = & $ffmpeg -hide_banner -v info -nostdin -i $file -map 0:v:0 -frames:v 1 -vf "signalstats,metadata=print:key=lavfi.signalstats.$key" -f null - 2>&1 | Out-String
        $m = [regex]::Match($out, ('lavfi\.signalstats\.' + $key + '=([0-9.]+)'))
        if ($m.Success) { if ($key -eq 'YAVG') { $res.yavg = [double]$m.Groups[1].Value } else { $res.ymax = [double]$m.Groups[1].Value } }
    }
    $md5 = & $ffmpeg -v error -nostdin -i $file -map 0:v:0 -t 1 -f framemd5 - 2>&1 | Out-String
    $hashes = @([regex]::Matches($md5, '(?m)^\s*\d+\s*,.*?,\s*([0-9a-f]{32})\s*$') | ForEach-Object { $_.Groups[1].Value })
    $res.frames = $hashes.Count; $res.unique = ($hashes | Sort-Object -Unique).Count
    $out = & $ffmpeg -v error -nostdin -i $file -f null - 2>&1 | Out-String
    $res.decodeRc = $LASTEXITCODE; $res.decode = $out.Trim()
    $res.maxvol = Get-MaxVolume $file
    return $res
}
function Validate-Capture([string]$dir, [string]$label) {
    $files = @(Get-ChildItem -LiteralPath $dir -Filter *.mp4 | Sort-Object Name)
    Add-Check ($label + '_mp4_count') ($files.Count -ge 2) ("files=" + $files.Count)
    $probeBad = @(); $decodeBad = @(); $audioBad = @(); $videoBad = @(); $total = 0.0
    $rows = @()
    foreach ($f in $files) {
        $probe = Get-Probe $f.FullName
        if (-not $probe) { $probeBad += $f.Name; continue }
        $v = @($probe.streams | Where-Object codec_type -eq 'video') | Select-Object -First 1
        $a = @($probe.streams | Where-Object codec_type -eq 'audio') | Select-Object -First 1
        if (-not $v -or -not $a) { $probeBad += ($f.Name + ':stream'); continue }
        if ($v.codec_name -ne 'h264' -or [int]$v.width -ne 1920 -or [int]$v.height -ne 1080 -or $v.avg_frame_rate -ne '60/1') { $probeBad += ($f.Name + ':video') }
        if ($a.codec_name -ne 'aac' -or [int]$a.sample_rate -ne 44100 -or [int]$a.channels -ne 2) { $probeBad += ($f.Name + ':audio') }
        $dur = [double]$probe.format.duration; $total += $dur
        $m = Get-Metrics $f.FullName
        if ($m.decodeRc -ne 0 -or $m.decode.Length -gt 0) { $decodeBad += ($f.Name + ':' + $m.decode) }
        if ($null -eq $m.maxvol -or $m.maxvol -le -40.0) { $audioBad += ($f.Name + ':' + $m.maxvol) }
        if (($null -eq $m.ymax) -or $m.ymax -lt 200 -or ($null -eq $m.yavg) -or $m.yavg -lt 20 -or $m.frames -eq 0 -or ($m.unique / $m.frames) -lt 0.8) {
            $videoBad += ($f.Name + ':yavg=' + $m.yavg + ',ymax=' + $m.ymax + ',uniq=' + $m.unique + '/' + $m.frames)
        }
        $rows += [ordered]@{ name = $f.Name; size = $f.Length; duration_s = [Math]::Round($dur, 3); max_volume_db = $m.maxvol; yavg = $m.yavg; ymax = $m.ymax; unique_frames = ($m.unique.ToString() + '/' + $m.frames) }
    }
    Add-Check ($label + '_streams') ($probeBad.Count -eq 0) ($(if ($probeBad.Count) { $probeBad -join ';' } else { 'h264 1920x1080@60 + aac 44100/2' }))
    Add-Check ($label + '_decode_clean') ($decodeBad.Count -eq 0) ($(if ($decodeBad.Count) { $decodeBad -join ';' } else { '0 errors' }))
    Add-Check ($label + '_audio_non_silent') ($audioBad.Count -eq 0) ($(if ($audioBad.Count) { $audioBad -join ';' } else { 'max_volume > -40 dB' }))
    Add-Check ($label + '_video_nonblack_changing') ($videoBad.Count -eq 0) ($(if ($videoBad.Count) { $videoBad -join ';' } else { 'nonblack + changing' }))
    return [ordered]@{ dir = $dir; files = $rows; total_duration_s = [Math]::Round($total, 3) }
}

$summary = [ordered]@{
    timestamp_utc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    run_dir = $runDir; status = 'running'
    exe = $exe; exe_sha256 = $exeHash
    synthetic_window = $windowInfo; growing_raw = $growingRaw
    endpoint_before = $endpointBefore
    scenario_a_auto_stop = $null; scenario_b_manual_stop = $null
    events = @(); checks = @(); environment = [ordered]@{}
}

$srvA = $null; $srvB = $null
try {
    # =========================================================== scenario A
    Write-Host '=== scenario A: auto-stop after duration_ms=6000 ==='
    $pipeA = 'auto-heros3-s3a-a-' + $stamp
    $srvA = Start-Server $pipeA 'a'
    Connect-Ipc $pipeA
    $hello = Invoke-Ipc 'hello'
    Add-Check 'a_hello_ok' ([bool]$hello.ok -and [int]$hello.protocol -eq 1 -and [bool]$hello.win_capture_module_loaded) `
        ("protocol=" + $hello.protocol + " obs=" + $hello.obs_version + " win_capture_loaded=" + $hello.win_capture_module_loaded)
    Add-Check 'a_hello_commands' (($hello.commands -contains 'prepare') -and ($hello.commands -contains 'start') -and ($hello.commands -contains 'stop') -and ($hello.commands -contains 'shutdown')) ($hello.commands -join ',')
    $caps = Invoke-Ipc 'capabilities'
    Add-Check 'a_capabilities_ok' ([bool]$caps.ok -and [bool]$caps.capabilities.wgc_window_capture -and [bool]$caps.capabilities.raw_file_audio) ($caps.capabilities | ConvertTo-Json -Compress)
    $badJson = Invoke-Raw '{not json'
    Add-Check 'a_invalid_json_explicit' ((-not [bool]$badJson.ok) -and $badJson.error.code -eq 'invalid_json') ($badJson.error.code + ': ' + $badJson.error.message)
    $unknown = Invoke-Ipc 'frobnicate'
    Add-Check 'a_unknown_command_explicit' ((-not [bool]$unknown.ok) -and $unknown.error.code -eq 'unsupported_command') ($unknown.error.code)
    $startEarly = Invoke-Ipc 'start'
    Add-Check 'a_start_before_prepare' ((-not [bool]$startEarly.ok) -and $startEarly.error.code -eq 'not_prepared') ($startEarly.error.code)
    $stopEarly = Invoke-Ipc 'stop'
    Add-Check 'a_stop_before_prepare_idempotent' ([bool]$stopEarly.ok -and [bool]$stopEarly.already_stopped) ("state=" + $stopEarly.state)
    $prepBadWin = Invoke-Ipc 'prepare' @{ out_dir = $captureA; audio_raw = $growingRaw; window_title = '___no_such_s3a_window___'; max_time_sec = 2; duration_ms = 6000 }
    Add-Check 'a_prepare_bad_window' ((-not [bool]$prepBadWin.ok) -and $prepBadWin.error.code -eq 'window_not_found') ($prepBadWin.error.code + ': ' + $prepBadWin.error.message)
    $missingRaw = Join-Path $tmpDir 'does-not-exist-s3a.raw'
    Remove-Item -LiteralPath $missingRaw -ErrorAction SilentlyContinue
    $prepBadRaw = Invoke-Ipc 'prepare' @{ out_dir = $captureA; audio_raw = $missingRaw; window_pid = [int]$windowInfo.pid; window_class = $windowInfo.class; window_title = $windowInfo.title; max_time_sec = 2; duration_ms = 6000 }
    Add-Check 'a_prepare_bad_audio' ((-not [bool]$prepBadRaw.ok) -and $prepBadRaw.error.code -eq 'audio_raw_not_found') ($prepBadRaw.error.code)
    $prep = Invoke-Ipc 'prepare' @{ out_dir = $captureA; audio_raw = $growingRaw; window_pid = [int]$windowInfo.pid; window_class = $windowInfo.class; window_title = $windowInfo.title; max_time_sec = 2; duration_ms = 6000 }
    Add-Check 'a_prepare_ok' ([bool]$prep.ok -and [bool]$prep.prepared) ("session=" + $prep.session_id + " out=" + $prep.out_dir)
    Add-Check 'a_prepare_window' ([bool]$prep.window.found -and [int]$prep.window.pid -eq [int]$windowInfo.pid) ($prep.window | ConvertTo-Json -Compress)
    Add-Check 'a_prepare_audio' (([int64]$prep.audio_raw.size_bytes -gt 0) -and (-not [bool]$prep.audio_raw.start_at_zero)) ("size=" + $prep.audio_raw.size_bytes + " start_at_zero=" + $prep.audio_raw.start_at_zero)
    $prepAgain = Invoke-Ipc 'prepare' @{ out_dir = $captureA; audio_raw = $growingRaw; window_pid = [int]$windowInfo.pid }
    Add-Check 'a_prepare_twice_explicit' ((-not [bool]$prepAgain.ok) -and $prepAgain.error.code -eq 'already_prepared') ($prepAgain.error.code)
    $start = Invoke-Ipc 'start'
    Add-Check 'a_start_ok' ([bool]$start.ok -and [bool]$start.active -and -not [bool]$start.already_running) ("state=" + $start.state + " duration_ms=" + $start.duration_ms)
    $startAgain = Invoke-Ipc 'start'
    Add-Check 'a_start_idempotent' ([bool]$startAgain.ok -and [bool]$startAgain.already_running) ("already_running=" + $startAgain.already_running)
    Start-Sleep -Milliseconds 1200
    $statusLive = Invoke-Ipc 'status'
    Add-Check 'a_status_live' ([bool]$statusLive.ok -and [bool]$statusLive.active -and $statusLive.state -eq 'recording') ("state=" + $statusLive.state + " active=" + $statusLive.active + " elapsed_ms=" + $statusLive.recording_elapsed_ms)
    $liveModules = @((Get-Process -Id $srvA.proc.Id).Modules | ForEach-Object ModuleName)
    $badMods = @($liveModules | Where-Object { $_ -match '(?i)^(MMDevAPI|audioses|wdmaud|win-wasapi)' })
    Add-Check 'a_no_audio_device_modules_live' ($badMods.Count -eq 0) ("modules=" + $liveModules.Count + " bad=" + ($(if ($badMods.Count) { $badMods -join ',' } else { 'none' })))
    Add-Check 'a_live_modules_include_wgc_stack' (($liveModules -contains 'win-capture.dll') -and ($liveModules -contains 'libobs-winrt.dll')) 'win-capture.dll + libobs-winrt.dll'
    $ev = Wait-IpcEvent 'auto_stop' 20000
    Add-Check 'a_auto_stop_event' ([bool]$ev.ok -and [int]$ev.report_exit_code -eq 0 -and [bool]$ev.checks_ok) ("report_rc=" + $ev.report_exit_code + " checks_ok=" + $ev.checks_ok + " files=" + @($ev.files).Count)
    $statusStopped = Invoke-Ipc 'status'
    Add-Check 'a_status_stopped' ((-not [bool]$statusStopped.active) -and $statusStopped.state -eq 'stopped' -and [bool]$statusStopped.result.available) ("state=" + $statusStopped.state + " files=" + @($statusStopped.segments).Count)
    Add-Check 'a_selftest_manifest_paths' (($statusStopped.result.selftest -match 'selftest\.json$') -and ($statusStopped.result.manifest -match 'manifest\.json$')) ($statusStopped.result | ConvertTo-Json -Compress)
    $stopAgain = Invoke-Ipc 'stop'
    Add-Check 'a_stop_idempotent' ([bool]$stopAgain.ok -and [bool]$stopAgain.already_stopped) ("already_stopped=" + $stopAgain.already_stopped)
    $shutdown = Invoke-Ipc 'shutdown'
    Add-Check 'a_shutdown_ok' ([bool]$shutdown.ok -and [bool]$shutdown.shutting_down) ("state=" + $shutdown.state)
    $exitA = Stop-Server $srvA; $srvA = $null
    Add-Check 'a_server_exit_zero' ($exitA -eq 0) ("exit=" + $exitA)
    $summary.scenario_a_auto_stop = [ordered]@{
        pipe = $pipeA; exit_code = $exitA; prepared = $prep; start = $start; status_live = $statusLive;
        auto_stop_event = $ev; status_stopped = $statusStopped; shutdown = $shutdown;
        live_modules = $liveModules; media = (Validate-Capture $captureA 'a')
    }
    $selftestA = Join-Path $captureA 'selftest.json'
    $manifestA = Join-Path $captureA 'manifest.json'
    Add-Check 'a_selftest_json_exists' (Test-Path $selftestA) $selftestA
    if (Test-Path $selftestA) {
        $st = Get-Content $selftestA -Raw | ConvertFrom-Json
        Add-Check 'a_selftest_pass' ($st.status -eq 'pass' -and $st.mode -eq 's3a-ipc-wgc-window-plus-raw-file-audio-to-split-mp4') ("status=" + $st.status + " mode=" + $st.mode)
        Add-Check 'a_selftest_wgc' ($st.window_capture.method_chosen -eq 'WGC') ("method=" + $st.window_capture.method_chosen)
        Add-Check 'a_selftest_raw_audio' ([bool]$st.raw_audio.enabled -and [bool]$st.raw_audio.opened -and [int64]$st.raw_audio.samples_pushed -gt 0) ("samples=" + $st.raw_audio.samples_pushed + " bytes=" + $st.raw_audio.bytes_read)
        Add-Check 'a_selftest_no_devices' ((-not [bool]$st.process_modules.has_mmdevapi) -and (-not [bool]$st.process_modules.has_audioses) -and (-not [bool]$st.process_modules.has_wdmaud)) 'no MMDevAPI/audioses/wdmaud'
        Add-Check 'a_selftest_monitoring_none' ([int]$st.raw_audio.source_monitoring_type -eq 0) ("monitoring=" + $st.raw_audio.source_monitoring_type)
    }
    Add-Check 'a_manifest_exists' (Test-Path $manifestA) $manifestA
    if (Test-Path $manifestA) {
        $mf = Get-Content $manifestA -Raw | ConvertFrom-Json
        Add-Check 'a_manifest_ok' ($mf.schema -eq 's3a-ipc-manifest/1' -and $mf.status -eq 'pass' -and [int]$mf.report_exit_code -eq 0 -and @($mf.files).Count -ge 2) ("status=" + $mf.status + " files=" + @($mf.files).Count)
    }
    Add-Check 'a_log_method_wgc' (Select-String -Path (Join-Path $logsDir 'a-server.stdout.log') -Pattern 'method chosen: WGC' -Quiet) 'method chosen: WGC in server stdout'

    # =========================================================== scenario B
    Write-Host '=== scenario B: explicit stop() (duration_ms=0) ==='
    $pipeB = 'auto-heros3-s3a-b-' + $stamp
    $srvB = Start-Server $pipeB 'b'
    Connect-Ipc $pipeB
    $null = Invoke-Ipc 'hello'
    $prepB = Invoke-Ipc 'prepare' @{ out_dir = $captureB; audio_raw = $growingRaw; window_pid = [int]$windowInfo.pid; window_class = $windowInfo.class; window_title = $windowInfo.title; max_time_sec = 2; duration_ms = 0 }
    Add-Check 'b_prepare_ok' ([bool]$prepB.ok -and (-not [bool]$prepB.auto_stop)) ("duration_ms=" + $prepB.duration_ms + " auto_stop=" + $prepB.auto_stop)
    $startB = Invoke-Ipc 'start'
    Add-Check 'b_start_ok' ([bool]$startB.ok -and [bool]$startB.active) ("state=" + $startB.state)
    Start-Sleep -Milliseconds 6200
    $statusB = Invoke-Ipc 'status'
    Add-Check 'b_status_recording' ([bool]$statusB.ok -and [bool]$statusB.active) ("elapsed_ms=" + $statusB.recording_elapsed_ms)
    $stopB = Invoke-Ipc 'stop'
    Add-Check 'b_stop_ok' ([bool]$stopB.ok -and -not [bool]$stopB.already_stopped -and [bool]$stopB.checks_ok) ("report_rc=" + $stopB.report_exit_code + " checks_ok=" + $stopB.checks_ok + " files=" + @($stopB.files).Count)
    $stopB2 = Invoke-Ipc 'stop'
    Add-Check 'b_stop_idempotent' ([bool]$stopB2.ok -and [bool]$stopB2.already_stopped) ("already_stopped=" + $stopB2.already_stopped)
    $shutdownB = Invoke-Ipc 'shutdown'
    Add-Check 'b_shutdown_ok' ([bool]$shutdownB.ok -and [bool]$shutdownB.shutting_down) ("state=" + $shutdownB.state)
    $exitB = Stop-Server $srvB; $srvB = $null
    Add-Check 'b_server_exit_zero' ($exitB -eq 0) ("exit=" + $exitB)
    $summary.scenario_b_manual_stop = [ordered]@{
        pipe = $pipeB; exit_code = $exitB; prepared = $prepB; start = $startB; status = $statusB;
        stop = $stopB; shutdown = $shutdownB; media = (Validate-Capture $captureB 'b')
    }
    $stB = Get-Content (Join-Path $captureB 'selftest.json') -Raw | ConvertFrom-Json
    Add-Check 'b_selftest_pass' ($stB.status -eq 'pass' -and $stB.window_capture.method_chosen -eq 'WGC') ("status=" + $stB.status + " method=" + $stB.window_capture.method_chosen)
    Add-Check 'b_manifest_exists' (Test-Path (Join-Path $captureB 'manifest.json')) 'manifest.json'
} catch {
    Add-Check 'driver_exception' $false ($_.Exception.Message + ' @ ' + $_.InvocationInfo.PositionMessage)
    $summary.error = $_.Exception.Message
} finally {
    try { if ($srvA) { Stop-Server $srvA | Out-Null } } catch { }
    try { if ($srvB) { Stop-Server $srvB | Out-Null } } catch { }
    if ($appender -and -not $appender.HasExited) { Stop-Process -Id $appender.Id -Force -ErrorAction SilentlyContinue }
    if ($tk -and -not $tk.HasExited) { Stop-Process -Id $tk.Id -Force -ErrorAction SilentlyContinue }
}
Start-Sleep -Milliseconds 300
$endpointAfter = Get-Endpoint
Add-Check 'endpoint_unchanged' ($endpointBefore -eq $endpointAfter) ("before=" + $endpointBefore + " after=" + $endpointAfter)
$leftover = @(Get-Process | Where-Object { $_.ProcessName -match 'obs-media-selftest|ffmpeg|ffplay|obs64' })
Add-Check 'no_leftover_recorder_processes' ($leftover.Count -eq 0) ($(if ($leftover.Count) { ($leftover | ForEach-Object { $_.ProcessName }) -join ',' } else { 'none' }))
$summary.endpoint_after = $endpointAfter
$summary.events = $script:events
$summary.checks = $checks
$summary.status = $(if (All-Checks) { 'pass' } else { 'fail' })
$summary.environment = [ordered]@{
    ffprobe = $ffprobe; ffmpeg = $ffmpeg; python = $PythonExe; venv_python = $VenvPython
    powershell = $PSVersionTable.PSVersion.ToString(); repo_head = (& git -C $RepoRoot rev-parse --short HEAD 2>$null)
}
$summary | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $runDir 'summary.json') -Encoding UTF8
Write-Host ''
if (All-Checks) {
    Write-Host 'S3A_IPC_SMOKE_RESULT PASS'
    Write-Host ("run_dir={0}" -f $runDir)
    exit 0
} else {
    Write-Host 'S3A_IPC_SMOKE_RESULT FAIL'
    Write-Host ("run_dir={0}" -f $runDir)
    exit 1
}
