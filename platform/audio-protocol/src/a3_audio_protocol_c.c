#include "a3_audio_protocol_c.h"

uint32_t a3_audio_crc32(const void *data, size_t bytes)
{
    const uint8_t *p = (const uint8_t *)data;
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < bytes; ++i) {
        crc ^= p[i];
        for (int bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
        }
    }
    return ~crc;
}

void a3_audio_finalize_packet(A3AudioPacketHeader *header, const void *payload,
                              size_t payload_bytes)
{
    if (!header) {
        return;
    }
    header->payload_crc32 = a3_audio_crc32(payload, payload_bytes);
    header->header_crc32 = 0;
    header->header_crc32 = a3_audio_crc32(header, sizeof(*header));
}
