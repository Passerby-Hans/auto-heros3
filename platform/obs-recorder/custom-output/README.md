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

## Stop and presentation contract

The host records one immutable stop request in the capture clock. The output
finishes any rollover already in progress, then uses the next H.264 IDR whose
capture timestamp is at or after that request as an exclusive presentation
boundary. The IDR itself is not written. Sources, encoders and libobs's
interleaver stay active until the preceding closed GOP and the AAC packet
covering that boundary have arrived. The audio tail is trimmed in the sample
domain, and completion is published after mux finalization and writer close.

This is a bounded continuation to a decodable boundary, not an encoder flush
API or an exact trim at the caller's wall-clock instant. With the host's one
second GOP the boundary can extend the request by up to one GOP, plus the
time required to finish an already pending rollover. The host has a ten second
stop deadline; timeout/forced stop is a failed or partial recording.

The checked profile is one `obs_x264` H.264 track with closed GOPs and one
44.1 kHz AAC track, on an integral audio sample/video frame grid. The current
host uses 1080p60, `scenecut=0 open-gop=0`, and no presentation-delay offset.
Unsupported formats, missing capture-clock metadata, packet gaps, incomplete
source coverage or a writer error fail closed. Contract version 2 records the
request, fixed fence, encoded endpoints and source coverage separately.

MP4 edit lists define the presented samples. An AAC access unit may contain
decoder pre-roll or samples past a segment boundary; these may be present in
raw decoder output even though the edit list excludes them. Concatenating
uncropped decoded PCM is not a supported way to join these segments.
`verify_decoded_continuity.py` reports raw decode extras separately from
source continuity within the declared presentation intervals.

## License

Derived from OBS Studio, licensed GPL-2.0-or-later. See the repository
`vendor/obs-studio/COPYING` and the OBS source distribution
for the complete corresponding source. Local modifications are in this
directory; build instructions and the OBS source version are in
`../scripts/build-private-outputs.ps1` and `../NOTICE.md`.

The binary artifact is not committed. A build manifest is written to
`../bin/a3-obs-outputs.build.json` when built. It points to a create-new original
record in `../bin/provenance/`, containing the pre-build source identity and
individual input hashes. Do not edit a build record to attach a later commit.

For a frozen checkpoint use `../scripts/build-native-checkpoint.ps1` with the
complete reviewed commit. See `../NOTICE.md` for build/validation boundaries and
the corresponding-source artifact delivery checklist.
