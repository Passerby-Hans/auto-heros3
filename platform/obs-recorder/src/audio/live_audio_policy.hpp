// Deterministic Win32 shared-memory attachment/snapshot policy. No libobs/I/O.
#pragma once
#include "a3_audio_live.h"
#include "a3_audio_protocol.h"
#include <cstring>

namespace a3::audio {
constexpr uint64_t kLiveAttachMaxAgeNs = 70000000ULL;
constexpr uint64_t kArchiveDrainNs = 5000000000ULL;

inline const char* ValidateLiveIdentity(const A3AudioLiveHeader& h, const PacketHeader& p,
                                      uint32_t expected_pid, uint64_t creation, bool alive)
{
    if (!expected_pid || !alive || !creation || h.producer_pid != expected_pid ||
        h.producer_creation_filetime != creation) return "live_owner_mismatch";
    if (h.magic != A3_AUDIO_LIVE_MAGIC || h.version != A3_AUDIO_LIVE_VERSION ||
        h.header_bytes != sizeof(A3AudioLiveHeader) || h.struct_bytes != sizeof(A3AudioLiveShared) ||
        h.capacity != A3_AUDIO_LIVE_CAPACITY || h.slot_bytes != sizeof(A3AudioLiveSlot) ||
        h.payload_capacity != A3_AUDIO_LIVE_PAYLOAD_BYTES || h.packet_header_bytes != kHeaderBytes ||
        h.frames_per_packet != 1024 || h.reserved != 0) return "live_map_format";
    if (h.sample_rate != kNativeSampleRate || h.channels != kChannelsStereo ||
        h.sample_format != kFormatS16LE || h.sample_rate != p.sample_rate ||
        h.channels != p.channels || h.sample_format != p.sample_format) return "live_map_format";
    if (h.capture_id != p.capture_id || h.generation != p.generation ||
        h.capture_epoch_qpc != p.capture_epoch_qpc || h.qpc_frequency != p.qpc_frequency)
        return "live_map_identity";
    return nullptr;
}

inline uint64_t LiveAttachSequence(uint64_t published_plus1)
{
    return published_plus1 > 3 ? published_plus1 - 3 : 0;
}

enum class LiveSnapshotDecision { Retry, Accept, Overwritten };
inline LiveSnapshotDecision ClassifyLiveSnapshot(uint64_t published_plus1, uint64_t expected,
                                                uint64_t before, uint64_t after)
{
    if (published_plus1 > expected && published_plus1 - expected > A3_AUDIO_LIVE_CAPACITY)
        return LiveSnapshotDecision::Overwritten;
    const uint64_t wanted = (expected + 1) * 2;
    // Odd stamp is (writer_sequence+1)*2+1. A later writer also proves overwrite.
    if ((before >= 2 && before / 2 - 1 > expected) ||
        (after >= 2 && after / 2 - 1 > expected)) return LiveSnapshotDecision::Overwritten;
    if (published_plus1 <= expected || before != wanted || after != wanted)
        return LiveSnapshotDecision::Retry;
    return LiveSnapshotDecision::Accept;
}

inline bool ArchiveCommitted(uint64_t committed, uint64_t submitted_end,
                             bool saw_last, bool archive_closed)
{
    return committed >= submitted_end && (!saw_last || archive_closed);
}

// This operation changes no shared byte and retains no ownership to clean up.
// Even process termination during memcpy cannot leave the producer locked.
inline LiveSnapshotDecision ReadLiveSnapshot(A3AudioLiveShared* shared, uint64_t expected,
                                             uint8_t* copy, size_t capacity, uint32_t* bytes)
{
    const uint64_t published = static_cast<uint64_t>(a3_audio_live_load_sequence(shared));
    auto* slot = &shared->slots[expected % A3_AUDIO_LIVE_CAPACITY];
    const uint64_t before = static_cast<uint64_t>(a3_audio_live_load_stamp(slot));
    *bytes = slot->packet_bytes;
    if (*bytes >= kHeaderBytes && *bytes <= capacity) std::memcpy(copy, slot->packet, *bytes);
    MemoryBarrier();
    const uint64_t after = static_cast<uint64_t>(a3_audio_live_load_stamp(slot));
    return ClassifyLiveSnapshot(published, expected, before, after);
}
} // namespace a3::audio
