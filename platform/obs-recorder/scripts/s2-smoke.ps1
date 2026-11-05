# S2a evidence runner: WGC window video + native raw-file (VCMI game-audio.raw)
# audio in one scene/output.
#
# What it does (evidence under var\runs\obs-wgc-audio-s2-<timestamp>\):
#   1. (default) builds obs-media-selftest.exe via scripts/build-media-selftest.ps1;
#   2. generates an offline 17 s s16le/44100/2 tone file (NO ffplay, no playback)
#      with scripts/gen-test-raw-tone.py;
#   3. starts the synthetic animated Tk window (scripts/wgc-synthetic-window.py);
#   4. POSITIVE STATIC: records 8 s with WGC window source + raw audio source
#      (--audio-raw <tone> --audio-start-zero), split max_time_sec=3;
#   5. EOF-STATIC behaviour: same static file without --audio-start-zero (default
#      start at EOF) -> explicit silent chunks, silent AAC track, exit 0;
#   6. MISSING-FILE: nonexistent raw path -> explicit failure (exit 9, no MP4);
#   7. LIVE GROWTH: a background generator appends the raw file in real time,
#      recorder uses the default EOF start and must capture non-silent audio;
#   8. independent ffprobe/ffmpeg validation of every produced MP4:
#      streams, full decode, non-black/changing video, per-file audio level
#      (tone > -40 dB / silent <= -60 dB), split durations, A/V delta;
#   9. no MMDevAPI/audioses/wdmaud/win-wasapi in the live process (no audio
#      playback/monitoring/device), source monitoring NONE, volume 1.0.
#
# No game, no obs64/UI/WebSocket, no audio playback, no ffplay.
#
# Usage:
#   pwsh -NoProfile -ExecutionPolicy Bypass -File platform\obs-recorder\scripts\s2-smoke.ps1
#   ... -SkipBuild
[CmdletBinding()]
param(
    [string]$RepoRoot = '',
    [string]$BuildDir = '',
    [int]$DurationMs = 8000,
    [int]$MaxTimeSec = 3,
    [string]$ObsDir = 'C:\Program Files\obs-studio',
    [string]$RunRoot = '',
    [string]$PythonExe = 'C:\Python315\python.exe',
    [string]$PythonwExe = 'C:\Python315\pythonw.exe',
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
$runDir = Join-Path $RunRoot ("obs-wgc-audio-s2-" + $stamp)
$audioDir = Join-Path $runDir 'audio'
$staticCaptureDir = Join-Path $runDir 'capture-static-tone'
$eofCaptureDir = Join-Path $runDir 'capture-eof-static'
$missingCaptureDir = Join-Path $runDir 'capture-missing-file'
$growthCaptureDir = Join-Path $runDir 'capture-growth'
$framesDir = Join-Path $runDir 'frames'
$logsDir = Join-Path $runDir 'logs'
foreach ($d in @($runDir, $audioDir, $staticCaptureDir, $eofCaptureDir, $missingCaptureDir, $growthCaptureDir, $framesDir, $logsDir)) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
$runDir = (Resolve-Path -LiteralPath $runDir).Path

$checks = New-Object System.Collections.Generic.List[object]
function Add-Check([string]$name, [bool]$ok, [string]$detail) {
    $script:checks.Add([ordered]@{ name = $name; ok = $ok; detail = $detail })
    $status = if ($ok) { 'PASS' } else { 'FAIL' }
    Write-Host ("CHECK {0,-48} {1}  {2}" -f $name, $status, $detail)
}
function Test-AllChecks {
    foreach ($c in $script:checks) { if (-not $c.ok) { return $false } }
    return $true
}

# --- prerequisites -----------------------------------------------------------
$ffprobe = (Get-Command ffprobe.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
$ffmpeg = (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $ffprobe) { Fail 'ffprobe.exe not found on PATH' }
if (-not $ffmpeg) { Fail 'ffmpeg.exe not found on PATH' }
foreach ($py in @($PythonExe, $PythonwExe)) {
    if (-not (Test-Path -LiteralPath $py)) { Fail ("python executable not found: " + $py) }
}
$ffprobeVersion = (& $ffprobe -version 2>&1 | Select-Object -First 1)

# --- build -------------------------------------------------------------------
if (-not $SkipBuild) {
    Write-Host '=== building obs-media-selftest.exe ==='
    & (Join-Path $scriptDir 'build-media-selftest.ps1') -RepoRoot $RepoRoot -BuildDir $BuildDir -ObsDir $ObsDir
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
if (Test-Path -LiteralPath (Join-Path $BuildDir 'sdk\sdk_summary.json')) {
    Copy-Item -LiteralPath (Join-Path $BuildDir 'sdk\sdk_summary.json') -Destination $logsDir
}
$exeHash = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
$exeSize = (Get-Item -LiteralPath $exe).Length

# --- helpers -----------------------------------------------------------------
function Get-Probe([string]$file) {
    $json = (& $ffprobe -v error -print_format json -show_format -show_streams $file 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($json | ConvertFrom-Json)
}
function Get-FrameMetrics([string]$file, [double]$seekSec) {
    $metrics = [ordered]@{ yavg = $null; ymax = $null }
    foreach ($key in @('YAVG', 'YMAX')) {
        $out = (& $ffmpeg -hide_banner -v info -nostdin -ss $seekSec -i $file -map 0:v:0 -frames:v 1 `
                -vf "signalstats,metadata=print:key=lavfi.signalstats.$key" -f null - 2>&1 | Out-String)
        $m = [regex]::Match($out, ('lavfi\.signalstats\.' + $key + '=([0-9.]+)'))
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

# Validate one capture directory. audioMode: 'tone' (>-40 dB) or 'silent' (<=-60 dB).
function Test-Capture([string]$dir, [string]$audioMode, [string]$tag) {
    $result = [ordered]@{
        dir = $dir; audio_mode = $audioMode; files = @(); total_duration = 0.0
        probe_failures = @(); decode_failures = @(); video_failures = @(); audio_failures = @()
        max_av_delta_ms = 0.0
    }
    $files = @(Get-ChildItem -LiteralPath $dir -Filter *.mp4 | Sort-Object LastWriteTime)
    if ($files.Count -lt 3) { $result.probe_failures += ("file_count=" + $files.Count + " (<3)") }
    foreach ($f in $files) {
        $probe = Get-Probe $f.FullName
        if (-not $probe) { $result.probe_failures += ($f.Name + ': ffprobe failed'); continue }
        $video = @($probe.streams | Where-Object { $_.codec_type -eq 'video' }) | Select-Object -First 1
        $audio = @($probe.streams | Where-Object { $_.codec_type -eq 'audio' }) | Select-Object -First 1
        if (-not $video -or -not $audio) { $result.probe_failures += ($f.Name + ': missing stream'); continue }
        if ($video.codec_name -ne 'h264' -or [int]$video.width -ne 1920 -or [int]$video.height -ne 1080 -or $video.avg_frame_rate -ne '60/1') {
            $result.probe_failures += ($f.Name + ': video ' + $video.codec_name + ' ' + $video.width + 'x' + $video.height + ' ' + $video.avg_frame_rate)
        }
        if ($audio.codec_name -ne 'aac' -or [int]$audio.sample_rate -ne 44100 -or [int]$audio.channels -ne 2) {
            $result.probe_failures += ($f.Name + ': audio ' + $audio.codec_name + ' ' + $audio.sample_rate + '/' + $audio.channels)
        }
        $decodeOut = (& $ffmpeg -v error -nostdin -i $f.FullName -f null - 2>&1 | Out-String)
        if ($LASTEXITCODE -ne 0 -or $decodeOut.Trim().Length -ne 0) {
            $result.decode_failures += ($f.Name + ': exit=' + $LASTEXITCODE + ' ' + $decodeOut.Trim().Substring(0, [Math]::Min(160, $decodeOut.Trim().Length)))
        }
        $dur = [double]$probe.format.duration
        $result.total_duration += $dur
        $seek = [Math]::Min(0.5, [Math]::Max(0.1, $dur / 3.0))
        $metrics = Get-FrameMetrics $f.FullName $seek
        $uniq = Get-UniqueFrameRatio $f.FullName ([Math]::Min(1.0, [Math]::Max(0.5, $dur - 0.1)))
        if ($null -eq $metrics.ymax -or $metrics.ymax -lt 200 -or $null -eq $metrics.yavg -or $metrics.yavg -lt 30) {
            $result.video_failures += ($f.Name + ': YAVG=' + $metrics.yavg + ' YMAX=' + $metrics.ymax)
        }
        if ($uniq.ratio -lt 0.80) { $result.video_failures += ($f.Name + ': unique ' + $uniq.unique + '/' + $uniq.frames) }
        $maxVol = Get-MaxVolumeDb $f.FullName
        if ($null -eq $maxVol) {
            $result.audio_failures += ($f.Name + ': volumedetect failed')
        } elseif ($audioMode -eq 'tone' -and ($maxVol -le -40.0)) {
            $result.audio_failures += ($f.Name + ': max_volume=' + $maxVol + ' dB (expected tone > -40)')
        } elseif ($audioMode -eq 'silent' -and ($maxVol -gt -60.0)) {
            $result.audio_failures += ($f.Name + ': max_volume=' + $maxVol + ' dB (expected silent <= -60)')
        }
        $avDelta = [Math]::Abs([double]$video.duration - [double]$audio.duration) * 1000.0
        if ($avDelta -gt $result.max_av_delta_ms) { $result.max_av_delta_ms = $avDelta }
        $result.files += [ordered]@{
            name = $f.Name; size_bytes = $f.Length; duration_s = [Math]::Round($dur, 3)
            video = [ordered]@{ codec = $video.codec_name; width = [int]$video.width; height = [int]$video.height
                avg_frame_rate = $video.avg_frame_rate; nb_frames = [int]$video.nb_frames }
            audio = [ordered]@{ codec = $audio.codec_name; sample_rate = [int]$audio.sample_rate
                channels = [int]$audio.channels; max_volume_db = $maxVol }
            frame_metrics = [ordered]@{ seek_s = $seek; yavg = $metrics.yavg; ymax = $metrics.ymax }
            frame_uniqueness = $uniq
        }
    }
    $result.ok = ($result.probe_failures.Count -eq 0 -and $result.decode_failures.Count -eq 0 -and
                  $result.video_failures.Count -eq 0 -and $result.audio_failures.Count -eq 0 -and
                  $result.max_av_delta_ms -le 250.0)
    return $result
}

# --- generate offline test tone ---------------------------------------------
$tonePath = Join-Path $audioDir 'tone-17s.raw'
Write-Host '=== generating 17 s s16le/44100/2 tone (offline, no ffplay) ==='
& $PythonExe (Join-Path $scriptDir 'gen-test-raw-tone.py') --out $tonePath --seconds 17 2>&1 | ForEach-Object { Write-Host $_ }
Add-Check 'tone_file_generated' ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $tonePath)) ("exit=" + $LASTEXITCODE + " path=" + $tonePath)
$toneSize = if (Test-Path -LiteralPath $tonePath) { (Get-Item -LiteralPath $tonePath).Length } else { 0 }
$toneExpected = 17 * 44100 * 2 * 2
Add-Check 'tone_file_size' ($toneSize -eq $toneExpected) ("size=" + $toneSize + " expected=" + $toneExpected)

# --- start synthetic Tk window (once for all recorder runs) ------------------
$readyFile = Join-Path $runDir 'window-info.json'
Remove-Item -LiteralPath $readyFile -ErrorAction SilentlyContinue
Write-Host '=== starting synthetic animated Tk window (no audio, no game) ==='
$tkArgs = '"' + (Join-Path $scriptDir 'wgc-synthetic-window.py') + '" --title "' + $WindowTitle + '" --ready-file "' + $readyFile + '"'
$tk = Start-Process -FilePath $PythonwExe -ArgumentList $tkArgs -PassThru `
    -RedirectStandardOutput (Join-Path $logsDir 'synthetic-window.stdout.log') `
    -RedirectStandardError (Join-Path $logsDir 'synthetic-window.stderr.log')
$deadline = (Get-Date).AddSeconds(15)
while (-not (Test-Path -LiteralPath $readyFile) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 200 }
$windowReady = Test-Path -LiteralPath $readyFile
Add-Check 'synthetic_window_ready' $windowReady ("ready_file=" + $readyFile + " tk_pid=" + $tk.Id)
if (-not $windowReady) {
    if (-not $tk.HasExited) { Stop-Process -Id $tk.Id -Force -ErrorAction SilentlyContinue }
    Fail 'synthetic Tk window did not become ready'
}
$windowInfo = Get-Content -LiteralPath $readyFile -Raw | ConvertFrom-Json
Write-Host ("window: hwnd={0} pid={1} title='{2}' class='{3}' exe={4}" -f $windowInfo.hwnd_hex,
    $windowInfo.pid, $windowInfo.title, $windowInfo.class, $windowInfo.exe)

function Invoke-Recorder {
    param([string]$outDir, [string]$extraArgs = '', [switch]$SampleModules)
    $stdout = Join-Path $outDir 'recorder.stdout.log'
    $stderr = Join-Path $outDir 'recorder.stderr.log'
    $argLine = '--mode window --window-pid ' + $windowInfo.pid +
        ' --window-title "' + $windowInfo.title + '"' +
        ' --window-class "' + $windowInfo.class + '"' +
        ' --obs-dir "' + $ObsDir + '" --out-dir "' + $outDir + '"' +
        ' --duration-ms ' + $DurationMs + ' --max-time-sec ' + $MaxTimeSec + ' ' + $extraArgs
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $proc = Start-Process -FilePath $exe -PassThru -NoNewWindow -ArgumentList $argLine `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $liveModules = @()
    if ($SampleModules) {
        Start-Sleep -Milliseconds 4000
        try { $liveModules = @((Get-Process -Id $proc.Id -ErrorAction Stop).Modules | Select-Object -ExpandProperty ModuleName) } catch { }
    }
    $proc.WaitForExit()
    $sw.Stop()
    return [ordered]@{
        exit_code = $proc.ExitCode; wall_ms = $sw.ElapsedMilliseconds
        stdout = $stdout; stderr = $stderr; live_modules = $liveModules
    }
}

function Read-Selftest([string]$outDir) {
    $path = Join-Path $outDir 'selftest.json'
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    return (Get-Content -LiteralPath $path -Raw | ConvertFrom-Json)
}

# --- 1. POSITIVE STATIC: WGC video + raw-file tone audio ---------------------
Write-Host '=== run 1: static 17s raw tone, start_at_zero (WGC + audio) ==='
$r1 = Invoke-Recorder -outDir $staticCaptureDir -extraArgs ('--audio-raw "' + $tonePath + '" --audio-start-zero') -SampleModules
Write-Host ("run1 exit={0} wall={1} ms" -f $r1.exit_code, $r1.wall_ms)
Add-Check 'static_tone_exit_zero' ($r1.exit_code -eq 0) ("exit=" + $r1.exit_code)
$log1 = Get-Content -LiteralPath $r1.stdout -Raw
Add-Check 'log_method_chosen_wgc' ($log1 -match 'method chosen:\s*WGC') 'method chosen: WGC'
Add-Check 'log_method_not_bitblt' ($log1 -notmatch 'method chosen:\s*BitBlt') 'no BitBlt fallback'
$wasapiLive = @($r1.live_modules | Where-Object { $_ -match '(?i)^(MMDevAPI|audioses|wdmaud|win-wasapi)' })
Add-Check 'no_audio_endpoint_modules_live' ($wasapiLive.Count -eq 0) ("wasapi-related modules: " + $(if ($wasapiLive.Count) { $wasapiLive -join ',' } else { 'none' }))
Add-Check 'live_modules_sampled' ($r1.live_modules.Count -gt 0) ("module_count=" + $r1.live_modules.Count)

$st1 = Read-Selftest $staticCaptureDir
if (-not $st1) { Fail 'run1 selftest.json missing' }
Add-Check 'static_tone_selftest_pass' ($st1.status -eq 'pass') ("status=" + $st1.status)
$failedSteps1 = @($st1.steps | Where-Object { -not $_.ok } | ForEach-Object { $_.name })
Add-Check 'static_tone_steps_all_pass' ($failedSteps1.Count -eq 0) ("failed_steps=" + $(if ($failedSteps1.Count) { $failedSteps1 -join ',' } else { 'none' }))
Add-Check 'selftest_mode_s2a' ($st1.mode -eq 's2a-wgc-window-plus-raw-file-audio-to-split-mp4') ("mode=" + $st1.mode)
$ra1 = $st1.raw_audio
Add-Check 'raw_audio_enabled_opened' ([bool]$ra1.enabled -and [bool]$ra1.opened) ("enabled=" + $ra1.enabled + " opened=" + $ra1.opened + " open_attempts=" + $ra1.open_attempts)
Add-Check 'raw_audio_start_at_zero' ([bool]$ra1.start_at_zero -and [int64]$ra1.start_offset -eq 0) ("start_at_zero=" + $ra1.start_at_zero + " start_offset=" + $ra1.start_offset + " file_size_at_open=" + $ra1.file_size_at_open)
Add-Check 'raw_audio_source_flags' ((([int]$ra1.output_flags -band 2) -ne 0) -and (([int]$ra1.output_flags -band 4) -ne 0)) ("output_flags=0x" + ('{0:x}' -f [int]$ra1.output_flags) + " (AUDIO|ASYNC)")
Add-Check 'raw_audio_source_settings' (($ra1.source_settings_json -match '"start_at_zero":true') -and ($ra1.source_settings_json -match 'tone-17s\.raw')) $ra1.source_settings_json
Add-Check 'raw_audio_monitoring_none' ([int]$ra1.source_monitoring_type -eq 0) ("monitoring_type=" + $ra1.source_monitoring_type + " volume=" + $ra1.source_volume + " mixers=" + $ra1.audio_mixers)
$expectedSamples = [int64]($DurationMs / 1000.0 * 44100 * 0.85)
Add-Check 'raw_audio_samples_pushed' ([int64]$ra1.samples_pushed -ge $expectedSamples -and [int64]$ra1.chunks_pushed -gt 0) `
    ("samples_pushed=" + $ra1.samples_pushed + " chunks=" + $ra1.chunks_pushed + " bytes_read=" + $ra1.bytes_read + " silence_chunks=" + $ra1.silence_chunks)
Add-Check 'raw_audio_no_endpoint_in_selftest' `
    ((-not [bool]$st1.process_modules.has_mmdevapi) -and (-not [bool]$st1.process_modules.has_audioses) -and (-not [bool]$st1.process_modules.has_wdmaud)) `
    ("mmdevapi=" + $st1.process_modules.has_mmdevapi + " audioses=" + $st1.process_modules.has_audioses + " wdmaud=" + $st1.process_modules.has_wdmaud)
$winCap1 = @($st1.modules | Where-Object { $_.file -eq 'win-capture.dll' -and $_.init_ok })
Add-Check 'win_capture_module_loaded' ($winCap1.Count -eq 1) 'win-capture.dll init_ok'

$media1 = Test-Capture $staticCaptureDir 'tone' 'static'
Add-Check 'static_tone_mp4_files' ($media1.files.Count -ge 3) ("file_count=" + $media1.files.Count + " total_duration=" + [Math]::Round($media1.total_duration, 3) + "s")
Add-Check 'static_tone_probe_ok' ($media1.probe_failures.Count -eq 0) ($(if ($media1.probe_failures.Count) { $media1.probe_failures -join '; ' } else { 'streams ok' }))
Add-Check 'static_tone_decode_clean' ($media1.decode_failures.Count -eq 0) ($(if ($media1.decode_failures.Count) { $media1.decode_failures -join '; ' } else { 'all decoded cleanly' }))
Add-Check 'static_tone_audio_non_silent' ($media1.audio_failures.Count -eq 0) ($(if ($media1.audio_failures.Count) { $media1.audio_failures -join '; ' } else { 'every file max_volume > -40 dB' }))
Add-Check 'static_tone_video_nonblack_changing' ($media1.video_failures.Count -eq 0) ($(if ($media1.video_failures.Count) { $media1.video_failures -join '; ' } else { 'nonblack + framemd5 change' }))
Add-Check 'static_tone_av_delta' ($media1.max_av_delta_ms -le 250.0) ("max |V-A| = " + [Math]::Round($media1.max_av_delta_ms, 1) + " ms")
Add-Check 'static_tone_split_sum' ([Math]::Abs($media1.total_duration - ($DurationMs / 1000.0)) -le 1.5) ("sum=" + [Math]::Round($media1.total_duration, 3) + "s requested=" + ($DurationMs / 1000.0) + "s")

# --- 2. EOF-STATIC: default start at EOF, no file growth -> explicit silence -
Write-Host '=== run 2: static file, default EOF start (expected explicit silence) ==='
$r2 = Invoke-Recorder -outDir $eofCaptureDir -extraArgs ('--audio-raw "' + $tonePath + '"')
Write-Host ("run2 exit={0}" -f $r2.exit_code)
Add-Check 'eof_static_exit_zero' ($r2.exit_code -eq 0) ("exit=" + $r2.exit_code)
$st2 = Read-Selftest $eofCaptureDir
if (-not $st2) { Fail 'run2 selftest.json missing' }
$ra2 = $st2.raw_audio
Add-Check 'eof_static_start_offset_eof' ((-not [bool]$ra2.start_at_zero) -and ([int64]$ra2.start_offset -eq $toneSize)) `
    ("start_at_zero=" + $ra2.start_at_zero + " start_offset=" + $ra2.start_offset + " tone_size=" + $toneSize)
Add-Check 'eof_static_explicit_no_data' ([int64]$ra2.bytes_read -eq 0 -and [int64]$ra2.samples_pushed -eq 0 -and [int64]$ra2.silence_chunks -gt 0 -and [int64]$ra2.eof_waits -gt 0) `
    ("bytes_read=" + $ra2.bytes_read + " samples=" + $ra2.samples_pushed + " silence_chunks=" + $ra2.silence_chunks + " eof_waits=" + $ra2.eof_waits)
$media2 = Test-Capture $eofCaptureDir 'silent' 'eof-static'
Add-Check 'eof_static_audio_silent' ($media2.audio_failures.Count -eq 0) ($(if ($media2.audio_failures.Count) { $media2.audio_failures -join '; ' } else { 'every file max_volume <= -60 dB (digital silence)' }))
Add-Check 'eof_static_video_ok' ($media2.video_failures.Count -eq 0) ($(if ($media2.video_failures.Count) { $media2.video_failures -join '; ' } else { 'nonblack + changing (WGC unaffected)' }))
Add-Check 'eof_static_files' ($media2.files.Count -ge 3) ("file_count=" + $media2.files.Count)

# --- 3. MISSING-FILE: explicit failure, no output ---------------------------
Write-Host '=== run 3: missing raw file (explicit failure expected) ==='
$missingPath = Join-Path $audioDir 'does-not-exist-s2a.raw'
Remove-Item -LiteralPath $missingPath -ErrorAction SilentlyContinue
$r3 = Invoke-Recorder -outDir $missingCaptureDir -extraArgs ('--audio-raw "' + $missingPath + '"')
Write-Host ("run3 exit={0}" -f $r3.exit_code)
Add-Check 'missing_file_exit_9' ($r3.exit_code -eq 9) ("exit=" + $r3.exit_code + " (raw audio source failure)")
$st3 = Read-Selftest $missingCaptureDir
if (-not $st3) { Fail 'run3 selftest.json missing' }
$failedSteps3 = @($st3.steps | Where-Object { -not $_.ok } | ForEach-Object { $_.name })
Add-Check 'missing_file_selftest_fail' ($st3.status -eq 'fail' -and [int]$st3.exit_code -eq 9) ("status=" + $st3.status + " exit_code=" + $st3.exit_code)
Add-Check 'missing_file_raw_audio_opened_false' (-not [bool]$st3.raw_audio.opened) ("opened=" + $st3.raw_audio.opened + " open_error='" + $st3.raw_audio.open_error + "'")
Add-Check 'missing_file_step_failed' ($failedSteps3 -contains 'raw_audio_opened') ("failed_steps=" + ($failedSteps3 -join ','))
$missingMp4 = @(Get-ChildItem -LiteralPath $missingCaptureDir -Filter *.mp4 -ErrorAction SilentlyContinue)
Add-Check 'missing_file_no_output' ($missingMp4.Count -eq 0) ("mp4_count=" + $missingMp4.Count)
Add-Check 'missing_file_clean_shutdown' ((Get-Content -LiteralPath $r3.stdout -Raw) -match 'CLEANUP obs_shutdown_done') 'obs_shutdown completed (no crash)'

# --- 4. LIVE GROWTH: default EOF start with a growing file ------------------
Write-Host '=== run 4: live growing raw file (default EOF start) ==='
$growingPath = Join-Path $audioDir 'growing.raw'
Remove-Item -LiteralPath $growingPath -ErrorAction SilentlyContinue
$appArgs = '"' + (Join-Path $scriptDir 'gen-test-raw-tone.py') + '" --out "' + $growingPath + '" --seconds 20 --stream'
$app = Start-Process -FilePath $PythonExe -ArgumentList $appArgs -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logsDir 'raw-tone-stream.stdout.log') `
    -RedirectStandardError (Join-Path $logsDir 'raw-tone-stream.stderr.log')
$streamDeadline = (Get-Date).AddSeconds(8)
while (-not (Test-Path -LiteralPath $growingPath) -and (Get-Date) -lt $streamDeadline) { Start-Sleep -Milliseconds 100 }
Add-Check 'growth_writer_started' ((Test-Path -LiteralPath $growingPath) -and (-not $app.HasExited)) ("appender_pid=" + $app.Id)
$r4 = Invoke-Recorder -outDir $growthCaptureDir -extraArgs ('--audio-raw "' + $growingPath + '"')
Write-Host ("run4 exit={0}" -f $r4.exit_code)
if (-not $app.HasExited) { Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue }
Add-Check 'growth_exit_zero' ($r4.exit_code -eq 0) ("exit=" + $r4.exit_code)
$st4 = Read-Selftest $growthCaptureDir
if (-not $st4) { Fail 'run4 selftest.json missing' }
$ra4 = $st4.raw_audio
Add-Check 'growth_audio_data_read' ([bool]$ra4.opened -and [int64]$ra4.bytes_read -gt 0 -and [int64]$ra4.chunks_pushed -gt 0 -and [int64]$ra4.samples_pushed -gt 0) `
    ("start_offset=" + $ra4.start_offset + " bytes_read=" + $ra4.bytes_read + " chunks=" + $ra4.chunks_pushed + " samples=" + $ra4.samples_pushed + " silence_chunks=" + $ra4.silence_chunks)
$media4 = Test-Capture $growthCaptureDir 'tone' 'growth'
Add-Check 'growth_audio_non_silent' ($media4.audio_failures.Count -eq 0) ($(if ($media4.audio_failures.Count) { $media4.audio_failures -join '; ' } else { 'every file max_volume > -40 dB (live appended audio)' }))
Add-Check 'growth_video_ok' ($media4.video_failures.Count -eq 0) ($(if ($media4.video_failures.Count) { $media4.video_failures -join '; ' } else { 'nonblack + changing' }))
Add-Check 'growth_files' ($media4.files.Count -ge 3) ("file_count=" + $media4.files.Count)
Add-Check 'growth_av_delta' ($media4.max_av_delta_ms -le 250.0) ("max |V-A| = " + [Math]::Round($media4.max_av_delta_ms, 1) + " ms")

# --- cleanup synthetic window ------------------------------------------------
if (-not $tk.HasExited) { Stop-Process -Id $tk.Id -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 200
Add-Check 'synthetic_window_stopped' ($null -eq (Get-Process -Id $tk.Id -ErrorAction SilentlyContinue)) 'Tk window process stopped'

# --- summary -----------------------------------------------------------------
$commands = @(
    ('"' + $exe + '" --mode window --window-pid <pid> --window-title "' + $WindowTitle + '" --audio-raw "' + $tonePath + '" --audio-start-zero --out-dir "' + $staticCaptureDir + '" --duration-ms ' + $DurationMs + ' --max-time-sec ' + $MaxTimeSec),
    ('"' + $exe + '" --mode window --window-pid <pid> --window-title "' + $WindowTitle + '" --audio-raw "' + $tonePath + '" --out-dir "' + $eofCaptureDir + '" --duration-ms ' + $DurationMs + ' --max-time-sec ' + $MaxTimeSec),
    ('"' + $exe + '" --mode window --window-pid <pid> --window-title "' + $WindowTitle + '" --audio-raw "' + $missingPath + '" --out-dir "' + $missingCaptureDir + '" --duration-ms ' + $DurationMs + ' --max-time-sec ' + $MaxTimeSec),
    ('"' + $exe + '" --mode window --window-pid <pid> --window-title "' + $WindowTitle + '" --audio-raw "' + $growingPath + '" --out-dir "' + $growthCaptureDir + '" --duration-ms ' + $DurationMs + ' --max-time-sec ' + $MaxTimeSec),
    ('ffprobe= ' + $ffprobe),
    ('ffmpeg=  ' + $ffmpeg),
    ('python=  ' + $PythonExe),
    ('pythonw= ' + $PythonwExe)
)
Set-Content -LiteralPath (Join-Path $runDir 'commands.txt') -Value $commands -Encoding UTF8

$allOk = Test-AllChecks
$summary = [ordered]@{
    timestamp_utc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    run_dir = $runDir
    status = $(if ($allOk) { 'pass' } else { 'fail' })
    recorder = [ordered]@{
        exe_path = $exe; exe_sha256 = $exeHash; exe_size_bytes = $exeSize
        obs_version = $st1.host.obs_version; obs_dir = $st1.host.obs_dir
        graphics_module = $st1.host.graphics_module
        requested_ms = $DurationMs; max_time_sec = $MaxTimeSec
    }
    synthetic_window = $windowInfo
    tone_file = [ordered]@{ path = $tonePath; size_bytes = $toneSize; expected_bytes = $toneExpected; format = 's16le/44100/2 interleaved' }
    runs = [ordered]@{
        static_tone = [ordered]@{ exit_code = $r1.exit_code; wall_ms = $r1.wall_ms; dir = $staticCaptureDir; selftest_status = $st1.status; raw_audio = $ra1; media = $media1 }
        eof_static = [ordered]@{ exit_code = $r2.exit_code; wall_ms = $r2.wall_ms; dir = $eofCaptureDir; selftest_status = $st2.status; raw_audio = $ra2; media = $media2 }
        missing_file = [ordered]@{ exit_code = $r3.exit_code; dir = $missingCaptureDir; selftest_status = $st3.status; raw_audio = $st3.raw_audio; failed_steps = $failedSteps3; mp4_count = $missingMp4.Count }
        live_growth = [ordered]@{ exit_code = $r4.exit_code; wall_ms = $r4.wall_ms; dir = $growthCaptureDir; selftest_status = $st4.status; raw_audio = $ra4; media = $media4 }
    }
    live_process_modules = [ordered]@{
        module_count = $r1.live_modules.Count
        wasapi_related = $wasapiLive
    }
    environment = [ordered]@{
        ffprobe = $ffprobe; ffprobe_version = $ffprobeVersion
        ffmpeg = $ffmpeg
        powershell = $PSVersionTable.PSVersion.ToString()
        repo_head = (& git -C $RepoRoot rev-parse --short HEAD 2>$null)
    }
    checks = $checks
}
$summary | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $runDir 'summary.json') -Encoding UTF8
$environment = [ordered]@{
    timestamp_utc = $summary.timestamp_utc
    run_dir = $runDir
    exe = $exe; exe_sha256 = $exeHash
    obs = $st1.host
    modules = $st1.modules
    requested_config = $st1.requested_config
    ffprobe = $ffprobeVersion
    ffmpeg = $ffmpeg
    python = $PythonExe
}
$environment | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $runDir 'environment.json') -Encoding UTF8

Write-Host ''
if ($allOk) {
    Write-Host 'S2A_SMOKE_RESULT PASS'
    Write-Host ("run_dir={0}" -f $runDir)
    exit 0
} else {
    Write-Host 'S2A_SMOKE_RESULT FAIL'
    Write-Host ("run_dir={0}" -f $runDir)
    exit 1
}
