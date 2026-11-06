// S2a raw-file audio source for auto-heros3.
//
// Reads VCMI's existing raw game audio file (s16le interleaved, 44100 Hz,
// stereo) and pushes FLOAT_PLANAR stereo PCM into libobs through an
// asynchronous audio-only source (OBS_SOURCE_AUDIO | OBS_SOURCE_ASYNC).
// There is no playback, no WASAPI/MMDevice access, no monitoring: the samples
// only enter libobs' in-memory audio mix.
//
// Modes:
//   * default: start reading at EOF (live capture semantics: VCMI appends to
//     game-audio.raw while recording; old samples are not replayed);
//   * start_at_zero=true: read from the beginning (test mode for a static file).
//
// Input is variable-length, output is wall-clock continuous: the reader takes
// whatever real frames are available and, when they are fewer than 4410, linearly
// resamples them to a full 4410-frame 100 ms output chunk.  It no longer
// manufactures all-zero chunks for partial input.  The source declares 44100 Hz,
// so the AAC mux remains 44100; small pitch changes from the stretch are accepted.
// Missing file: retried; no real samples after startup is counted as underflow.
#pragma once

#include <cstdint>
#include <string>

namespace a3 {

struct RawAudioConfig {
    std::string path;
    bool start_at_zero = false;
    uint32_t sample_rate = 44100;
    uint32_t channels = 2;
    uint32_t chunk_frames = 4410; // 100 ms, same cadence used by S1b PCM source
};

struct RawAudioStats {
    bool created = false;
    bool opened = false;
    std::string path;
    std::string open_error;
    uint32_t open_attempts = 0;
    uint64_t start_offset = 0;      // 0 for start_at_zero, else initial file size
    uint64_t file_size_at_open = 0;
    uint64_t bytes_read = 0;
    uint64_t frames_read = 0;
    uint64_t chunks_pushed = 0;     // real (non-silence) chunks
    uint64_t silence_chunks = 0;    // legacy zero-filled chunks (must stay 0 in live capture)
    uint64_t partial_chunks = 0;    // ticks whose real input was shorter than chunk_frames
    uint64_t resampled_chunks = 0;  // partial real input stretched to a full 4410-frame output
    uint64_t underflow_events = 0;  // ticks after first real data with no available frames
    uint64_t startup_underflow_events = 0; // pre-roll ticks before the first real sample
    uint64_t eof_waits = 0;         // read attempts that hit current EOF
    uint64_t samples_pushed = 0;    // real sample frames pushed
};

// Registers the source type with id "a3_raw_audio".  Call after obs_startup().
void RegisterRawAudioSource();

// Stops the last-created source's reader/worker thread (idempotent).  Called by
// the host after the output drained and before libobs teardown.
void StopRawAudioSourceThread();

// Snapshot of the last-created source instance (test evidence only).
RawAudioStats GetRawAudioStats();

} // namespace a3
