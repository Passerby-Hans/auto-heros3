// A4 native source: consume a3-audio/1 framed PCM and push timestamped
// FLOAT_PLANAR audio into libobs without resampling or silence injection.
#pragma once

#include <cstdint>
#include <string>
#include "audio_delivery_policy.hpp"

namespace a3 {

struct FramedAudioConfig {
    std::string path;
    uint32_t sample_rate = 44100;
    uint32_t channels = 2;
    bool live = false;
    std::string transport = "shared_memory";
    uint32_t expected_producer_pid = 0;
    bool start_at_zero = true;
    uint64_t replay_origin_ns = 0;
    uint64_t late_budget_ns = audio::kDefaultLateBudgetNs;
};

struct FramedStageTiming {
    uint64_t calls = 0;
    uint64_t last_ns = 0;
    uint64_t max_ns = 0;
    uint64_t slow_calls = 0; // >= 50 ms; diagnostic only, never changes delivery policy
};

struct FramedAudioStats {
    bool created = false;
    bool opened = false;
    std::string path;
    std::string mode;
    std::string transport;
    std::string map_name;
    uint32_t producer_pid = 0;
    uint64_t producer_creation_filetime = 0;
    uint64_t producer_fatal_code = 0;
    uint64_t archive_committed_end_sample = 0;
    uint64_t archive_lag_frames = 0;
    uint64_t archive_lag_max_frames = 0;
    uint64_t archive_drain_wait_ns = 0;
    uint64_t snapshot_retries = 0;
    bool archive_closed = false;
    bool archive_commit_ok = false;
    bool mmcss_registered = false;
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
    uint64_t last_packet_sequence = 0;
    uint64_t last_packet_sample = 0;
    uint64_t last_packet_offset = 0;
    uint64_t last_packet_pts_ns = 0;
    uint64_t last_packet_age_ns = 0;
    uint64_t first_submitted_age_ns = 0;
    uint64_t steady_max_packet_age_ns = 0;
    uint64_t preroll_max_packet_age_ns = 0;
    uint64_t eof_reopens = 0;
    FramedStageTiming open_timing;
    FramedStageTiming read_timing;
    FramedStageTiming parse_timing;
    FramedStageTiming submit_timing;
    FramedStageTiming loop_timing; // start-to-start; overlaps stage timings, not additive
    FramedStageTiming idle_timing;
    FramedStageTiming publish_lock_timing;
    bool fatal_latched = false;
    bool fatal_has_packet = false;
    uint64_t fatal_sequence = 0;
    uint64_t fatal_sample = 0;
    uint64_t fatal_now_ns = 0;
    uint64_t fatal_pts_ns = 0;
    uint64_t fatal_age_ns = 0;
    uint64_t fatal_file_offset = 0;
    uint64_t fatal_read_offset = 0;
    uint64_t first_timestamp_ns = 0;
    uint64_t last_timestamp_ns = 0;
    // Half-open end of the last submitted packet, unlike last_timestamp_ns
    // which is the packet's start.  Neither proves encoder/output completion.
    uint64_t submitted_end_timestamp_ns = 0;
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
