# Private a3 obs-outputs module

Test/experimental output module derived from OBS Studio 32.1.0 `plugins/obs-outputs`.

Local changes:

- split-trigger logging for video keyframe and audio packet coverage;
- audio edit-list handling:
  - head trim for the packet containing the video T0;
  - tail trim for the old segment;
  - no fixed 46.44 ms compensation in the successful path;
- final-packet flush so the last queued audio/video packet is not dropped;
- `tkhd` and `elst` presentation durations use a single sample-domain calculation.

The module is **not** intended as a default OBS installation module; it is an
experimental artifact for the auto-heros3 native recording path.

## License

Derived from OBS Studio, licensed GPL-2.0-or-later. See the repository
`vendor/obs-studio/COPYING` and `COPYING.LGPL` and the OBS source distribution
for the complete corresponding source. Local modifications are in this
directory; build instructions and the OBS source version are in
`../scripts/build-private-outputs.ps1` and `../NOTICE.md`.

The binary artifact is not committed. A build manifest is written to
`../bin/a3-obs-outputs.build.json` when built.
