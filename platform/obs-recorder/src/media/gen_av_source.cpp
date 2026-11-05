// S1b in-memory test source implementation.
//
// Design notes (kept deliberately close to OBS' own libobs test inputs):
//  * output_flags = OBS_SOURCE_ASYNC_VIDEO | OBS_SOURCE_AUDIO
//    => libobs treats frame/audio pushes as asynchronous media (see
//       libobs/obs-source.h: OBS_SOURCE_ASYNC_VIDEO + OBS_SOURCE_AUDIO).
//  * video: 1920x1080 VIDEO_FORMAT_I420 frames generated in a std::thread at
//    the configured FPS and pushed with obs_source_output_video().  Every
//    frame has an absolute os_gettime_ns() timestamp on a fixed 60 Hz grid;
//    the Y plane is a static vertical gradient/checker template copied per
//    frame plus a moving 48 px bright bar (so consecutive frames differ).
//  * audio: FLOAT_PLANAR 44.1 kHz stereo 440 Hz sine at 0.2 full scale,
//    generated in 100 ms chunks on the same os_gettime_ns() clock and pushed
//    with obs_source_output_audio().
//  * nothing is ever played back or monitored; the source only writes into
//    libobs' in-memory async frame/audio buffers.
#include "gen_av_source.hpp"

// Include obs.h before obs-source.h: obs.h itself includes obs-source.h, and
// obs-source.h is NOT self-sufficient under `#pragma once` when entered first
// (obs_source_enum_proc_t is not yet declared at obs.h's function prototypes).
#include <obs.h>
#include <obs-source.h>

#include <util/platform.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstring>
#include <memory>
#include <thread>
#include <vector>

namespace a3 {
namespace {

constexpr char kSourceId[] = "a3_test_av_source";
constexpr char kSourceName[] = "A3 In-Memory Test AV Source (S1b)";
constexpr double kPi = 3.14159265358979323846;

struct AvSourceData {
    obs_source_t* source = nullptr;
    AvSourceConfig cfg;

    // I420 planes (Y: width*height, U/V: (width/2)*(height/2)).
    std::vector<uint8_t> y_plane;
    std::vector<uint8_t> y_template;
    std::vector<uint8_t> u_plane;
    std::vector<uint8_t> v_plane;

    // FLOAT_PLANAR stereo PCM, one chunk at a time.
    std::vector<float> audio_left;
    std::vector<float> audio_right;

    // YUV->RGB conversion constants for limited-range Rec.709 I420.  Without
    // these, libobs' GPU conversion path (obs-source.c update_async_texrender
    // sets color_range_min/max shader parameters from the frame) clamps every
    // sample to 0 and the recorded video is black.
    float color_matrix[16] = {};
    float color_range_min[3] = {};
    float color_range_max[3] = {};

    std::thread thread;
    std::atomic<bool> stopping{false};

    std::atomic<uint64_t> video_frames_pushed{0};
    std::atomic<uint64_t> audio_chunks_pushed{0};
    std::atomic<uint64_t> audio_frames_pushed{0};

    double phase = 0.0;
};

// Only one instance is created by the S1b host; the pointer is used to expose
// a read-only push counter snapshot and is cleared on destroy.
std::atomic<AvSourceData*> g_last_source{nullptr};

void FillYTemplate(AvSourceData* d)
{
    const uint32_t w = d->cfg.width;
    const uint32_t h = d->cfg.height;
    d->y_template.resize(static_cast<size_t>(w) * h);

    for (uint32_t y = 0; y < h; ++y) {
        // Vertical gradient inside the limited (16..235) luma range.
        const int gradient = 40 + static_cast<int>((200 - 40) * y / (h - 1));
        for (uint32_t x = 0; x < w; ++x) {
            // 64 px checker cells for a lightweight spatial pattern.
            const int checker = (((x / 64) + (y / 64)) & 1) ? 12 : -12;
            int v = gradient + checker;
            v = std::max(16, std::min(235, v));
            d->y_template[static_cast<size_t>(y) * w + x] = static_cast<uint8_t>(v);
        }
    }
}

void PushVideoFrame(AvSourceData* d, uint64_t timestamp_ns)
{
    const uint32_t w = d->cfg.width;
    const uint32_t h = d->cfg.height;
    const uint32_t bar_w = 48;
    const uint64_t frame_idx = d->video_frames_pushed.load(std::memory_order_relaxed);

    std::memcpy(d->y_plane.data(), d->y_template.data(), d->y_template.size());

    // Moving bright vertical bar; wraps around the frame edge.
    const uint32_t bar_x = static_cast<uint32_t>((frame_idx * 16) % w);
    for (uint32_t y = 0; y < h; ++y) {
        uint8_t* row = d->y_plane.data() + static_cast<size_t>(y) * w;
        const uint32_t first = std::min(bar_w, w - bar_x);
        std::memset(row + bar_x, 235, first);
        if (first < bar_w) {
            std::memset(row, 235, bar_w - first);
        }
    }

    obs_source_frame frame = {};
    frame.data[0] = d->y_plane.data();
    frame.data[1] = d->u_plane.data();
    frame.data[2] = d->v_plane.data();
    frame.linesize[0] = w;
    frame.linesize[1] = w / 2;
    frame.linesize[2] = w / 2;
    frame.width = w;
    frame.height = h;
    frame.format = VIDEO_FORMAT_I420;
    frame.full_range = false; // limited-range I420 (16..235 luma)
    frame.timestamp = timestamp_ns;
    // Same values a capture plugin passes for limited-range Rec.709 content
    // (see plugins/win-dshow/ffmpeg-decode.c or plugins/linux-v4l2/v4l2-input.c
    // calling video_format_get_parameters_for_format()).
    std::memcpy(frame.color_matrix, d->color_matrix, sizeof(frame.color_matrix));
    std::memcpy(frame.color_range_min, d->color_range_min, sizeof(frame.color_range_min));
    std::memcpy(frame.color_range_max, d->color_range_max, sizeof(frame.color_range_max));

    obs_source_output_video(d->source, &frame);
    d->video_frames_pushed.fetch_add(1, std::memory_order_relaxed);
}

void PushAudioChunk(AvSourceData* d, uint64_t timestamp_ns)
{
    const uint32_t frames = d->cfg.audio_chunk_frames;
    const double two_pi = 2.0 * kPi;
    const double step = two_pi * d->cfg.tone_frequency_hz / static_cast<double>(d->cfg.audio_rate);

    for (uint32_t i = 0; i < frames; ++i) {
        d->phase += step;
        if (d->phase > two_pi) {
            d->phase -= two_pi;
        }
        const float sample = static_cast<float>(std::sin(d->phase) * d->cfg.tone_amplitude);
        d->audio_left[i] = sample;
        d->audio_right[i] = sample;
    }

    obs_source_audio audio = {};
    audio.data[0] = reinterpret_cast<const uint8_t*>(d->audio_left.data());
    audio.data[1] = reinterpret_cast<const uint8_t*>(d->audio_right.data());
    audio.frames = frames;
    audio.speakers = SPEAKERS_STEREO;
    audio.format = AUDIO_FORMAT_FLOAT_PLANAR;
    audio.samples_per_sec = d->cfg.audio_rate;
    audio.timestamp = timestamp_ns;

    obs_source_output_audio(d->source, &audio);
    d->audio_chunks_pushed.fetch_add(1, std::memory_order_relaxed);
    d->audio_frames_pushed.fetch_add(frames, std::memory_order_relaxed);
}

// Paces video/audio on a shared os_gettime_ns() grid.  os_sleepto_ns() is used
// because libobs' own media clock is based on os_gettime_ns(); using the same
// clock keeps pushed timestamps comparable to obs->video.video_time and to the
// audio output clock (see libobs/obs-video.c tick_sources() and
// libobs/media-io/audio-io.c audio_thread()).
void SourceThread(AvSourceData* d)
{
    const uint64_t video_period_ns =
        1000000000ULL / static_cast<uint64_t>(d->cfg.fps);
    const uint64_t audio_period_ns =
        static_cast<uint64_t>(d->cfg.audio_chunk_frames) * 1000000000ULL /
        d->cfg.audio_rate;

    uint64_t next_video = os_gettime_ns();
    uint64_t next_audio = next_video;

    while (!d->stopping.load(std::memory_order_acquire)) {
        const uint64_t now = os_gettime_ns();

        // Bound catch-up: if the thread was descheduled for a long time, snap
        // the grid back to "now" instead of pushing a burst of stale frames.
        if (now > next_video + 100000000ULL) {
            next_video = now;
        }
        if (now > next_audio + 300000000ULL) {
            next_audio = now;
        }

        if (now >= next_video) {
            PushVideoFrame(d, next_video);
            next_video += video_period_ns;
        }
        if (now >= next_audio) {
            PushAudioChunk(d, next_audio);
            next_audio += audio_period_ns;
        }

        os_sleepto_ns(std::min(next_video, next_audio));
    }
}

const char* AvGetName(void*)
{
    return kSourceName;
}

void* AvCreate(obs_data_t* /*settings*/, obs_source_t* source)
{
    auto data = std::make_unique<AvSourceData>();
    data->source = source;
    data->cfg = AvSourceConfig{};

    FillYTemplate(data.get());
    data->y_plane.resize(data->y_template.size());
    data->u_plane.assign(static_cast<size_t>(data->cfg.width / 2) * (data->cfg.height / 2), 128);
    data->v_plane.assign(static_cast<size_t>(data->cfg.width / 2) * (data->cfg.height / 2), 128);
    data->audio_left.resize(data->cfg.audio_chunk_frames);
    data->audio_right.resize(data->cfg.audio_chunk_frames);

    // Limited-range Rec.709 YUV->RGB parameters, exactly as a capture plugin
    // would provide for I420 frames (video-io.h video_format_get_parameters;
    // libobs/obs-source.c update_async_texrender() feeds these to the
    // format_conversion effect: color_range_min/max clamp and color_vec0..2
    // build the conversion matrix).
    video_format_get_parameters_for_format(VIDEO_CS_709, VIDEO_RANGE_PARTIAL, VIDEO_FORMAT_I420,
                                           data->color_matrix, data->color_range_min,
                                           data->color_range_max);

    // Media is generated only after video/audio subsystems are up; the host
    // creates the source after obs_reset_video()/obs_reset_audio() so async
    // pushes are consumed from the very first frame.
    data->thread = std::thread(SourceThread, data.get());

    AvSourceData* raw = data.release();
    g_last_source.store(raw, std::memory_order_release);
    return raw;
}

void AvDestroy(void* ptr)
{
    auto* data = static_cast<AvSourceData*>(ptr);
    if (!data) {
        return;
    }

    std::printf("AV_SOURCE destroy_begin\n");
    std::fflush(stdout);
    g_last_source.store(nullptr, std::memory_order_release);
    data->stopping.store(true, std::memory_order_release);
    if (data->thread.joinable()) {
        data->thread.join();
    }
    delete data;
    std::printf("AV_SOURCE destroy_end\n");
    std::fflush(stdout);
}

const obs_source_info kAvSourceInfo = [] {
    obs_source_info info = {};
    info.id = kSourceId;
    info.type = OBS_SOURCE_TYPE_INPUT;
    info.output_flags = OBS_SOURCE_ASYNC_VIDEO | OBS_SOURCE_AUDIO;
    info.get_name = AvGetName;
    info.create = AvCreate;
    info.destroy = AvDestroy;
    return info;
}();

} // namespace

void RegisterAvTestSource()
{
    obs_register_source_s(&kAvSourceInfo, sizeof(kAvSourceInfo));
}

void StopAvTestSourceThread()
{
    AvSourceData* data = g_last_source.load(std::memory_order_acquire);
    if (!data) {
        return;
    }
    data->stopping.store(true, std::memory_order_release);
    if (data->thread.joinable()) {
        data->thread.join();
    }
    std::printf("AV_SOURCE thread_stopped video_frames=%llu audio_chunks=%llu\n",
                static_cast<unsigned long long>(data->video_frames_pushed.load()),
                static_cast<unsigned long long>(data->audio_chunks_pushed.load()));
    std::fflush(stdout);
}

AvSourceStats GetAvSourceStats()
{
    AvSourceStats stats;
    AvSourceData* data = g_last_source.load(std::memory_order_acquire);
    if (data) {
        stats.created = true;
        stats.video_frames_pushed = data->video_frames_pushed.load(std::memory_order_relaxed);
        stats.audio_chunks_pushed = data->audio_chunks_pushed.load(std::memory_order_relaxed);
        stats.audio_frames_pushed = data->audio_frames_pushed.load(std::memory_order_relaxed);
    }
    return stats;
}

} // namespace a3
