// Offline A0/A1 tests: wire protocol + absolute QPC scheduling.
// Build target: audio-core-tests (see platform/obs-recorder/CMakeLists.txt).
#include "a3_audio_protocol.h"
#include "audio_clock.hpp"

#include <cstdint>
#include <cstring>
#include <iostream>
#include <string>
#include <vector>

extern "C" int a3_wire_c_layout_ok(void);

namespace {

int g_failures = 0;
int g_checks = 0;

void Check(const char* name, bool ok, const std::string& detail = {})
{
    ++g_checks;
    std::cout << (ok ? "PASS " : "FAIL ") << name;
    if (!detail.empty()) {
        std::cout << "  " << detail;
    }
    std::cout << '\n';
    if (!ok) {
        ++g_failures;
    }
}

a3::audio::PacketHeader MakeHeader(uint64_t sample_start, uint64_t samples)
{
    a3::audio::PacketHeader h{};
    std::memcpy(h.magic, a3::audio::kMagic, sizeof(h.magic));
    h.version = a3::audio::kVersion;
    h.header_bytes = a3::audio::kHeaderBytes;
    h.flags = a3::audio::kFlagFirst;
    h.capture_id = 0x123456789abcdef0ULL;
    h.generation = 1;
    h.sequence = 0;
    h.sample_rate = 44100;
    h.channels = a3::audio::kChannelsStereo;
    h.sample_format = a3::audio::kFormatS16LE;
    h.payload_bytes = static_cast<uint32_t>(samples * 2 * 2);
    h.sample_start = sample_start;
    h.sample_count = samples;
    h.planned_qpc = 1000000;
    h.actual_start_qpc = 1000100;
    h.actual_end_qpc = 1000200;
    h.qpc_frequency = 10000000;
    h.capture_epoch_qpc = 1000000;
    return h;
}

bool Valid(a3::audio::PacketHeader& h, std::vector<uint8_t>& payload, std::string* err)
{
    a3::audio::FinalizePacket(h, payload.data(), payload.size());
    return a3::audio::ValidatePacket(h, payload.data(), payload.size(), err);
}

}  // namespace

int main()
{
    using namespace a3::audio;

    Check("protocol_header_is_128_bytes", sizeof(PacketHeader) == kHeaderBytes);
    Check("c_wire_layout", a3_wire_c_layout_ok() == 1);
    Check("protocol_descriptor", ProtocolDescriptor().find("a3-audio/1") != std::string::npos,
          ProtocolDescriptor());

    std::vector<uint8_t> payload(1024 * 2 * 2, 0x5a);
    auto good = MakeHeader(0, 1024);
    std::string err;
    Check("valid_packet_roundtrip", Valid(good, payload, &err), err);

    // CRC32 value zero is a legal result.  9d 0a d9 6d is one stereo s16le
    // frame whose payload CRC32 is exactly zero; validation must accept it.
    std::vector<uint8_t> zero_crc_payload = {0x9d, 0x0a, 0xd9, 0x6d};
    auto zero_crc_header = MakeHeader(0, 1);
    Check("zero_payload_crc_vector",
          Valid(zero_crc_header, zero_crc_payload, &err) &&
              zero_crc_header.payload_crc32 == 0,
          "crc=" + std::to_string(zero_crc_header.payload_crc32) + " " + err);

    auto bad = good;
    bad.magic[0] = 'X';
    Check("reject_bad_magic", !ValidatePacket(bad, payload.data(), payload.size(), &err), err);

    bad = good;
    bad.version = 2;
    Check("reject_version", !ValidatePacket(bad, payload.data(), payload.size(), &err), err);

    bad = good;
    bad.sample_format = 99;
    Check("reject_format", !ValidatePacket(bad, payload.data(), payload.size(), &err), err);

    bad = good;
    bad.reserved0 = 1;
    Check("reject_nonzero_reserved",
          !ValidatePacket(bad, payload.data(), payload.size(), &err), err);

    bad = good;
    bad.payload_bytes -= 4;
    Check("reject_payload_mismatch",
          !ValidatePacket(bad, payload.data(), payload.size(), &err), err);

    bad = good;
    bad.header_crc32 ^= 1;
    Check("reject_header_crc", !ValidatePacket(bad, payload.data(), payload.size(), &err), err);

    bad = good;
    bad.payload_crc32 ^= 1;
    bad.header_crc32 = 0;
    bad.header_crc32 = Crc32(&bad, sizeof(bad));
    Check("reject_payload_crc", !ValidatePacket(bad, payload.data(), payload.size(), &err), err);

    bad = good;
    bad.flags |= kFlagDiscontinuity;
    bad.discontinuity_start = 10;
    bad.discontinuity_count = 0;
    Check("reject_empty_discontinuity",
          !ValidatePacket(bad, payload.data(), payload.size(), &err), err);

    bad = good;
    bad.sample_start = UINT64_MAX - 10;
    bad.sample_count = 1024;
    Check("reject_range_overflow",
          !ValidatePacket(bad, payload.data(), payload.size(), &err), err);

    // A zero-sample last packet is the explicit end marker.
    auto last = MakeHeader(1024, 0);
    last.flags |= kFlagLast;
    std::vector<uint8_t> empty;
    Check("accept_empty_last", Valid(last, empty, &err), err);

    constexpr uint64_t epoch = 1000000000ULL;
    constexpr uint64_t freq = 10000000ULL;
    constexpr uint32_t rate = 44100;
    SampleClock clock(epoch, freq, rate);
    Check("clock_valid", clock.valid());
    Check("clock_frame0", clock.deadline_qpc(0) == epoch);
    Check("clock_frame1", clock.deadline_qpc(1) == epoch + 226,
          std::to_string(clock.deadline_qpc(1).value_or(0)));
    Check("clock_frame2", clock.deadline_qpc(2) == epoch + 453);
    Check("clock_one_second", clock.deadline_qpc(rate) == epoch + freq);
    Check("clock_one_hour", clock.deadline_qpc(static_cast<uint64_t>(rate) * 3600) ==
                                epoch + freq * 3600);
    Check("clock_ten_hours", clock.deadline_qpc(static_cast<uint64_t>(rate) * 36000) ==
                                 epoch + freq * 36000);
    Check("clock_overflow_rejected", !clock.deadline_qpc(UINT64_MAX).has_value());
    Check("clock_invalid", !SampleClock(epoch, 0, rate).valid());
    Check("clock_invalid_deadline", !SampleClock(epoch, 0, rate).deadline_qpc(1).has_value());

    FrameScheduler scheduler(clock, 0);
    const auto d0 = scheduler.next_deadline_qpc();
    Check("scheduler_advance_ok", scheduler.advance(1024));
    const auto d1 = scheduler.next_deadline_qpc();
    Check("scheduler_absolute_deadline", d0.has_value() && d1.has_value() && *d1 > *d0);
    Check("scheduler_frame_cursor", scheduler.next_frame() == 1024);
    Check("scheduler_catchup_ok", scheduler.within_catchup(epoch, freq / 2));
    Check("scheduler_catchup_reject", !scheduler.within_catchup(epoch + freq * 2, freq / 2));
    Check("scheduler_remaining_positive", scheduler.remaining_qpc(epoch) > 0);
    Check("scheduler_remaining_negative", scheduler.remaining_qpc(epoch + freq * 2) < 0);

    // Processing time is not a scheduling input: do not advance the frame
    // cursor when a callback merely runs late; the next deadline must stay
    // tied to the sample count.
    const auto before = scheduler.next_deadline_qpc();
    Check("processing_time_does_not_move_deadline",
          before == scheduler.next_deadline_qpc());

    FrameScheduler overflow_scheduler(clock, UINT64_MAX - 1);
    Check("scheduler_advance_overflow_rejected", !overflow_scheduler.advance(2));

    std::cout << (g_failures == 0 ? "ALL_PASS" : "FAILURES") << " checks=" << g_checks
              << " failures=" << g_failures << '\n';
    return g_failures == 0 ? 0 : 1;
}
