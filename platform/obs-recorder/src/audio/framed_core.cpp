#include "framed_core.hpp"

#include "audio_clock_c.h"

#include <algorithm>
#include <cstring>
#include <limits>

namespace a3::audio {
namespace {
constexpr size_t kHeader = kHeaderBytes;
constexpr size_t kMaxPending = 2 * (kMaxPayloadBytes + kHeaderBytes);

bool CheckedAdd(uint64_t a, uint64_t b, uint64_t* out)
{
    if (a > std::numeric_limits<uint64_t>::max() - b) return false;
    *out = a + b;
    return true;
}

bool CheckedMul(uint64_t a, uint64_t b, uint64_t* out)
{
    if (a != 0 && b > std::numeric_limits<uint64_t>::max() / a) return false;
    *out = a * b;
    return true;
}
}  // namespace

FramedParser::FramedParser(uint32_t max_payload)
    : max_payload_(max_payload)
{
}

bool FramedParser::feed(const uint8_t* data, size_t bytes,
                        std::vector<ParsedPacket>& out, std::string* error)
{
    if (failed_) {
        if (error) *error = first_error_;
        return false;
    }
    if (bytes && !data) {
        failed_ = true;
        first_error_ = "null_input";
        if (error) *error = first_error_;
        return false;
    }
    if (bytes) {
        pending_.insert(pending_.end(), data, data + bytes);
    }
    while (pending_.size() >= kHeader) {
        PacketHeader header {};
        std::memcpy(&header, pending_.data(), sizeof(header));
        std::string err;
        if (!ValidateHeader(header, &err)) {
            failed_ = true;
            first_error_ = err;
            if (error) *error = err;
            return false;
        }
        if (header.payload_bytes > max_payload_) {
            failed_ = true;
            first_error_ = "payload_too_large";
            if (error) *error = first_error_;
            return false;
        }
        const size_t full = kHeader + header.payload_bytes;
        if (pending_.size() < full) {
            return true;
        }
        std::vector<uint8_t> payload(header.payload_bytes);
        if (header.payload_bytes) {
            std::memcpy(payload.data(), pending_.data() + kHeader, header.payload_bytes);
        }
        if (!ValidatePayload(header, payload.data(), payload.size(), &err)) {
            failed_ = true;
            first_error_ = err;
            if (error) *error = err;
            return false;
        }
        ParsedPacket packet;
        packet.header = header;
        packet.payload = std::move(payload);
        packet.stream_offset = stream_offset_;
        stream_offset_ += full;
        pending_.erase(pending_.begin(), pending_.begin() + full);
        out.push_back(std::move(packet));
    }
    if (pending_.size() > kMaxPending) {
        failed_ = true;
        first_error_ = "pending_buffer_limit";
        if (error) *error = first_error_;
        return false;
    }
    status_ = pending_.empty() ? ParseStatus::NeedMore : ParseStatus::NeedMore;
    return true;
}

bool FramedParser::finish(std::string* error)
{
    if (failed_) {
        if (error) *error = first_error_;
        return false;
    }
    if (!pending_.empty()) {
        status_ = ParseStatus::Truncated;
        first_error_ = "truncated_packet";
        if (error) *error = first_error_;
        return false;
    }
    status_ = ParseStatus::NeedMore;
    if (error) error->clear();
    return true;
}

bool FramedStreamValidator::Fail(const char* reason, std::string* error)
{
    if (first_error_.empty()) first_error_ = reason;
    status_ = StreamStatus::Failed;
    integrity_ok_ = false;
    if (error) *error = reason;
    return false;
}

bool FramedStreamValidator::StartAtFirst(std::string* error)
{
    status_ = StreamStatus::Validating;
    attached_ = false;
    identity_set_ = false;
    if (error) error->clear();
    return true;
}

bool FramedStreamValidator::StartAttached(const AttachDescriptor& descriptor,
                                           std::string* error)
{
    if (descriptor.generation == 0) {
        return Fail("invalid_attach_descriptor", error);
    }
    attach_ = descriptor;
    attached_ = true;
    identity_set_ = false;
    status_ = StreamStatus::Validating;
    if (error) error->clear();
    return true;
}

bool FramedStreamValidator::AcceptAttach(const ParsedPacket& packet,
                                          std::string* error)
{
    if (packet.stream_offset != attach_.stream_offset ||
        packet.header.capture_id != attach_.capture_id ||
        packet.header.generation != attach_.generation ||
        packet.header.sequence != attach_.sequence ||
        packet.header.sample_start != attach_.sample_start) {
        return Fail("attach_descriptor_mismatch", error);
    }
    return true;
}

bool FramedStreamValidator::CheckFirst(const ParsedPacket& packet,
                                        std::string* error)
{
    if (attached_) return AcceptAttach(packet, error);
    if ((packet.header.flags & kFlagFirst) == 0)
        return Fail("first_packet_missing_first", error);
    if (packet.header.sequence != 0)
        return Fail("first_packet_sequence_not_zero", error);
    if (packet.header.sample_start != 0)
        return Fail("first_packet_sample_not_zero", error);
    return true;
}

bool FramedStreamValidator::Accept(const ParsedPacket& packet, std::string* error)
{
    if (status_ == StreamStatus::Failed) {
        if (error) *error = first_error_;
        return false;
    }
    if (seen_last_) return Fail("packet_after_last", error);
    if (sequence_exhausted_) return Fail("sequence_exhausted", error);
    if (!identity_set_) {
        identity_set_ = true;
        capture_id_ = packet.header.capture_id;
        generation_ = packet.header.generation;
        capture_epoch_qpc_ = packet.header.capture_epoch_qpc;
        qpc_frequency_ = packet.header.qpc_frequency;
        expected_sequence_ = packet.header.sequence;
        expected_sample_start_ = packet.header.sample_start;
        stats_.first_sample = packet.header.sample_start;
        if (!CheckFirst(packet, error)) return false;
        seen_first_ = attached_ || ((packet.header.flags & kFlagFirst) != 0);
        stats_.saw_first = seen_first_;
    } else {
        if (packet.header.capture_id != capture_id_ ||
            packet.header.generation != generation_)
            return Fail("capture_identity_changed", error);
        if (packet.header.capture_epoch_qpc != capture_epoch_qpc_)
            return Fail("capture_epoch_changed", error);
        if (packet.header.qpc_frequency != qpc_frequency_)
            return Fail("qpc_frequency_changed", error);
        if ((packet.header.flags & kFlagFirst) != 0)
            return Fail("duplicate_first", error);
        if (packet.header.sequence != expected_sequence_) {
            if (packet.header.sequence < expected_sequence_)
                return Fail("sequence_backwards_or_duplicate", error);
            if (!(packet.header.flags & kFlagDiscontinuity))
                return Fail("sequence_gap_without_discontinuity", error);
            ++stats_.sequence_gaps;
        }
        const uint64_t expected = expected_sample_start_;
        if ((packet.header.flags & kFlagDiscontinuity) != 0) {
            if (packet.header.sample_start < expected)
                return Fail("sample_overlap", error);
            const uint64_t gap = packet.header.sample_start - expected;
            if (gap != 0) {
                if (packet.header.discontinuity_start != expected ||
                    packet.header.discontinuity_count != gap)
                    return Fail("discontinuity_range_mismatch", error);
                stats_.sample_gaps += gap;
            }
            ++stats_.discontinuity_packets;
        } else if (packet.header.sample_start != expected) {
            if (packet.header.sample_start < expected) {
                ++stats_.sample_overlaps;
                return Fail("sample_overlap", error);
            }
            ++stats_.sample_gaps;
            return Fail("sample_gap_without_discontinuity", error);
        }
        if (packet.header.flags & kFlagQueueOverflow) ++stats_.overflow_packets;
        if (packet.header.flags & kFlagProducerError) ++stats_.producer_error_packets;
    }
    if (packet.header.flags & (kFlagDiscontinuity | kFlagQueueOverflow |
                               kFlagProducerError)) {
        integrity_ok_ = false;
    }
    const uint64_t end = packet.header.sample_start + packet.header.sample_count;
    expected_sample_start_ = end;
    expected_sequence_ = packet.header.sequence + 1;
    if (packet.header.sequence == std::numeric_limits<uint32_t>::max()) {
        sequence_exhausted_ = true;
    }
    stats_.validated_packets++;
    stats_.validated_frames += packet.header.sample_count;
    stats_.validated_end_sample = end;
    if ((packet.header.flags & kFlagLast) != 0) {
        if (packet.header.sample_count != 0)
            return Fail("last_packet_has_samples", error);
        seen_last_ = true;
        stats_.saw_last = true;
        status_ = StreamStatus::Complete;
    }
    if (error) error->clear();
    return true;
}

bool FramedStreamValidator::Finish(bool require_last, bool* complete,
                                    std::string* error)
{
    if (status_ == StreamStatus::Failed) {
        if (complete) *complete = false;
        if (error) *error = first_error_;
        return false;
    }
    if (require_last && !seen_last_) {
        return Fail("missing_last", error);
    }
    if (complete) *complete = integrity_ok_ && seen_last_;
    if (status_ != StreamStatus::Complete) status_ = StreamStatus::Validating;
    if (error) error->clear();
    return true;
}

bool QpcToNs(uint64_t qpc, uint64_t frequency, uint64_t* out_ns)
{
    if (!out_ns || frequency == 0) return false;
    const uint64_t whole = qpc / frequency;
    const uint64_t rem = qpc % frequency;
    uint64_t a = 0;
    uint64_t b = 0;
    if (!CheckedMul(whole, 1000000000ULL, &a)) return false;
    if (!CheckedMul(rem, 1000000000ULL, &b)) return false;
    if (!CheckedAdd(a, b / frequency, out_ns)) return false;
    return true;
}

bool SampleToNs(uint64_t delta, uint32_t sample_rate, uint64_t* out_ns)
{
    if (!out_ns || sample_rate == 0) return false;
    return CheckedMul(delta, 1000000000ULL, out_ns) &&
           (*out_ns /= sample_rate, true);
}

bool SampleTimeline::Configure(const TimelineConfig& cfg, std::string* error)
{
    cfg_ = cfg;
    valid_ = false;
    if (cfg.sample_rate == 0) {
        if (error) *error = "bad_sample_rate";
        return false;
    }
    if (cfg.mode == TimelineMode::LiveCapture) {
        if (cfg.qpc_frequency == 0) {
            if (error) *error = "bad_qpc_frequency";
            return false;
        }
        uint64_t probe = 0;
        if (!QpcToNs(cfg.capture_epoch_qpc, cfg.qpc_frequency, &probe)) {
            if (error) *error = "qpc_epoch_overflow";
            return false;
        }
    }
    valid_ = true;
    if (error) error->clear();
    return true;
}

bool SampleTimeline::LiveSampleTimeNs(uint64_t sample, uint64_t* out_ns,
                                       std::string* error) const
{
    if (!valid_ || cfg_.mode != TimelineMode::LiveCapture) {
        if (error) *error = "timeline_not_live";
        return false;
    }
    a3_qpc_clock clock {cfg_.capture_epoch_qpc, cfg_.qpc_frequency, cfg_.sample_rate};
    uint64_t qpc = 0;
    if (!a3_qpc_deadline(&clock, sample, &qpc)) {
        if (error) *error = "sample_qpc_overflow";
        return false;
    }
    if (!QpcToNs(qpc, cfg_.qpc_frequency, out_ns)) {
        if (error) *error = "sample_time_overflow";
        return false;
    }
    return true;
}

bool SampleTimeline::ReplaySampleTimeNs(uint64_t sample, uint64_t* out_ns,
                                         std::string* error) const
{
    if (!valid_ || cfg_.mode != TimelineMode::Replay) {
        if (error) *error = "timeline_not_replay";
        return false;
    }
    if (sample < cfg_.replay_start_sample) {
        if (error) *error = "sample_before_replay_start";
        return false;
    }
    uint64_t delta = 0;
    if (!SampleToNs(sample - cfg_.replay_start_sample, cfg_.sample_rate, &delta) ||
        !CheckedAdd(cfg_.replay_origin_ns, delta, out_ns)) {
        if (error) *error = "replay_time_overflow";
        return false;
    }
    return true;
}

bool SampleTimeline::PlannedMatches(const PacketHeader& header,
                                     std::string* error) const
{
    if (!valid_ || cfg_.qpc_frequency == 0) {
        if (error) *error = "timeline_not_ready";
        return false;
    }
    if (header.capture_epoch_qpc != cfg_.capture_epoch_qpc ||
        header.qpc_frequency != cfg_.qpc_frequency) {
        if (error) *error = "timeline_clock_mismatch";
        return false;
    }
    a3_qpc_clock clock {header.capture_epoch_qpc, header.qpc_frequency, header.sample_rate};
    uint64_t planned = 0;
    if (!a3_qpc_deadline(&clock, header.sample_start, &planned) ||
        planned != header.planned_qpc) {
        if (error) *error = "planned_qpc_mismatch";
        return false;
    }
    return true;
}

}  // namespace a3::audio
