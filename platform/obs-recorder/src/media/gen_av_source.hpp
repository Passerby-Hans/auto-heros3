// S1b in-memory test source: synthetic video frames + PCM sine tone, pushed
// into libobs as an asynchronous audio/video source (no capture, no playback).
//
// This header is intentionally small: the source is registered into libobs
// from the host executable (obs_register_source_s), modelled on OBS' own
// libobs test sources:
//   vendor/obs-studio/test/test-input/sync-async-source.c
//   vendor/obs-studio/test/test-input/test-random.c
//   vendor/obs-studio/test/test-input/test-sinewave.c
#pragma once

#include <cstdint>

namespace a3 {

struct AvSourceConfig {
    uint32_t width = 1920;
    uint32_t height = 1080;
    uint32_t fps = 60;
    uint32_t audio_rate = 44100;   // must match obs_reset_audio()
    uint32_t audio_channels = 2;   // stereo float planar
    uint32_t audio_chunk_frames = 4410; // 100 ms per pushed PCM block
    double tone_frequency_hz = 440.0;
    double tone_amplitude = 0.2;   // 0.2 full scale (never near clipping)
};

// Registers the source type with id "a3_test_av_source".  Must be called after
// obs_startup() (the type table only exists while libobs is initialized).
void RegisterAvTestSource();

// Stops the last-created source instance's media-generating worker thread
// (idempotent).  The host calls this once recording has finished, before
// libobs teardown, so no synthetic frames/PCM can be pushed into libobs while
// the video/audio subsystems are being shut down.  The source object itself is
// still owned/released by libobs as usual.
void StopAvTestSourceThread();

// Snapshot of how much media the last-created source instance pushed into
// libobs.  Test-only convenience so the host can record evidence; it does not
// affect the media path.
struct AvSourceStats {
    bool created = false;
    uint64_t video_frames_pushed = 0;
    uint64_t audio_chunks_pushed = 0;
    uint64_t audio_frames_pushed = 0;
};
AvSourceStats GetAvSourceStats();

} // namespace a3
