/* C-compatible absolute QPC sample clock.
 *
 * The private SDL backend is C and must share the same deadline math as the
 * C++ A1 core.  This header is the canonical C contract; audio_clock.cpp wraps
 * these functions for the C++ tests.
 */
#ifndef A3_AUDIO_CLOCK_C_H
#define A3_AUDIO_CLOCK_C_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct a3_qpc_clock {
    uint64_t epoch_qpc;
    uint64_t qpc_frequency;
    uint32_t sample_rate;
} a3_qpc_clock;

/* deadline = epoch + floor(frame * frequency / sample_rate).
 * Returns 1 on success, 0 on invalid clock or uint64 overflow. */
int a3_qpc_deadline(const a3_qpc_clock *clock, uint64_t frame, uint64_t *out_deadline);

/* Signed remaining ticks; returns INT64_MIN on invalid/overflow. */
int64_t a3_qpc_remaining(const a3_qpc_clock *clock, uint64_t now_qpc, uint64_t frame);

/* Measured elapsed QPC ticks, rounded up to microseconds.  A nonzero measured
 * interval never appears as an unobserved zero.  Reject backwards/invalid
 * clocks and overflow rather than publishing a fabricated duration. */
int a3_qpc_elapsed_us(uint64_t start_qpc, uint64_t end_qpc,
                      uint64_t qpc_frequency, uint64_t *out_us);

#ifdef __cplusplus
}
#endif

#endif /* A3_AUDIO_CLOCK_C_H */
