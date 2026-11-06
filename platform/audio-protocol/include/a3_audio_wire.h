/* C-compatible a3-audio/1 wire layout.
 *
 * Kept separate from the C++ validation API so the private SDL backend (C) and
 * the native/relay code (C++) compile against the same field layout.  Changes
 * here must update a3_audio_protocol.h's static assertions and the golden
 * vectors in tests.
 */
#ifndef A3_AUDIO_WIRE_H
#define A3_AUDIO_WIRE_H

#include <stdint.h>

#define A3_AUDIO_WIRE_VERSION 1u
#define A3_AUDIO_HEADER_BYTES 128u
#define A3_AUDIO_CHANNELS_STEREO 2u
#define A3_AUDIO_FORMAT_S16LE 1u

#define A3_AUDIO_FLAG_FIRST (1u << 0)
#define A3_AUDIO_FLAG_LAST (1u << 1)
#define A3_AUDIO_FLAG_DISCONTINUITY (1u << 2)
#define A3_AUDIO_FLAG_QUEUE_OVERFLOW (1u << 3)
#define A3_AUDIO_FLAG_DEVICE_RESTART (1u << 4)
#define A3_AUDIO_FLAG_PRODUCER_ERROR (1u << 5)

#if defined(_MSC_VER)
#pragma pack(push, 1)
#endif
typedef struct A3AudioPacketHeader {
    char magic[4];                 /* "A3AP" */
    uint16_t version;              /* 1 */
    uint16_t header_bytes;         /* 128 */
    uint32_t flags;
    uint64_t capture_id;
    uint32_t generation;
    uint32_t sequence;
    uint32_t sample_rate;
    uint16_t channels;
    uint16_t sample_format;
    uint32_t payload_bytes;
    uint64_t sample_start;
    uint64_t sample_count;
    uint64_t planned_qpc;
    uint64_t actual_start_qpc;
    uint64_t actual_end_qpc;
    uint64_t qpc_frequency;
    uint64_t capture_epoch_qpc;
    uint64_t discontinuity_start;
    uint64_t discontinuity_count;
    uint32_t header_crc32;
    uint32_t payload_crc32;
    uint32_t reserved0;
    uint32_t reserved1;
} A3AudioPacketHeader;
#if defined(_MSC_VER)
#pragma pack(pop)
#endif

#if defined(__STDC_VERSION__) && __STDC_VERSION__ >= 201112L
_Static_assert(sizeof(A3AudioPacketHeader) == A3_AUDIO_HEADER_BYTES,
               "a3 audio wire header size changed");
#endif

#endif /* A3_AUDIO_WIRE_H */
