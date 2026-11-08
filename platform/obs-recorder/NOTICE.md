# Artifact / license notice — native recording experimental path

Date: 2026-11-08

## OBS-derived output module

`platform/obs-recorder/custom-output/` is derived from OBS Studio 32.1.0
`plugins/obs-outputs`. It is licensed under **GPL-2.0-or-later**.

- source: local `vendor/obs-studio` (not committed) and `custom-output/` (committed)
- exact local modifications: `custom-output/`
- build script: `scripts/build-private-outputs.ps1`
- artifact: `bin/a3-obs-outputs.dll` (not committed)
- build manifest: `bin/a3-obs-outputs.build.json` (not committed)
- corresponding-source delivery: the GPL source tree + local patch files + build
  script; binary recipients must receive the corresponding source and build
  materials required by GPL-2.0-or-later.

The DLL is an experimental artifact, not a replacement for the OBS installation.
It must only be loaded through the explicit `AUTO_HEROS3_OUTPUTS_DLL` /
`--outputs-module` test path.

## SDL2 a3capture build

`platform/sdl-capture-patch/` patches SDL2 **2.32.10**, which is zlib-licensed.
The private DLL and its source patch are separate from the GPL OBS output
module. Modifications are disclosed in `platform/sdl-capture-patch/` and
`source-lock.json`; the staged DLL is not committed.

## Evidence/video artifacts

`var/` recordings, MP4 review files and frames are evidence artifacts, not
source. They are not part of the source delivery and are excluded by
`.gitignore`.

## Default policy

The native path is experimental. The repository default is native only for
this checkpoint's controlled tests; the documented manual fallback is:

```text
AUTO_HEROS3_AUDIO_DRIVER=disk
recording_start(backend='legacy')
```

Default switching and artifact distribution still require the Codex review
and the remaining P2 items.
