// A3 audio capture protocol, wire version 1.
//
// This header is the single source of truth shared by the private SDL capture
// backend, the relay and the native libobs audio source.  All multi-byte fields
// are little-endian; the current supported platforms are little-endian Windows.
//
// A packet is: 128-byte header followed by payload_bytes bytes of interleaved
// PCM.  sample_start/sample_count are a half-open range in capture sample
// frames.  relay may split/coalesce transport writes, but must preserve packet
// boundaries using header_bytes/payload_bytes and the two CRC32 values.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

namespace a3::audio {

constexpr char kMagic[4] = {'A', '3', 'A', 'P'};
constexpr uint16_t kVersion = 1;
constexpr uint16_t kHeaderBytes = 128;
constexpr uint16_t kChannelsStereo = 2;
constexpr uint16_t kFormatS16LE = 1;

enum PacketFlags : uint32_t {
    kFlagFirst = 1u << 0,          // first packet of a capture generation
    kFlagLast = 1u << 1,           // producer cleanly closed this generation
    kFlagDiscontinuity = 1u << 2,  // explicit missing sample interval
    kFlagQueueOverflow = 1u << 3,  // producer queue overflowed
    kFlagDeviceRestart = 1u << 4,  // SDL device reopened
    kFlagProducerError = 1u << 5,  // producer-side fatal/degraded condition
};

#pragma pack(push, 1)
struct PacketHeader {
    char magic[4];                 // "A3AP"
    uint16_t version;              // kVersion
    uint16_t header_bytes;         // kHeaderBytes
    uint32_t flags;
    uint64_t capture_id;           // random per capture lifetime
    uint32_t generation;           // increments on device/capture reset
    uint32_t sequence;             // per-generation packet sequence, starts 0
    uint32_t sample_rate;          // e.g. 44100
    uint16_t channels;             // 2
    uint16_t sample_format;        // kFormatS16LE
    uint32_t payload_bytes;        // interleaved PCM bytes
    uint64_t sample_start;         // half-open sample-frame range
    uint64_t sample_count;         // frames, not bytes
    uint64_t planned_qpc;          // absolute QPC deadline for this callback
    uint64_t actual_start_qpc;     // observed callback start
    uint64_t actual_end_qpc;       // observed callback end
    uint64_t qpc_frequency;        // ticks per second
    uint64_t capture_epoch_qpc;    // QPC at sample_start == 0
    uint64_t discontinuity_start;  // first missing frame, valid with flag
    uint64_t discontinuity_count;  // number of missing frames
    uint32_t header_crc32;         // header with this field zeroed
    uint32_t payload_crc32;        // payload bytes (0 for empty payload)
    uint32_t reserved0;
    uint32_t reserved1;
};
#pragma pack(pop)

static_assert(sizeof(PacketHeader) == kHeaderBytes, "wire header size changed");

uint32_t Crc32(const void* data, size_t bytes);

// Fills header_crc32 and payload_crc32.  Call after every other header field
// and the payload bytes have been populated.
void FinalizePacket(PacketHeader& header, const void* payload, size_t payload_bytes);

// Validates all version/format/range/CRC invariants.  Returns false and sets
// *error to a short machine-readable reason.  Never trusts payload_bytes alone.
bool ValidatePacket(const PacketHeader& header, const void* payload,
                    size_t available_payload_bytes, std::string* error = nullptr);

// Convenience for tests and the relay's schema lock: returns a stable textual
// description of the source lock fields this build understands.
std::string ProtocolDescriptor();

}  // namespace a3::audio
