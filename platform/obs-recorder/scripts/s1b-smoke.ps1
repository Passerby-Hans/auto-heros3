# S1b evidence runner: build + run obs-media-selftest.exe, then independently
# validate the produced split MP4 files with ffprobe/ffmpeg.
#
# What it does (all evidence lands in var\runs\obs-media-s1b-<timestamp>\):
#   1. (optional, default on) build obs-media-selftest.exe via
#      scripts/build-media-selftest.ps1 (generates obs.lib from installed OBS);
#   2. run the recorder for 17 s -> Hybrid MP4 output with split_file=true and
#      max_time_sec=5 (expected: >= 3 consecutive files);
#   3. while it runs, sample the live process module list (external check that
#      no WASAPI/MMDevAPI audio endpoint stack is loaded);
#   4. validate every produced file with ffprobe/ffmpeg: streams (H.264
#      1920x1080@60 + AAC 44.1 kHz stereo), full decode with zero errors,
#      non-zero audio level, changing video frames, segment durations, A/V
#      duration delta;
#   5. cross-check the recorder's own selftest.json;
#   6. write summary.json / environment.json / commands.txt and PASS/FAIL lines.
#
# No game, no OBS GUI, no OBS WebSocket, no audio playback/monitoring, no ffplay.
#
# Usage:
#   pwsh -NoProfile -ExecutionPolicy Bypass -File platform\obs-recorder\scripts\s1b-smoke.ps1
#   ... -SkipBuild   (reuse an existing build)
[CmdletBinding()]
param(
    [string]$RepoRoot = '',
    [string]$BuildDir = '',
    [int]$DurationMs = 17000,
    [int]$MaxTimeSec = 5,
    [string]$ObsDir = 'C:\Program Files\obs-studio',
    [string]$RunRoot = '',
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
$runDir = Join-Path $RunRoot ("obs-media-s1b-" + $stamp)
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
$runDir = (Resolve-Path -LiteralPath $runDir).Path
$framesDir = Join-Path $runDir 'frames'
New-Item -ItemType Directory -Force -Path $framesDir | Out-Null
$logsDir = Join-Path $runDir 'logs'
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

$checks = New-Object System.Collections.Generic.List[object]
function Add-Check([string]$name, [bool]$ok, [string]$detail) {
    $script:checks.Add([ordered]@{ name = $name; ok = $ok; detail = $detail })
    $status = if ($ok) { 'PASS' } else { 'FAIL' }
    Write-Host ("CHECK {0,-42} {1}  {2}" -f $name, $status, $detail)
}
function Test-AllChecks {
    foreach ($c in $script:checks) { if (-not $c.ok) { return $false } }
    return $true
}

# --- toolchain / prerequisites ----------------------------------------------
$ffprobe = (Get-Command ffprobe.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
$ffmpeg = (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $ffprobe) { Fail 'ffprobe.exe not found on PATH (needed for independent validation)' }
if (-not $ffmpeg) { Fail 'ffmpeg.exe not found on PATH (needed for independent validation)' }
$ffprobeVersion = (& $ffprobe -version 2>&1 | Select-Object -First 1)
$ffmpegVersion = (& $ffmpeg -version 2>&1 | Select-Object -First 1)

# --- build -------------------------------------------------------------------
$env:Path = (Split-Path -Parent $ffprobe) + ';' + $env:Path
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

# --- run recorder, sampling live process modules -----------------------------
$stdout = Join-Path $runDir 'recorder.stdout.log'
$stderr = Join-Path $runDir 'recorder.stderr.log'
$commands = @(
    ('"' + $exe + '" --obs-dir "' + $ObsDir + '" --out-dir "' + $runDir + '" --duration-ms ' + $DurationMs + ' --max-time-sec ' + $MaxTimeSec),
    ('ffprobe= ' + $ffprobe),
    ('ffmpeg=  ' + $ffmpeg)
)
Set-Content -LiteralPath (Join-Path $runDir 'commands.txt') -Value $commands -Encoding UTF8

Write-Host '=== running obs-media-selftest.exe ==='
$sw = [System.Diagnostics.Stopwatch]::StartNew()
# Start-Process joins ArgumentList with spaces without quoting, so quote paths
# that may contain spaces explicitly.
$argLine = '--obs-dir "' + $ObsDir + '" --out-dir "' + $runDir + '" --duration-ms ' + $DurationMs +
    ' --max-time-sec ' + $MaxTimeSec
$proc = Start-Process -FilePath $exe -PassThru -NoNewWindow `
    -ArgumentList $argLine `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr
Start-Sleep -Milliseconds 5000
$liveModules = @()
try { $liveModules = @((Get-Process -Id $proc.Id -ErrorAction Stop).Modules | Select-Object -ExpandProperty ModuleName) } catch { }
$proc.WaitForExit()
$sw.Stop()
$exitCode = $proc.ExitCode
$runWallMs = $sw.ElapsedMilliseconds
Write-Host ("recorder exit={0} wall={1} ms" -f $exitCode, $runWallMs)
Add-Check 'run_exit_code_zero' ($exitCode -eq 0) ("exit=" + $exitCode)

$hasWasapiModule = @($liveModules | Where-Object { $_ -match '(?i)^(MMDevAPI|audioses|wdmaud)' }).Count -gt 0
Add-Check 'no_audio_endpoint_modules_live' (-not $hasWasapiModule) `
    ("MMDevAPI/audioses/wdmaud in live process modules: " + $(if ($hasWasapiModule) { 'YES' } else { 'no' }))
Add-Check 'live_process_audio_modules_sampled' ($liveModules.Count -gt 0) ("module_count=" + $liveModules.Count)

# --- recorder self report ----------------------------------------------------
$selftestPath = Join-Path $runDir 'selftest.json'
if (-not (Test-Path -LiteralPath $selftestPath)) { Fail "recorder selftest.json missing: $selftestPath" }
$selftest = Get-Content -LiteralPath $selftestPath -Raw | ConvertFrom-Json
Add-Check 'selftest_json_status_pass' ($selftest.status -eq 'pass') ("status=" + $selftest.status)
$failedSteps = @($selftest.steps | Where-Object { -not $_.ok } | ForEach-Object { $_.name })
Add-Check 'selftest_steps_all_pass' ($failedSteps.Count -eq 0) ("failed_steps=" + ($(if ($failedSteps.Count) { $failedSteps -join ',' } else { 'none' })))
Add-Check 'selftest_monitoring_none' ([int]$selftest.media.audio.source_monitoring_type -eq 0) `
    ("source_monitoring_type=" + $selftest.media.audio.source_monitoring_type + " (NONE=0)")
Add-Check 'selftest_source_pushed_media' ([uint64]$selftest.media.source_pushed.video_frames -ge [uint64]($DurationMs * 60 / 1000 * 0.8)) `
    ("video_frames=" + $selftest.media.source_pushed.video_frames + " audio_frames=" + $selftest.media.source_pushed.audio_frames)
$selftestFileChanged = @($selftest.output_events | Where-Object { $_.name -eq 'output_file_changed' })
Add-Check 'selftest_split_signal_observed' ($selftestFileChanged.Count -ge 2) `
    ("file_changed events=" + $selftestFileChanged.Count)

# --- independent ffprobe/ffmpeg validation -----------------------------------
$mp4Files = @(Get-ChildItem -LiteralPath $runDir -Filter *.mp4 | Sort-Object LastWriteTime)
Add-Check 'mp4_file_count_ge_3' ($mp4Files.Count -ge 3) ("file_count=" + $mp4Files.Count)

$decodeErrors = New-Object System.Collections.Generic.List[string]
$toneFailures = New-Object System.Collections.Generic.List[string]
$contentFailures = New-Object System.Collections.Generic.List[string]
$probeFailures = New-Object System.Collections.Generic.List[string]
$fileReports = New-Object System.Collections.Generic.List[object]
$totalVideoFrames = 0
$totalVideoDuration = 0.0
$totalAudioDuration = 0.0
$avDeltaMaxMs = 0.0
$prevFile = $null

foreach ($f in $mp4Files) {
    $probeJson = (& $ffprobe -v error -print_format json -show_format -show_streams $f.FullName 2>&1 | Out-String)
    $probeOk = ($LASTEXITCODE -eq 0)
    if (-not $probeOk) {
        $probeFailures.Add($f.Name + ': ffprobe exit ' + $LASTEXITCODE)
        continue
    }
    $probe = $probeJson | ConvertFrom-Json
    $video = @($probe.streams | Where-Object { $_.codec_type -eq 'video' }) | Select-Object -First 1
    $audio = @($probe.streams | Where-Object { $_.codec_type -eq 'audio' }) | Select-Object -First 1
    if (-not $video -or -not $audio) {
        $probeFailures.Add($f.Name + ': missing video or audio stream')
        continue
    }
    $vDuration = [double]$video.duration
    $aDuration = [double]$audio.duration
    $fmtDuration = [double]$probe.format.duration
    $totalVideoDuration += $fmtDuration
    $totalAudioDuration += $aDuration
    $avDeltaMs = [Math]::Abs($vDuration - $aDuration) * 1000.0
    if ($avDeltaMs -gt $avDeltaMaxMs) { $avDeltaMaxMs = $avDeltaMs }
    if ($video.codec_name -ne 'h264') { $probeFailures.Add($f.Name + ': video codec ' + $video.codec_name) }
    if ([int]$video.width -ne 1920 -or [int]$video.height -ne 1080) { $probeFailures.Add($f.Name + ': video size ' + $video.width + 'x' + $video.height) }
    if ($video.avg_frame_rate -ne '60/1') { $probeFailures.Add($f.Name + ': avg_frame_rate ' + $video.avg_frame_rate) }
    if ($audio.codec_name -ne 'aac') { $probeFailures.Add($f.Name + ': audio codec ' + $audio.codec_name) }
    if ([int]$audio.sample_rate -ne 44100) { $probeFailures.Add($f.Name + ': audio sample_rate ' + $audio.sample_rate) }
    if ([int]$audio.channels -ne 2) { $probeFailures.Add($f.Name + ': audio channels ' + $audio.channels) }

    # Full decode of every stream must produce zero error output.
    $decodeOut = (& $ffmpeg -v error -nostdin -i $f.FullName -f null - 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0 -or $decodeOut.Trim().Length -ne 0) {
        $decodeErrors.Add($f.Name + ': exit=' + $LASTEXITCODE + ' stderr=' + $decodeOut.Trim().Substring(0, [Math]::Min(200, $decodeOut.Trim().Length)))
    }

    # Audio tone (440 Hz sine at 0.2 amplitude) must be present and non-silent.
    $volOut = (& $ffmpeg -v info -nostdin -i $f.FullName -map 0:a:0 -af volumedetect -f null - 2>&1 | Out-String)
    $maxVolume = $null
    if ($volOut -match 'max_volume:\s*(-?[0-9.]+)\s*dB') { $maxVolume = [double]$Matches[1] }
    if ($null -eq $maxVolume -or $maxVolume -lt -40.0) {
        $toneFailures.Add($f.Name + ': max_volume=' + $maxVolume)
    }

    # Video content must change over time (moving bar): two decoded frames differ.
    $frameA = Join-Path $framesDir ($f.BaseName + '-a.png')
    $frameB = Join-Path $framesDir ($f.BaseName + '-b.png')
    & $ffmpeg -v error -nostdin -ss 0.2 -i $f.FullName -frames:v 1 -y $frameA 2>&1 | Out-Null
    & $ffmpeg -v error -nostdin -ss 0.5 -i $f.FullName -frames:v 1 -y $frameB 2>&1 | Out-Null
    $frameOk = $false
    if ((Test-Path -LiteralPath $frameA) -and (Test-Path -LiteralPath $frameB)) {
        $hashA = (Get-FileHash -LiteralPath $frameA -Algorithm SHA256).Hash
        $hashB = (Get-FileHash -LiteralPath $frameB -Algorithm SHA256).Hash
        $frameOk = ($hashA -ne $hashB)
    }
    if (-not $frameOk) { $contentFailures.Add($f.Name + ': frames at 0.2s/0.5s identical or missing') }

    $nbReadFrames = (& $ffprobe -v error -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of default=noprint_wrappers=1:nokey=1 $f.FullName 2>&1 | Out-String).Trim()
    $nbInt = 0
    if ($nbReadFrames -match '^\d+$') { $nbInt = [int]$nbReadFrames }
    $totalVideoFrames += $nbInt

    $fileReports.Add([ordered]@{
        name              = $f.Name
        size_bytes        = $f.Length
        format_duration_s = [Math]::Round($fmtDuration, 3)
        video             = [ordered]@{
            codec           = $video.codec_name
            width           = [int]$video.width
            height          = [int]$video.height
            avg_frame_rate  = $video.avg_frame_rate
            duration_s      = [Math]::Round($vDuration, 3)
            nb_frames       = [int]$video.nb_frames
            decoded_frames  = $nbInt
        }
        audio             = [ordered]@{
            codec        = $audio.codec_name
            sample_rate  = [int]$audio.sample_rate
            channels     = [int]$audio.channels
            duration_s   = [Math]::Round($aDuration, 3)
            max_volume_db = $maxVolume
        }
        mtime_unix        = [int64]([DateTimeOffset]::new($f.LastWriteTimeUtc).ToUnixTimeSeconds())
    })
    $prevFile = $f
}

Add-Check 'all_files_probe_ok' ($probeFailures.Count -eq 0) ($(if ($probeFailures.Count) { $probeFailures -join '; ' } else { 'all files have h264 1920x1080@60 + aac 44100 stereo' }))
Add-Check 'all_files_decode_clean' ($decodeErrors.Count -eq 0) ($(if ($decodeErrors.Count) { $decodeErrors -join '; ' } else { 'ffmpeg -v error -f null produced no errors' }))
Add-Check 'audio_tone_present_all_files' ($toneFailures.Count -eq 0) ($(if ($toneFailures.Count) { $toneFailures -join '; ' } else { 'max_volume > -40 dB on every file' }))
Add-Check 'video_content_changing_all_files' ($contentFailures.Count -eq 0) ($(if ($contentFailures.Count) { $contentFailures -join '; ' } else { 'sampled frames differ in every file' }))
Add-Check 'av_duration_delta_le_200ms' ($avDeltaMaxMs -le 200.0) ("max |video-audio| duration delta = " + [Math]::Round($avDeltaMaxMs, 1) + " ms")

# Segment-duration sanity: every segment except the last must be around
# max_time_sec, the final segment may be short; total must match the recording.
$durationProblems = New-Object System.Collections.Generic.List[string]
for ($i = 0; $i -lt $fileReports.Count; $i++) {
    $d = [double]$fileReports[$i].format_duration_s
    $isLast = ($i -eq $fileReports.Count - 1)
    if ($isLast) {
        if ($d -lt 0.3) { $durationProblems.Add($fileReports[$i].name + ' last segment too short: ' + $d) }
    } else {
        if ($d -lt ($MaxTimeSec - 0.75) -or $d -gt ($MaxTimeSec + 2.5)) {
            $durationProblems.Add($fileReports[$i].name + ' duration ' + $d + ' outside [' + ($MaxTimeSec - 0.75) + ',' + ($MaxTimeSec + 2.5) + ']')
        }
    }
}
$recordWallSec = $runWallMs / 1000.0
$expectedMediaSec = $DurationMs / 1000.0
$totalDelta = [Math]::Abs($totalVideoDuration - $expectedMediaSec)
if ($totalDelta -gt ($expectedMediaSec * 0.10 + 0.5)) {
    $durationProblems.Add('total duration ' + [Math]::Round($totalVideoDuration, 3) + 's vs requested ' + $expectedMediaSec + 's')
}
Add-Check 'segment_durations_sane' ($durationProblems.Count -eq 0) ($(if ($durationProblems.Count) { $durationProblems -join '; ' } else { 'segments: ' + (($fileReports | ForEach-Object { $_.format_duration_s }) -join ', ') + ' s' }))

$expectedFrames = [int]($expectedMediaSec * 60)
$frameRatio = if ($expectedFrames -gt 0) { $totalVideoFrames / $expectedFrames } else { 0 }
Add-Check 'total_video_frames_expected' ($frameRatio -ge 0.90 -and $frameRatio -le 1.10) `
    ("decoded " + $totalVideoFrames + " frames vs expected ~" + $expectedFrames + " (ratio " + [Math]::Round($frameRatio, 3) + ")")

# --- summary -----------------------------------------------------------------
$allOk = Test-AllChecks
$summary = [ordered]@{
    timestamp_utc    = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    run_dir          = $runDir
    status           = $(if ($allOk) { 'pass' } else { 'fail' })
    recorder         = [ordered]@{
        exe_path        = $exe
        exe_sha256      = $exeHash
        exe_size_bytes  = $exeSize
        exit_code       = $exitCode
        wall_ms         = $runWallMs
        requested_ms    = $DurationMs
        max_time_sec    = $MaxTimeSec
        stdout_log      = $stdout
        stderr_log      = $stderr
        selftest_json   = $selftestPath
        obs_version     = $selftest.host.obs_version
        obs_dir         = $selftest.host.obs_dir
        graphics_module = $selftest.host.graphics_module
    }
    environment      = [ordered]@{
        ffprobe          = $ffprobe
        ffprobe_version  = $ffprobeVersion
        ffmpeg           = $ffmpeg
        ffmpeg_version   = $ffmpegVersion
        powershell       = $PSVersionTable.PSVersion.ToString()
        repo_head        = (& git -C $RepoRoot rev-parse --short HEAD 2>$null)
    }
    files            = $fileReports
    totals           = [ordered]@{
        file_count         = $mp4Files.Count
        video_duration_s   = [Math]::Round($totalVideoDuration, 3)
        audio_duration_s   = [Math]::Round($totalAudioDuration, 3)
        decoded_video_frames = $totalVideoFrames
        max_av_delta_ms    = [Math]::Round($avDeltaMaxMs, 1)
    }
    live_process_audio_modules = [ordered]@{
        module_count = $liveModules.Count
        wasapi_related = @($liveModules | Where-Object { $_ -match '(?i)(MMDevAPI|audioses|wdmaud|wasapi)' })
        all = $liveModules
    }
    checks           = $checks
}
$summaryPath = Join-Path $runDir 'summary.json'
$summary | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $summaryPath -Encoding UTF8
$environmentPath = Join-Path $runDir 'environment.json'
([ordered]@{
    timestamp_utc = $summary.timestamp_utc
    run_dir = $runDir
    exe = $exe
    exe_sha256 = $exeHash
    obs = $selftest.host
    requested = $selftest.requested_config
    modules = $selftest.modules
    ffprobe = $ffprobeVersion
    ffmpeg = $ffmpegVersion
}) | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $environmentPath -Encoding UTF8

Write-Host ''
if ($allOk) {
    Write-Host 'S1B_SMOKE_RESULT PASS'
    Write-Host ("run_dir={0}" -f $runDir)
    exit 0
} else {
    Write-Host 'S1B_SMOKE_RESULT FAIL'
    Write-Host ("run_dir={0}" -f $runDir)
    exit 1
}
