/******************************************************************************
    Copyright (C) 2024 by Dennis Sädtler <dennis@obsproject.com>

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 2 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
******************************************************************************/

#include "mp4-mux.h"
#include "a3-stop-policy.h"
#include "a3-split-policy.h"

#include <inttypes.h>

#include <obs-module.h>
#include <obs-avc.h>
#include <util/platform.h>
#include <util/deque.h>
#include <util/dstr.h>
#include <util/threading.h>
#include <util/buffered-file-serializer.h>
#include <bpm.h>

#include <opts-parser.h>

#define do_log(level, format, ...)                                                                 \
	blog(level, "[%s output: '%s'] " format, out->muxer_flavor == FLAVOR_MOV ? "mov" : "mp4", \
	     obs_output_get_name(out->output), ##__VA_ARGS__)

#define warn(format, ...) do_log(LOG_WARNING, format, ##__VA_ARGS__)
#define info(format, ...) do_log(LOG_INFO, format, ##__VA_ARGS__)

struct chapter {
	uint64_t ts;
	char *name;
};

struct mp4_output {
	obs_output_t *output;
	struct dstr path;

	/* File serializer buffer configuration */
	size_t buffer_size;
	size_t chunk_size;
	struct serializer serializer;

	bool enable_bpm;

	volatile bool active;
	volatile bool stopping;
	uint64_t stop_ts;
	uint64_t stop_request_ns;

	bool allow_overwrite;
	uint64_t total_bytes;

	pthread_mutex_t mutex;

	struct mp4_mux *muxer;
	enum mp4_flavor muxer_flavor;
	int flags;

	size_t chapter_ctr;
	struct deque chapters;

	/* File splitting stuff */
	bool split_file_enabled;
	bool split_file_ready;
	volatile bool manual_split;

	size_t cur_size;
	size_t max_size;

	int64_t start_time;
	int64_t start_pts_usec;
	enum a3_split_reason split_reason;
	int64_t max_time;
	int64_t global_audio_delay_usec;
	int64_t segment_audio_delay_usec;

	/* Metadata ring of recent audio packets, for split-origin diagnostics. */
	struct audio_ring_entry {
		int64_t pts_usec;
		int64_t end_usec;
		int64_t dts_usec;
		size_t bytes;
	} audio_ring[8];
	unsigned audio_ring_pos;
	unsigned audio_ring_count;
	bool has_last_audio;
	bool has_prev_audio;
	uint64_t audio_packets;
	uint64_t video_packets;
	struct a3_stop_policy stop;
	uint64_t fence_observed_usec;
	bool packet_time_valid;
	int64_t packet_time_pts;
	int64_t packet_timing_pts;
	uint64_t packet_capture_ns;
	bool fence_capture_valid;
	uint64_t fence_capture_ns;
	int64_t fence_timing_pts;
	struct encoder_packet last_audio;
	struct encoder_packet prev_audio;

	/* Buffer for packets while we reinitialise the muxer after splitting */
	DARRAY(struct encoder_packet) split_buffer;
};

static inline bool stopping(struct mp4_output *out)
{
	return os_atomic_load_bool(&out->stopping);
}

static inline bool active(struct mp4_output *out)
{
	return os_atomic_load_bool(&out->active);
}

static inline int64_t packet_pts_usec(struct encoder_packet *packet)
{
	return packet->pts * packet->timebase_num * 1000000 / packet->timebase_den;
}

static inline int64_t audio_packet_end_usec(struct mp4_output *out,
					  struct encoder_packet *packet)
{
	return a3_samples_usec(&out->stop, packet->pts + out->stop.audio_frame_size);
}

static void mp4_output_actual_stop(struct mp4_output *out, int code);
void mp4_pkt_callback(obs_output_t *output, struct encoder_packet *pkt,
		      struct encoder_packet_time *pkt_time, void *param);

static const char *mp4_output_name(void *unused)
{
	UNUSED_PARAMETER(unused);
	return obs_module_text("MP4Output");
}

static const char *mov_output_name(void *unused)
{
	UNUSED_PARAMETER(unused);
	return obs_module_text("MOVOutput");
}

static void mp4_clear_chapters(struct mp4_output *out)
{
	while (out->chapters.size) {
		struct chapter *chap = deque_data(&out->chapters, 0);
		bfree(chap->name);
		deque_pop_front(&out->chapters, NULL, sizeof(struct chapter));
	}
	out->chapter_ctr = 0;
}

static void mp4_output_destroy(void *data)
{
	struct mp4_output *out = data;
	obs_output_remove_packet_callback(out->output, mp4_pkt_callback, out);
	if (out && out->has_last_audio) {
		obs_encoder_packet_release(&out->last_audio);
		out->has_last_audio = false;
	}
	if (out && out->has_prev_audio) {
		obs_encoder_packet_release(&out->prev_audio);
		out->has_prev_audio = false;
	}

	pthread_mutex_destroy(&out->mutex);
	mp4_clear_chapters(out);
	deque_free(&out->chapters);
	dstr_free(&out->path);
	bfree(out);
}

void mp4_pkt_callback(obs_output_t *output, struct encoder_packet *pkt, struct encoder_packet_time *pkt_time,
		      void *param)
{
	UNUSED_PARAMETER(output);
	struct mp4_output *out = param;

	/* Only the primary video track is used as the reference for the chapter track. */
	if (!pkt || pkt->type != OBS_ENCODER_VIDEO || pkt->track_idx != 0)
		return;

	pthread_mutex_lock(&out->mutex);
	if (!active(out)) {
		pthread_mutex_unlock(&out->mutex);
		return;
	}
	/* libobs invokes this callback immediately before encoded_packet, using
	 * the same interleaved packet.  It offsets packet_time.pts alongside packet
	 * PTS, while CTS remains the os_gettime_ns() composition/capture clock. */
	out->packet_time_pts = pkt->pts;
	out->packet_timing_pts = pkt_time ? pkt_time->pts : INT64_MIN;
	out->packet_capture_ns = pkt_time ? pkt_time->cts : 0;
	out->packet_time_valid = pkt_time && a3_stop_capture_valid(pkt->pts, pkt_time->pts,
		pkt_time->cts, pkt_time->fer, pkt_time->ferc, pkt_time->pir);
	if (!pkt_time) {
		pthread_mutex_unlock(&out->mutex);
		return;
	}
	struct chapter *chap = NULL;
	/* Write all queued chapters with a timestamp <= the current frame's composition time. */
	while (out->chapters.size) {
		chap = deque_data(&out->chapters, 0);
		if (chap->ts > pkt_time->cts)
			break;

		/* Video frames can be out of order (b-frames), so instead of using the video packet's dts_usec we need to calculate
		 * the chapter DTS from the frame's PTS (for chapters DTS == PTS). */
		int64_t chap_dts_usec = (pkt->pts * 1000000 / pkt->timebase_den) - out->start_time;
		int64_t chap_dts_msec = chap_dts_usec / 1000;
		int64_t chap_dts_sec = chap_dts_msec / 1000;

		int milliseconds = (int)(chap_dts_msec % 1000);
		int seconds = (int)chap_dts_sec % 60;
		int minutes = ((int)chap_dts_sec / 60) % 60;
		int hours = (int)chap_dts_sec / 3600;
		info("Adding chapter \"%s\" at %02d:%02d:%02d.%03d", chap->name, hours, minutes, seconds, milliseconds);

		mp4_mux_add_chapter(out->muxer, chap_dts_usec, chap->name);
		/* Free name and remove chapter from queue. */
		bfree(chap->name);
		deque_pop_front(&out->chapters, NULL, sizeof(struct chapter));
	}
	pthread_mutex_unlock(&out->mutex);
}

static void mp4_add_chapter_proc(void *data, calldata_t *cd)
{
	struct mp4_output *out = data;
	struct dstr name = {0};

	dstr_copy(&name, calldata_string(cd, "chapter_name"));

	if (name.len == 0) {
		/* Generate name if none provided. */
		dstr_catf(&name, "%s %zu", obs_module_text("MP4Output.UnnamedChapter"), out->chapter_ctr + 1);
	}

	pthread_mutex_lock(&out->mutex);
	/* Enqueue chapter marker to be inserted once a packet containing the video frame with the corresponding timestamp is
	 * being processed. */
	struct chapter chap = {0};
	chap.ts = obs_get_video_frame_time();
	chap.name = name.array;
	deque_push_back(&out->chapters, &chap, sizeof(struct chapter));
	out->chapter_ctr++;
	pthread_mutex_unlock(&out->mutex);
}


static void a3_audio_state_proc(void *data, calldata_t *cd)
{
	struct mp4_output *out = data;
	pthread_mutex_lock(&out->mutex);
	const struct a3_stop_policy *p = &out->stop;
	const int64_t video_end = a3_stop_video_end_usec(p);
	const int64_t fence = p->fence_set ? a3_samples_usec(p, p->fence_sample) : 0;
	const int64_t audio_end = p->has_audio ? a3_samples_usec(p, p->audio_end_sample) : 0;
	calldata_set_int(cd, "contract_version", 2);
	calldata_set_int(cd, "audio_packets", (long long)out->audio_packets);
	calldata_set_int(cd, "audio_end_usec", (long long)audio_end);
	calldata_set_int(cd, "video_target_usec", (long long)(p->fence_set ? fence : video_end));
	calldata_set_int(cd, "audio_ready", a3_stop_audio_ready(p));
	calldata_set_int(cd, "stop_requested", p->requested);
	calldata_set_int(cd, "waiting_for_split", p->waiting_for_split);
	calldata_set_int(cd, "fence_set", p->fence_set);
	calldata_set_int(cd, "fence_usec", (long long)fence);
	calldata_set_int(cd, "video_end_usec", (long long)video_end);
	calldata_set_int(cd, "audio_end_sample", (long long)p->audio_end_sample);
	calldata_set_int(cd, "fence_sample", (long long)p->fence_sample);
	calldata_set_int(cd, "complete", p->complete);
	calldata_set_int(cd, "failed", p->failed);
	calldata_set_int(cd, "stop_error", p->error);
	calldata_set_int(cd, "stop_request_usec", (long long)p->request_usec);
	calldata_set_int(cd, "stop_request_ns", (long long)out->stop_request_ns);
	calldata_set_int(cd, "keyframe_wait_usec", (long long)(p->fence_set
		? out->fence_observed_usec - p->request_usec : 0));
	calldata_set_int(cd, "fence_capture_valid", out->fence_capture_valid);
	calldata_set_int(cd, "fence_capture_ns", (long long)out->fence_capture_ns);
	calldata_set_int(cd, "fence_packet_pts", (long long)p->fence_video_pts);
	calldata_set_int(cd, "fence_timing_pts", (long long)out->fence_timing_pts);
	calldata_set_int(cd, "audio_frame_size", p->audio_frame_size);
	calldata_set_int(cd, "audio_sample_rate", p->sample_rate);
	calldata_set_int(cd, "video_fps_num", p->fps_num);
	calldata_set_int(cd, "video_fps_den", p->fps_den);
	pthread_mutex_unlock(&out->mutex);
}

static void split_file_proc(void *data, calldata_t *cd)
{
	struct mp4_output *out = data;

	calldata_set_bool(cd, "split_file_enabled", out->split_file_enabled);
	if (!out->split_file_enabled)
		return;

	os_atomic_set_bool(&out->manual_split, true);
}

static void *mp4_output_create_internal(obs_data_t *settings, obs_output_t *output, enum mp4_flavor flavor)
{
	struct mp4_output *out = bzalloc(sizeof(struct mp4_output));
	out->output = output;
	out->muxer_flavor = flavor;
	pthread_mutex_init(&out->mutex, NULL);

	signal_handler_t *sh = obs_output_get_signal_handler(output);
	signal_handler_add(sh, "void file_changed(string next_file, string split_reason, int boundary_pts_usec, int start_pts_usec, int closed_bytes)");

	proc_handler_t *ph = obs_output_get_proc_handler(output);
	proc_handler_add(ph, "void split_file(out bool split_file_enabled)", split_file_proc, out);
	proc_handler_add(ph, "void get_a3_audio_state(out int contract_version, out int audio_packets, out int audio_end_usec, out int video_target_usec, out int audio_ready, out int stop_requested, out int waiting_for_split, out int fence_set, out int fence_usec, out int video_end_usec, out int audio_end_sample, out int fence_sample, out int complete, out int failed, out int stop_error, out int stop_request_usec, out int stop_request_ns, out int keyframe_wait_usec, out int fence_capture_valid, out int fence_capture_ns, out int fence_packet_pts, out int fence_timing_pts, out int audio_frame_size, out int audio_sample_rate, out int video_fps_num, out int video_fps_den)", a3_audio_state_proc, out);
	proc_handler_add(ph, "void add_chapter(string chapter_name)", mp4_add_chapter_proc, out);

	UNUSED_PARAMETER(settings);
	return out;
}

static void *mp4_output_create(obs_data_t *settings, obs_output_t *output)
{
	return mp4_output_create_internal(settings, output, FLAVOR_MP4);
}

static void *mov_output_create(obs_data_t *settings, obs_output_t *output)
{
	return mp4_output_create_internal(settings, output, FLAVOR_MOV);
}

static inline void apply_flag(int *flags, const char *value, int flag_value)
{
	if (atoi(value))
		*flags |= flag_value;
	else
		*flags &= ~flag_value;
}

static void parse_custom_options(struct mp4_output *out, const char *opts_str)
{
	int flags = MP4_USE_NEGATIVE_CTS;

	struct obs_options opts = obs_parse_options(opts_str);

	for (size_t i = 0; i < opts.count; i++) {
		struct obs_option opt = opts.options[i];

		if (strcmp(opt.name, "skip_soft_remux") == 0) {
			apply_flag(&flags, opt.value, MP4_SKIP_FINALISATION);
		} else if (strcmp(opt.name, "write_encoder_info") == 0) {
			apply_flag(&flags, opt.value, MP4_WRITE_ENCODER_INFO);
		} else if (strcmp(opt.name, "use_metadata_tags") == 0) {
			apply_flag(&flags, opt.value, MP4_USE_MDTA_KEY_VALUE);
		} else if (strcmp(opt.name, "use_negative_cts") == 0) {
			apply_flag(&flags, opt.value, MP4_USE_NEGATIVE_CTS);
		} else if (strcmp(opt.name, "buffer_size") == 0) {
			out->buffer_size = strtoull(opt.value, 0, 10) * 1048576ULL;
		} else if (strcmp(opt.name, "chunk_size") == 0) {
			out->chunk_size = strtoull(opt.value, 0, 10) * 1048576ULL;
		} else if (strcmp(opt.name, "bpm") == 0) {
			out->enable_bpm = !!atoi(opt.value);
		} else {
			blog(LOG_WARNING, "Unknown muxer option: %s = %s", opt.name, opt.value);
		}
	}

	obs_free_options(opts);

	out->flags = flags;
}

static void generate_filename(struct mp4_output *out, struct dstr *dst, bool overwrite);

static bool mp4_output_start(void *data)
{
	struct mp4_output *out = data;

	if (!obs_output_can_begin_data_capture(out->output, 0))
		return false;
	if (!obs_output_initialize_encoders(out->output, 0))
		return false;

	/* This private output deliberately has a narrower, checked contract than
	 * upstream's generic MP4 output.  Packet durations come from the initialized
	 * encoders; a keyframe is additionally checked for an actual H.264 IDR. */
	obs_encoder_t *audio = obs_output_get_audio_encoder(out->output, 0);
	obs_encoder_t *video = obs_output_get_video_encoder2(out->output, 0);
	const struct video_output_info *vi = video && obs_encoder_video(video)
		? video_output_get_info(obs_encoder_video(video)) : NULL;
	const uint32_t rate = audio ? obs_encoder_get_sample_rate(audio) : 0;
	const size_t audio_frames = audio ? obs_encoder_get_frame_size(audio) : 0;
	const char *audio_codec = audio ? obs_encoder_get_codec(audio) : NULL;
	const char *video_codec = video ? obs_encoder_get_codec(video) : NULL;
	const char *video_id = video ? obs_encoder_get_id(video) : NULL;
	bool single_track = true;
	for (size_t i = 1; i < MAX_OUTPUT_AUDIO_ENCODERS; i++)
		if (obs_output_get_audio_encoder(out->output, i)) single_track = false;
	for (size_t i = 1; i < MAX_OUTPUT_VIDEO_ENCODERS; i++)
		if (obs_output_get_video_encoder2(out->output, i)) single_track = false;
	if (!audio || !video || !vi || !single_track || rate != 44100 || vi->fps_den != 1 ||
	    !audio_frames || audio_frames > UINT32_MAX ||
	    !audio_codec || strcmp(audio_codec, "aac") != 0 ||
	    !video_codec || strcmp(video_codec, "h264") != 0 ||
	    !video_id || strcmp(video_id, "obs_x264") != 0 ||
	    !a3_stop_init(&out->stop, rate, (uint32_t)audio_frames, vi->fps_num, vi->fps_den)) {
		a3_stop_fail(&out->stop, A3_STOP_UNSUPPORTED_FORMAT);
		obs_output_set_last_error(out->output, "a3_stop_unsupported_format");
		warn("a3 stop requires single-track obs_x264 H264 + AAC 44100 and an integral sample/frame grid");
		return false;
	}
	info("a3 stop contract=2 closed-GOP IDR fence, fps=%u/%u audio_rate=%u audio_frame_size=%zu; B-frame PTS reordering is allowed, open-GOP keyframes fail closed",
	     vi->fps_num, vi->fps_den, rate, audio_frames);

	os_atomic_set_bool(&out->stopping, false);
	os_atomic_set_bool(&out->manual_split, false);

	obs_data_t *settings = obs_output_get_settings(out->output);
	out->max_time = obs_data_get_int(settings, "max_time_sec") * 1000000LL;
	out->max_size = obs_data_get_int(settings, "max_size_mb") * 1024 * 1024;
	out->split_file_enabled = obs_data_get_bool(settings, "split_file");
	out->allow_overwrite = obs_data_get_bool(settings, "allow_overwrite");
	out->cur_size = 0;
	out->total_bytes = 0;
	out->start_time = 0;
	out->start_pts_usec = 0;
	out->split_reason = A3_SPLIT_NONE;
	out->global_audio_delay_usec = 0;
	out->segment_audio_delay_usec = 0;
	out->audio_ring_pos = 0;
	out->audio_ring_count = 0;
	out->audio_packets = 0;
	out->video_packets = 0;
	out->fence_observed_usec = 0;
	out->stop_request_ns = 0;
	out->packet_time_valid = false;
	out->fence_capture_valid = false;
	out->fence_capture_ns = 0;
	out->fence_timing_pts = 0;
	if (out->has_last_audio) obs_encoder_packet_release(&out->last_audio);
	if (out->has_prev_audio) obs_encoder_packet_release(&out->prev_audio);
	out->has_last_audio = false;
	out->has_prev_audio = false;
	memset(&out->last_audio, 0, sizeof(out->last_audio));
	memset(&out->prev_audio, 0, sizeof(out->prev_audio));
	{
		const char *delay_env = getenv("A3_MP4_AUDIO_DELAY_US");
		if (delay_env && *delay_env) {
			long long v = atoll(delay_env);
			if (v > 0) out->global_audio_delay_usec = v;
		}
	}
	{
		const char *seg_env = getenv("A3_MP4_SEGMENT_DELAY_US");
		if (seg_env && *seg_env) {
			long long v = atoll(seg_env);
			if (v >= 0) out->segment_audio_delay_usec = v;
		}
	}
	if (out->global_audio_delay_usec || out->segment_audio_delay_usec) {
		a3_stop_fail(&out->stop, A3_STOP_UNSUPPORTED_FORMAT);
		obs_output_set_last_error(out->output, "a3_stop_presentation_delay_unsupported");
		obs_data_release(settings);
		return false;
	}

	/* Get path */
	const char *path = obs_data_get_string(settings, "path");
	if (path && *path) {
		dstr_copy(&out->path, path);
	} else {
		generate_filename(out, &out->path, out->allow_overwrite);
		info("Output path not specified. Using generated path '%s'", out->path.array);
	}

	/* Allow skipping the remux step for debugging purposes. */
	const char *muxer_settings = obs_data_get_string(settings, "muxer_settings");
	parse_custom_options(out, muxer_settings);

	obs_data_release(settings);
	if (out->enable_bpm || (out->flags & MP4_SKIP_FINALISATION)) {
		a3_stop_fail(&out->stop, A3_STOP_UNSUPPORTED_FORMAT);
		obs_output_set_last_error(out->output, "a3_stop_finalization_options_unsupported");
		return false;
	}

	if (out->enable_bpm) {
		info("Enabling BPM");
		obs_output_add_packet_callback(out->output, bpm_inject, NULL);
	}

	if (!buffered_file_serializer_init(&out->serializer, out->path.array, out->buffer_size, out->chunk_size)) {
		warn("Unable to open file '%s'", out->path.array);
		return false;
	}

	/* Add packet callback for accurate chapter markers. */
	obs_output_remove_packet_callback(out->output, mp4_pkt_callback, out);
	obs_output_add_packet_callback(out->output, mp4_pkt_callback, (void *)out);

	/* Initialise muxer and start capture */
	out->muxer = mp4_mux_create(out->output, &out->serializer, out->flags, out->muxer_flavor);
	mp4_mux_set_audio_delay(out->muxer, out->global_audio_delay_usec);
	os_atomic_set_bool(&out->active, true);
	obs_output_begin_data_capture(out->output, 0);

	info("Writing Hybrid MP4/MOV file '%s'...", out->path.array);
	return true;
}

static inline bool should_split(struct mp4_output *out, struct encoder_packet *packet)
{
	/* A stop owns the next IDR after any in-flight split has drained.  Do not
	 * create a new split; the existing split_buffer branch still runs. */
	if (stopping(out)) return false;
	/* split at video frame on primary track */
	if (packet->type != OBS_ENCODER_VIDEO || packet->track_idx > 0)
		return false;

	/* don't split group of pictures */
	if (!packet->keyframe)
		return false;

	out->split_reason = a3_split_decide(os_atomic_load_bool(&out->manual_split),
		out->cur_size, packet->size, out->max_size, packet_pts_usec(packet),
		out->start_pts_usec, out->max_time);
	return out->split_reason != A3_SPLIT_NONE;
}

static void find_best_filename(struct dstr *path, bool space)
{
	int num = 2;

	if (!os_file_exists(path->array))
		return;

	const char *ext = strrchr(path->array, '.');
	if (!ext)
		return;

	size_t extstart = ext - path->array;
	struct dstr testpath;
	dstr_init_copy_dstr(&testpath, path);
	for (;;) {
		dstr_resize(&testpath, extstart);
		dstr_catf(&testpath, space ? " (%d)" : "_%d", num++);
		dstr_cat(&testpath, ext);

		if (!os_file_exists(testpath.array)) {
			dstr_free(path);
			dstr_init_move(path, &testpath);
			break;
		}
	}
}

static void generate_filename(struct mp4_output *out, struct dstr *dst, bool overwrite)
{
	obs_data_t *settings = obs_output_get_settings(out->output);
	const char *dir = obs_data_get_string(settings, "directory");
	const char *fmt = obs_data_get_string(settings, "format");
	const char *ext = obs_data_get_string(settings, "extension");
	bool space = obs_data_get_bool(settings, "allow_spaces");

	char *filename = os_generate_formatted_filename(ext, space, fmt);

	dstr_copy(dst, dir);
	dstr_replace(dst, "\\", "/");
	if (dstr_end(dst) != '/')
		dstr_cat_ch(dst, '/');
	dstr_cat(dst, filename);

	char *slash = strrchr(dst->array, '/');
	if (slash) {
		*slash = 0;
		os_mkdirs(dst->array);
		*slash = '/';
	}

	if (!overwrite)
		find_best_filename(dst, space);

	bfree(filename);
	obs_data_release(settings);
}

static bool change_file(struct mp4_output *out, struct encoder_packet *pkt)
{
	uint64_t start_time = os_gettime_ns();

	/* finalise file */
	if (!mp4_mux_finalise(out->muxer) || serializer_get_pos(&out->serializer) == -1) {
		a3_stop_fail(&out->stop, A3_STOP_MUX_ERROR);
		return false;
	}

	info("Waiting for file writer to finish...");

	/* flush/close file and destroy old muxer */
	buffered_file_serializer_free(&out->serializer);
	memset(&out->serializer, 0, sizeof(out->serializer));
	mp4_mux_destroy(out->muxer);
	out->muxer = NULL;
	mp4_clear_chapters(out);
	/* Finalization seeks back to rewrite MP4 headers. The serializer cursor
	 * is therefore not file length; inspect the fully closed artifact. */
	const int64_t closed_bytes = os_get_file_size(out->path.array);

	info("File split complete. Finalization took %" PRIu64 " ms.", (os_gettime_ns() - start_time) / 1000000);

	/* open new file */
	generate_filename(out, &out->path, out->allow_overwrite);
	info("Changing output file to '%s'", out->path.array);

	if (!buffered_file_serializer_init(&out->serializer, out->path.array, out->buffer_size, out->chunk_size)) {
		warn("Unable to open file '%s'", out->path.array);
		return false;
	}

	out->muxer = mp4_mux_create(out->output, &out->serializer, out->flags, out->muxer_flavor);

	calldata_t cd = {0};
	signal_handler_t *sh = obs_output_get_signal_handler(out->output);
	calldata_set_string(&cd, "next_file", out->path.array);
	calldata_set_string(&cd, "split_reason", a3_split_name(out->split_reason));
	calldata_set_int(&cd, "boundary_pts_usec", packet_pts_usec(pkt));
	calldata_set_int(&cd, "start_pts_usec", out->start_pts_usec);
	calldata_set_int(&cd, "closed_bytes", closed_bytes);
	info("a3 rollover reason=%s start_pts_usec=%" PRId64 " boundary_pts_usec=%" PRId64
	     " closed_bytes=%" PRId64 " size_trigger=%zu duration_trigger_usec=%" PRId64,
	     a3_split_name(out->split_reason), out->start_pts_usec, packet_pts_usec(pkt),
	     closed_bytes, out->max_size, out->max_time);
	signal_handler_signal(sh, "file_changed", &cd);
	calldata_free(&cd);

	out->cur_size = 0;
	out->start_time = pkt->dts_usec;
	out->start_pts_usec = packet_pts_usec(pkt);

	return true;
}

static void mp4_output_stop(void *data, uint64_t ts)
{
	struct mp4_output *out = data;
	pthread_mutex_lock(&out->mutex);
	if (!active(out)) goto done;
	if (!ts) {
		a3_stop_fail(&out->stop, A3_STOP_FORCED);
		mp4_output_actual_stop(out, OBS_OUTPUT_ERROR);
		goto done;
	}
	if (!stopping(out)) {
		out->stop_ts = ts / 1000;
		out->stop_request_ns = ts;
		os_atomic_set_bool(&out->stopping, true);
		if (!a3_stop_request(&out->stop, out->stop_ts, out->split_buffer.num != 0)) {
			warn("a3 stop rejected error=%d: missing or invalid media", out->stop.error);
			mp4_output_actual_stop(out, OBS_OUTPUT_ERROR);
			goto done;
		}
		info("a3 stop requested system_usec=%" PRIu64 "; waiting for the next closed-GOP IDR, producers remain active",
		     out->stop_ts);
		if (out->stop.waiting_for_split)
			info("a3 stop defers fence until the existing rollover buffer drains (%zu packets)", out->split_buffer.num);
	}
done:
	pthread_mutex_unlock(&out->mutex);
}

static void mp4_mux_destroy_task(void *ptr)
{
	struct mp4_mux *muxer = ptr;
	mp4_mux_destroy(muxer);
}

static void mp4_output_actual_stop(struct mp4_output *out, int code)
{
	os_atomic_set_bool(&out->active, false);

	uint64_t start_time = os_gettime_ns();

	if (!out->muxer || !mp4_mux_finalise(out->muxer) ||
	    serializer_get_pos(&out->serializer) == -1) {
		a3_stop_fail(&out->stop, A3_STOP_MUX_ERROR);
		code = OBS_OUTPUT_ERROR;
	}

	if (out->enable_bpm) {
		obs_output_remove_packet_callback(out->output, bpm_inject, NULL);
		bpm_destroy(out->output);
	}

	info("Waiting for file writer to finish...");

	/* Flush/close output file and destroy muxer */
	buffered_file_serializer_free(&out->serializer);
	memset(&out->serializer, 0, sizeof(out->serializer));
	if (out->muxer)
		obs_queue_task(OBS_TASK_DESTROY, mp4_mux_destroy_task, out->muxer, false);
	out->muxer = NULL;
	/* On an explicitly failed stop these buffered packets are incomplete
	 * evidence, never a successful segment.  Release all retained references. */
	for (size_t i = 0; i < out->split_buffer.num; i++)
		obs_encoder_packet_release(&out->split_buffer.array[i]);
	da_free(out->split_buffer);
	out->split_file_ready = false;

	/* Clear chapter data */
	mp4_clear_chapters(out);

	info("File output complete. Finalization took %" PRIu64 " ms.", (os_gettime_ns() - start_time) / 1000000);
	/* Publish completion only after mux finalization and writer close.  The
	 * host must never observe inactive while the last file is still closing. */
	if (!code && !a3_stop_finish(&out->stop)) code = OBS_OUTPUT_ERROR;
	if (code) {
		if (!out->stop.failed) a3_stop_fail(&out->stop, A3_STOP_ENCODE_ERROR);
		obs_output_set_last_error(out->output, "a3_stop_incomplete");
		obs_output_signal_stop(out->output, code);
	} else {
		obs_output_end_data_capture(out->output);
	}
}

static void push_back_packet(struct mp4_output *out, struct encoder_packet *packet)
{
	struct encoder_packet pkt;
	obs_encoder_packet_ref(&pkt, packet);
	da_push_back(out->split_buffer, &pkt);
}

static inline bool submit_packet(struct mp4_output *out, struct encoder_packet *pkt)
{
	out->total_bytes += pkt->size;
	out->cur_size += pkt->size;

	return mp4_mux_submit_packet(out->muxer, pkt);
}

static void a3_finish_covered_stop(struct mp4_output *out)
{
	if (!a3_stop_audio_ready(&out->stop)) return;
	const int64_t tail_samples = out->stop.audio_end_sample - out->stop.fence_sample;
	if (tail_samples < 0 || tail_samples >= out->stop.audio_frame_size) {
		a3_stop_fail(&out->stop, A3_STOP_AUDIO_INTERVAL);
		mp4_output_actual_stop(out, OBS_OUTPUT_ERROR);
		return;
	}
	/* Round only at the existing mux API boundary; its usec->sample round
	 * recovers the exact sample count for the supported 44.1 kHz profile. */
	const int64_t tail_usec = (tail_samples * 1000000 + out->stop.sample_rate / 2) /
		out->stop.sample_rate;
	mp4_mux_set_audio_end_trim(out->muxer, tail_usec);
	info("a3 stop covered fence_sample=%" PRId64 " audio_end_sample=%" PRId64
	     " tail_trim_samples=%" PRId64 " video_frames=%" PRId64,
	     out->stop.fence_sample, out->stop.audio_end_sample, tail_samples, out->stop.video_frames);
	mp4_output_actual_stop(out, 0);
}

static void mp4_output_packet(void *data, struct encoder_packet *packet)
{
	struct mp4_output *out = data;

	pthread_mutex_lock(&out->mutex);

	if (!active(out))
		goto unlock;

	if (!packet) {
		a3_stop_fail(&out->stop, A3_STOP_ENCODE_ERROR);
		mp4_output_actual_stop(out, OBS_OUTPUT_ENCODE_ERROR);
		goto unlock;
	}

	const bool is_audio = packet->type == OBS_ENCODER_AUDIO;
	const bool is_video = packet->type == OBS_ENCODER_VIDEO;
	if (packet->track_idx != 0 || (!is_audio && !is_video) ||
	    (is_audio && (packet->timebase_num != 1 ||
			  packet->timebase_den != (int32_t)out->stop.sample_rate)) ||
	    (is_video && (packet->timebase_num != (int32_t)out->stop.fps_den ||
			  packet->timebase_den != (int32_t)out->stop.fps_num))) {
		a3_stop_fail(&out->stop, A3_STOP_PACKET_FORMAT);
		mp4_output_actual_stop(out, OBS_OUTPUT_ERROR);
		goto unlock;
	}

	if (out->stop.fence_set && is_video) {
		/* Keep libobs/encoders/interleaver running, but this IDR and all
		 * subsequent video belong after the fixed presentation fence. */
		goto unlock;
	}
	if (stopping(out) && !out->split_buffer.num && is_video && packet->keyframe) {
		if (!out->packet_time_valid || out->packet_time_pts != packet->pts) {
			a3_stop_fail(&out->stop, A3_STOP_CAPTURE_MAPPING);
			warn("a3 stop capture mapping unavailable: pts=%" PRId64 " timing_pts=%" PRId64 " cts=%" PRIu64,
			     packet->pts, out->packet_timing_pts, out->packet_capture_ns);
			mp4_output_actual_stop(out, OBS_OUTPUT_ERROR);
			goto unlock;
		}
		/* An IDR can be delivered after stop but have been captured before
		 * the request.  Keep it; only a genuine post-request frame is a fence. */
		if (out->packet_capture_ns < out->stop_request_ns) goto retain_packet;
		const bool idr = obs_avc_keyframe(packet->data, packet->size);
		if (!a3_stop_set_fence(&out->stop, packet->pts, idr, out->split_buffer.num != 0)) {
			warn("a3 stop fence rejected error=%d pts=%" PRId64 " idr=%d",
			     out->stop.error, packet->pts, idr);
			mp4_output_actual_stop(out, OBS_OUTPUT_ERROR);
			goto unlock;
		}
		out->fence_observed_usec = os_gettime_ns() / 1000;
		out->fence_capture_ns = out->packet_capture_ns;
		out->fence_timing_pts = out->packet_timing_pts;
		out->fence_capture_valid = true;
		info("a3 stop fence fixed pts=%" PRId64 " sample=%" PRId64
		     " capture_ns=%" PRIu64 " timing_pts=%" PRId64
		     " request_wait_usec=%" PRIu64 "; IDR is excluded from file",
		     packet->pts, out->stop.fence_sample, out->fence_capture_ns,
		     out->fence_timing_pts, out->fence_observed_usec - out->stop.request_usec);
		a3_finish_covered_stop(out);
		goto unlock;
	}
retain_packet:
	if (out->stop.fence_set && is_audio && packet->pts >= out->stop.fence_sample) {
		/* The crossing AU must have been received first.  Never synthesize
		 * a missing encoded AU or report success after an audio gap. */
		if (a3_stop_audio_ready(&out->stop)) a3_finish_covered_stop(out);
		else {
			a3_stop_fail(&out->stop, A3_STOP_AUDIO_INTERVAL);
			mp4_output_actual_stop(out, OBS_OUTPUT_ERROR);
		}
		goto unlock;
	}
	if ((is_audio && !a3_stop_note_audio(&out->stop, packet->pts)) ||
	    (is_video && !a3_stop_note_video(&out->stop, packet->pts))) {
		warn("a3 packet timeline rejected error=%d pts=%" PRId64, out->stop.error, packet->pts);
		mp4_output_actual_stop(out, OBS_OUTPUT_ERROR);
		goto unlock;
	}

	if (packet->type == OBS_ENCODER_AUDIO) {
		if (out->has_prev_audio)
			obs_encoder_packet_release(&out->prev_audio);
		if (out->has_last_audio) {
			out->prev_audio = out->last_audio; /* transfer ownership */
			out->has_prev_audio = true;
		}
		obs_encoder_packet_ref(&out->last_audio, packet);
		out->has_last_audio = true;
		out->audio_ring[out->audio_ring_pos].pts_usec = packet_pts_usec(packet);
		out->audio_ring[out->audio_ring_pos].end_usec = audio_packet_end_usec(out, packet);
		out->audio_ring[out->audio_ring_pos].dts_usec = packet->dts_usec;
		out->audio_ring[out->audio_ring_pos].bytes = packet->size;
		out->audio_ring_pos = (out->audio_ring_pos + 1) % 8;
		if (out->audio_ring_count < 8) out->audio_ring_count++;
		out->audio_packets++;
	} else if (packet->type == OBS_ENCODER_VIDEO) {
		out->video_packets++;
	}
	if (out->stop.fence_set) {
		if (!submit_packet(out, packet) || serializer_get_pos(&out->serializer) == -1) {
			a3_stop_fail(&out->stop, A3_STOP_MUX_ERROR);
			mp4_output_actual_stop(out, OBS_OUTPUT_ERROR);
		} else a3_finish_covered_stop(out);
		goto unlock;
	}

	if (out->split_file_enabled) {
		if (out->split_buffer.num) {
			int64_t pts_usec = packet_pts_usec(packet);
			struct encoder_packet *first_pkt = out->split_buffer.array;
			int64_t first_pts_usec = packet_pts_usec(first_pkt);

			/* The audio packet that contains the video keyframe often
			 * arrives after the video packet.  Keep it as pre-roll so
			 * the edit list can trim from the exact T0. */
			if (packet->type == OBS_ENCODER_AUDIO &&
			    pts_usec < first_pts_usec) {
				/* The packet spans the boundary.  The old segment must
				 * present its pre-T0 tail; the new segment trims it at T0.
				 * Duplicating encoded bytes is allowed for decoder
				 * pre-roll, but the presented intervals stay disjoint. */
				int64_t tail = audio_packet_end_usec(out, packet) - first_pts_usec;
				if (tail > 0) {
					mp4_mux_set_audio_end_trim(out->muxer, tail);
				}
				if (!submit_packet(out, packet)) {
					a3_stop_fail(&out->stop, A3_STOP_MUX_ERROR);
					mp4_output_actual_stop(out, OBS_OUTPUT_ERROR);
					goto unlock;
				}
				push_back_packet(out, packet);
				info("a3 split late pre-roll audio=%" PRId64
				     " video=%" PRId64 " old_tail_trim=%" PRId64,
				     pts_usec, first_pts_usec, tail > 0 ? tail : 0);
				goto unlock;
			}

			if (pts_usec >= first_pts_usec) {
				if (packet->type != OBS_ENCODER_AUDIO) {
					push_back_packet(out, packet);
					goto unlock;
				}

				if (!change_file(out, first_pkt)) {
					mp4_output_actual_stop(out, OBS_OUTPUT_ERROR);
					goto unlock;
				}
				/* a3 segment T0 fence: trim the portion of the first
				 * audio packet before the new video keyframe. */
				int64_t video_pts = packet_pts_usec(first_pkt);
				int64_t audio_pts = INT64_MAX;
				for (size_t i = 0; i < out->split_buffer.num; i++) {
					struct encoder_packet *pkt = &out->split_buffer.array[i];
					if (pkt->type == OBS_ENCODER_AUDIO) {
						int64_t p = packet_pts_usec(pkt);
						if (p < audio_pts) audio_pts = p;
					}
				}
				if (packet->type == OBS_ENCODER_AUDIO) {
					int64_t p = packet_pts_usec(packet);
					if (p < audio_pts) audio_pts = p;
				}
				info("a3 split fence: video_pts=%" PRId64 " audio_pts=%" PRId64 " trim=%" PRId64,
				     video_pts, audio_pts, audio_pts == INT64_MAX ? 0 : video_pts - audio_pts);
				for (unsigned ai = 0; ai < out->audio_ring_count; ai++) {
					unsigned idx = (out->audio_ring_pos + 8 - out->audio_ring_count + ai) % 8;
					int64_t p = out->audio_ring[idx].pts_usec;
					int64_t e = out->audio_ring[idx].end_usec;
					info("a3 split audio ring[%u] pts=%" PRId64 " end=%" PRId64
					     " bytes=%zu covers=%d",
					     ai, p, e, out->audio_ring[idx].bytes,
					     (p <= video_pts && video_pts < e));
				}
				for (size_t di = 0; di < out->split_buffer.num; di++) {
					struct encoder_packet *dp = &out->split_buffer.array[di];
					info("a3 split buf[%zu] type=%d pts=%" PRId64 " dts=%" PRId64,
					     di, (int)dp->type, packet_pts_usec(dp), dp->dts_usec);
				}
				if (audio_pts != INT64_MAX && audio_pts < video_pts) {
					int64_t trim = video_pts - audio_pts;
					mp4_mux_set_audio_trim(out->muxer, trim);
					mp4_mux_set_audio_delay(out->muxer,
						out->global_audio_delay_usec);
					info("a3 split trim applied=%" PRId64, trim);
				} else {
					int64_t phase_usec = audio_pts == INT64_MAX
						? 0 : audio_pts - video_pts;
					int64_t seg_delay =
						out->segment_audio_delay_usec - phase_usec;
					if (seg_delay < 0) seg_delay = 0;
					mp4_mux_set_audio_delay(out->muxer,
						seg_delay + out->global_audio_delay_usec);
				}
				out->split_file_ready = true;
			}
		} else if (should_split(out, packet)) {
			push_back_packet(out, packet);
			info("a3 split video trigger video=%" PRId64 " has_last=%d last=%" PRId64
			     " has_prev=%d prev=%" PRId64,
			     packet_pts_usec(packet), out->has_last_audio,
			     out->has_last_audio ? packet_pts_usec(&out->last_audio) : -1,
			     out->has_prev_audio,
			     out->has_prev_audio ? packet_pts_usec(&out->prev_audio) : -1);
			if (out->has_last_audio || out->has_prev_audio) {
				int64_t video_pts = packet_pts_usec(packet);
				struct encoder_packet *cand = NULL;
				int64_t cand_pts = 0;
				if (out->has_last_audio) {
					int64_t p = packet_pts_usec(&out->last_audio);
					if (p < video_pts && audio_packet_end_usec(out, &out->last_audio) > video_pts) {
						cand = &out->last_audio; cand_pts = p;
					}
				}
				if (!cand && out->has_prev_audio) {
					int64_t p = packet_pts_usec(&out->prev_audio);
					if (p < video_pts && audio_packet_end_usec(out, &out->prev_audio) > video_pts) {
						cand = &out->prev_audio; cand_pts = p;
					}
				}
				if (cand) {
					mp4_mux_set_audio_end_trim(out->muxer,
						audio_packet_end_usec(out, cand) - video_pts);
					push_back_packet(out, cand);
					info("a3 split pre-roll audio pts=%" PRId64
					     " video=%" PRId64,
					     cand_pts, video_pts);
				}
			}
			goto unlock;
		}
	}

	if (out->split_file_ready) {
		for (size_t i = 0; i < out->split_buffer.num; i++) {
			struct encoder_packet *pkt = &out->split_buffer.array[i];
			if (!submit_packet(out, pkt)) {
				a3_stop_fail(&out->stop, A3_STOP_MUX_ERROR);
				mp4_output_actual_stop(out, OBS_OUTPUT_ERROR);
				goto unlock;
			}
			/* libobs release clears the slot to zero.  If a later submit
			 * fails, actual_stop safely releases only the remaining refs. */
			obs_encoder_packet_release(pkt);
		}

		da_free(out->split_buffer);
		out->split_file_ready = false;
		if (out->stop.waiting_for_split) {
			a3_stop_split_drained(&out->stop);
			info("a3 stop rollover drained; original request remains %" PRIu64,
			     out->stop.request_usec);
		}
		os_atomic_set_bool(&out->manual_split, false);
	}

	if (!submit_packet(out, packet)) {
		a3_stop_fail(&out->stop, A3_STOP_MUX_ERROR);
		mp4_output_actual_stop(out, OBS_OUTPUT_ERROR);
		goto unlock;
	}

	if (serializer_get_pos(&out->serializer) == -1) {
		a3_stop_fail(&out->stop, A3_STOP_MUX_ERROR);
		mp4_output_actual_stop(out, OBS_OUTPUT_ERROR);
	}

unlock:
	pthread_mutex_unlock(&out->mutex);
}

static obs_properties_t *mp4_output_properties(void *unused)
{
	UNUSED_PARAMETER(unused);

	obs_properties_t *props = obs_properties_create();

	obs_properties_add_text(props, "path", obs_module_text("MP4Output.FilePath"), OBS_TEXT_DEFAULT);
	obs_properties_add_text(props, "muxer_settings", "muxer_settings", OBS_TEXT_DEFAULT);
	return props;
}

uint64_t mp4_output_total_bytes(void *data)
{
	struct mp4_output *out = data;
	return out->total_bytes;
}

struct obs_output_info mp4_output_info = {
	.id = "mp4_output",
	.flags = OBS_OUTPUT_AV | OBS_OUTPUT_ENCODED | OBS_OUTPUT_MULTI_TRACK_AV | OBS_OUTPUT_CAN_PAUSE,
	.encoded_video_codecs = "h264",
	.encoded_audio_codecs = "aac",
	.get_name = mp4_output_name,
	.create = mp4_output_create,
	.destroy = mp4_output_destroy,
	.start = mp4_output_start,
	.stop = mp4_output_stop,
	.encoded_packet = mp4_output_packet,
	.get_properties = mp4_output_properties,
	.get_total_bytes = mp4_output_total_bytes,
};

struct obs_output_info mov_output_info = {
	.id = "mov_output",
	.flags = OBS_OUTPUT_AV | OBS_OUTPUT_ENCODED | OBS_OUTPUT_MULTI_TRACK_AV | OBS_OUTPUT_CAN_PAUSE,
	.encoded_video_codecs = "h264",
	.encoded_audio_codecs = "aac",
	.get_name = mov_output_name,
	.create = mov_output_create,
	.destroy = mp4_output_destroy,
	.start = mp4_output_start,
	.stop = mp4_output_stop,
	.encoded_packet = mp4_output_packet,
	.get_properties = mp4_output_properties,
	.get_total_bytes = mp4_output_total_bytes,
};
