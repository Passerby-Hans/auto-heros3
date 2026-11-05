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
// Explicit no-data handling: if the file does not exist yet the reader retries
// and the host fails with an explicit error if it never opens; if the file is
// open but has no new bytes for a chunk period, a silent FLOAT_PLANAR chunk is
// pushed on the same timestamp grid and counted (`silence_chunks`,
// `eof_waits`) so the audio timeline stays continuous and the condition is
// visible in the report instead of silently stalling.
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
    uint64_t silence_chunks = 0;    // pushed because no complete chunk was available
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
