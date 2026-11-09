#include "live_audio_policy.hpp"
#include "framed_core.hpp"
#include <cstdio>
#include <memory>
#include <vector>

using namespace a3::audio;
namespace {
int checks = 0, failures = 0;
void Check(const char* name, bool ok) {
    ++checks;
    if (!ok) { ++failures; std::printf("FAIL %s\n", name); }
}
ParsedPacket Packet(uint32_t seq, bool last = false) {
    ParsedPacket p;
    auto& h = p.header;
    std::memcpy(h.magic, kMagic, 4);
    h.version = kVersion; h.header_bytes = kHeaderBytes;
    h.capture_id = 99; h.generation = 3; h.sequence = seq;
    h.sample_rate = 44100; h.channels = 2; h.sample_format = kFormatS16LE;
    h.sample_start = static_cast<uint64_t>(seq) * 1024;
    h.sample_count = last ? 0 : 1024;
    h.flags = (seq == 0 ? kFlagFirst : 0) | (last ? kFlagLast : 0);
    h.capture_epoch_qpc = 10000000; h.qpc_frequency = 10000000;
    h.planned_qpc = h.capture_epoch_qpc + h.sample_start * h.qpc_frequency / 44100;
    h.actual_start_qpc = h.planned_qpc; h.actual_end_qpc = h.planned_qpc + 1;
    p.payload.resize(static_cast<size_t>(h.sample_count) * 4, 0x31);
    h.payload_bytes = static_cast<uint32_t>(p.payload.size());
    FinalizePacket(h, p.payload.data(), p.payload.size());
    return p;
}
void Publish(A3AudioLiveShared& shared, const ParsedPacket& p) {
    auto& slot = shared.slots[p.header.sequence % A3_AUDIO_LIVE_CAPACITY];
    const LONG64 stamp = (static_cast<LONG64>(p.header.sequence) + 1) * 2;
    InterlockedExchange64(&slot.stamp, stamp + 1);
    slot.packet_bytes = kHeaderBytes + p.header.payload_bytes;
    std::memcpy(slot.packet, &p.header, kHeaderBytes);
    if (!p.payload.empty()) std::memcpy(slot.packet + kHeaderBytes, p.payload.data(), p.payload.size());
    MemoryBarrier();
    InterlockedExchange64(&slot.stamp, stamp);
    InterlockedExchange64(&shared.header.published_sequence_plus1, p.header.sequence + 1);
}
}
int main() {
    auto shared = std::make_unique<A3AudioLiveShared>();
    std::memset(shared.get(), 0, sizeof(*shared));
    auto first = Packet(0);
    auto& h = shared->header;
    h.magic = A3_AUDIO_LIVE_MAGIC; h.version = A3_AUDIO_LIVE_VERSION;
    h.header_bytes = sizeof(h); h.struct_bytes = sizeof(*shared);
    h.capacity = A3_AUDIO_LIVE_CAPACITY; h.slot_bytes = sizeof(A3AudioLiveSlot);
    h.payload_capacity = A3_AUDIO_LIVE_PAYLOAD_BYTES; h.packet_header_bytes = kHeaderBytes;
    h.producer_pid = 77; h.producer_creation_filetime = 123456;
    h.capture_id = first.header.capture_id; h.generation = first.header.generation;
    h.sample_rate = 44100; h.channels = 2; h.sample_format = kFormatS16LE; h.frames_per_packet = 1024;
    h.capture_epoch_qpc = first.header.capture_epoch_qpc; h.qpc_frequency = first.header.qpc_frequency;
    Check("abi_sizes", sizeof(h) == 128 && sizeof(A3AudioLiveSlot) == 4240 && sizeof(*shared) == 1085568);
    Check("abi_offsets", offsetof(A3AudioLiveSlot, packet) == 16 && offsetof(A3AudioLiveHeader, archive_closed) == 56);
    Check("identity_valid", ValidateLiveIdentity(h, first.header, 77, 123456, true) == nullptr);
    Check("pid_reused", ValidateLiveIdentity(h, first.header, 77, 123457, true) != nullptr);
    Check("owner_exited", ValidateLiveIdentity(h, first.header, 77, 123456, false) != nullptr);
    Check("pid_wrong", ValidateLiveIdentity(h, first.header, 78, 123456, true) != nullptr);
    Check("pid_missing", ValidateLiveIdentity(h, first.header, 0, 123456, true) != nullptr);
    ++h.generation;
    Check("generation_wrong", ValidateLiveIdentity(h, first.header, 77, 123456, true) != nullptr);
    --h.generation; ++h.capture_id;
    Check("capture_wrong", ValidateLiveIdentity(h, first.header, 77, 123456, true) != nullptr);
    --h.capture_id; h.sample_rate = 48000;
    Check("format_wrong", ValidateLiveIdentity(h, first.header, 77, 123456, true) != nullptr);
    h.sample_rate = 44100; h.capacity = 255;
    Check("capacity_wrong", ValidateLiveIdentity(h, first.header, 77, 123456, true) != nullptr);
    h.capacity = 256;
    Check("attach_three", LiveAttachSequence(1000) == 997);
    Check("attach_initial", LiveAttachSequence(0) == 0 && LiveAttachSequence(2) == 0);
    Check("snapshot_accept", ClassifyLiveSnapshot(11, 10, 22, 22) == LiveSnapshotDecision::Accept);
    Check("snapshot_busy", ClassifyLiveSnapshot(11, 10, 23, 23) == LiveSnapshotDecision::Retry);
    Check("snapshot_changed", ClassifyLiveSnapshot(11, 10, 22, 23) == LiveSnapshotDecision::Retry);
    Check("snapshot_unpublished", ClassifyLiveSnapshot(10, 10, 22, 22) == LiveSnapshotDecision::Retry);
    Check("oldest_retained", ClassifyLiveSnapshot(266, 10, 22, 22) == LiveSnapshotDecision::Accept);
    Check("overwritten", ClassifyLiveSnapshot(267, 10, 22, 22) == LiveSnapshotDecision::Overwritten);
    Check("overwrite_busy_before_global_publish", ClassifyLiveSnapshot(266, 10, 535, 535) == LiveSnapshotDecision::Overwritten);
    std::vector<uint8_t> copy(A3_AUDIO_LIVE_PACKET_BYTES);
    uint32_t bytes = 0;
    Publish(*shared, first);
    const auto old_header = shared->header.published_sequence_plus1;
    const auto old_stamp = shared->slots[0].stamp;
    Check("real_snapshot", ReadLiveSnapshot(shared.get(), 0, copy.data(), copy.size(), &bytes) == LiveSnapshotDecision::Accept);
    Check("reader_has_no_owned_state", shared->header.published_sequence_plus1 == old_header && shared->slots[0].stamp == old_stamp);
    PacketHeader copied {};
    std::memcpy(&copied, copy.data(), sizeof(copied));
    Check("snapshot_crc", ValidatePacket(copied, copy.data() + kHeaderBytes, bytes - kHeaderBytes));
    // Simulate abandonment after snapshot; next writer needs no reader cleanup.
    Publish(*shared, Packet(256));
    Check("reader_death_cannot_lock_writer", a3_audio_live_load_sequence(shared.get()) == 257 &&
          ReadLiveSnapshot(shared.get(), 0, copy.data(), copy.size(), &bytes) == LiveSnapshotDecision::Overwritten);
    auto middle = Packet(400);
    FramedStreamValidator validator;
    AttachDescriptor attach {0, 99, 3, 400, 400 * 1024};
    std::string error;
    Check("start_attached", validator.StartAttached(attach, &error) && validator.Accept(middle, &error));
    auto next = Packet(401); next.stream_offset = 4224;
    Check("attached_continuity", validator.Accept(next, &error));
    auto last = Packet(402, true); last.stream_offset = 8448;
    Check("attached_last", validator.Accept(last, &error) && validator.saw_last());
    bool complete = false;
    Check("attached_finish", validator.Finish(true, &complete, &error) && complete);
    Check("archive_coverage", ArchiveCommitted(100, 100, false, false));
    Check("archive_lag", !ArchiveCommitted(99, 100, false, false));
    Check("live_last_not_disk_last", !ArchiveCommitted(100, 100, true, false));
    Check("disk_last_complete", ArchiveCommitted(100, 100, true, true));
    middle.payload[0] ^= 1;
    Check("snapshot_payload_crc_rejected", !ValidatePacket(middle.header, middle.payload.data(), middle.payload.size()));
    FramedStreamValidator gap;
    gap.StartAttached(attach);
    gap.Accept(Packet(400));
    Check("attached_gap_rejected", !gap.Accept(Packet(402), &error));
    SampleTimeline timeline;
    TimelineConfig tc; tc.mode = TimelineMode::LiveCapture; tc.sample_rate = 44100;
    tc.qpc_frequency = 10000000; tc.capture_epoch_qpc = 10000000;
    uint64_t timestamp = 0;
    Check("capture_qpc_preserved", timeline.Configure(tc) && timeline.LiveSampleTimeNs(44100, &timestamp) && timestamp == 2000000000ULL);
    std::printf("%s checks=%d failures=%d\n", failures ? "FAIL" : "ALL_PASS", checks, failures);
    return failures ? 1 : 0;
}
