# obs-recorder — S0/S1a native host scaffold + S1b libobs media self-test

Native Windows host process (`obs-recorder.exe`, **S0/S1a**) that **inspects the
installed OBS runtime via dynamic loading** and exposes a **local named-pipe
JSON-lines IPC skeleton**, plus a second, opt-in executable
(`obs-media-selftest.exe`, **S1b**, verified 2026-11-05) that **links libobs**
and records a synthetic in-memory H.264+AAC signal to **split Hybrid MP4** files.

- S0/S1a scaffold: **verified 2026-11-05**, repo HEAD `88cf273` (branch
  `feature/av1b-vdd-wgc`); it never calls `obs_startup`/`obs_shutdown`, never
  creates a video/audio/output subsystem, never captures, never records, never
  plays audio.
- S1b media self-test: **verified 2026-11-05**, repo HEAD `5e153d6`; it does
  call `obs_startup` and the real media pipeline (see the dedicated S1b section
  below). It is a **test host with synthetic media only** — no screen capture
  of the game, no game PCM, not wired into MCP/Python, and not the product
  recorder.
- The OBS GUI (`obs64.exe`) is never started by any tool in this directory.

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
    gen-obs-sdk.ps1              # S1b: generate obs.lib + obsconfig.h from installed obs.dll
    build-media-selftest.ps1     # S1b: build obs-media-selftest.exe (links libobs)
    s1b-smoke.ps1                # S1b: build + record + ffprobe/ffmpeg validation, writes summary.json
  src/
    main.cpp                     # CLI modes + capabilities JSON assembly
    json_util.hpp/.cpp           # minimal JSON writer
    win_util.hpp/.cpp            # paths, Windows version resources, OS info
    obs_probe.hpp/.cpp           # dynamic obs.dll load, symbol probe, SEH-guarded version calls
    ipc_pipe.hpp/.cpp            # named-pipe server + probe client + in-process self-test
    media/
      gen_av_source.hpp/.cpp     # S1b in-memory async AV source (I420 frames + float PCM)
      media_selftest.cpp         # S1b obs-media-selftest.exe host
```

Build output (git-ignored): `platform/obs-recorder/build/`
– `bin/obs-recorder.exe`
– `build_summary.json`
– `logs/{toolchain,configure,build}.log`

S1b build output (git-ignored): `platform/obs-recorder/build-media/`
– `bin/obs-media-selftest.exe` (+ staged OBS runtime DLLs)
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

What it does (and only this):

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
   pattern and the audio is a generated 440 Hz sine. No WGC/game capture, no
   VCMI game PCM (`win-capture`/`win-wasapi` not loaded or tested).
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

## Remaining blockers / next steps after S1b

1. **Real capture inputs (S1c)**: feed the same libobs pipeline with WGC desktop
   capture (`win-capture.dll`) and/or VCMI's real audio/PCM path; this requires
   loading additional modules (`win-capture`, `win-wasapi`) and testing against
   the game/VDD without breaking the S0/S1a host.
2. **Product integration**: expose start/stop/status through the existing
   `obs-recorder` IPC and the MCP/Python recorder interface; keep the old Python
   recorder as the only usable backend until that is verified end-to-end.
3. **Rollover/recovery**: size-triggered split (`max_size_mb`), 5–10 min+ soak,
   crash-safe manifest/state machine, watchdog, clean restart.
4. **A/V acceptance**: real-content listening/visual checks, drift measurement,
   per-frame sync, not just container-level durations.
5. **Distribution/licensing**: confirm GPL-2.0-or-later obligations for a host
   that links libobs and ships the generated import library/build scripts.

## Non-goals of the S0/S1a scaffold (unchanged)

- Starting the OBS GUI (`obs64.exe`) — never done.
- Recording, screen capture, audio capture/playback — no code path exists in
  `obs-recorder.exe` (the separate S1b self-test uses synthetic media only).
- Claiming codec/container/capture capability without a real probe.
- Touching Python product code or tracked docs.
