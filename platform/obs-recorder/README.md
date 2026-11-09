# obs-recorder — S0/S1a scaffold + S1b/S1c/S2a/S3a libobs media self-test

Native Windows host process (`obs-recorder.exe`, **S0/S1a**) that **inspects the
installed OBS runtime via dynamic loading** and exposes a **local named-pipe
JSON-lines IPC skeleton**, plus a second, opt-in executable
(`obs-media-selftest.exe`, **S1b/S1c/S2a/S3a**, verified 2026-11-05) that **links
libobs** and records synthetic media, a real WGC window, and/or a native raw-file
audio source to **split Hybrid MP4** files — one-shot CLI or through the S3a
in-process named-pipe server.

- S0/S1a scaffold: **verified 2026-11-05**, repo HEAD `88cf273` (branch
  `feature/av1b-vdd-wgc`); it never calls `obs_startup`/`obs_shutdown`, never
  creates a video/audio/output subsystem, never captures, never records, never
  plays audio.
- S1b media self-test (in-memory mode): **verified 2026-11-05**, repo HEAD
  `5e153d6`; it does call `obs_startup` and the real media pipeline (see the
  dedicated S1b section below). It is a **test host with synthetic media only**
  — no screen capture of the game, no game PCM, not wired into MCP/Python, and
  not the product recorder.
- S1c WGC window-capture mode: **verified 2026-11-05**, repo HEAD `6510b51`;
  targets a real window through OBS `window_capture`/WGC (still a synthetic Tk
  test window, no game, no audio capture; see the S1c section below).
- S2a WGC + raw-file audio mode: **verified 2026-11-05**, repo HEAD `e510405`;
  adds a native audio-only source that reads VCMI `game-audio.raw`
  (s16le/44100/2 → FLOAT_PLANAR) and records it together with the S1c WGC
  window source in one scene/output. Still a synthetic Tk video + offline/raw
  test audio; **not live VCMI**, not wired into MCP/Python (see the S2a section
  below).
- S2b live VCMI: **verified 2026-11-05**, repo HEAD `53453af`; the same S2a
  pipeline was driven against a real VCMI client (`display_index=2`, live
  `game-audio.raw`, default EOF) through MCP stdio + isolated temp state;
  43/43 checks (`var/runs/obs-vcmi-live-s2b-20261105-173819/S2B_LIVE_REPORT.md`).
- S3a IPC server: **verified 2026-11-05**, repo HEAD `53453af`;
  `obs-media-selftest.exe --serve --pipe NAME` initialises libobs once and
  exposes `hello/capabilities/prepare/start/status/stop/shutdown` over a named
  pipe, reusing the S2a WGC + raw-audio + x264/AAC/MP4 pipeline (58/58 checks;
  see the S3a section below).
- The OBS GUI (`obs64.exe`) is never started by any tool in this directory.

The current media host's `--serve` mode requires `--owner-pid` and
`--owner-created-filetime` (the exact Win32 process creation FILETIME).
The production `ObsRecorder` adapter supplies both automatically. The host pins
and verifies that controller's process handle, accepts only its pipe connection,
and exits if the owner dies or no first connection arrives within 30 seconds.
Historical hand-written media-host `--serve` commands below must include these
identity arguments; the separate S0 `obs-recorder` probe is unchanged.
After a complete `shutdown` response, `ObsRecorder` sends a one-way
`shutdown_ack` with the response's `seq` in `shutdown_seq`. The host waits at most one
second for this receipt (or owner/pipe loss) before disconnecting; older clients
without receipt support still exit within that deadline. Repeated adapter
shutdown/close calls reuse the received response instead of issuing another
shutdown command.

## Verified capability summary (actual observations)

| Item | Observed |
|---|---|
| OBS install | `C:\Program Files\obs-studio` |
| `obs.dll` | `...\bin\64bit\obs.dll`, 1,176,336 bytes |
| `obs.dll` file version | `32.1.0` (fixed `32.1.0.0`) |
| Dynamic load | `LoadLibraryExW(LOAD_WITH_ALTERED_SEARCH_PATH)` → **loaded** |
| `obs_get_version_string()` called dynamically | returns `32.1.0` (SEH-guarded, no exception) |
| `obs_get_version()` called dynamically | raw packed value `0x20010000` (not decoded here) |
| Export probe | 19 of 21 probed exports resolved (`obs_startup`, `obs_shutdown`, `obs_get_version`, `obs_add_data_path`, `obs_add_module_path`, `obs_load_all_modules`, `obs_post_load_modules`, `obs_reset_video/audio`, `obs_source_create`, `obs_scene_create`, `obs_output_create/start/stop`, `obs_data_create/get_json`, …) |
| Probed exports absent | `obs_frontend_get_main_window` (frontend API lives in `obs-frontend-api.dll`), `obs_start_recording` (not a libobs export) |
| SDK headers | **absent** (`sdk_headers=false`; searched install include/lib dirs, `OBS_SDK_DIR`, `vendor\obs-studio\libobs`) |
| SDK import library | **absent** (`sdk_import_lib=false`) |
| `obs_startup` | **not called** (no SDK/module/data lifecycle in S0/S1a) |
| Media pipeline | **not started** (`media_active=false`) |
| WGC / x264 / AAC / MP4 | **NOT probed** — no claim made |
| Plugin files on disk (presence only) | `obs-ffmpeg.dll`, `obs-x264.dll`, `win-capture.dll`, `win-wasapi.dll`, `obs-outputs.dll`, `coreaudio-encoder.dll`, `libx264-164.dll`, `avcodec-61.dll`, `avformat-61.dll` all present; 31 DLLs in `obs-plugins\64bit` |

`plugin_presence` in the JSON means *files exist on disk*. It is **not** a
capability probe: no plugin was loaded and no encoder/capture/mux capability was
tested.

> Note: the table above describes **S0/S1a `obs-recorder.exe` only**. S1b below
> adds a separate executable that does generate an SDK import library from the
> installed runtime, calls `obs_startup`, loads a selective plugin whitelist and
> exercises the real x264/AAC/MP4 pipeline.

## Layout

```
platform/obs-recorder/
  CMakeLists.txt
  .gitignore                     # ignores /build/ and /build-media/
  README.md
  scripts/
    build.ps1                    # S0/S1a: vcvars64 + CMake/Ninja build, writes build_summary.json
    s0-smoke.ps1                 # reproducible S0 evidence run (build/capabilities/IPC)
    gen-obs-sdk.ps1              # S1b/S1c/S2a/S3a: generate obs.lib + obsconfig.h from installed obs.dll
    build-media-selftest.ps1     # S1b/S1c/S2a/S3a: build obs-media-selftest.exe (links libobs)
    s1b-smoke.ps1                # S1b: build + record + ffprobe/ffmpeg validation, writes summary.json
    s1c-smoke.ps1                # S1c: WGC window capture + validation + explicit no-window failure run
    s2-smoke.ps1                 # S2a: WGC + raw-file audio, static/EOF/missing/growth runs + validation
    s3a-ipc-smoke.ps1            # S3a: named-pipe IPC server hello/prepare/start/status/stop/shutdown + validation
    wgc-synthetic-window.py      # S1c/S2a: synthetic animated Tk window (no audio/game)
    gen-test-raw-tone.py         # S2a: offline s16le/44100/2 tone file / real-time appender (no playback)
  src/
    main.cpp                     # CLI modes + capabilities JSON assembly
    json_util.hpp/.cpp           # minimal JSON writer + flat JSON request extractors (S3a)
    win_util.hpp/.cpp            # paths, Windows version resources, OS info
    obs_probe.hpp/.cpp           # dynamic obs.dll load, symbol probe, SEH-guarded version calls
    ipc_pipe.hpp/.cpp            # named-pipe server + probe client + in-process self-test
    media/
      gen_av_source.hpp/.cpp     # S1b in-memory async AV source (I420 frames + float PCM)
      raw_audio_source.hpp/.cpp  # S2a async audio-only raw-file source (s16le → FLOAT_PLANAR)
      media_selftest.cpp         # S1b/S1c/S2a/S3a obs-media-selftest.exe host
```

Build output (git-ignored): `platform/obs-recorder/build/`
– `bin/obs-recorder.exe`
– `build_summary.json`
– `logs/{toolchain,configure,build}.log`

Media self-test build output (git-ignored): `platform/obs-recorder/build-media/`
– `bin/obs-media-selftest.exe` (+ staged OBS runtime DLLs incl. `libobs-winrt.dll`, `libcurl.dll`)
– `sdk/{obs.def,obs.lib,obsconfig.h,sdk_summary.json}`
– `build_media_summary.json`, `logs/`

## Build

Prerequisites (verified on this host):

- Visual Studio with the C++ x64 toolset (`vcvars64.bat`); tested with
  MSVC 19.51.36260.0 (`VC\Tools\MSVC\14.51.36231`, VS 18 BuildTools discovered by
  `vswhere`; VS 18 Community is also installed)
- CMake ≥ 3.20 (`C:\msys64\mingw64\bin\cmake.exe`, tested 4.1.1)
- Ninja (`C:\msys64\mingw64\bin\ninja.exe`, tested 1.13.1)

Exact command:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File platform\obs-recorder\scripts\build.ps1
```

The script:

1. imports the `vcvars64.bat` environment into the PowerShell process;
2. detects the localized MSVC `/showIncludes` prefix and patches CMake's
   generated Ninja rule (`msvc_deps_prefix`) to the raw UTF-8 bytes `cl.exe`
   emits — without this, header dependency tracking silently breaks on this
   non-English MSVC install (header edits would be ignored);
3. configures with `-G Ninja -DCMAKE_BUILD_TYPE=Release`;
4. builds and writes `build_summary.json` (toolchain paths, exit codes, exe
   SHA-256).

Underlying commands (recorded in the evidence `commands.txt`/`configure.log`):

```
"C:\msys64\mingw64\bin\cmake.exe" -S platform\obs-recorder -B platform\obs-recorder\build -G Ninja ^
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_MAKE_PROGRAM="C:\msys64\mingw64\bin\ninja.exe"
"C:\msys64\mingw64\bin\cmake.exe" --build platform\obs-recorder\build --config Release
```

Clean rebuild from scratch:

```powershell
Remove-Item -Recurse -Force platform\obs-recorder\build
pwsh -NoProfile -ExecutionPolicy Bypass -File platform\obs-recorder\scripts\build.ps1
```

Last verified build: **exit 0** (toolchain 0 / configure 0 / build 0),
`obs-recorder.exe` 196,608 bytes,
SHA-256 `4acb0c5f92469f44ec0fdc442c8297092462aff639890bcca0fcf286b074c3f5`.

## Usage

```text
obs-recorder --capabilities [--obs-dir DIR] [--json-out FILE]
obs-recorder --serve [--pipe NAME] [--obs-dir DIR] [--log FILE]
                    [--idle-timeout-ms N] [--read-timeout-ms N] [--max-requests N]
obs-recorder --ipc-probe [--pipe NAME] [--log FILE]
                    [--connect-timeout-ms N] [--io-timeout-ms N] [--no-shutdown]
obs-recorder --self-test [--obs-dir DIR]
obs-recorder --version | --help
```

Exit codes: `0` ok, `1` IPC checks failed, `2` usage error, `3` runtime/obs
probe error.

### `--capabilities`

Loads the installed `obs.dll` dynamically, resolves the whitelisted exports,
safely calls the two version accessors, and prints one JSON document to stdout
(optional `--json-out FILE` mirrors it). It does **not** call `obs_startup`.
`capability_matrix` states explicitly what was and was not probed, and
`sdk.sdk_headers` / `sdk.sdk_import_lib` report the actual absence of the OBS SDK
with the candidate paths that were searched.

### IPC named pipe

- Default pipe: `\\.\pipe\auto-heros3-obs-recorder-<pid>` (one server instance,
  local only — `PIPE_REJECT_REMOTE_CLIENTS`, byte mode, newline-delimited JSON).
- Commands (S0/S1a): `hello`, `status`, `shutdown`. No media command exists.
- Both directions use JSON Lines; each response echoes the request `id`.
- Overlapped I/O with timeouts: `--idle-timeout-ms` (wait for a client),
  `--read-timeout-ms` (per request). The probe client has
  `--connect-timeout-ms`/`--io-timeout-ms`.

Example session (server stdout event + probe transcript):

```jsonl
{"ts":"2026-11-05T07:12:13Z","event":"listening","pipe":"\\\\.\\pipe\\auto-heros3-obs-recorder-12345","server":"obs-recorder","version":"0.1.0-s0","proto":1,"pid":12345,"media_active":false,"obs_dll_loaded":true,"obs_startup_called":false}
{"id":1,"cmd":"hello","proto":1,"client":"obs-recorder-probe"}
{"id":1,"ok":true,"cmd":"hello","server":"obs-recorder","version":"0.1.0-s0","proto":1,"pid":12345,"state":"idle","obs":{"dll_loaded":true,"dll_path":"C:\\Program Files\\obs-studio\\bin\\64bit\\obs.dll","file_version":"32.1.0","sdk_headers":false,"obs_startup_called":false},"media":{"media_active":false,"recording_started":false,"audio_playback_active":false}}
{"id":2,"cmd":"status","client":"obs-recorder-probe"}
{"id":2,"ok":true,"cmd":"status","state":"idle","requests_handled":2,...}
{"id":3,"cmd":"shutdown","client":"obs-recorder-probe"}
{"id":3,"ok":true,"cmd":"shutdown","state":"shutting_down"}
```

Two-process test:

```powershell
$pipe = '\\.\pipe\auto-heros3-obs-recorder-s0-test'
Start-Process platform\obs-recorder\build\bin\obs-recorder.exe `
  -ArgumentList @('--serve','--pipe',$pipe) -NoNewWindow
platform\obs-recorder\build\bin\obs-recorder.exe --ipc-probe --pipe $pipe
```

`--self-test` runs the same client/server exchange in one process (server thread
+ client on the default pid-based pipe) and exits 0 on success.

## Verification / evidence

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File platform\obs-recorder\scripts\s0-smoke.ps1
```

Latest evidence run: **`var/runs/obs-native-s0-20261105-1530/`** — 19/19 checks
PASS. It contains `capabilities.json`, `ipc_selftest.jsonl`,
`ipc_client.probe.jsonl`, server stdout/stderr, build/configure/toolchain logs,
`build_summary.json`, `environment.json`, `summary.json`, and
`OBS_NATIVE_S0_REPORT.md`. Auxiliary `dumpbin /exports` / `/dependents` captures
for `obs.dll`/`obs64.exe` are under `logs/`.

## S1b media self-test (`obs-media-selftest.exe`)

Opt-in second executable that is the first binary in this directory to **link
libobs** instead of `dlopen()`ing it. Verified 2026-11-05 (repo HEAD `5e153d6`):
**17/17 independent checks PASS**, evidence in
`var/runs/obs-media-s1b-20261105-160609/`.

The same executable has an S1c window mode (`--mode window`) that captures a real
window through OBS `window_capture` with the **WGC** method — see the dedicated
S1c section below.

What the default (`--mode in-memory`, S1b) path does (and only this):

1. `obs_startup("en-US", NULL, NULL)` (libobs 32.1.0);
2. loads only `obs-ffmpeg.dll`, `obs-x264.dll`, `obs-outputs.dll` (explicit
   whitelist; **never** `obs_load_all_modules`, so no Qt/UI/frontend plugin);
3. `obs_reset_audio(44100 Hz stereo)` and
   `obs_reset_video(1920x1080@60, I420, Rec.709/Partial, D3D11)`;
4. registers/creates one in-memory async AV source
   (`a3_test_av_source`: 1920x1080 I420 frames with a moving-bar pattern via
   `obs_source_output_video()` + 440 Hz float PCM via
   `obs_source_output_audio()`), puts it in an `obs_scene`, assigns the scene to
   main-canvas channel 0;
5. creates `obs_x264` (H.264) + `ffmpeg_aac` encoders and an `mp4_output`
   (Hybrid MP4) with `split_file=true`, `max_time_sec=5`;
6. records for the requested duration (default 17 s), stops cleanly, and writes
   `selftest.json` (modules, applied settings, output signals/events,
   frame/byte counters, produced files, process module list).

No screen capture, no audio capture, no audio monitoring/playback, no physical
audio output, no OBS UI/WebSocket, no game. The media is **synthetic**.

### S1b build

Prerequisites: installed OBS **32.1.0**, `vendor/obs-studio` OBS 32.1.0 source
clone (git-ignored), VS C++ toolset, CMake/Ninja.

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File platform\obs-recorder\scripts\build-media-selftest.ps1
```

`scripts/gen-obs-sdk.ps1` first generates `obs.lib` (from `dumpbin /exports` of
the installed `obs.dll` → `lib /def:`, 1,782 exports) and a minimal
`obsconfig.h` under `build-media/sdk/`, then CMake builds the target with the
OBS 32.1.0 source headers and stages the required OBS runtime DLLs next to the
exe (all inside the git-ignored `build-media/`; no generated binary is
committed). The S0/S1a `obs-recorder` target is unaffected and still builds with
`scripts/build.ps1`.

### S1b run / verification

```powershell
# full evidence run: build + record + independent ffprobe/ffmpeg validation
pwsh -NoProfile -ExecutionPolicy Bypass -File platform\obs-recorder\scripts\s1b-smoke.ps1

# recorder only
platform\obs-recorder\build-media\bin\obs-media-selftest.exe `
  --out-dir var\runs\obs-media-s1b-manual --duration-ms 17000 --max-time-sec 5
```

Observed result of the verified run:

| Item | Observed |
|---|---|
| Recorder exit | **0**; wall 18.4 s for a 17.0 s recording (drain 0.9 s) |
| Files | **4** split Hybrid MP4s: 5.990 / 4.983 / 4.992 / 1.000 s |
| Streams (every file) | H.264 1920x1080, `avg_frame_rate=60/1` + AAC 44.1 kHz 2 ch |
| Decode | `ffmpeg -v error -i <file> -f null -` → **zero errors** on all files |
| Audio | 440 Hz tone present on every file (`max_volume` ≈ −13…−14 dB) |
| Video content | sampled frames differ; 119/120 unique decoded frame hashes in 2 s |
| A/V (container) | max |video−audio| duration delta **48 ms**; total 16.965 s vs 17.0 s requested |
| Split behavior | boundaries at 6.0 / 11.0 / 16.0 s (see below) |
| No audio device | live process modules contain **no MMDevAPI/audioses/wdmaud**; source `monitoring_type=NONE`; no monitoring APIs called; `win-wasapi` not loaded |

**Actual split behavior (honest, not "exactly 3x5 s"):** `mp4_output` splits only
at a primary-track keyframe whose `dts_usec - start_time >= max_time_sec`
(`plugins/obs-outputs/mp4-output.c:333 should_split()`, `:414 change_file()`).
With `keyint_sec=1` and x264 B-frames, the PTS=5 s keyframe has DTS≈4.95 s, so
the first eligible keyframe is PTS=6.0 s → first segment 6.0 s; then +5.0 s
boundaries. A 17 s recording therefore yields 4 files (6+5+5+1 s). The task
target "16–18 s → at least 3 valid split files" is met; an exact 3×5 s result
would need a different duration/keyframe/B-frame configuration.

### S1b limitations (still test/stand-in, not the product recorder)

1. **Synthetic media only**: the picture is a generated moving-bar/checker
   pattern and the audio is a generated 440 Hz sine. This in-memory mode does
   not load `win-capture` (S1c adds WGC window capture — see below) and no game
   capture / VCMI game PCM is tested here.
2. **Not wired into the product**: standalone exe; no MCP/Python integration, no
   IPC/manifest hooks, does not replace the existing Python recorder.
3. **Split semantics/rollover**: 5 s is a keyframe-aligned lower bound (observed
   6/11/16 s); no 1.5 GB size-triggered rollover (`max_size_mb=0`), no
   crash-safe recovery/segment resume/atomic manifest.
4. **Durability**: single 17 s run; no multi-minute/1.5 GB soak, no watchdog, no
   leak checks.
5. **A/V quality scope**: container-level durations + full decode + tone/frame
   checks only; no human listening/visual or per-frame sync validation.
6. **Licensing**: OBS/libobs is GPL-2.0-or-later; distributing a libobs-linked
   host needs GPL compliance review (not done here).
7. **Host-specific**: OBS 32.1.0 install layout and DLL names are hard-coded in
   the scripts/staging list; changing OBS versions requires regenerating
   `obs.lib` and updating the staged DLL list.

See `var/runs/obs-media-s1b-20261105-160609/S1B_REPORT.md` for the full report
(API/source-line references, per-file probes, crash/fix notes).

## S1c WGC window-capture mode (`--mode window`)

Verified 2026-11-05 (repo HEAD `6510b51`): **36/36 independent checks PASS**,
evidence in `var/runs/obs-wgc-s1c-20261105-163717/`. It captures a **real
window** through OBS' `window_capture` source using the **WGC** method (not
BitBlt), while keeping the no-audio/no-playback constraints.

What it does:

1. loads the same base whitelist (`obs-ffmpeg`, `obs-x264`, `obs-outputs`) **and
   `win-capture.dll`** — the latter is loaded **after `obs_reset_video()`** so the
   plugin sees the D3D11 graphics context and enables WGC
   (`plugins/win-capture/plugin-main.c:104 obs_module_load()`,
   `:102 wgc_supported`). Loading it before graphics init permanently forces the
   BitBlt fallback;
2. targets a window by PID/title/class/exe using libobs' helpers:
   `ms_find_window()` (`libobs/util/windows/window-helpers.h:40`) and
   `ms_get_window_title/class/exe()`; the OBS escaped `title:class:exe` string is
   built from the actual HWND attributes (`ms_build_window_strings()` /
   `encode_dstr()` conventions). **Class is required** for the WGC path because
   `window-capture.c:630` uses `ms_find_window_top_level()`, which returns NULL
   when class is NULL;
3. creates `obs_source_create("window_capture", ...)` with
   `method=2` (WGC), `client_area=true`, `capture_audio=false`, `force_sdr=true`
   and puts it in a scene on main-canvas channel 0;
4. waits for the source `hooked` signal + non-zero captured size (WGC frame
   init), then records 8 s with the existing x264/AAC/`mp4_output`
   (split_file=true, `max_time_sec=3`);
5. verifies the **actual** method from the win-capture plugin log
   (`method selected: WGC` / `method chosen: WGC`, see
   `plugins/win-capture/window-capture.c:187 log_settings()`) and records it in
   `selftest.json`; the smoke script fails if the log shows BitBlt;
6. runs a negative test with no matching window and requires an explicit
   failure: exit code **7**, `WINDOW_NOT_FOUND` on stdout,
   `selftest.json` `status=fail/exit_code=7/window_capture.found=false`, zero
   MP4 files and a clean `obs_shutdown_done` (no crash).

### S1c run / verification

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File platform\obs-recorder\scripts\s1c-smoke.ps1

# recorder only (target window must already exist)
platform\obs-recorder\build-media\bin\obs-media-selftest.exe --mode window `
  --window-pid <pid> --window-title "VCMI WGC Synthetic" --window-class TkTopLevel `
  --out-dir var\runs\obs-wgc-manual --duration-ms 8000 --max-time-sec 3
```

The smoke script starts `scripts/wgc-synthetic-window.py` (Tk, animated, no
audio/game, title `VCMI WGC Synthetic`) and passes its PID/title/class/exe to the
recorder.

Observed result of the verified run:

| Item | Observed |
|---|---|
| Modules | 4/4: obs-ffmpeg, obs-x264, obs-outputs, **win-capture** (init ok) |
| Window | hwnd `0x9112aa`, pid 61628, `title='VCMI WGC Synthetic'`, `class='TkTopLevel'`, `exe='pythonw.exe'` |
| source settings (read back) | `method=2, capture_audio=false, client_area=true, force_sdr=true, priority=1, window="VCMI WGC Synthetic:TkTopLevel:pythonw.exe"` |
| Actual method (plugin log) | **`method selected: WGC` / `method chosen: WGC`**, not BitBlt |
| Capture size | **960x720** physical client area (Tk logical 640x480, 150% DPI scale 1.5x1.5; client_area crop excludes the title bar) |
| Files | 3 split MP4s: 3.994 / 2.983 / 1.033 s (239/179/62 frames), total 8.011 s |
| Streams | H.264 1920x1080@60 + AAC 44.1 kHz 2 ch on every file |
| Decode | `ffmpeg -v error -i <file> -f null -` → **zero errors** |
| Frames | YAVG 94.4/66.9/62.5, YMAX ≈ 240 (**non-black**); framemd5 unique ratio 60/60, 57/60, 55/56 (**changing**) |
| Audio | silent by design (`capture_audio=false`): max_volume = **−91 dB** |
| No audio device | live process modules contain **no MMDevAPI/audioses/wdmaud/win-wasapi** (98 modules; win-capture/libobs-winrt/libcurl present); `monitoring_type=NONE` |
| Split behavior | keyframe-aligned: boundaries at 4 s / 7 s (B-frame DTS shift), so 8 s → 4+3+1 s files; `file_changed` events logged |
| Negative run | no matching window → **exit 7**, explicit `window_not_found` step/log, 0 MP4, clean shutdown |

### S1c limitations (still test/stand-in)

1. **Not game capture**: the target is a synthetic animated Tk window. VCMI/VDD
   window capture (battle etc.), real window sizes and long runs are not tested.
2. **No audio in S1c mode**: `capture_audio=false` on purpose; the AAC track is
   silence. S2a (below) adds a separate raw-file audio source; VCMI's real PCM
   path is only wired through that file source, not through win-wasapi.
3. **HDR**: this host's LG display is HDR; without `force_sdr=true` the WGC
   RGBA16F/`GS_CS_709_EXTENDED` path produced black frames in this SDR pipeline.
   `force_sdr=true` is used and recorded; the real HDR→SDR path is not validated.
4. **DPI**: the host is Per-Monitor-V2 aware (like obs64.exe); the DPI-unaware Tk
   window is captured at physical 960x720 for a 640x480 logical size. This is
   reported (`dpi_scale=1.5x1.5`) rather than hidden.
5. **Plugin source lifetime**: scene/window_capture sources are now destroyed
   **before** `obs_shutdown()` via `obs_canvas_scene_remove()` + destroy-queue
   flushing (the main canvas holds a `SCENE_REF` strong reference and
   `obs_shutdown()` unloads plugin modules before freeing sources). This is
   required for any plugin-provided source and is covered by the clean-shutdown
   checks.
6. **Not wired into the product**: standalone test exe; no MCP/Python/IPC/
   manifest integration; no rollover/recovery/long soak; GPL compliance for a
   libobs-linked host is still to be reviewed.

See `var/runs/obs-wgc-s1c-20261105-163717/S1C_REPORT.md` for the full report
(source-line/API references, per-file probes, DPI/HDR/fix notes).

## S2a WGC window + raw-file audio mode (`--audio-raw`)

Verified 2026-11-05 (repo HEAD `e510405`): **47/47 independent checks PASS**,
evidence in `var/runs/obs-wgc-audio-s2-20261105-172903/`. It adds a **native
audio-only libobs source** that reads VCMI's existing
`game-audio.raw` (s16le interleaved, 44100 Hz, stereo) and records it **together
with the S1c WGC window source in one scene and one output** — no WASAPI,
no audio device, no playback, no monitoring.

What it does:

1. registers a custom source id `a3_raw_audio` with
   `OBS_SOURCE_AUDIO|OBS_SOURCE_ASYNC`; settings `path` + `start_at_zero`
   (default **false** = start at the current **EOF**, so only data appended by
   the running game is consumed; `--audio-start-zero` is the test mode that reads
   a static file from byte 0);
2. one reader thread per source opens the file with
   `CreateFileW(... FILE_SHARE_READ|WRITE|DELETE)`, seeks to 0/EOF, then every
   100 ms (4410 frames) converts interleaved s16le → `FLOAT_PLANAR` (+-1.0,
   /32768) and calls `obs_source_output_audio()` with an absolute
   `os_gettime_ns()` timestamp grid;
3. handles no-data explicitly: missing file is retried (host fails with **exit
   9** after a 3 s timeout if it never opens); an open file with fewer than 4410
   new frames pushes a **silent** chunk on the same grid and counts
   `silence_chunks/eof_waits` in `selftest.json` (no silent stall);
4. creates the audio source **in the same `obs_scene`** as `window_capture`, so
   the scene's audio mix feeds the same `ffmpeg_aac` encoder → same
   `mp4_output` (`split_file=true`, `max_time_sec=3`), with `capture_audio=false`
   on the window source and `monitoring_type=NONE`/volume 1.0 on the audio
   source;
5. records 8 s and writes a `raw_audio` evidence block: settings JSON,
   `output_flags`, opened/open_error/open_attempts, start_offset,
   file_size_at_open, bytes_read, frames_read, chunks_pushed, silence_chunks,
   eof_waits, samples_pushed, monitoring/volume/mixers;
6. stops the reader after the output drained (idempotent) and only then tears
   down libobs.

### S2a run / verification

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File platform\obs-recorder\scripts\s2-smoke.ps1

# recorder only (Tk target window must already exist)
platform\obs-recorder\build-media\bin\obs-media-selftest.exe --mode window `
  --window-pid <pid> --window-title "VCMI WGC Synthetic" `
  --audio-raw E:\path\to\game-audio.raw --audio-start-zero `
  --out-dir var\runs\obs-wgc-audio-manual --duration-ms 8000 --max-time-sec 3
```

`s2-smoke.ps1` generates a 17 s s16le/44100/2 tone file offline
(`gen-test-raw-tone.py`, no ffplay/playback), starts the same synthetic Tk
window, and runs four recorder paths (static tone + start-zero, static file + EOF
default, missing file, real-time growing file).

Observed result of the verified run:

| Item | Observed |
|---|---|
| Audio source | `a3_raw_audio`, output_flags `0x6` (AUDIO\|ASYNC), s16le→FLOAT_PLANAR, 4410-frame chunks, volume 1.0, mixers 255, `monitoring_type=NONE` |
| Static tone + start-zero | exit 0; 3 MP4s 3.994/2.983/1.017 s; `samples_pushed=396900`, `bytes_read=1587600` (=176400 B/s×9 s), `silence_chunks=0` |
| Audio in MP4 | **AAC max_volume −11.8/−11.9/−12.0 dB** on every file (tone present); video 1920×1080@60 H.264, YAVG 98.8/62.5/61.6, YMAX≈240, unique frames 59/60+ |
| Full decode | `ffmpeg -v error -i <file> -f null -` → **zero errors** on all files; A/V container delta **41.7 ms** |
| EOF default + static file | exit 0; `start_offset=2998800` (EOF), `bytes_read=0`, `samples_pushed=0`, **`silence_chunks=90/eof_waits=90`**, AAC −91 dB per file (explicit no-data) |
| Missing file | **exit 9**; `raw_audio_opened FAIL` with `win32 error 2` after 61 open attempts; `status=fail/exit_code=9`; 0 MP4; clean `obs_shutdown_done` |
| Live growth | real-time appended file, default EOF: `start_offset=264600`, `bytes_read=1569960`, `chunks=89`, `silence_chunks=1`; **AAC −11.8/−12.0/−12.0 dB** |
| Real VCMI raw (offline) | 15-min real `game-audio.raw` (176,898,048 B) replayed from byte 0 → AAC max/mean −11.5/−24.2, −11.8/−29.6, −30.0/−43.4 dB; full decode 0 errors (not live VCMI) |
| No audio device | 98 live modules: **no MMDevAPI/audioses/wdmaud/win-wasapi**; only 4 whitelist modules (incl. win-capture); no obs64/UI/WebSocket/game/ffplay |

### S2a limitations (still test/stand-in)

1. **Not live VCMI**: the video is the synthetic Tk window; live VCMI via MCP
   `session_start(display_index=2)` was not run. Real-VCMI PCM is only replayed
   offline from an existing 15-min session file.
2. **No audio device path** by design (no WASAPI/MMDevice/playback/monitoring);
   that also means device capture was not exercised (not needed).
3. **Silence filling**: if the file grows slower than real time, the source
   pushes up to one 100 ms silent chunk per starvation and counts it; there is no
   PLC/sample-accurate resync. A static file with the default EOF start therefore
   records a silent AAC track (intended and visible in stats).
4. **100 ms chunk latency**; A/V acceptance is container-level (41.7 ms max
   delta), no per-frame sync/drift measurement.
5. **Short single runs** (4 paths × 8 s + 8 s offline real raw); no long soak,
   1.5 GB rollover, source-restart/crash recovery.
6. **Not wired into the product**: standalone test source/exe, no MCP/Python/IPC/
   manifest integration; old Python recorder remains the only product backend.
7. GPL-2.0-or-later compliance and OBS-version pinning are the same open items
   as S1b/S1c.

See `var/runs/obs-wgc-audio-s2-20261105-172903/S2A_REPORT.md` for the full
report (source design, per-file probes, all four run outcomes, limitations).

## S3a named-pipe IPC server (`--serve --pipe`)

Verified 2026-11-05 (repo HEAD `53453af`): **58/58 independent checks PASS**,
evidence in `var/runs/obs-ipc-s3-20261105-181821/` (and the earlier
`obs-vcmi-live-s2b-*` run for the live CLI path). It makes the media pipeline
controllable through a named pipe **inside our own process**, with libobs
initialised exactly once and torn down only at shutdown.

What it does:

1. `obs-media-selftest.exe --serve --pipe <name>` initialises libobs + base
   modules + audio/video + `win-capture` once (`--pipe` default
   `auto-heros3-media-<pid>`), prints `SERVE_READY … protocol=1`, and accepts a
   byte-mode named-pipe JSON-lines client;
2. commands `hello`, `capabilities`, `prepare`, `start`, `status`, `stop`,
   `shutdown`; every response echoes `request_id` and a monotonic `seq`;
   malformed/unknown/out-of-state requests return explicit
   `error.code/error.message`;
3. `prepare {out_dir, window_pid|window_title/class/exe, audio_raw,
   max_time_sec?, duration_ms?}` validates/creates `out_dir`, validates the
   window through libobs' helpers and stats the raw file, then moves to
   `prepared`; missing window/raw are explicit errors;
4. `start` builds the S2a WGC `window_capture` + `a3_raw_audio` sources in one
   scene and starts the existing x264/AAC/`mp4_output` (split) pipeline; a
   second `start` is idempotent (`already_running=true`);
5. `status` reports `active`, `recording_elapsed_ms`, `auto_stop_in_ms`,
   segments/files, output counters and raw-audio stats while recording;
   `duration_ms>0` auto-stops and pushes an `event=auto_stop` line;
6. `stop` stops/drains the output, writes `selftest.json` **and**
   `manifest.json` (schema `s3a-ipc-manifest/1`) and returns the result;
   repeated `stop` is idempotent (`already_stopped=true`);
7. `shutdown` finalises any active recording, releases libobs objects, unloads
   modules and exits 0.

### S3a run / verification

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File platform\obs-recorder\scripts\s3a-ipc-smoke.ps1

# server only
platform\obs-recorder\build-media\bin\obs-media-selftest.exe --serve --pipe auto-heros3-test
# then connect and send one JSON object per line, e.g.
#   {"cmd":"hello","request_id":"r1"}
#   {"cmd":"prepare","request_id":"r2","out_dir":"...","window_pid":1234,"audio_raw":"...","max_time_sec":2,"duration_ms":6000}
#   {"cmd":"start","request_id":"r3"}
#   {"cmd":"status","request_id":"r4"}
#   {"cmd":"stop","request_id":"r5"}
#   {"cmd":"shutdown","request_id":"r6"}
```

The smoke script starts a synthetic animated Tk window and a real-time growing
s16le/44100/2 tone file, then drives two server sessions over a real
`NamedPipeClientStream` (auto-stop and explicit-stop), validates all protocol
responses/errors/idempotency, the produced MP4s and selftest/manifest, samples
the live server process modules, and checks the audio endpoint before/after.

Observed result of the verified run:

| Item | Observed |
|---|---|
| Build | exe 303,616 B, SHA-256 `db1ce2e6…f64f4c0`; libobs 32.1.0; 1,782 SDK exports |
| Protocol | 7 commands, protocol=1, `SERVE_READY`/`SERVE_EXIT` logs; request_id echoed; seq monotonic |
| Explicit errors | `invalid_json`, `unsupported_command`, `not_prepared`, `window_not_found`, `audio_raw_not_found`, `already_prepared` all returned as `ok=false` + code |
| Idempotency | second `start` → `already_running=true`; second `stop` → `already_stopped=true`; stop before prepare is ok/idempotent |
| Scenario A (auto-stop 6 s) | status live: `recording, active=true, elapsed=1223 ms, auto_stop_in_ms=4776`; `auto_stop` event `report_rc=0 checks_ok=true files=3`; 3 MP4s 2.995/1.983/0.983 s |
| Scenario B (manual stop) | 6.2 s then `stop` → `checks_ok=true`, files=3; 3 MP4s 2.995/1.983/1.200 s |
| Files | every segment h264 1920×1080@60 + AAC 44100/2; `ffmpeg -v error` 0 errors |
| Audio | every segment AAC max_volume −12.0…−11.6 dB (tone; >−40); raw source `start_at_zero=false`, `start_offset=529200`, `bytes_read=1,199,520`, `samples=299,880`, `monitoring=NONE` |
| Video | YAVG 62.5–87.3, YMAX 236–241, framemd5 unique ~58–60/60 (non-black, changing) |
| No device/playback | live server process: 98 modules, no MMDevAPI/audioses/wdmaud/win-wasapi; win-capture/libobs-winrt present; no obs64/UI/WebSocket/ffplay |
| Endpoint | before/after identical (`Realtek USB Audio`, mute=false, volume=0.4) |
| Cleanup | both servers exit 0, `CLEANUP obs_shutdown_done`, no leftover processes |
| selftest/manifest | selftest status pass, mode `s3a-ipc-wgc-window-plus-raw-file-audio-to-split-mp4`; manifest status pass, report_exit_code 0, 3 files |

### S3a limitations (still test/stand-in)

1. **Single recording per process**: one prepare→start→stop lifecycle; a second
   prepare returns `already_prepared/session_finished`; a new process is needed
   for another recording. No reconnect/resume/multi-session.
2. **One client at a time**; client disconnect while recording finalises the
   recording (implemented, not separately tested). No authentication.
3. **Synthetic inputs only in this run**: Tk window + real-time appended tone
   raw; the real VCMI live path was proven for the CLI (S2b) but **not yet
   driven over S3a IPC**.
4. **Auto-stop is serviced at event-loop polling points** (~5–20 ms); a steady
   request stream can defer it by one loop. Shutdown-while-recording exists but
   is not separately tested.
5. **No rollover/1.5 GB/long soak/crash recovery**; selftest/manifest are the
   simplified S3a versions, not the product state machine.
6. **Not the final product binary**: still `obs-media-selftest.exe`; the
   S0/S1a `obs-recorder.exe` remains a separate scaffold. No MCP/Python client
   library is added (per scope).
7. The historical S0 smoke's `sdk_headers_absent` check is now false because
   `vendor/obs-studio` headers are required for S1b+; S0 `build.ps1` still
   exits 0 and its IPC checks still pass.

## Remaining blockers / next steps after S1b/S1c/S2a/S3a

1. **Product host + S3b**: move the S3a server into the final
   `auto-heros3-recorder.exe` (single long-lived binary), connect it to the
   product MCP/Python recorder, and keep the old Python recorder as the only
   usable backend until end-to-end verification.
2. **Real VCMI over IPC**: drive the S3a `prepare` with the live VCMI window/raw
   sink (S2b recipe), add source restart/reconnect, and verify battle content
   and long runs through the server.
3. **Rollover/recovery**: size-triggered split (`max_size_mb`), 5–10 min+ soak,
   crash-safe manifest/state machine, watchdog, clean restart; multi-recording
   lifecycle on one process.
4. **A/V acceptance**: real-content listening/visual checks, drift measurement,
   per-frame sync, not just container-level durations.
5. **Distribution/licensing**: confirm GPL-2.0-or-later obligations for a host
   that links libobs (and win-capture) and ships the generated import
   library/build scripts.

## Non-goals of the S0/S1a scaffold (unchanged)

- Starting the OBS GUI (`obs64.exe`) — never done.
- Recording, screen capture, audio capture/playback — no code path exists in
  `obs-recorder.exe` (the separate S1b self-test uses synthetic media only).
- Claiming codec/container/capture capability without a real probe.
- Touching Python product code or tracked docs.
