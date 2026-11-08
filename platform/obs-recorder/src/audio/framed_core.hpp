// Deterministic a3-audio/1 parser, stream validator and sample timeline.
// This file is independent of libobs and is shared by the native source and
// the offline tests.
#pragma once

#include "a3_audio_protocol.h"

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace a3::audio {

enum class ParseStatus { NeedMore, Packet, Error, Truncated };

struct ParsedPacket {
    PacketHeader header {};
    std::vector<uint8_t> payload;
    uint64_t stream_offset = 0;
};

class FramedParser {
public:
    explicit FramedParser(uint32_t max_payload = kMaxPayloadBytes);

    // Appends bytes and returns every complete, validated packet available.
    // False means a fatal parse error; no later feed may be treated as success.
    bool feed(const uint8_t* data, size_t bytes, std::vector<ParsedPacket>& out,
              std::string* error = nullptr);

    // True only if the stream ended exactly on a packet boundary.
    bool finish(std::string* error = nullptr);

    ParseStatus status() const { return status_; }
    size_t pending_bytes() const { return pending_.size(); }
    uint64_t next_offset() const { return stream_offset_; }
    const std::string& first_error() const { return first_error_; }

private:
    std::vector<uint8_t> pending_;
    uint64_t stream_offset_ = 0;
    uint32_t max_payload_ = kMaxPayloadBytes;
    ParseStatus status_ = ParseStatus::NeedMore;
    std::string first_error_;
    bool failed_ = false;
};

enum class StreamStatus { NotStarted, Validating, Complete, Failed };

struct AttachDescriptor {
    uint64_t stream_offset = 0;
    uint64_t capture_id = 0;
    uint32_t generation = 0;
    uint32_t sequence = 0;
    uint64_t sample_start = 0;
};

struct StreamStats {
    uint64_t validated_packets = 0;
    uint64_t validated_frames = 0;
    uint64_t sequence_gaps = 0;
    uint64_t sample_gaps = 0;
    uint64_t sample_overlaps = 0;
    uint64_t discontinuity_packets = 0;
    uint64_t overflow_packets = 0;
    uint64_t producer_error_packets = 0;
    uint64_t late_packets = 0;
    uint64_t max_packet_age_ns = 0;
    uint64_t first_sample = 0;
    uint64_t validated_end_sample = 0;
    bool saw_first = false;
    bool saw_last = false;
};

class FramedStreamValidator {
public:
    bool StartAtFirst(std::string* error = nullptr);
    bool StartAttached(const AttachDescriptor& descriptor, std::string* error = nullptr);

    bool Accept(const ParsedPacket& packet, std::string* error = nullptr);
    bool Finish(bool require_last, bool* complete, std::string* error = nullptr);

    StreamStatus status() const { return status_; }
    bool integrity_ok() const { return integrity_ok_; }
    const std::string& first_error() const { return first_error_; }
    const StreamStats& stats() const { return stats_; }
    uint64_t capture_id() const { return capture_id_; }
    uint32_t generation() const { return generation_; }
    uint64_t capture_epoch_qpc() const { return capture_epoch_qpc_; }
    uint64_t qpc_frequency() const { return qpc_frequency_; }
    uint64_t expected_sample_start() const { return expected_sample_start_; }
    uint32_t expected_sequence() const { return expected_sequence_; }
    bool attached() const { return attached_; }
    bool saw_first() const { return seen_first_; }
    bool saw_last() const { return seen_last_; }

private:
    bool Fail(const char* reason, std::string* error);
    bool AcceptAttach(const ParsedPacket& packet, std::string* error);
    bool CheckFirst(const ParsedPacket& packet, std::string* error);

    StreamStatus status_ = StreamStatus::NotStarted;
    bool integrity_ok_ = true;
    bool attached_ = false;
    bool identity_set_ = false;
    bool seen_first_ = false;
    bool seen_last_ = false;
    bool sequence_exhausted_ = false;
    uint64_t capture_id_ = 0;
    uint32_t generation_ = 0;
    uint32_t expected_sequence_ = 0;
    uint64_t expected_sample_start_ = 0;
    uint64_t capture_epoch_qpc_ = 0;
    uint64_t qpc_frequency_ = 0;
    AttachDescriptor attach_ {};
    StreamStats stats_ {};
    std::string first_error_;
};

enum class TimelineMode { LiveCapture, Replay };

struct TimelineConfig {
    TimelineMode mode = TimelineMode::Replay;
    uint32_t sample_rate = kNativeSampleRate;
    uint64_t qpc_frequency = 0;
    uint64_t capture_epoch_qpc = 0;
    uint64_t replay_origin_ns = 0;
    uint64_t replay_start_sample = 0;
};

class SampleTimeline {
public:
    bool Configure(const TimelineConfig& cfg, std::string* error = nullptr);
    bool LiveSampleTimeNs(uint64_t sample, uint64_t* out_ns, std::string* error = nullptr) const;
    bool ReplaySampleTimeNs(uint64_t sample, uint64_t* out_ns, std::string* error = nullptr) const;
    bool PlannedMatches(const PacketHeader& header, std::string* error = nullptr) const;

    TimelineMode mode() const { return cfg_.mode; }
    uint64_t sample_rate() const { return cfg_.sample_rate; }
    bool valid() const { return valid_; }

private:
    TimelineConfig cfg_ {};
    bool valid_ = false;
};

// Checked conversions used by the tests and the native source.
bool QpcToNs(uint64_t qpc, uint64_t frequency, uint64_t* out_ns);
bool SampleToNs(uint64_t sample_delta, uint32_t sample_rate, uint64_t* out_ns);

}  // namespace a3::audio
