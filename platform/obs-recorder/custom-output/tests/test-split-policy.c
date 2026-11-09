#include "../a3-split-policy.h"
#include <stdio.h>
#include <stdint.h>

int main(void)
{
    unsigned checks = 0, failed = 0;
#define CHECK(expr) do { checks++; if (!(expr)) { failed++; printf("FAIL line=%d\n", __LINE__); } } while (0)
    CHECK(a3_split_decide(0, 0, 0, 0, 900000000, 0, 900000000) == A3_SPLIT_TIME);
    CHECK(a3_split_decide(0, 0, 0, 0, 899999999, 0, 900000000) == A3_SPLIT_NONE);
    CHECK(a3_split_decide(0, 0, 0, 0, 1800000000, 900000000, 900000000) == A3_SPLIT_TIME);
    CHECK(a3_split_decide(0, 0, 0, 0, 1799999999, 900000000, 900000000) == A3_SPLIT_NONE);
    CHECK(a3_split_decide(0, 90, 10, 100, 1, 0, 1000) == A3_SPLIT_SIZE);
    CHECK(a3_split_decide(0, 90, 9, 100, 1, 0, 1000) == A3_SPLIT_NONE);
    CHECK(a3_split_decide(0, 101, 0, 100, 1, 0, 1000) == A3_SPLIT_SIZE);
    CHECK(a3_split_decide(0, UINT64_MAX - 1, 3, UINT64_MAX, 1, 0, 1000) == A3_SPLIT_SIZE);
    CHECK(a3_split_decide(0, UINT64_MAX, 3, 0, 1, 0, 0) == A3_SPLIT_NONE);
    CHECK(a3_split_decide(0, 100, 0, 100, 900000000, 0, 900000000) == A3_SPLIT_SIZE);
    CHECK(a3_split_decide(1, 100, 0, 100, 900000000, 0, 900000000) == A3_SPLIT_MANUAL);
    CHECK(a3_split_decide(0, 0, 0, 0, 0, 1, 10) == A3_SPLIT_NONE);
    CHECK(a3_split_decide(0, 0, 0, 0, INT64_MAX, 0, INT64_MAX) == A3_SPLIT_TIME);
    /* Every second on a 60 Hz closed-GOP grid, including the first segment:
     * a two-frame encoder reorder delay cannot defer a PTS boundary. */
    for (int second = 1; second <= 1800; ++second) {
        int64_t pts = (int64_t)second * 1000000;
        CHECK(a3_split_decide(0, 0, 0, 0, pts, 0, 900000000) ==
              (second < 900 ? A3_SPLIT_NONE : A3_SPLIT_TIME));
    }
    printf("%u/%u checks passed\n", checks - failed, checks);
    return failed ? 1 : 0;
}
