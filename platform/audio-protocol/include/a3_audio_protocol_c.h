/* C-compatible a3-audio/1 packet finalization.
 *
 * Used by the private SDL backend and covered by the C/C++ layout tests.  Must
 * stay byte-identical to the C++ FinalizePacket implementation.
 */
#ifndef A3_AUDIO_PROTOCOL_C_H
#define A3_AUDIO_PROTOCOL_C_H

#include "a3_audio_wire.h"

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

uint32_t a3_audio_crc32(const void *data, size_t bytes);
void a3_audio_finalize_packet(A3AudioPacketHeader *header, const void *payload,
                              size_t payload_bytes);

#ifdef __cplusplus
}
#endif

#endif /* A3_AUDIO_PROTOCOL_C_H */
