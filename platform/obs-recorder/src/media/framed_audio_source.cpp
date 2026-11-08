#include "framed_audio_source.hpp"

#include "a3_audio_protocol_c.h"
#include "framed_core.hpp"

#include <obs.h>
#include <obs-source.h>

#include <util/platform.h>

#include <algorithm>
#include <atomic>
#include <cstring>
#include <windows.h>
#include <mutex>
#include <thread>
#include <vector>

namespace a3 {
namespace {

constexpr char kSourceId[] = "a3_framed_audio";
constexpr char kSourceName[] = "A3 Framed VCMI Audio Source (A4)";

struct FramedAudioData {
    obs_source_t* source = nullptr;
    FramedAudioConfig cfg;
    std::thread thread;
    std::atomic<bool> stopping {false};
    mutable std::mutex stats_mutex;
    FramedAudioStats stats;
};

std::atomic<FramedAudioData*> g_last_source {nullptr};

FramedAudioStats CopyStats(FramedAudioData* d)
{
    std::lock_guard<std::mutex> lock(d->stats_mutex);
    return d->stats;
}

void Publish(FramedAudioData* d, const FramedAudioStats& stats)
{
    std::lock_guard<std::mutex> lock(d->stats_mutex);
    d->stats = stats;
}

void ConvertAndPush(FramedAudioData* d, const audio::ParsedPacket& packet,
                    uint64_t timestamp_ns, FramedAudioStats* stats)
{
    const uint32_t frames = static_cast<uint32_t>(packet.header.sample_count);
    if (frames == 0) return;
    std::vector<float> left(frames);
    std::vector<float> right(frames);
    const uint8_t* p = packet.payload.data();
    for (uint32_t i = 0; i < frames; ++i) {
        int16_t l = 0;
        int16_t r = 0;
        std::memcpy(&l, p, 2);
        std::memcpy(&r, p + 2, 2);
        p += 4;
        left[i] = static_cast<float>(l) / 32768.0f;
        right[i] = static_cast<float>(r) / 32768.0f;
    }
    obs_source_audio audio = {};
    audio.data[0] = reinterpret_cast<const uint8_t*>(left.data());
    audio.data[1] = reinterpret_cast<const uint8_t*>(right.data());
    audio.frames = frames;
    audio.speakers = SPEAKERS_STEREO;
    audio.format = AUDIO_FORMAT_FLOAT_PLANAR;
    audio.samples_per_sec = 44100;
    audio.timestamp = timestamp_ns;
    obs_source_output_audio(d->source, &audio);
    stats->submitted_packets++;
    stats->submitted_frames += frames;
    stats->submitted_end_sample = packet.header.sample_start + frames;
    if (stats->first_timestamp_ns == 0) stats->first_timestamp_ns = timestamp_ns;
    stats->last_timestamp_ns = timestamp_ns;
}

void ReaderThread(FramedAudioData* d)
{
    FramedAudioStats stats;
    stats.created = true;
    stats.path = d->cfg.path;
    stats.mode = d->cfg.live ? "live_capture" : "replay";
    audio::FramedParser parser;
    audio::FramedStreamValidator validator;
    audio::SampleTimeline timeline;
    bool stream_started = false;
    bool first_submitted = false;
    bool fatal_error = false;
    bool replay_finished = false;
    uint64_t last_progress_ns = os_gettime_ns();
    std::string err;
    std::vector<uint8_t> readbuf(65536);
    uint64_t offset = 0;
    HANDLE file = INVALID_HANDLE_VALUE;

    auto fail = [&](const std::string& code, const std::string& detail) {
        if (!fatal_error) {
            stats.error_code = code;
            stats.error = detail;
        }
        fatal_error = true;
        stats.stream_integrity_ok = false;
        stats.packet_integrity_ok = false;
        stats.timeline_ok = false;
        stats.validation_performed = true;
        if (code.find("crc") != std::string::npos) stats.crc_errors++;
        Publish(d, stats);
    };

    auto open_file = [&]() -> HANDLE {
        return CreateFileA(d->cfg.path.c_str(), GENERIC_READ,
                           FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    };
    const uint64_t open_deadline_ns = os_gettime_ns() + 10000000000ULL;
    while (!d->cfg.live && file == INVALID_HANDLE_VALUE &&
           !d->stopping.load(std::memory_order_acquire) &&
           os_gettime_ns() < open_deadline_ns) {
        file = open_file();
        if (file == INVALID_HANDLE_VALUE) {
            stats.error = "cannot open";
            os_sleep_ms(50);
        }
    }
    if (d->cfg.live) {
        if (!d->cfg.start_at_zero) {
            fail("attach_not_ready", "live attach requires a validated descriptor");
            return;
        }
        for (int i = 0; i < 200 && !d->stopping.load(std::memory_order_acquire); ++i) {
            file = open_file();
            if (file != INVALID_HANDLE_VALUE) break;
            os_sleep_ms(50);
        }
    }
    if (file == INVALID_HANDLE_VALUE) {
        if (stats.error.empty()) stats.error = d->stopping.load() ? "stopped" : "cannot open";
        stats.error_code = "open_failed";
        stats.stream_integrity_ok = false;
        stats.timeline_ok = false;
        Publish(d, stats);
        return;
    }
    stats.opened = true;
    Publish(d, stats);

    while (!d->stopping.load(std::memory_order_acquire)) {
        LARGE_INTEGER li;
        li.QuadPart = static_cast<LONGLONG>(offset);
        SetFilePointerEx(file, li, nullptr, FILE_BEGIN);
        DWORD got = 0;
        const BOOL read_ok = ReadFile(file, readbuf.data(),
                                      static_cast<DWORD>(readbuf.size()), &got, nullptr);
        if (!read_ok) {
            const DWORD read_error = GetLastError();
            if (read_error != ERROR_HANDLE_EOF) {
                fail("io_error", "ReadFile error " + std::to_string(read_error));
                break;
            }
            got = 0;
        }
        if (got > 0) last_progress_ns = os_gettime_ns();
        if (got > 0)
        if (got > 0) {
            offset += static_cast<uint64_t>(got);
            stats.file_bytes = offset;
            std::vector<audio::ParsedPacket> packets;
            if (!parser.feed(readbuf.data(), got, packets, &err)) {
                fail(err, err);
                break;
            }
            stats.packet_integrity_ok = true;
            stats.validation_performed = true;
            for (auto& packet : packets) {
                if (!stream_started) {
                    if (!validator.StartAtFirst(&err)) {
                        fail("stream_start", err);
                        break;
                    }
                    audio::TimelineConfig tc;
                    tc.mode = d->cfg.live ? audio::TimelineMode::LiveCapture
                                          : audio::TimelineMode::Replay;
                    tc.sample_rate = packet.header.sample_rate;
                    tc.qpc_frequency = packet.header.qpc_frequency;
                    tc.capture_epoch_qpc = packet.header.capture_epoch_qpc;
                    tc.replay_origin_ns = d->cfg.replay_origin_ns;
                    tc.replay_start_sample = packet.header.sample_start;
                    if (!d->cfg.live && d->cfg.replay_origin_ns == 0) {
                        fail("replay_origin_required", "replay_origin_ns must be provided");
                        break;
                    }
                    if (!timeline.Configure(tc, &err)) {
                        fail("timeline_config", err);
                        break;
                    }
                    stats.qpc_frequency = tc.qpc_frequency;
                    stats.capture_epoch_qpc = tc.capture_epoch_qpc;
                    stream_started = true;
                }
                if (!validator.Accept(packet, &err)) {
                    fail(err, err);
                    break;
                }
                const bool planned_ok = timeline.PlannedMatches(packet.header, &err);
                if (!planned_ok) {
                    fail("timeline_planned", err);
                    break;
                }
                uint64_t ts = 0;
                if (d->cfg.live) {
                    if (!timeline.LiveSampleTimeNs(packet.header.sample_start, &ts, &err)) {
                        fail("timeline_live", err);
                        break;
                    }
                    const uint64_t now = os_gettime_ns();
                    const uint64_t age = now > ts ? now - ts : 0;
                    if (age > d->cfg.late_budget_ns) {
                        stats.max_packet_age_ns = std::max(stats.max_packet_age_ns, age);
                        stats.late_packets++;
                        if (!first_submitted) {
                            /* live attach: skip pre-roll history, but keep the
                             * validated sample timeline unchanged. */
                            stats.preroll_skipped_packets++;
                            stats.preroll_skipped_frames += packet.header.sample_count;
                            continue;
                        }
                        fail("late_budget_exceeded",
                             "packet age " + std::to_string(age) + "ns");
                        break;
                    }
                    stats.max_packet_age_ns = std::max(stats.max_packet_age_ns, age);
                    if (age > 0) stats.late_packets++;
                } else {
                    if (!timeline.ReplaySampleTimeNs(packet.header.sample_start, &ts, &err)) {
                        fail("timeline_replay", err);
                        break;
                    }
                    while (!d->stopping.load(std::memory_order_acquire) &&
                           os_gettime_ns() < ts) {
                        os_sleepto_ns(std::min(ts, os_gettime_ns() + 2000000ULL));
                    }
                    if (d->stopping.load(std::memory_order_acquire)) break;
                }
                if (stats.submitted_packets > 0 &&
                    packet.header.sample_start != stats.submitted_end_sample) {
                    stats.submitted_gaps++;
                    fail("submitted_gap", "submitted samples are not contiguous");
                    break;
                }
                if (stats.submitted_packets == 0)
                    stats.submitted_start_sample = packet.header.sample_start;
                ConvertAndPush(d, packet, ts, &stats);
                if (packet.header.sample_count > 0) first_submitted = true;
            }
            stats.validated_packets = validator.stats().validated_packets;
            stats.validated_frames = validator.stats().validated_frames;
            stats.sequence_gaps = validator.stats().sequence_gaps;
            stats.sample_gaps = validator.stats().sample_gaps;
            stats.sample_overlaps = validator.stats().sample_overlaps;
            stats.discontinuity_packets = validator.stats().discontinuity_packets;
            stats.overflow_packets = validator.stats().overflow_packets;
            stats.producer_error_packets = validator.stats().producer_error_packets;
            stats.first_sample = validator.stats().first_sample;
            stats.validated_end_sample = validator.stats().validated_end_sample;
            stats.saw_first = validator.saw_first();
            stats.saw_last = validator.saw_last();
            stats.pending_bytes = parser.pending_bytes();
            Publish(d, stats);
            if (!stats.error.empty()) break;
        }
        if (d->cfg.live) {
            if (got == 0 && d->stopping.load(std::memory_order_acquire)) break;
            if (got == 0) {
                /* A synchronous handle can remain at its old EOF while the
                 * SDL worker appends.  Reopen to refresh the file size. */
                if (file != INVALID_HANDLE_VALUE) CloseHandle(file);
                file = open_file();
                if (file == INVALID_HANDLE_VALUE) {
                    fail("io_error", "cannot reopen live framed file");
                    break;
                }
                LARGE_INTEGER file_size;
                if (GetFileSizeEx(file, &file_size) &&
                    static_cast<uint64_t>(file_size.QuadPart) < offset) {
                    fail("file_truncated", "live framed file shrank below read offset");
                    break;
                }
                if (os_gettime_ns() - last_progress_ns > 5000000000ULL) {
                    fail("capture_stalled", "live framed file made no progress for 5s");
                    break;
                }
                os_sleep_ms(10);
            }
        } else {
            if (got == 0) {
                replay_finished = true;
                break;
            }
        }
    }
    if (file != INVALID_HANDLE_VALUE) CloseHandle(file);

    if (!stream_started) {
        if (stats.error.empty()) {
            stats.error_code = d->cfg.live ? "capture_not_ready" : "empty_stream";
            stats.error = stats.error_code;
        }
        Publish(d, stats);
        return;
    }
    if (!parser.finish(&err)) {
        fail("truncated", err);
    }
    bool complete = false;
    const bool host_stopped = d->stopping.load(std::memory_order_acquire);
    if (!validator.Finish(!d->cfg.live && !host_stopped, &complete, &err)) {
        fail(err, err);
    } else {
        stats.stream_integrity_ok = validator.integrity_ok() && !fatal_error;
        stats.packet_integrity_ok = !fatal_error;
        stats.validation_performed = true;
        stats.saw_first = validator.saw_first();
        stats.saw_last = validator.saw_last();
        stats.validated_end_sample = validator.stats().validated_end_sample;
        if (!fatal_error) {
            stats.error.clear();
            stats.error_code.clear();
        }
    }
    stats.timeline_ok = !fatal_error;
    stats.submitted_end_sample = stats.submitted_packets
        ? stats.submitted_end_sample : 0;
    if (fatal_error) {
        stats.stream_integrity_ok = false;
        stats.packet_integrity_ok = false;
        stats.timeline_ok = false;
    }
    if (!d->cfg.live && replay_finished && !stats.saw_last) {
        stats.stream_integrity_ok = false;
        stats.error_code = "missing_last";
        stats.error = "replay ended without LAST";
    }
    Publish(d, stats);
}

const char* FramedGetName(void*)
{
    return kSourceName;
}

void FramedGetDefaults(obs_data_t* defaults)
{
    obs_data_set_default_string(defaults, "path", "");
    obs_data_set_default_bool(defaults, "live", false);
    obs_data_set_default_bool(defaults, "start_at_zero", true);
    obs_data_set_default_int(defaults, "replay_origin_ns", 0);
    obs_data_set_default_int(defaults, "late_budget_ns", 500000000);
}

void* FramedCreate(obs_data_t* settings, obs_source_t* source)
{
    auto* d = new FramedAudioData();
    d->source = source;
    const char* path = obs_data_get_string(settings, "path");
    d->cfg.path = path ? path : "";
    d->cfg.live = obs_data_get_bool(settings, "live");
    d->cfg.start_at_zero = obs_data_get_bool(settings, "start_at_zero");
    d->cfg.replay_origin_ns =
        static_cast<uint64_t>(obs_data_get_int(settings, "replay_origin_ns"));
    d->cfg.late_budget_ns =
        static_cast<uint64_t>(obs_data_get_int(settings, "late_budget_ns"));
    d->stats.created = true;
    d->stats.path = d->cfg.path;
    g_last_source.store(d, std::memory_order_release);
    d->thread = std::thread(ReaderThread, d);
    return d;
}

void FramedDestroy(void* ptr)
{
    auto* d = static_cast<FramedAudioData*>(ptr);
    if (!d) return;
    g_last_source.store(nullptr, std::memory_order_release);
    d->stopping.store(true, std::memory_order_release);
    if (d->thread.joinable()) d->thread.join();
    delete d;
}

const obs_source_info kFramedAudioInfo = [] {
    obs_source_info info = {};
    info.id = kSourceId;
    info.type = OBS_SOURCE_TYPE_INPUT;
    info.output_flags = OBS_SOURCE_AUDIO | OBS_SOURCE_ASYNC;
    info.get_name = FramedGetName;
    info.create = FramedCreate;
    info.destroy = FramedDestroy;
    info.get_defaults = FramedGetDefaults;
    return info;
}();

}  // namespace

void RegisterFramedAudioSource()
{
    obs_register_source_s(&kFramedAudioInfo, sizeof(kFramedAudioInfo));
}

void StopFramedAudioSourceThread()
{
    FramedAudioData* d = g_last_source.load(std::memory_order_acquire);
    if (!d) return;
    d->stopping.store(true, std::memory_order_release);
    if (d->thread.joinable()) d->thread.join();
}

FramedAudioStats GetFramedAudioStats()
{
    FramedAudioData* d = g_last_source.load(std::memory_order_acquire);
    if (!d) return {};
    FramedAudioStats stats = CopyStats(d);
    stats.created = true;
    return stats;
}

}  // namespace a3
