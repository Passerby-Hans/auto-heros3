# Private a3 obs-outputs module (test-only)

Built from vendored OBS 32.1.0 source with these local patches:

- `mp4-output.c`: split T0 logging + `mp4_mux_set_audio_delay(46440)` at every new segment.
- `mp4-mux.c/.h/internal.h`: audio edit list with an empty edit before the first audio media entry
  (sample-accurate presentation delay; no packet timestamp rewrite).
- Audio packet trim API is present but not used because AAC packet PTS is later than the
  video keyframe while the packet's decoded content starts earlier.

Build artifact (ignored):
`platform/obs-recorder/build-media/a3outputs/a3-obs-outputs.dll`

Load only in tests with:
`AUTO_HEROS3_OUTPUTS_DLL=<path>/a3-obs-outputs.dll`

This directory is not yet committed; it is a test-only output module.
