#include "a3_audio_wire.h"

#include <stddef.h>

int a3_wire_c_layout_ok(void)
{
    return sizeof(A3AudioPacketHeader) == A3_AUDIO_HEADER_BYTES &&
           offsetof(A3AudioPacketHeader, magic) == 0 &&
           offsetof(A3AudioPacketHeader, version) == 4 &&
           offsetof(A3AudioPacketHeader, header_bytes) == 6 &&
           offsetof(A3AudioPacketHeader, flags) == 8 &&
           offsetof(A3AudioPacketHeader, capture_id) == 12 &&
           offsetof(A3AudioPacketHeader, sample_start) == 40 &&
           offsetof(A3AudioPacketHeader, planned_qpc) == 56 &&
           offsetof(A3AudioPacketHeader, header_crc32) == 112;
}
