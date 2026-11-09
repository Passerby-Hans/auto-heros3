#include "audio_clock_c.h"

#include <limits.h>
#include <stddef.h>

int a3_qpc_deadline(const a3_qpc_clock *clock, uint64_t frame, uint64_t *out_deadline)
{
    if (!clock || !out_deadline || clock->qpc_frequency == 0 || clock->sample_rate == 0) {
        return 0;
    }
    const uint64_t rate = clock->sample_rate;
    const uint64_t q = frame / rate;
    const uint64_t r = frame % rate;
    if (q != 0 && clock->qpc_frequency > (UINT64_MAX - clock->epoch_qpc) / q) {
        return 0;
    }
    const uint64_t base = q * clock->qpc_frequency;
    uint64_t remainder = 0;
    if (r != 0) {
        if (clock->qpc_frequency > UINT64_MAX / r) {
            return 0;
        }
        remainder = (r * clock->qpc_frequency) / rate;
    }
    if (base > UINT64_MAX - clock->epoch_qpc ||
        remainder > UINT64_MAX - clock->epoch_qpc - base) {
        return 0;
    }
    *out_deadline = clock->epoch_qpc + base + remainder;
    return 1;
}

int64_t a3_qpc_remaining(const a3_qpc_clock *clock, uint64_t now_qpc, uint64_t frame)
{
    uint64_t deadline = 0;
    if (!a3_qpc_deadline(clock, frame, &deadline)) {
        return INT64_MIN;
    }
    if (deadline >= now_qpc) {
        const uint64_t delta = deadline - now_qpc;
        return delta > (uint64_t)INT64_MAX ? INT64_MAX : (int64_t)delta;
    }
    const uint64_t delta = now_qpc - deadline;
    if (delta > (uint64_t)INT64_MAX) {
        return INT64_MIN;
    }
    return -(int64_t)delta;
}

int a3_qpc_elapsed_us(uint64_t start_qpc, uint64_t end_qpc,
                      uint64_t qpc_frequency, uint64_t *out_us)
{
    uint64_t ticks, seconds, remainder, fractional;
    if (!out_us || qpc_frequency == 0 || end_qpc < start_qpc) return 0;
    ticks = end_qpc - start_qpc;
    seconds = ticks / qpc_frequency;
    remainder = ticks % qpc_frequency;
    if (seconds > UINT64_MAX / 1000000ULL ||
        remainder > UINT64_MAX / 1000000ULL) return 0;
    fractional = remainder * 1000000ULL / qpc_frequency;
    if (remainder * 1000000ULL % qpc_frequency != 0) ++fractional;
    if (fractional > UINT64_MAX - seconds * 1000000ULL) return 0;
    *out_us = seconds * 1000000ULL + fractional;
    return 1;
}
