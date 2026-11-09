#pragma once
#include <stdbool.h>
#include <stdint.h>

/* Host deactivation is separate from a mux fence/stop signal. A timeout is
 * terminal even if the output becomes inactive just after the deadline. */
#define A3_HOST_STOP_BUDGET_NS UINT64_C(10000000000)
#define A3_HOST_QUARANTINE_BUDGET_MS 5000u
enum a3_host_stop_result {
	A3_HOST_STOP_WAITING,
	A3_HOST_STOP_INACTIVE,
	A3_HOST_STOP_UNCONFIRMED
};

static inline enum a3_host_stop_result a3_host_stop_observe(
	enum a3_host_stop_result previous, uint64_t elapsed_ns,
	bool output_active, bool test_force_timeout)
{
	if (previous != A3_HOST_STOP_WAITING)
		return previous;
	if (elapsed_ns >= A3_HOST_STOP_BUDGET_NS)
		return A3_HOST_STOP_UNCONFIRMED;
	return !output_active && !test_force_timeout ? A3_HOST_STOP_INACTIVE : A3_HOST_STOP_WAITING;
}
