// A4 native source: consume a3-audio/1 framed PCM and push timestamped
// FLOAT_PLANAR audio into libobs without resampling or silence injection.
#pragma once

#include <cstdint>
#include <string>

namespace a3 {

struct FramedAudioConfig {
    std::string path;
    uint32_t sample_rate = 44100;
    uint32_t channels = 2;
};

struct FramedAudioStats {
    bool created = false;
    bool opened = false;
    std::string path;
    std::string error;
    uint64_t file_bytes = 0;
    uint64_t packets = 0;
    uint64_t frames = 0;
    uint64_t pushed_packets = 0;
    uint64_t pushed_frames = 0;
    uint64_t crc_errors = 0;
    uint64_t sequence_gaps = 0;
    uint64_t discontinuity_packets = 0;
    uint64_t first_timestamp_ns = 0;
    uint64_t last_timestamp_ns = 0;
    uint64_t capture_id = 0;
    uint32_t generation = 0;
    bool saw_first = false;
    bool saw_last = false;
};

void RegisterFramedAudioSource();
void StopFramedAudioSourceThread();
FramedAudioStats GetFramedAudioStats();

} // namespace a3
