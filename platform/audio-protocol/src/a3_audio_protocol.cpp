#include "a3_audio_protocol.h"

#include <limits>
#include <sstream>

namespace a3::audio {
namespace {

constexpr uint32_t kKnownFlags = kFlagFirst | kFlagLast | kFlagDiscontinuity |
                                 kFlagQueueOverflow | kFlagDeviceRestart |
                                 kFlagProducerError;

bool Fail(std::string* error, const char* reason)
{
    if (error) {
        *error = reason;
    }
    return false;
}

}  // namespace

uint32_t Crc32(const void* data, size_t bytes)
{
    const auto* p = static_cast<const uint8_t*>(data);
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < bytes; ++i) {
        crc ^= p[i];
        for (int bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
        }
    }
    return ~crc;
}

void FinalizePacket(PacketHeader& header, const void* payload, size_t payload_bytes)
{
    header.payload_crc32 = Crc32(payload, payload_bytes);
    header.header_crc32 = 0;
    header.header_crc32 = Crc32(&header, sizeof(header));
}

bool ValidatePacket(const PacketHeader& header, const void* payload,
                    size_t available_payload_bytes, std::string* error)
{
    if (header.magic[0] != kMagic[0] || header.magic[1] != kMagic[1] ||
        header.magic[2] != kMagic[2] || header.magic[3] != kMagic[3]) {
        return Fail(error, "bad_magic");
    }
    if (header.version != kVersion) {
        return Fail(error, "unsupported_version");
    }
    if (header.header_bytes != kHeaderBytes) {
        return Fail(error, "bad_header_bytes");
    }
    if (header.flags & ~kKnownFlags) {
        return Fail(error, "unknown_flags");
    }
    if (header.sample_rate == 0 || header.channels != kChannelsStereo ||
        header.sample_format != kFormatS16LE) {
        return Fail(error, "unsupported_format");
    }
    if (header.qpc_frequency == 0) {
        return Fail(error, "bad_qpc_frequency");
    }
    if (header.actual_end_qpc < header.actual_start_qpc) {
        return Fail(error, "bad_qpc_interval");
    }
    if (header.sample_count > 0 &&
        header.actual_end_qpc == header.actual_start_qpc) {
        return Fail(error, "empty_qpc_interval");
    }
    if (header.payload_bytes != available_payload_bytes) {
        return Fail(error, "payload_length_mismatch");
    }
    if (header.sample_count >
        (std::numeric_limits<uint64_t>::max() - header.sample_start)) {
        return Fail(error, "sample_range_overflow");
    }
    if (header.sample_count >
        (std::numeric_limits<size_t>::max() / (kChannelsStereo * 2u))) {
        return Fail(error, "sample_count_too_large");
    }
    const size_t expected_bytes = static_cast<size_t>(header.sample_count) *
                                  kChannelsStereo * 2u;
    if (header.payload_bytes != expected_bytes) {
        return Fail(error, "sample_count_payload_mismatch");
    }
    if (header.payload_bytes > 0 && payload == nullptr) {
        return Fail(error, "null_payload");
    }
    if (header.sample_count == 0 && !(header.flags & kFlagLast)) {
        return Fail(error, "empty_packet_without_last");
    }
    if (header.reserved0 != 0 || header.reserved1 != 0) {
        return Fail(error, "nonzero_reserved");
    }
    if ((header.flags & kFlagDiscontinuity) != 0) {
        if (header.discontinuity_count == 0 ||
            header.discontinuity_start >
                std::numeric_limits<uint64_t>::max() - header.discontinuity_count) {
            return Fail(error, "bad_discontinuity_range");
        }
    } else if (header.discontinuity_count != 0) {
        return Fail(error, "discontinuity_without_flag");
    }
    // CRC32 value 0 is a legal result; presence is proven by comparing the
    // computed value with the field, not by a non-zero sentinel.

    PacketHeader copy = header;
    copy.header_crc32 = 0;
    if (Crc32(&copy, sizeof(copy)) != header.header_crc32) {
        return Fail(error, "bad_header_crc");
    }
    if (header.payload_crc32 != Crc32(payload, header.payload_bytes)) {
        return Fail(error, "bad_payload_crc");
    }
    if (error) {
        error->clear();
    }
    return true;
}

std::string ProtocolDescriptor()
{
    std::ostringstream os;
    os << "a3-audio/1 magic=A3AP header_bytes=" << kHeaderBytes
       << " sample_format=s16le channels=" << kChannelsStereo
       << " endian=little";
    return os.str();
}

}  // namespace a3::audio
