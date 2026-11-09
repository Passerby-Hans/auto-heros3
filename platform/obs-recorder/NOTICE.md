# Artifact / license notice — native recording experimental path

Date: 2026-11-09

## OBS-derived output module

`platform/obs-recorder/custom-output/` is derived from OBS Studio 32.1.0
`plugins/obs-outputs`. It is licensed under **GPL-2.0-or-later**.

- source: local `vendor/obs-studio` (not committed) and `custom-output/` (committed)
- exact local modifications: `custom-output/`
- build script: `scripts/build-private-outputs.ps1`
- artifact: `bin/a3-obs-outputs.dll` (not committed)
- build manifest: `bin/a3-obs-outputs.build.json` (latest convenience copy)
- original build records: `bin/provenance/<build-id>.start.json` and
  `<build-id>.json` (not committed; retained for every build)
- corresponding-source delivery: the GPL source tree + local patch files + build
  script; binary recipients must receive the corresponding source and build
  materials required by GPL-2.0-or-later.

The DLL is an experimental artifact, not a replacement for the OBS installation.
The native recorder resolves the private module from the project artifact
directory or the explicit `AUTO_HEROS3_OUTPUTS_DLL` / `--outputs-module` path.

## SDL2 a3capture build

`platform/sdl-capture-patch/` patches SDL2 **2.32.10**, which is zlib-licensed.
The private DLL and its source patch are separate from the GPL OBS output
module. Modifications are disclosed in `platform/sdl-capture-patch/` and
`source-lock.json`; the staged DLL is not committed.
The source lock describes the input recipe. Builds no longer rewrite it with
artifact hashes; `stage/build.json` and its referenced provenance record bind
the built DLL to the source. Historical `private_dll_*` values in older source
locks are not proof of a current build.

## Frozen build and artifact identity

After committing the reviewed source, use its complete 40-character commit:

```powershell
pwsh -NoProfile -File platform/obs-recorder/scripts/build-native-checkpoint.ps1 -ExpectedCommit <full-commit>
```

This reuses the recorder, private output and private SDL build scripts with
`-RequireClean`; CMake targets are rebuilt with `--clean-first`. It performs no
game, recording, playback, or SDL smoke run. Tests and media validation are a
separate step, bound to the artifact hashes in `bin/native-checkpoint.json`.

Each build captures commit/tree/dirty status **before compilation**, individual
source/header/dependency hashes, vendor commit, actual compiler/tool hashes and
file versions, configuration, and output hashes. The end check rejects a
changed checkout or changed inputs. Original start/final records use create-new
filenames; later commits never update them. The latest summary may be replaced
by another build and is not the historical record. This is audit provenance,
not a digital signature or a claim of bit-for-bit reproducible binaries.

An interrupted build has a start record without a successful final record.
`built_unvalidated` in the unified manifest means compilation succeeded only.
Do not replace its source identity after tests or a subsequent commit.

For binary delivery, archive the unified manifest, its referenced original
records, artifacts, the exact project commit, the matching OBS corresponding
source tree (including bundled dependencies/license notices), the pinned SDL
archive and patches, and the build scripts/toolchain requirements. A local
ignored vendor directory or a README reference alone is not a delivered source
bundle. Keep validation reports separate and identify the tested hashes.

## Evidence/video artifacts

`var/` recordings, MP4 review files and frames are evidence artifacts, not
source. They are not part of the source delivery and are excluded by
`.gitignore`.

## Default policy

The native path is experimental. The repository default is **legacy/disk**;
the documented manual fallback from a failed native attempt is:

```text
AUTO_HEROS3_AUDIO_DRIVER=disk
recording_start(backend='legacy')
```

Default switching and artifact distribution still require the Codex review
and the remaining P2 items.
