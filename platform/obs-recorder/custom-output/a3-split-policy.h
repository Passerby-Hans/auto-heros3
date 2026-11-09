/* Private output rollover policy. Presentation time, never encoder DTS. */
#ifndef A3_SPLIT_POLICY_H
#define A3_SPLIT_POLICY_H
#include <stdint.h>

enum a3_split_reason { A3_SPLIT_NONE, A3_SPLIT_MANUAL, A3_SPLIT_SIZE, A3_SPLIT_TIME };

static inline enum a3_split_reason a3_split_decide(int manual, uint64_t bytes,
    uint64_t next_bytes, uint64_t size_trigger, int64_t pts_usec,
    int64_t start_pts_usec, int64_t duration_usec)
{
    if (manual) return A3_SPLIT_MANUAL;
    /* Subtraction avoids overflow. This is a trigger, not a final-file cap:
     * the preceding GOP and MP4 overhead need a separate safety reserve. */
    if (size_trigger && (bytes >= size_trigger || next_bytes >= size_trigger - bytes))
        return A3_SPLIT_SIZE;
    if (duration_usec > 0 && pts_usec >= start_pts_usec &&
        (uint64_t)pts_usec - (uint64_t)start_pts_usec >= (uint64_t)duration_usec)
        return A3_SPLIT_TIME;
    return A3_SPLIT_NONE;
}

static inline const char *a3_split_name(enum a3_split_reason reason)
{
    switch (reason) {
    case A3_SPLIT_MANUAL: return "MANUAL";
    case A3_SPLIT_SIZE: return "SIZE";
    case A3_SPLIT_TIME: return "TIME";
    default: return "NONE";
    }
}
#endif
