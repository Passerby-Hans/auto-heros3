/* Standalone offline test; no OBS, codecs, clocks, devices or media output. */
#include "../a3-stop-policy.h"
#include "../../src/media/host-stop-policy.h"
#include <stdio.h>

static unsigned checks, failures;
#define CHECK(expr) do { checks++; if (!(expr)) { failures++; \
	printf("FAIL line=%d %s\n", __LINE__, #expr); } } while (0)

static struct a3_stop_policy begun(void)
{
	struct a3_stop_policy p;
	CHECK(a3_stop_init(&p, 44100, 1024, 60, 1));
	/* Decode order is not presentation order. */
	const int pts[] = {0, 3, 1, 2};
	for (unsigned i = 0; i < sizeof(pts) / sizeof(pts[0]); i++)
		CHECK(a3_stop_note_video(&p, pts[i]));
	CHECK(a3_stop_note_audio(&p, -1024));
	CHECK(a3_stop_note_audio(&p, 0));
	CHECK(a3_stop_note_audio(&p, 1024));
	CHECK(a3_stop_video_end_usec(&p) == 66666);
	return p;
}

int main(void)
{
	/* Host stop code/mux completeness cannot stand in for encoder deactivation.
	 * After timeout, a late inactive observation cannot authorize destructor
	 * waits or a second force-stop that would reset libobs' completion event. */
	enum a3_host_stop_result host = A3_HOST_STOP_WAITING;
	host = a3_host_stop_observe(host, A3_HOST_STOP_BUDGET_NS - 1, true, false);
	CHECK(host == A3_HOST_STOP_WAITING);
	host = a3_host_stop_observe(host, A3_HOST_STOP_BUDGET_NS, true, false);
	CHECK(host == A3_HOST_STOP_UNCONFIRMED);
	CHECK(a3_host_stop_observe(host, A3_HOST_STOP_BUDGET_NS + 1, false, false) ==
	      A3_HOST_STOP_UNCONFIRMED);
	CHECK(a3_host_stop_observe(A3_HOST_STOP_WAITING, A3_HOST_STOP_BUDGET_NS, false, false) ==
	      A3_HOST_STOP_UNCONFIRMED);
	CHECK(a3_host_stop_observe(A3_HOST_STOP_WAITING, A3_HOST_STOP_BUDGET_NS - 1, false, false) ==
	      A3_HOST_STOP_INACTIVE);
	CHECK(a3_host_stop_observe(A3_HOST_STOP_WAITING, 1, false, true) == A3_HOST_STOP_WAITING);
	CHECK(a3_host_stop_observe(A3_HOST_STOP_WAITING, A3_HOST_STOP_BUDGET_NS, false, true) ==
	      A3_HOST_STOP_UNCONFIRMED);
	CHECK(a3_host_stop_observe(A3_HOST_STOP_INACTIVE, A3_HOST_STOP_BUDGET_NS + 1, true, false) ==
	      A3_HOST_STOP_INACTIVE);
	struct a3_stop_policy p = begun();
	CHECK(a3_stop_request(&p, 1000000, false));
	CHECK(a3_stop_request(&p, 2000000, false));
	CHECK(p.request_usec == 1000000); /* idempotent; never moves T0 */
	CHECK(a3_stop_set_fence(&p, 4, true, false));
	CHECK(p.fence_sample == 2940 && !a3_stop_audio_ready(&p));
	CHECK(a3_stop_note_audio(&p, 2048));
	CHECK(a3_stop_audio_ready(&p));
	CHECK(p.audio_end_sample - p.fence_sample == 132);
	CHECK(a3_stop_finish(&p) && p.complete && !p.failed);

	/* The same crossing AU may arrive before the IDR. */
	p = begun();
	CHECK(a3_stop_note_audio(&p, 2048));
	CHECK(a3_stop_request(&p, 1000000, false));
	CHECK(a3_stop_set_fence(&p, 4, true, false));
	CHECK(a3_stop_audio_ready(&p) && a3_stop_finish(&p));

	/* Multiple phases, including an exact AAC boundary, and a different
	 * frame size prove the policy uses encoder parameters, not 23220 us. */
	const int fences[] = {60, 120, 180, 1024};
	const int frame_sizes[] = {1024, 960};
	for (unsigned k = 0; k < sizeof(frame_sizes) / sizeof(frame_sizes[0]); k++) {
		for (unsigned j = 0; j < sizeof(fences) / sizeof(fences[0]); j++) {
			CHECK(a3_stop_init(&p, 44100, frame_sizes[k], 60, 1));
			for (int n = 0; n < fences[j]; n++) CHECK(a3_stop_note_video(&p, n));
			CHECK(a3_stop_note_audio(&p, -frame_sizes[k]));
			CHECK(a3_stop_request(&p, 1, false));
			CHECK(a3_stop_set_fence(&p, fences[j], true, false));
			while (!a3_stop_audio_ready(&p))
				CHECK(a3_stop_note_audio(&p, p.audio_end_sample));
			CHECK(p.audio_end_sample - p.fence_sample < frame_sizes[k]);
			CHECK(a3_stop_finish(&p));
		}
	}

	/* A stop racing an ordinary rollover waits for that owned buffer, then
	 * uses the next IDR without changing its original request clock. */
	p = begun();
	CHECK(a3_stop_request(&p, 123, true) && p.waiting_for_split && !p.failed);
	CHECK(a3_stop_request(&p, 999, false) && p.request_usec == 123);
	CHECK(a3_stop_note_audio(&p, 2048));
	a3_stop_split_drained(&p);
	CHECK(!p.waiting_for_split && p.request_usec == 123);
	CHECK(a3_stop_set_fence(&p, 4, true, false));
	CHECK(a3_stop_finish(&p));

	/* Missing streams, undrained split, non-IDR, audio gaps, incomplete B-frame
	 * intervals and forced stop all remain failed, never a successful file. */
	CHECK(a3_stop_init(&p, 44100, 1024, 60, 1));
	CHECK(!a3_stop_request(&p, 1, false) && p.error == A3_STOP_MISSING_MEDIA);
	p = begun();
	CHECK(a3_stop_request(&p, 1, true));
	CHECK(!a3_stop_set_fence(&p, 4, true, false) && p.error == A3_STOP_PENDING_SPLIT);
	p = begun();
	CHECK(a3_stop_request(&p, 1, false));
	CHECK(!a3_stop_set_fence(&p, 4, false, false) && p.error == A3_STOP_NOT_IDR);
	p = begun();
	CHECK(a3_stop_request(&p, 1, false));
	CHECK(!a3_stop_set_fence(&p, 4, true, true) && p.error == A3_STOP_PENDING_SPLIT);
	p = begun();
	CHECK(!a3_stop_note_audio(&p, 3072) && p.error == A3_STOP_AUDIO_INTERVAL);
	p = begun();
	CHECK(!a3_stop_note_audio(&p, 1024) && p.error == A3_STOP_AUDIO_INTERVAL);
	p = begun();
	CHECK(a3_stop_request(&p, 1, false));
	CHECK(!a3_stop_set_fence(&p, 5, true, false) && p.error == A3_STOP_VIDEO_INTERVAL);
	p = begun();
	CHECK(!a3_stop_finish(&p) && !p.complete);
	p = begun();
	CHECK(a3_stop_request(&p, 1, false));
	CHECK(a3_stop_set_fence(&p, 4, true, false));
	CHECK(!a3_stop_finish(&p) && !p.complete); /* audio has not caught up */
	p = begun();
	CHECK(!a3_stop_fail(&p, A3_STOP_FORCED));
	CHECK(!a3_stop_finish(&p) && p.error == A3_STOP_FORCED && !p.complete);
	p = begun();
	CHECK(a3_stop_request(&p, 1, false));
	CHECK(a3_stop_set_fence(&p, 4, true, false));
	CHECK(!a3_stop_set_fence(&p, 8, true, false) && p.fence_sample == 2940);
	CHECK(!a3_stop_init(&p, 44100, 1024, 60000, 1001));
	CHECK(!a3_stop_init(&p, 0, 1024, 60, 1));
	CHECK(!a3_stop_init(&p, 44100, 0, 60, 1));
	CHECK(a3_stop_capture_valid(60, 60, 1000, 1100, 1200, 1300));
	CHECK(!a3_stop_capture_valid(60, 59, 1000, 1100, 1200, 1300));
	CHECK(!a3_stop_capture_valid(60, 60, 0, 1100, 1200, 1300));
	CHECK(!a3_stop_capture_valid(60, 60, 1200, 1100, 1200, 1300));
	CHECK(!a3_stop_capture_valid(60, 60, 1000, 1200, 1100, 1300));
	CHECK(!a3_stop_capture_valid(60, 60, 1000, 1100, 1400, 1300));

	printf("STOP_POLICY checks=%u failures=%u\n", checks, failures);
	return failures ? 1 : 0;
}
