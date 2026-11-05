# S1c evidence runner: OBS window_capture (WGC) capture path self-test.
#
# What it does (all evidence lands in var\runs\obs-wgc-s1c-<timestamp>\):
#   1. (default) builds obs-media-selftest.exe via scripts/build-media-selftest.ps1;
#   2. starts a completely synthetic, audio-free, animated Tk window titled
#      "VCMI WGC Synthetic" (scripts/wgc-synthetic-window.py) and waits for its
#      ready JSON (hwnd/pid/title/class);
#   3. runs obs-media-selftest.exe --mode window targeting that window by
#      PID+title+class+exe; the host loads win-capture.dll (after video init so
#      WGC is enabled), creates a "window_capture" source with method=2 (WGC),
#      client_area=true, capture_audio=false, records 8 s to split MP4
#      (max_time_sec=3) with the existing x264/AAC/mp4_output path;
#   4. verifies the actual capture method from the win-capture plugin log
#      ("method selected/chosen: WGC") plus selftest.json evidence (hook,
#      dimensions, applied settings, no audio device modules), and that it is
#      NOT BitBlt;
#   5. independently validates the produced MP4s with ffprobe/ffmpeg: streams,
#      full decode, non-black frames (YMAX/YAVG), frame-to-frame change
#      (framemd5 uniqueness), silent AAC track (capture_audio=false), split
#      file count/durations;
#   6. runs a negative test with no matching window and requires an explicit
#      failure: exit code 7, selftest.json status=fail, found=false,
#      "window_not_found" step, zero MP4 files, clean process exit (no crash);
#   7. writes summary.json / environment.json / commands.txt and PASS/FAIL lines.
#
# No game, no OBS GUI, no WebSocket, no audio playback/monitoring, no ffplay.
#
# Usage:
#   pwsh -NoProfile -ExecutionPolicy Bypass -File platform\obs-recorder\scripts\s1c-smoke.ps1
#   ... -SkipBuild        (reuse existing build)
#   ... -DurationMs 8000 -MaxTimeSec 3
[CmdletBinding()]
param(
    [string]$RepoRoot = '',
    [string]$BuildDir = '',
    [int]$DurationMs = 8000,
    [int]$MaxTimeSec = 3,
    [string]$ObsDir = 'C:\Program Files\obs-studio',
    [string]$RunRoot = '',
    [string]$PythonExe = 'C:\Python315\pythonw.exe',
    [string]$WindowTitle = 'VCMI WGC Synthetic',
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'

function Fail([string]$Message) {
    Write-Host ("FATAL: " + $Message)
    exit 1
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = (Resolve-Path (Join-Path $scriptDir '..')).Path
if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $projectDir '..\..')).Path }
if (-not $BuildDir) { $BuildDir = Join-Path $projectDir 'build-media' }
$BuildDir = [System.IO.Path]::GetFullPath($BuildDir)
if (-not $RunRoot) { $RunRoot = Join-Path (Join-Path $RepoRoot 'var\runs') '' }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$runDir = Join-Path $RunRoot ("obs-wgc-s1c-" + $stamp)
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
$runDir = (Resolve-Path -LiteralPath $runDir).Path
$captureDir = Join-Path $runDir 'capture'
$noWindowDir = Join-Path $runDir 'no-window-fail'
$framesDir = Join-Path $runDir 'frames'
New-Item -ItemType Directory -Force -Path $captureDir | Out-Null
New-Item -ItemType Directory -Force -Path $noWindowDir | Out-Null
New-Item -ItemType Directory -Force -Path $framesDir | Out-Null
$logsDir = Join-Path $runDir 'logs'
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

$checks = New-Object System.Collections.Generic.List[object]
function Add-Check([string]$name, [bool]$ok, [string]$detail) {
    $script:checks.Add([ordered]@{ name = $name; ok = $ok; detail = $detail })
    $status = if ($ok) { 'PASS' } else { 'FAIL' }
    Write-Host ("CHECK {0,-46} {1}  {2}" -f $name, $status, $detail)
}
function Test-AllChecks {
    foreach ($c in $script:checks) { if (-not $c.ok) { return $false } }
    return $true
}

# --- prerequisites -----------------------------------------------------------
$ffprobe = (Get-Command ffprobe.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
$ffmpeg = (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $ffprobe) { Fail 'ffprobe.exe not found on PATH (needed for independent validation)' }
if (-not $ffmpeg) { Fail 'ffmpeg.exe not found on PATH (needed for independent validation)' }
if (-not (Test-Path -LiteralPath $PythonExe)) { Fail "pythonw.exe not found: $PythonExe" }
$ffprobeVersion = (& $ffprobe -version 2>&1 | Select-Object -First 1)
$ffmpegVersion = (& $ffmpeg -version 2>&1 | Select-Object -First 1)

# --- build -------------------------------------------------------------------
if (-not $SkipBuild) {
    Write-Host '=== building obs-media-selftest.exe ==='
    $buildScript = Join-Path $scriptDir 'build-media-selftest.ps1'
    & $buildScript -RepoRoot $RepoRoot -BuildDir $BuildDir -ObsDir $ObsDir
    if ($LASTEXITCODE -ne 0) { Fail ("build failed with exit {0}" -f $LASTEXITCODE) }
} else {
    Write-Host '=== reusing existing build (--SkipBuild) ==='
}
$exe = Join-Path (Join-Path $BuildDir 'bin') 'obs-media-selftest.exe'
if (-not (Test-Path -LiteralPath $exe)) { Fail "obs-media-selftest.exe not found: $exe" }
Add-Check 'build_exe_exists' $true $exe
if (Test-Path -LiteralPath (Join-Path $BuildDir 'build_media_summary.json')) {
    Copy-Item -LiteralPath (Join-Path $BuildDir 'build_media_summary.json') -Destination $logsDir
    Copy-Item -Path (Join-Path $BuildDir 'logs\*') -Destination $logsDir -Recurse -Force -ErrorAction SilentlyContinue
}
$sdkSummary = Join-Path $BuildDir 'sdk\sdk_summary.json'
if (Test-Path -LiteralPath $sdkSummary) { Copy-Item -LiteralPath $sdkSummary -Destination $logsDir }

$exeHash = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
$exeSize = (Get-Item -LiteralPath $exe).Length

# --- helper: ffprobe JSON ----------------------------------------------------
function Get-Probe([string]$file) {
    $json = (& $ffprobe -v error -print_format json -show_format -show_streams $file 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($json | ConvertFrom-Json)
}

function Get-FrameMetrics([string]$file, [double]$seekSec) {
    $metrics = [ordered]@{ yavg = $null; ymax = $null }
    foreach ($key in @('YAVG', 'YMAX')) {
        $pattern = 'lavfi.signalstats.' + $key + '=([0-9.]+)'
        $out = (& $ffmpeg -hide_banner -v info -nostdin -ss $seekSec -i $file -map 0:v:0 -frames:v 1 `
                -vf "signalstats,metadata=print:key=lavfi.signalstats.$key" -f null - 2>&1 | Out-String)
        $m = [regex]::Match($out, $pattern)
        if ($m.Success) {
            if ($key -eq 'YAVG') { $metrics.yavg = [double]$m.Groups[1].Value }
            else { $metrics.ymax = [double]$m.Groups[1].Value }
        }
    }
    return $metrics
}

function Get-UniqueFrameRatio([string]$file, [double]$seconds) {
    $out = (& $ffmpeg -v error -nostdin -i $file -map 0:v:0 -t $seconds -f framemd5 - 2>&1 | Out-String)
    $hashes = @()
    foreach ($line in ($out -split "`r?`n")) {
        if ($line -match '^\s*\d+\s*,') {
            $parts = $line.Split(',')
            $hashes += $parts[$parts.Count - 1].Trim()
        }
    }
    if ($hashes.Count -eq 0) { return [ordered]@{ frames = 0; unique = 0; ratio = 0.0 } }
    $unique = ($hashes | Sort-Object -Unique).Count
    return [ordered]@{ frames = $hashes.Count; unique = $unique; ratio = [Math]::Round($unique / $hashes.Count, 3) }
}

function Get-MaxVolumeDb([string]$file) {
    $out = (& $ffmpeg -hide_banner -v info -nostdin -i $file -map 0:a:0 -af volumedetect -f null - 2>&1 | Out-String)
    if ($out -match 'max_volume:\s*(-?inf|-?[0-9.]+)\s*dB') {
        $v = $Matches[1]
        if ($v -eq '-inf' -or $v -eq 'inf') { return [double]::NegativeInfinity }
        return [double]$v
    }
    return $null
}

$commands = @(
    ('"' + $exe + '" --mode window --window-pid <pid> --window-title "' + $WindowTitle + '" --out-dir "' + $captureDir + '" --duration-ms ' + $DurationMs + ' --max-time-sec ' + $MaxTimeSec),
    ('"' + $exe + '" --mode window --window-title "___no_such_window_s1c___" --out-dir "' + $noWindowDir + '" --duration-ms 3000 --max-time-sec 3'),
    ('ffprobe= ' + $ffprobe),
    ('ffmpeg=  ' + $ffmpeg),
    ('pythonw= ' + $PythonExe)
)

# --- start synthetic Tk window ----------------------------------------------
$readyFile = Join-Path $runDir 'window-info.json'
$windowScript = Join-Path $scriptDir 'wgc-synthetic-window.py'
$pythonOut = Join-Path $logsDir 'synthetic-window.stdout.log'
$pythonErr = Join-Path $logsDir 'synthetic-window.stderr.log'
Write-Host '=== starting synthetic animated Tk window (no audio, no game) ==='
$pyArgs = '"' + $windowScript + '" --title "' + $WindowTitle + '" --ready-file "' + $readyFile + '"'
$python = Start-Process -FilePath $PythonExe -ArgumentList $pyArgs -PassThru `
    -RedirectStandardOutput $pythonOut -RedirectStandardError $pythonErr
$readyDeadline = (Get-Date).AddSeconds(15)
while (-not (Test-Path -LiteralPath $readyFile) -and (Get-Date) -lt $readyDeadline) {
    Start-Sleep -Milliseconds 200
}
$windowReady = Test-Path -LiteralPath $readyFile
Add-Check 'synthetic_window_ready' $windowReady ("ready_file=" + $readyFile + " python_pid=" + $python.Id)
if (-not $windowReady) {
    if (-not $python.HasExited) { Stop-Process -Id $python.Id -Force -ErrorAction SilentlyContinue }
    Fail 'synthetic Tk window did not become ready'
}
$windowInfo = Get-Content -LiteralPath $readyFile -Raw | ConvertFrom-Json
Write-Host ("window: hwnd={0} pid={1} title='{2}' class='{3}' exe={4}" -f $windowInfo.hwnd_hex,
    $windowInfo.pid, $windowInfo.title, $windowInfo.class, $windowInfo.exe)

# --- positive run: capture the synthetic window with WGC --------------------
Write-Host '=== capture run: window_capture method=WGC ==='
$captureStdout = Join-Path $runDir 'recorder.stdout.log'
$captureStderr = Join-Path $runDir 'recorder.stderr.log'
$argLine = '--mode window --window-pid ' + $windowInfo.pid +
    ' --window-title "' + $windowInfo.title + '"' +
    ' --window-class "' + $windowInfo.class + '"' +
    ' --window-exe "' + $windowInfo.exe + '"' +
    ' --obs-dir "' + $ObsDir + '" --out-dir "' + $captureDir + '"' +
    ' --duration-ms ' + $DurationMs + ' --max-time-sec ' + $MaxTimeSec
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$recProc = Start-Process -FilePath $exe -PassThru -NoNewWindow -ArgumentList $argLine `
    -RedirectStandardOutput $captureStdout -RedirectStandardError $captureStderr
Start-Sleep -Milliseconds 4000
$liveModules = @()
try { $liveModules = @((Get-Process -Id $recProc.Id -ErrorAction Stop).Modules | Select-Object -ExpandProperty ModuleName) } catch { }
$recProc.WaitForExit()
$sw.Stop()
$captureExit = $recProc.ExitCode
$captureWallMs = $sw.ElapsedMilliseconds
Write-Host ("capture exit={0} wall={1} ms" -f $captureExit, $captureWallMs)
Add-Check 'capture_run_exit_zero' ($captureExit -eq 0) ("exit=" + $captureExit)

$captureLog = Get-Content -LiteralPath $captureStdout -Raw
Add-Check 'log_method_selected_wgc' ($captureLog -match 'method selected:\s*WGC') 'method selected: WGC'
Add-Check 'log_method_chosen_wgc' ($captureLog -match 'method chosen:\s*WGC') 'method chosen: WGC'
Add-Check 'log_method_not_bitblt' ($captureLog -notmatch 'method chosen:\s*BitBlt') 'no "method chosen: BitBlt" fallback in log'
$wasapiLive = @($liveModules | Where-Object { $_ -match '(?i)^(MMDevAPI|audioses|wdmaud|win-wasapi)' })
Add-Check 'no_audio_endpoint_modules_live' ($wasapiLive.Count -eq 0) ("wasapi-related live modules: " + $(if ($wasapiLive.Count) { $wasapiLive -join ',' } else { 'none' }))
Add-Check 'live_process_modules_sampled' ($liveModules.Count -gt 0) ("module_count=" + $liveModules.Count)

$selftestPath = Join-Path $captureDir 'selftest.json'
if (-not (Test-Path -LiteralPath $selftestPath)) { Fail "capture selftest.json missing: $selftestPath" }
$selftest = Get-Content -LiteralPath $selftestPath -Raw | ConvertFrom-Json
Add-Check 'selftest_status_pass' ($selftest.status -eq 'pass') ("status=" + $selftest.status)
$failedSteps = @($selftest.steps | Where-Object { -not $_.ok } | ForEach-Object { $_.name })
Add-Check 'selftest_steps_all_pass' ($failedSteps.Count -eq 0) ("failed_steps=" + $(if ($failedSteps.Count) { $failedSteps -join ',' } else { 'none' }))
$win = $selftest.window_capture
Add-Check 'selftest_mode_window' ($selftest.mode -eq 's1c-wgc-window-capture-to-split-mp4') ("mode=" + $selftest.mode)
Add-Check 'selftest_window_found' ([bool]$win.found) ("hwnd=" + $win.hwnd + " pid=" + $win.pid + " title='" + $win.title + "'")
Add-Check 'selftest_window_matches_target' (([int64]$win.pid -eq [int64]$windowInfo.pid) -and ($win.title -eq $windowInfo.title) -and ($win.hwnd -eq $windowInfo.hwnd_hex)) `
    ("selftest hwnd/title/pid vs Tk window: " + $win.hwnd + "/" + $win.pid + "/'" + $win.title + "'")
Add-Check 'selftest_method_chosen_wgc' ($win.method_chosen -eq 'WGC') `
    ("method_selected='" + $win.method_selected + "' method_chosen='" + $win.method_chosen + "' (win-capture plugin log)")
Add-Check 'selftest_hooked' (([bool]$win.hooked_signal) -and ([bool]$win.hooked_proc)) `
    ("hooked_signal=" + $win.hooked_signal + " hooked_proc=" + $win.hooked_proc)
# The Tk window is DPI-unaware while obs-media-selftest.exe is Per-Monitor-V2
# aware (like obs64.exe), so the physical client area WGC reports can be a DPI
# scaled multiple of the Tk logical geometry.  Require a plausible physical
# client size with the same aspect ratio, and record the observed scale.
$dpiScaleX = [double]$win.capture_width / [double]$windowInfo.width
$dpiScaleY = [double]$win.capture_height / [double]$windowInfo.height
$aspect = [Math]::Abs(($win.capture_width / $win.capture_height) - ($windowInfo.width / $windowInfo.height))
Add-Check 'selftest_capture_dimensions' `
    (([int]$win.capture_width -ge [int]$windowInfo.width) -and ([int]$win.capture_height -ge [int]$windowInfo.height) -and ($aspect -lt 0.01)) `
    ("capture=" + $win.capture_width + "x" + $win.capture_height + " logical_window=" + $windowInfo.width + "x" + $windowInfo.height + " dpi_scale=" + [Math]::Round($dpiScaleX, 3) + "x" + [Math]::Round($dpiScaleY, 3))
$winCapModule = @($selftest.modules | Where-Object { $_.file -eq 'win-capture.dll' } | Select-Object -First 1)
Add-Check 'selftest_win_capture_module_loaded' ($winCapModule -and [bool]$winCapModule.init_ok) `
    ("win-capture.dll init_ok=" + $(if ($winCapModule) { $winCapModule.init_ok } else { 'missing' }))
Add-Check 'selftest_source_settings_wgc' (($win.source_settings_json -match '"method":2') -and ($win.source_settings_json -match '"capture_audio":false') -and ($win.source_settings_json -match '"client_area":true') -and ($win.source_settings_json -match '"force_sdr":true')) `
    $win.source_settings_json
Add-Check 'selftest_window_recorded_no_audio_device' `
    ((-not [bool]$selftest.process_modules.has_mmdevapi) -and (-not [bool]$selftest.process_modules.has_audioses) -and (-not [bool]$selftest.process_modules.has_wdmaud) -and [bool]$selftest.process_modules.has_win_capture -and [bool]$selftest.process_modules.has_libobs_winrt) `
    ("mmdevapi=" + $selftest.process_modules.has_mmdevapi + " audioses=" + $selftest.process_modules.has_audioses + " wdmaud=" + $selftest.process_modules.has_wdmaud + " win-capture=" + $selftest.process_modules.has_win_capture + " libobs-winrt=" + $selftest.process_modules.has_libobs_winrt)

# --- independent MP4 validation ---------------------------------------------
$mp4Files = @(Get-ChildItem -LiteralPath $captureDir -Filter *.mp4 | Sort-Object LastWriteTime)
Add-Check 'capture_file_count_ge_3' ($mp4Files.Count -ge 3) ("file_count=" + $mp4Files.Count)

$probeFailures = New-Object System.Collections.Generic.List[string]
$decodeErrors = New-Object System.Collections.Generic.List[string]
$blackFrames = New-Object System.Collections.Generic.List[string]
$staticFrames = New-Object System.Collections.Generic.List[string]
$audioErrors = New-Object System.Collections.Generic.List[string]
$fileReports = New-Object System.Collections.Generic.List[object]
$totalDuration = 0.0
foreach ($f in $mp4Files) {
    $probe = Get-Probe $f.FullName
    if (-not $probe) { $probeFailures.Add($f.Name + ': ffprobe exit ' + $LASTEXITCODE); continue }
    $video = @($probe.streams | Where-Object { $_.codec_type -eq 'video' }) | Select-Object -First 1
    $audio = @($probe.streams | Where-Object { $_.codec_type -eq 'audio' }) | Select-Object -First 1
    if (-not $video -or -not $audio) { $probeFailures.Add($f.Name + ': missing video/audio stream'); continue }
    if ($video.codec_name -ne 'h264') { $probeFailures.Add($f.Name + ': video codec ' + $video.codec_name) }
    if ([int]$video.width -ne 1920 -or [int]$video.height -ne 1080) { $probeFailures.Add($f.Name + ': video size ' + $video.width + 'x' + $video.height) }
    if ($video.avg_frame_rate -ne '60/1') { $probeFailures.Add($f.Name + ': avg_frame_rate ' + $video.avg_frame_rate) }
    if ($audio.codec_name -ne 'aac') { $probeFailures.Add($f.Name + ': audio codec ' + $audio.codec_name) }
    if ([int]$audio.sample_rate -ne 44100 -or [int]$audio.channels -ne 2) { $probeFailures.Add($f.Name + ': audio ' + $audio.sample_rate + '/' + $audio.channels) }

    $decodeOut = (& $ffmpeg -v error -nostdin -i $f.FullName -f null - 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0 -or $decodeOut.Trim().Length -ne 0) {
        $decodeErrors.Add($f.Name + ': exit=' + $LASTEXITCODE + ' stderr=' + $decodeOut.Trim().Substring(0, [Math]::Min(200, $decodeOut.Trim().Length)))
    }

    $dur = [double]$probe.format.duration
    $totalDuration += $dur
    $seek = [Math]::Min(0.5, [Math]::Max(0.1, $dur / 3.0))
    $metrics = Get-FrameMetrics $f.FullName $seek
    if ($null -eq $metrics.ymax -or $metrics.ymax -lt 200 -or $null -eq $metrics.yavg -or $metrics.yavg -lt 30) {
        $blackFrames.Add($f.Name + ': YAVG=' + $metrics.yavg + ' YMAX=' + $metrics.ymax)
    }
    $uniq = Get-UniqueFrameRatio $f.FullName ([Math]::Min(1.0, [Math]::Max(0.5, $dur - 0.1)))
    if ($uniq.ratio -lt 0.80) {
        $staticFrames.Add($f.Name + ': unique=' + $uniq.unique + '/' + $uniq.frames + ' ratio=' + $uniq.ratio)
    }
    $maxVol = Get-MaxVolumeDb $f.FullName
    if ($null -eq $maxVol) { $audioErrors.Add($f.Name + ': volumedetect parse failed') }
    elseif ($maxVol -gt -60.0) { $audioErrors.Add($f.Name + ': max_volume=' + $maxVol + ' dB (expected silent, capture_audio=false)') }

    $fileReports.Add([ordered]@{
        name              = $f.Name
        size_bytes        = $f.Length
        format_duration_s = [Math]::Round($dur, 3)
        video             = [ordered]@{
            codec = $video.codec_name; width = [int]$video.width; height = [int]$video.height
            avg_frame_rate = $video.avg_frame_rate; duration_s = [Math]::Round([double]$video.duration, 3)
            nb_frames = [int]$video.nb_frames
        }
        audio             = [ordered]@{
            codec = $audio.codec_name; sample_rate = [int]$audio.sample_rate; channels = [int]$audio.channels
            duration_s = [Math]::Round([double]$audio.duration, 3); max_volume_db = $maxVol
        }
        frame_metrics     = [ordered]@{ seek_s = $seek; yavg = $metrics.yavg; ymax = $metrics.ymax }
        frame_uniqueness  = $uniq
        mtime_unix        = [int64]([DateTimeOffset]::new($f.LastWriteTimeUtc).ToUnixTimeSeconds())
    })
}
Add-Check 'all_files_probe_ok' ($probeFailures.Count -eq 0) ($(if ($probeFailures.Count) { $probeFailures -join '; ' } else { 'all files h264 1920x1080@60 + aac 44100/2ch' }))
Add-Check 'all_files_decode_clean' ($decodeErrors.Count -eq 0) ($(if ($decodeErrors.Count) { $decodeErrors -join '; ' } else { 'ffmpeg -v error -f null: no errors' }))
Add-Check 'frames_nonblack' ($blackFrames.Count -eq 0) ($(if ($blackFrames.Count) { $blackFrames -join '; ' } else { 'YAVG>=30 and YMAX>=200 on every file' }))
Add-Check 'frames_changing' ($staticFrames.Count -eq 0) ($(if ($staticFrames.Count) { $staticFrames -join '; ' } else { 'framemd5 uniqueness ratio >=0.80 on every file' }))
Add-Check 'audio_track_silent' ($audioErrors.Count -eq 0) ($(if ($audioErrors.Count) { $audioErrors -join '; ' } else { 'max_volume <= -60 dB on every file (no audio captured)' }))
Add-Check 'split_durations_sum' ([Math]::Abs($totalDuration - ($DurationMs / 1000.0)) -le 1.5) `
    ("sum=" + [Math]::Round($totalDuration, 3) + "s requested=" + ($DurationMs / 1000.0) + "s files=" + $mp4Files.Count)

# --- negative run: no matching window must fail explicitly -------------------
Write-Host '=== negative run: no matching window ==='
$noWindowStdout = Join-Path $noWindowDir 'recorder.stdout.log'
$noWindowStderr = Join-Path $noWindowDir 'recorder.stderr.log'
$noWindowArgs = '--mode window --window-title "___no_such_window_s1c___" --obs-dir "' + $ObsDir + '" --out-dir "' + $noWindowDir + '" --duration-ms 3000 --max-time-sec 3'
$nw = Start-Process -FilePath $exe -PassThru -NoNewWindow -ArgumentList $noWindowArgs `
    -RedirectStandardOutput $noWindowStdout -RedirectStandardError $noWindowStderr
$nw.WaitForExit()
$noWindowExit = $nw.ExitCode
Write-Host ("no-window run exit={0}" -f $noWindowExit)
Add-Check 'no_window_exit_code_7' ($noWindowExit -eq 7) ("exit=" + $noWindowExit + " (window_not_found)")
$noWindowLog = Get-Content -LiteralPath $noWindowStdout -Raw
Add-Check 'no_window_explicit_log' ($noWindowLog -match 'WINDOW_NOT_FOUND') 'stdout contains WINDOW_NOT_FOUND'
$noWindowSelftest = Join-Path $noWindowDir 'selftest.json'
if (-not (Test-Path -LiteralPath $noWindowSelftest)) { Fail "no-window selftest.json missing: $noWindowSelftest" }
$nwJson = Get-Content -LiteralPath $noWindowSelftest -Raw | ConvertFrom-Json
$nwFailed = @($nwJson.steps | Where-Object { -not $_.ok } | ForEach-Object { $_.name })
Add-Check 'no_window_selftest_status_fail' ($nwJson.status -eq 'fail' -and [int]$nwJson.exit_code -eq 7) `
    ("status=" + $nwJson.status + " exit_code=" + $nwJson.exit_code)
Add-Check 'no_window_found_false' (-not [bool]$nwJson.window_capture.found) ("found=" + $nwJson.window_capture.found + " lookup=" + $nwJson.window_capture.lookup)
Add-Check 'no_window_step_window_find_failed' ($nwFailed -contains 'window_find') ("failed_steps=" + ($nwFailed -join ','))
$nwMp4 = @(Get-ChildItem -LiteralPath $noWindowDir -Filter *.mp4 -ErrorAction SilentlyContinue)
Add-Check 'no_window_no_output_files' ($nwMp4.Count -eq 0) ("mp4_count=" + $nwMp4.Count)
Add-Check 'no_window_clean_shutdown' ($noWindowLog -match 'CLEANUP obs_shutdown_done') 'obs_shutdown completed (no crash)'

# --- stop synthetic window ---------------------------------------------------
if (-not $python.HasExited) {
    Stop-Process -Id $python.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 300
}
Add-Check 'synthetic_window_stopped' ($python.HasExited -or (Get-Process -Id $python.Id -ErrorAction SilentlyContinue) -eq $null) 'Tk window process stopped'

# --- summary -----------------------------------------------------------------
Set-Content -LiteralPath (Join-Path $runDir 'commands.txt') -Value $commands -Encoding UTF8
$allOk = Test-AllChecks
$summary = [ordered]@{
    timestamp_utc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    run_dir       = $runDir
    status        = $(if ($allOk) { 'pass' } else { 'fail' })
    recorder      = [ordered]@{
        exe_path = $exe; exe_sha256 = $exeHash; exe_size_bytes = $exeSize
        obs_version = $selftest.host.obs_version; obs_dir = $selftest.host.obs_dir
        graphics_module = $selftest.host.graphics_module
        capture_exit_code = $captureExit; capture_wall_ms = $captureWallMs
        requested_ms = $DurationMs; max_time_sec = $MaxTimeSec
    }
    synthetic_window = $windowInfo
    window_capture   = [ordered]@{
        module_loaded = $selftest.window_capture.module_loaded
        found = $selftest.window_capture.found
        hwnd = $selftest.window_capture.hwnd
        pid = $selftest.window_capture.pid
        title = $selftest.window_capture.title
        class = $selftest.window_capture.class
        exe = $selftest.window_capture.exe
        method_selected = $selftest.window_capture.method_selected
        method_chosen = $selftest.window_capture.method_chosen
        hooked_signal = $selftest.window_capture.hooked_signal
        hooked_proc = $selftest.window_capture.hooked_proc
        capture_width = $selftest.window_capture.capture_width
        capture_height = $selftest.window_capture.capture_height
        dpi_scale = [ordered]@{ x = [Math]::Round($dpiScaleX, 3); y = [Math]::Round($dpiScaleY, 3) }
        canvas_scale = $selftest.window_capture.canvas_scale
        source_settings_json = $selftest.window_capture.source_settings_json
        log_lines = $selftest.window_capture.log_lines
    }
    no_window_run = [ordered]@{
        exit_code = $noWindowExit
        status = $nwJson.status
        exit_code_json = $nwJson.exit_code
        found = $nwJson.window_capture.found
        lookup = $nwJson.window_capture.lookup
        failed_steps = $nwFailed
        mp4_count = $nwMp4.Count
    }
    live_process_modules = [ordered]@{
        module_count = $liveModules.Count
        wasapi_related = $wasapiLive
    }
    files = $fileReports
    totals = [ordered]@{
        file_count = $mp4Files.Count
        total_duration_s = [Math]::Round($totalDuration, 3)
    }
    environment = [ordered]@{
        ffprobe = $ffprobe; ffprobe_version = $ffprobeVersion
        ffmpeg = $ffmpeg; ffmpeg_version = $ffmpegVersion
        pythonw = $PythonExe
        powershell = $PSVersionTable.PSVersion.ToString()
        repo_head = (& git -C $RepoRoot rev-parse --short HEAD 2>$null)
    }
    checks = $checks
}
$summaryPath = Join-Path $runDir 'summary.json'
$summary | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $summaryPath -Encoding UTF8
$environmentPath = Join-Path $runDir 'environment.json'
([ordered]@{
    timestamp_utc = $summary.timestamp_utc
    run_dir = $runDir
    exe = $exe; exe_sha256 = $exeHash
    obs = $selftest.host
    modules = $selftest.modules
    requested_config = $selftest.requested_config
    ffprobe = $ffprobeVersion
    ffmpeg = $ffmpegVersion
    pythonw = $PythonExe
}) | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $environmentPath -Encoding UTF8

Write-Host ''
if ($allOk) {
    Write-Host 'S1C_SMOKE_RESULT PASS'
    Write-Host ("run_dir={0}" -f $runDir)
    exit 0
} else {
    Write-Host 'S1C_SMOKE_RESULT FAIL'
    Write-Host ("run_dir={0}" -f $runDir)
    exit 1
}
