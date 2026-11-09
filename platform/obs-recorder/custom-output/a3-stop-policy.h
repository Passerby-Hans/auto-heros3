#pragma once

/* Pure sample/frame-domain policy shared by the private output and offline
 * tests.  It does not stop an encoder: producers must keep running until the
 * output has received enough encoded media to cover the immutable IDR fence. */
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <limits.h>

enum a3_stop_error {
	A3_STOP_OK = 0,
	A3_STOP_UNSUPPORTED_FORMAT = 1,
	A3_STOP_FORCED = 2,
	A3_STOP_MISSING_MEDIA = 3,
	A3_STOP_PENDING_SPLIT = 4,
	A3_STOP_NOT_IDR = 5,
	A3_STOP_VIDEO_INTERVAL = 6,
	A3_STOP_AUDIO_INTERVAL = 7,
	A3_STOP_PACKET_FORMAT = 8,
	A3_STOP_MUX_ERROR = 9,
	A3_STOP_ENCODE_ERROR = 10,
	A3_STOP_CAPTURE_MAPPING = 11,
};

static inline bool a3_stop_capture_valid(int64_t packet_pts, int64_t timing_pts,
					uint64_t cts, uint64_t fer, uint64_t ferc, uint64_t pir)
{
	/* All four timestamps are libobs os_gettime_ns(), not media PTS/DTS.
	 * Zero/absent, stale and reordered metadata must never be guessed. */
	return packet_pts == timing_pts && cts > 0 && cts <= fer && fer <= ferc && ferc <= pir;
}

struct a3_stop_policy {
	bool requested, fence_set, complete, failed;
	bool waiting_for_split;
	bool has_audio, has_video;
	int error;
	uint64_t request_usec;
	uint32_t sample_rate, audio_frame_size, fps_num, fps_den;
	int64_t samples_per_video_frame;
	int64_t audio_last_pts, audio_end_sample;
	int64_t video_max_pts, video_frames;
	int64_t fence_video_pts, fence_sample;
};

static inline bool a3_stop_fail(struct a3_stop_policy *p, int error)
{
	if (!p->failed) p->error = error;
	p->failed = true;
	p->complete = false;
	return false;
}

static inline bool a3_stop_init(struct a3_stop_policy *p, uint32_t rate,
			      uint32_t audio_frames, uint32_t fps_num, uint32_t fps_den)
{
	memset(p, 0, sizeof(*p));
	p->video_max_pts = -1;
	p->sample_rate = rate;
	p->audio_frame_size = audio_frames;
	p->fps_num = fps_num;
	p->fps_den = fps_den;
	const uint64_t grid = (uint64_t)rate * fps_den;
	if (!rate || !audio_frames || !fps_num || !fps_den || grid % fps_num)
		return a3_stop_fail(p, A3_STOP_UNSUPPORTED_FORMAT);
	p->samples_per_video_frame = (int64_t)(grid / fps_num);
	return p->samples_per_video_frame > 0;
}

static inline bool a3_stop_note_video(struct a3_stop_policy *p, int64_t pts)
{
	if (p->failed || p->fence_set || pts < 0 ||
	    pts >= INT64_MAX / p->samples_per_video_frame)
		return a3_stop_fail(p, A3_STOP_VIDEO_INTERVAL);
	if (!p->has_video && pts != 0)
		return a3_stop_fail(p, A3_STOP_VIDEO_INTERVAL);
	p->has_video = true;
	p->video_frames++;
	if (pts > p->video_max_pts) p->video_max_pts = pts;
	return true;
}

static inline bool a3_stop_note_audio(struct a3_stop_policy *p, int64_t pts)
{
	if (p->failed || pts > INT64_MAX - p->audio_frame_size ||
	    (p->has_audio && pts != p->audio_end_sample))
		return a3_stop_fail(p, A3_STOP_AUDIO_INTERVAL);
	p->has_audio = true;
	p->audio_last_pts = pts;
	p->audio_end_sample = pts + p->audio_frame_size;
	return true;
}

static inline bool a3_stop_request(struct a3_stop_policy *p, uint64_t now_usec,
				 bool pending_split)
{
	if (p->requested) return !p->failed;
	p->requested = true;
	p->request_usec = now_usec;
	/* An in-flight rollover is not itself a failure.  Let its already-owned
	 * packets finish, without moving the original stop request or arming a
	 * competing fence inside that buffer. */
	p->waiting_for_split = pending_split;
	if (!p->has_video || !p->has_audio)
		return a3_stop_fail(p, A3_STOP_MISSING_MEDIA);
	return !p->failed;
}

static inline void a3_stop_split_drained(struct a3_stop_policy *p)
{
	p->waiting_for_split = false;
}

static inline bool a3_stop_set_fence(struct a3_stop_policy *p, int64_t pts,
				   bool is_idr, bool pending_split)
{
	if (p->failed || !p->requested || p->fence_set)
		return a3_stop_fail(p, A3_STOP_VIDEO_INTERVAL);
	if (pending_split || p->waiting_for_split) return a3_stop_fail(p, A3_STOP_PENDING_SPLIT);
	if (!is_idr) return a3_stop_fail(p, A3_STOP_NOT_IDR);
	/* Closed GOP: every pre-fence frame has arrived in decode order.  The
	 * frame count and maximum presentation endpoint must both equal T0. */
	if (!p->has_video || pts <= 0 || p->video_max_pts + 1 != pts ||
	    p->video_frames != pts || pts > INT64_MAX / p->samples_per_video_frame)
		return a3_stop_fail(p, A3_STOP_VIDEO_INTERVAL);
	p->fence_video_pts = pts;
	p->fence_sample = pts * p->samples_per_video_frame;
	p->fence_set = true;
	return true;
}

static inline bool a3_stop_audio_ready(const struct a3_stop_policy *p)
{
	return !p->failed && p->fence_set && p->has_audio &&
	       p->audio_end_sample >= p->fence_sample;
}

static inline bool a3_stop_finish(struct a3_stop_policy *p)
{
	if (!a3_stop_audio_ready(p)) return a3_stop_fail(p, A3_STOP_MISSING_MEDIA);
	p->complete = true;
	return true;
}

static inline int64_t a3_samples_usec(const struct a3_stop_policy *p, int64_t samples)
{
	if (!p->sample_rate) return 0;
	/* Split quotient/remainder so a long session cannot overflow samples*1e6. */
	return samples / p->sample_rate * 1000000 +
	       samples % p->sample_rate * 1000000 / p->sample_rate;
}

static inline int64_t a3_stop_video_end_usec(const struct a3_stop_policy *p)
{
	return p->has_video ? a3_samples_usec(p, (p->video_max_pts + 1) *
						p->samples_per_video_frame) : 0;
}
