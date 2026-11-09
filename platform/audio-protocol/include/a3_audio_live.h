/* Windows shared-memory transport for a3-audio/1. No file I/O on live delivery.
 *
 * This is a Win32 process-shared ABI, not a portable C++ atomic object layout.
 * All mutable control words use aligned Windows Interlocked operations. There
 * is exactly one slot writer and readers NEVER acquire a producer-owned lock.
 * A reader killed during a snapshot therefore cannot poison the producer.
 *
 * Writer: stamp=(sequence+1)*2+1 (odd), copy bytes, MemoryBarrier, publish the
 * even stamp=(sequence+1)*2, then publish header.published_sequence_plus1.
 * Reader: load an even expected stamp, copy packet_bytes and bytes into its
 * own buffer, MemoryBarrier, reload stamp. Only identical expected even stamps
 * authorize CRC/protocol validation. A changed/odd stamp retries; overwritten
 * expected sequence fails. Never expose a speculative copy as valid PCM.
 * Copying the slot is intentionally the Win32 shared-memory snapshot idiom;
 * no C++ object with a constructor/lifetime or std::atomic crosses processes.
 */
#ifndef A3_AUDIO_LIVE_H
#define A3_AUDIO_LIVE_H

#include <stddef.h>
#include <stdint.h>
#include <windows.h>
#include "a3_audio_wire.h"

#define A3_AUDIO_LIVE_MAGIC 0x4c413341u /* little-endian A3AL */
#define A3_AUDIO_LIVE_VERSION 1u
#define A3_AUDIO_LIVE_CAPACITY 256u
#define A3_AUDIO_LIVE_PAYLOAD_BYTES 4096u
#define A3_AUDIO_LIVE_PACKET_BYTES (A3_AUDIO_HEADER_BYTES + A3_AUDIO_LIVE_PAYLOAD_BYTES)
#define A3_AUDIO_LIVE_MAP_FORMAT "Local\\A3AudioLive_%016llx_%08x"

#define A3_AUDIO_LIVE_FATAL_NONE 0L
/* Value 1 is reserved; readers do not hold slot locks. */
#define A3_AUDIO_LIVE_FATAL_ARCHIVE_OVERFLOW 2L
#define A3_AUDIO_LIVE_FATAL_ARCHIVE_WRITE 3L
#define A3_AUDIO_LIVE_FATAL_CLOCK 4L
#define A3_AUDIO_LIVE_FATAL_SEQUENCE_EXHAUSTED 5L
#define A3_AUDIO_LIVE_FATAL_ARCHIVE_QUEUE_BUSY 6L
#define A3_AUDIO_LIVE_FATAL_PRODUCER_ABORT 7L

typedef struct A3AudioLiveHeader {
    uint32_t magic, version, header_bytes, struct_bytes;
    uint32_t capacity, slot_bytes, payload_capacity, packet_header_bytes;
    uint32_t producer_pid, generation, sample_rate, channels;
    uint32_t sample_format, frames_per_packet;
    volatile LONG archive_closed; /* disk LAST accepted; not power-loss durability */
    uint32_t reserved;
    uint64_t producer_creation_filetime, capture_id, capture_epoch_qpc, qpc_frequency;
    volatile LONG64 published_sequence_plus1;
    volatile LONG64 archive_committed_end_sample; /* contiguous successful writes only */
    volatile LONG fatal_code; /* first terminal error, never cleared */
    volatile LONG closed; /* live LAST published; archive may still be draining */
    volatile LONG archive_failed;
    volatile LONG initialized; /* last initialization store; acquire before metadata */
} A3AudioLiveHeader;

typedef struct A3AudioLiveSlot {
    volatile LONG64 stamp;
    uint32_t packet_bytes;
    uint32_t reserved;
    uint8_t packet[A3_AUDIO_LIVE_PACKET_BYTES];
} A3AudioLiveSlot;

typedef struct A3AudioLiveShared {
    A3AudioLiveHeader header;
    A3AudioLiveSlot slots[A3_AUDIO_LIVE_CAPACITY];
} A3AudioLiveShared;

/* typedef assertions work for SDL's C compiler and the host's C++ compiler. */
#define A3_LIVE_ASSERT(name, expression) typedef char name[(expression) ? 1 : -1]
A3_LIVE_ASSERT(a3_live_long_is_4, sizeof(LONG) == 4);
A3_LIVE_ASSERT(a3_live_long64_is_8, sizeof(LONG64) == 8);
A3_LIVE_ASSERT(a3_live_header_size, sizeof(A3AudioLiveHeader) == 128);
A3_LIVE_ASSERT(a3_live_archive_closed_offset, offsetof(A3AudioLiveHeader, archive_closed) == 56);
A3_LIVE_ASSERT(a3_live_owner_offset, offsetof(A3AudioLiveHeader, producer_creation_filetime) == 64);
A3_LIVE_ASSERT(a3_live_sequence_offset, offsetof(A3AudioLiveHeader, published_sequence_plus1) == 96);
A3_LIVE_ASSERT(a3_live_commit_offset, offsetof(A3AudioLiveHeader, archive_committed_end_sample) == 104);
A3_LIVE_ASSERT(a3_live_initialized_offset, offsetof(A3AudioLiveHeader, initialized) == 124);
A3_LIVE_ASSERT(a3_live_slot_size, sizeof(A3AudioLiveSlot) == 4240);
A3_LIVE_ASSERT(a3_live_slot_packet_offset, offsetof(A3AudioLiveSlot, packet) == 16);
A3_LIVE_ASSERT(a3_live_shared_size, sizeof(A3AudioLiveShared) == 1085568);
#undef A3_LIVE_ASSERT

static __inline LONG64 a3_audio_live_load_sequence(A3AudioLiveShared *shared)
{
    return InterlockedCompareExchange64(&shared->header.published_sequence_plus1, 0, 0);
}

static __inline LONG64 a3_audio_live_load_stamp(A3AudioLiveSlot *slot)
{
    return InterlockedCompareExchange64(&slot->stamp, 0, 0);
}

static __inline void a3_audio_live_set_fatal(A3AudioLiveShared *shared, LONG code)
{
    if (shared && code != A3_AUDIO_LIVE_FATAL_NONE)
        InterlockedCompareExchange(&shared->header.fatal_code, code, A3_AUDIO_LIVE_FATAL_NONE);
}

#endif /* A3_AUDIO_LIVE_H */
