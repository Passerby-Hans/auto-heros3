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
    bool live = false;
    bool start_at_zero = true;
    uint64_t replay_origin_ns = 0;
    uint64_t late_budget_ns = 500000000ULL;
};

struct FramedAudioStats {
    bool created = false;
    bool opened = false;
    std::string path;
    std::string mode;
    std::string error;
    std::string error_code;
    uint64_t file_bytes = 0;
    uint64_t validated_packets = 0;
    uint64_t validated_frames = 0;
    uint64_t submitted_packets = 0;
    uint64_t submitted_frames = 0;
    uint64_t crc_errors = 0;
    uint64_t sequence_gaps = 0;
    uint64_t sample_gaps = 0;
    uint64_t sample_overlaps = 0;
    uint64_t discontinuity_packets = 0;
    uint64_t overflow_packets = 0;
    uint64_t producer_error_packets = 0;
    uint64_t late_packets = 0;
    uint64_t preroll_skipped_packets = 0;
    uint64_t preroll_skipped_frames = 0;
    uint64_t submitted_gaps = 0;
    uint64_t max_packet_age_ns = 0;
    uint64_t first_timestamp_ns = 0;
    uint64_t last_timestamp_ns = 0;
    uint64_t capture_id = 0;
    uint32_t generation = 0;
    uint64_t first_sample = 0;
    uint64_t validated_end_sample = 0;
    uint64_t submitted_start_sample = 0;
    uint64_t submitted_end_sample = 0;
    uint64_t pending_bytes = 0;
    uint64_t qpc_frequency = 0;
    uint64_t capture_epoch_qpc = 0;
    bool validation_performed = false;
    bool packet_integrity_ok = true;
    bool stream_integrity_ok = true;
    bool timeline_ok = true;
    bool saw_first = false;
    bool saw_last = false;
};

void RegisterFramedAudioSource();
void StopFramedAudioSourceThread();
FramedAudioStats GetFramedAudioStats();

} // namespace a3
