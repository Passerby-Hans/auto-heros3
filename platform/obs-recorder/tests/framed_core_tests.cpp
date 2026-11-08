#include "framed_core.hpp"

#include "audio_clock_c.h"

#include <cstdint>
#include <cstring>
#include <iostream>
#include <string>
#include <vector>

namespace {
using namespace a3::audio;
int failures = 0;
int checks = 0;

void Check(const char* name, bool ok, const std::string& detail = {})
{
    ++checks;
    std::cout << (ok ? "PASS " : "FAIL ") << name;
    if (!detail.empty()) std::cout << "  " << detail;
    std::cout << '\n';
    if (!ok) ++failures;
}

struct RawPacket {
    PacketHeader h {};
    std::vector<uint8_t> payload;
    std::vector<uint8_t> bytes;
};

uint64_t Planned(uint64_t epoch, uint64_t freq, uint64_t start, uint32_t rate)
{
    a3_qpc_clock c {epoch, freq, rate};
    uint64_t out = 0;
    a3_qpc_deadline(&c, start, &out);
    return out;
}

RawPacket Make(uint64_t capture, uint32_t generation, uint32_t seq,
               uint64_t start, uint32_t frames, uint32_t flags,
               uint64_t epoch = 1000000000ULL, uint64_t freq = 10000000ULL,
               uint32_t rate = 44100)
{
    RawPacket p;
    auto& h = p.h;
    std::memcpy(h.magic, kMagic, 4);
    h.version = kVersion;
    h.header_bytes = kHeaderBytes;
    h.flags = flags;
    h.capture_id = capture;
    h.generation = generation;
    h.sequence = seq;
    h.sample_rate = rate;
    h.channels = kChannelsStereo;
    h.sample_format = kFormatS16LE;
    h.sample_start = start;
    h.sample_count = frames;
    h.payload_bytes = frames * 4;
    h.capture_epoch_qpc = epoch;
    h.qpc_frequency = freq;
    h.planned_qpc = Planned(epoch, freq, start, rate);
    h.actual_start_qpc = h.planned_qpc + 1;
    h.actual_end_qpc = h.actual_start_qpc + frames * freq / rate + 1;
    p.payload.assign(h.payload_bytes, 0x40);
    FinalizePacket(h, p.payload.data(), p.payload.size());
    p.bytes.resize(sizeof(h) + p.payload.size());
    std::memcpy(p.bytes.data(), &h, sizeof(h));
    if (!p.payload.empty()) std::memcpy(p.bytes.data() + sizeof(h), p.payload.data(), p.payload.size());
    return p;
}

std::vector<uint8_t> Cat(const std::vector<RawPacket>& packets)
{
    std::vector<uint8_t> out;
    for (const auto& p : packets) out.insert(out.end(), p.bytes.begin(), p.bytes.end());
    return out;
}

std::vector<ParsedPacket> ParseChunks(const std::vector<uint8_t>& bytes, size_t chunk)
{
    FramedParser parser;
    std::vector<ParsedPacket> out;
    std::string err;
    for (size_t i = 0; i < bytes.size(); i += chunk) {
        const size_t n = std::min(chunk, bytes.size() - i);
        if (!parser.feed(bytes.data() + i, n, out, &err)) {
            return out;
        }
    }
    return out;
}

}  // namespace

int main()
{
    using namespace a3::audio;

    RawPacket p0 = Make(0x1111, 1, 0, 0, 1024, kFlagFirst);
    RawPacket p1 = Make(0x1111, 1, 1, 1024, 1024, 0);
    RawPacket last = Make(0x1111, 1, 2, 2048, 0, kFlagLast);
    std::vector<RawPacket> normal {p0, p1, last};
    auto bytes = Cat(normal);

    for (size_t chunk : {size_t(1), size_t(7), size_t(128), size_t(4096), size_t(100000)}) {
        auto parsed = ParseChunks(bytes, chunk);
        Check(("split_bytes_" + std::to_string(chunk)).c_str(), parsed.size() == 3,
              "packets=" + std::to_string(parsed.size()));
        if (parsed.size() == 3) {
            Check(("split_pcm_" + std::to_string(chunk)).c_str(),
                  parsed[0].payload == p0.payload && parsed[1].payload == p1.payload);
        }
    }

    {
        FramedParser parser;
        std::vector<ParsedPacket> out;
        std::string err;
        Check("parser_header_only_pending",
              parser.feed(p0.bytes.data(), 64, out, &err) && out.empty() &&
                  parser.pending_bytes() == 64);
        Check("parser_finish_truncated", !parser.finish(&err), err);
        Check("parser_truncated_state", parser.status() == ParseStatus::Truncated);
    }

    {
        RawPacket bad = p0;
        bad.h.header_crc32 ^= 1;
        std::memcpy(bad.bytes.data(), &bad.h, sizeof(bad.h));
        FramedParser parser;
        std::vector<ParsedPacket> out;
        std::string err;
        Check("bad_header_crc_rejected",
              !parser.feed(bad.bytes.data(), 128, out, &err) && out.empty(),
              err);
    }

    {
        RawPacket bad = p0;
        bad.payload[0] ^= 0xff;
        std::memcpy(bad.bytes.data() + 128, bad.payload.data(), bad.payload.size());
        FramedParser parser;
        std::vector<ParsedPacket> out;
        std::string err;
        Check("bad_payload_crc_no_audio",
              !parser.feed(bad.bytes.data(), bad.bytes.size(), out, &err) && out.empty(),
              err);
    }

    {
        RawPacket huge = p0;
        huge.h.payload_bytes = kMaxPayloadBytes + 4;
        huge.h.sample_count = huge.h.payload_bytes / 4;
        huge.h.header_crc32 = 0;
        huge.h.header_crc32 = Crc32(&huge.h, sizeof(huge.h));
        FramedParser parser;
        std::vector<ParsedPacket> out;
        std::string err;
        Check("oversize_payload_rejected_before_alloc",
              !parser.feed(reinterpret_cast<const uint8_t*>(&huge.h), sizeof(huge.h), out, &err),
              err);
    }

    {
        RawPacket ov = p0;
        ov.h.sample_start = UINT64_MAX - 1;
        ov.h.sample_count = 2;
        ov.h.payload_bytes = 8;
        ov.h.planned_qpc = 0;
        ov.h.header_crc32 = 0;
        ov.h.header_crc32 = Crc32(&ov.h, sizeof(ov.h));
        FramedParser parser;
        std::vector<ParsedPacket> out;
        std::string err;
        Check("sample_range_overflow_rejected",
              !parser.feed(reinterpret_cast<const uint8_t*>(&ov.h), sizeof(ov.h), out, &err),
              err);
    }

    {
        FramedStreamValidator v;
        std::string err;
        Check("stream_start_first", v.StartAtFirst(&err), err);
        Check("stream_accept_first", v.Accept(ParseChunks(p0.bytes, 1).front(), &err), err);
        Check("stream_accept_second", v.Accept(ParseChunks(p1.bytes, 1).front(), &err), err);
        Check("stream_accept_last", v.Accept(ParseChunks(last.bytes, 1).front(), &err), err);
        bool complete = false;
        Check("stream_finish_complete", v.Finish(true, &complete, &err) && complete, err);
    }

    {
        FramedStreamValidator v;
        std::string err;
        v.StartAtFirst(&err);
        v.Accept(ParseChunks(p0.bytes, 1000).front(), &err);
        RawPacket dup = p0;
        Check("stream_duplicate_rejected",
              !v.Accept(ParseChunks(dup.bytes, 1000).front(), &err), err);
    }

    {
        FramedStreamValidator v;
        std::string err;
        v.StartAtFirst(&err);
        v.Accept(ParseChunks(p0.bytes, 1000).front(), &err);
        RawPacket gap = Make(0x1111, 1, 1, 2048, 1024, 0);
        Check("stream_gap_without_flag_rejected",
              !v.Accept(ParseChunks(gap.bytes, 1000).front(), &err), err);
    }

    {
        FramedStreamValidator v;
        std::string err;
        v.StartAtFirst(&err);
        v.Accept(ParseChunks(p0.bytes, 1000).front(), &err);
        RawPacket disc = Make(0x1111, 1, 1, 2048, 1024, kFlagDiscontinuity);
        disc.h.discontinuity_start = 1024;
        disc.h.discontinuity_count = 1024;
        disc.h.header_crc32 = 0;
        disc.h.header_crc32 = Crc32(&disc.h, sizeof(disc.h));
        std::memcpy(disc.bytes.data(), &disc.h, sizeof(disc.h));
        Check("stream_discontinuity_accepted_for_evidence",
              v.Accept(ParseChunks(disc.bytes, 1000).front(), &err), err);
        Check("stream_discontinuity_integrity_false", !v.integrity_ok());
    }

    {
        FramedStreamValidator v;
        std::string err;
        v.StartAtFirst(&err);
        v.Accept(ParseChunks(p0.bytes, 1000).front(), &err);
        RawPacket gen = Make(0x2222, 2, 1, 1024, 1024, 0);
        Check("stream_generation_change_rejected",
              !v.Accept(ParseChunks(gen.bytes, 1000).front(), &err), err);
    }

    {
        FramedStreamValidator v;
        std::string err;
        v.StartAtFirst(&err);
        v.Accept(ParseChunks(p0.bytes, 1000).front(), &err);
        RawPacket epoch = Make(0x1111, 1, 1, 1024, 1024, 0, 2000000000ULL);
        Check("stream_epoch_change_rejected",
              !v.Accept(ParseChunks(epoch.bytes, 1000).front(), &err), err);
    }

    {
        FramedStreamValidator v;
        std::string err;
        v.StartAtFirst(&err);
        v.Accept(ParseChunks(p0.bytes, 1000).front(), &err);
        Check("stream_missing_last_finish_rejected",
              !v.Finish(true, nullptr, &err), err);
    }

    {
        FramedStreamValidator v;
        std::string err;
        v.StartAtFirst(&err);
        v.Accept(ParseChunks(p0.bytes, 1000).front(), &err);
        bool complete = true;
        Check("stream_active_no_last_is_not_complete",
              v.Finish(false, &complete, &err) && !complete, err);
    }

    {
        FramedStreamValidator v;
        std::string err;
        v.StartAtFirst(&err);
        RawPacket nofirst = p0;
        nofirst.h.flags = 0;
        nofirst.h.header_crc32 = 0;
        nofirst.h.header_crc32 = Crc32(&nofirst.h, sizeof(nofirst.h));
        std::memcpy(nofirst.bytes.data(), &nofirst.h, sizeof(nofirst.h));
        Check("stream_first_required",
              !v.Accept(ParseChunks(nofirst.bytes, 1000).front(), &err), err);
    }

    {
        AttachDescriptor desc {0, 0x1111, 1, 1, 1024};
        FramedStreamValidator v;
        std::string err;
        Check("attach_start", v.StartAttached(desc, &err), err);
        Check("attach_accept_without_first",
              v.Accept(ParseChunks(p1.bytes, 1000).front(), &err), err);
    }

    {
        FramedStreamValidator v;
        std::string err;
        v.StartAtFirst(&err);
        RawPacket after = p0;
        after.h.sequence = 1;
        after.h.sample_start = 1024;
        after.h.header_crc32 = 0;
        after.h.header_crc32 = Crc32(&after.h, sizeof(after.h));
        std::memcpy(after.bytes.data(), &after.h, sizeof(after.h));
        v.Accept(ParseChunks(p0.bytes, 1000).front(), &err);
        v.Accept(ParseChunks(p1.bytes, 1000).front(), &err);
        v.Accept(ParseChunks(last.bytes, 1000).front(), &err);
        Check("stream_data_after_last_rejected",
              !v.Accept(ParseChunks(after.bytes, 1000).front(), &err), err);
    }

    {
        TimelineConfig cfg {};
        cfg.mode = TimelineMode::LiveCapture;
        cfg.qpc_frequency = 10000000;
        cfg.capture_epoch_qpc = 1000000000;
        cfg.sample_rate = 44100;
        SampleTimeline t;
        std::string err;
        Check("timeline_live_config", t.Configure(cfg, &err), err);
        uint64_t a = 0, b = 0, c = 0;
        Check("timeline_live_sample0", t.LiveSampleTimeNs(0, &a, &err), err);
        Check("timeline_live_jitter_invariant",
              t.LiveSampleTimeNs(1024, &b, &err) && t.LiveSampleTimeNs(1024, &c, &err) &&
                  b == c,
              err);
        Check("timeline_planned_matches", t.PlannedMatches(p0.h, &err), err);
        RawPacket wrong_plan = p0;
        wrong_plan.h.planned_qpc++;
        wrong_plan.h.header_crc32 = 0;
        wrong_plan.h.header_crc32 = Crc32(&wrong_plan.h, sizeof(wrong_plan.h));
        Check("timeline_planned_mismatch", !t.PlannedMatches(wrong_plan.h, &err), err);
        for (uint64_t sec : {1845ULL, 3600ULL, 36000ULL}) {
            uint64_t ns = 0;
            a3_qpc_clock clock {cfg.capture_epoch_qpc, cfg.qpc_frequency, cfg.sample_rate};
            uint64_t qpc = 0;
            a3_qpc_deadline(&clock, sec * cfg.sample_rate, &qpc);
            Check(("timeline_long_" + std::to_string(sec)).c_str(),
                  QpcToNs(qpc, cfg.qpc_frequency, &ns) && ns > qpc / 10,
                  "qpc=" + std::to_string(qpc));
        }
    }

    {
        TimelineConfig cfg {};
        cfg.mode = TimelineMode::Replay;
        cfg.sample_rate = 44100;
        cfg.replay_origin_ns = 5000000000ULL;
        cfg.replay_start_sample = 100;
        SampleTimeline t;
        std::string err;
        Check("timeline_replay_config", t.Configure(cfg, &err), err);
        uint64_t ns = 0;
        Check("timeline_replay_origin", t.ReplaySampleTimeNs(100, &ns, &err) && ns == 5000000000ULL, err);
        Check("timeline_replay_sample", t.ReplaySampleTimeNs(44200, &ns, &err) &&
                                          ns == 6000000000ULL, err);
        Check("timeline_replay_before_start",
              !t.ReplaySampleTimeNs(99, &ns, &err), err);
    }

    {
        TimelineConfig cfg {};
        cfg.mode = TimelineMode::LiveCapture;
        cfg.qpc_frequency = 0;
        SampleTimeline t;
        std::string err;
        Check("timeline_bad_frequency", !t.Configure(cfg, &err), err);
    }

    std::cout << "ALL_PASS checks=" << checks << " failures=" << failures << '\n';
    return failures == 0 ? 0 : 1;
}
