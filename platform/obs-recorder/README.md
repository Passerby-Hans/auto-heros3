# obs-recorder — S0/S1a native host scaffold

Native Windows host process (`obs-recorder.exe`) that **inspects the installed OBS
runtime via dynamic loading** and exposes a **local named-pipe JSON-lines IPC
skeleton**. This is deliberately *not* a recorder yet.

- Status: **S0/S1a scaffold only** (verified 2026-11-05, repo HEAD `88cf273`,
  branch `feature/av1b-vdd-wgc`).
- It never calls `obs_startup`/`obs_shutdown`, never creates a video/audio/output
  subsystem, never captures the screen, never records, never plays audio.
- The OBS GUI is never started by any tool in this directory.

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

## Layout

```
platform/obs-recorder/
  CMakeLists.txt
  .gitignore                     # ignores /build/
  README.md
  scripts/
    build.ps1                    # vcvars64 + CMake/Ninja build, writes build_summary.json
    s0-smoke.ps1                 # reproducible S0 evidence run (build/capabilities/IPC)
  src/
    main.cpp                     # CLI modes + capabilities JSON assembly
    json_util.hpp/.cpp           # minimal JSON writer
    win_util.hpp/.cpp            # paths, Windows version resources, OS info
    obs_probe.hpp/.cpp           # dynamic obs.dll load, symbol probe, SEH-guarded version calls
    ipc_pipe.hpp/.cpp            # named-pipe server + probe client + in-process self-test
```

Build output (git-ignored): `platform/obs-recorder/build/`
– `bin/obs-recorder.exe`
– `build_summary.json`
– `logs/{toolchain,configure,build}.log`

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

## Blockers and next prerequisite (S1b)

1. **No OBS SDK headers / import library.** `sdk_headers=false`,
   `sdk_import_lib=false`; this is why the S0/S1a binary is pure
   `LoadLibrary`/`GetProcAddress`. To use the real libobs lifecycle, add OBS
   Studio **32.1.0** source headers (`libobs/obs.h`, `obsconfig.h`, related) and
   a matching `obs.lib` import library (or the official obs-deps package), or
   keep the dynamic-loading route with hand-declared ABI for the needed subset.
2. **`obs_startup` lifecycle out of scope.** It needs locale/module/data path
   wiring (`obs_add_data_path`, `obs_add_module_path`), then
   `obs_load_all_modules`/`obs_post_load_modules`, plus video/audio/output
   subsystems for real media — none of this is safe or meaningful in a
   capabilities probe, so it is deliberately not called.
3. **Media capability probes are not implemented.** WGC capture, x264 encoding,
   AAC encoding and MP4 muxing must be probed after startup and plugin load
   before any WGC/x264/AAC/MP4 claim is made. File presence alone is not evidence.
4. **Long-lived host concerns** (crash-safe shutdown, leak checks, watchdog,
   reconnection) are not designed yet.

## Non-goals of this scaffold

- Starting the OBS GUI (`obs64.exe`) — never done.
- Recording, screen capture, audio capture/playback — no code path exists.
- Claiming codec/container/capture capability without a real probe.
- Touching Python product code or tracked docs.
