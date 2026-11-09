#include "framed_audio_source.hpp"

#include "a3_audio_protocol_c.h"
#include "framed_core.hpp"
#include "live_audio_policy.hpp"

#include <obs.h>
#include <obs-source.h>

#include <util/platform.h>
#include <util/base.h>

#include <algorithm>
#include <atomic>
#include <cstring>
#include <cstdio>
#include <windows.h>
#include <avrt.h>
#include <mutex>
#include <thread>
#include <cstdlib>
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

void AccumulateStage(FramedStageTiming& timing, uint64_t started, uint64_t now)
{
    timing.last_ns = now >= started ? now - started : 0;
    ++timing.calls;
    timing.max_ns = std::max(timing.max_ns, timing.last_ns);
    if (timing.last_ns >= 50000000ULL) ++timing.slow_calls;
}

void LogSlowStage(const FramedStageTiming& timing, uint64_t now, const char* stage,
                  const FramedAudioStats& stats)
{
    if (timing.last_ns < 50000000ULL) return;
    blog(LOG_WARNING, "A3_FRAMED_SLOW stage=%s duration_ns=%llu now_ns=%llu "
         "context_seq=%llu context_sample=%llu packet_offset=%llu read_offset=%llu",
         stage, static_cast<unsigned long long>(timing.last_ns),
         static_cast<unsigned long long>(now),
         static_cast<unsigned long long>(stats.last_packet_sequence),
         static_cast<unsigned long long>(stats.last_packet_sample),
         static_cast<unsigned long long>(stats.last_packet_offset),
         static_cast<unsigned long long>(stats.file_bytes));
    std::fflush(stdout);
    std::fflush(stderr);
}

void ObserveStage(FramedStageTiming& timing, uint64_t started, const char* stage,
                  const FramedAudioStats& stats)
{
    const uint64_t now = os_gettime_ns();
    AccumulateStage(timing, started, now);
    LogSlowStage(timing, now, stage, stats);
}

void Publish(FramedAudioData* d, FramedAudioStats& stats)
{
    const uint64_t started = os_gettime_ns();
    uint64_t acquired = 0;
    {
        std::lock_guard<std::mutex> lock(d->stats_mutex);
        acquired = os_gettime_ns();
        AccumulateStage(stats.publish_lock_timing, started, acquired);
        d->stats = stats;
    }
    // Never call the OBS logger while holding our statistics mutex.
    LogSlowStage(stats.publish_lock_timing, acquired, "publish_lock_wait", stats);
}

std::string StageSummary(const char* name, const FramedStageTiming& timing)
{
    return std::string(name) + "={calls=" + std::to_string(timing.calls) +
        ",last_ns=" + std::to_string(timing.last_ns) +
        ",max_ns=" + std::to_string(timing.max_ns) +
        ",slow=" + std::to_string(timing.slow_calls) + "} ";
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
    const uint64_t submit_started = os_gettime_ns();
    obs_source_output_audio(d->source, &audio);
    ObserveStage(stats->submit_timing, submit_started, "obs_source_output_audio", *stats);
    stats->submitted_packets++;
    stats->submitted_frames += frames;
    stats->submitted_end_sample = packet.header.sample_start + frames;
    if (stats->first_timestamp_ns == 0) stats->first_timestamp_ns = timestamp_ns;
    stats->last_timestamp_ns = timestamp_ns;
    stats->submitted_end_timestamp_ns = timestamp_ns +
        static_cast<uint64_t>(frames) * 1000000000ULL / packet.header.sample_rate;
}

void FileReaderThread(FramedAudioData* d)
{
    FramedAudioStats stats;
    stats.created = true;
    stats.path = d->cfg.path;
    stats.mode = d->cfg.live ? "live_capture" : "replay";
    stats.transport = "file";
    audio::FramedParser parser;
    audio::FramedStreamValidator validator;
    audio::SampleTimeline timeline;
    bool stream_started = false;
    bool first_submitted = false;
    bool fatal_error = false;
    bool replay_finished = false;
    // Test state belongs to this source, never to a subsequent recording.
    bool test_late_pending = false;
    bool test_delayed = false;
    uint64_t last_progress_ns = os_gettime_ns();
    uint64_t last_loop_start_ns = 0;
    std::string err;
    std::vector<uint8_t> readbuf(65536);
    uint64_t offset = 0;
    bool packet_context_valid = false;
    HANDLE file = INVALID_HANDLE_VALUE;

    auto fail = [&](const std::string& code, const std::string& detail) {
        if (!fatal_error) {
            stats.error_code = code;
            stats.error = detail;
            stats.fatal_latched = true;
            stats.fatal_has_packet = packet_context_valid;
            stats.fatal_sequence = packet_context_valid ? stats.last_packet_sequence : 0;
            stats.fatal_sample = packet_context_valid ? stats.last_packet_sample : 0;
            stats.fatal_now_ns = os_gettime_ns();
            stats.fatal_pts_ns = packet_context_valid ? stats.last_packet_pts_ns : 0;
            stats.fatal_age_ns = stats.fatal_pts_ns && stats.fatal_now_ns > stats.fatal_pts_ns
                ? stats.fatal_now_ns - stats.fatal_pts_ns : 0;
            stats.fatal_file_offset = packet_context_valid ? stats.last_packet_offset : parser.next_offset();
            stats.fatal_read_offset = offset;
            const std::string stages = StageSummary("open_reopen", stats.open_timing) +
                StageSummary("seek_read", stats.read_timing) + StageSummary("parse_crc", stats.parse_timing) +
                StageSummary("obs_submit", stats.submit_timing) + StageSummary("loop_gap", stats.loop_timing) +
                StageSummary("eof_sleep", stats.idle_timing) + StageSummary("publish_lock", stats.publish_lock_timing);
            blog(LOG_ERROR, "A3_FRAMED_FATAL code='%s' detail='%s' has_packet=%u "
                 "seq=%llu sample=%llu now_ns=%llu pts_ns=%llu age_ns=%llu "
                 "packet_offset=%llu read_offset=%llu eof_reopens=%llu stages='%s'",
                 code.c_str(), detail.c_str(), packet_context_valid ? 1u : 0u,
                 static_cast<unsigned long long>(stats.fatal_sequence),
                 static_cast<unsigned long long>(stats.fatal_sample),
                 static_cast<unsigned long long>(stats.fatal_now_ns),
                 static_cast<unsigned long long>(stats.fatal_pts_ns),
                 static_cast<unsigned long long>(stats.fatal_age_ns),
                 static_cast<unsigned long long>(stats.fatal_file_offset),
                 static_cast<unsigned long long>(stats.fatal_read_offset),
                 static_cast<unsigned long long>(stats.eof_reopens), stages.c_str());
            // The watchdog may terminate this host before final report generation.
            // Flush the first fatal event now; this is not a power-loss durability claim.
            std::fflush(stdout);
            std::fflush(stderr);
        }
        fatal_error = true;
        stats.stream_integrity_ok = false;
        stats.packet_integrity_ok = false;
        stats.timeline_ok = false;
        stats.validation_performed = true;
        if (code.find("crc") != std::string::npos) stats.crc_errors++;
        Publish(d, stats);
    };

    auto open_file = [&](bool reopen = false) -> HANDLE {
        const uint64_t started = os_gettime_ns();
        if (reopen && file != INVALID_HANDLE_VALUE) CloseHandle(file);
        HANDLE opened = CreateFileA(d->cfg.path.c_str(), GENERIC_READ,
                           FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        ObserveStage(stats.open_timing, started, reopen ? "close_reopen" : "open", stats);
        return opened;
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
        fail("open_failed", stats.error);
        return;
    }
    stats.opened = true;
    Publish(d, stats);

    while (!d->stopping.load(std::memory_order_acquire)) {
        if (last_loop_start_ns)
            ObserveStage(stats.loop_timing, last_loop_start_ns, "reader_loop_gap", stats);
        last_loop_start_ns = os_gettime_ns();
        packet_context_valid = false;
        const uint64_t read_started = os_gettime_ns();
        LARGE_INTEGER li;
        li.QuadPart = static_cast<LONGLONG>(offset);
        SetFilePointerEx(file, li, nullptr, FILE_BEGIN);
        DWORD got = 0;
        const BOOL read_ok = ReadFile(file, readbuf.data(),
                                      static_cast<DWORD>(readbuf.size()), &got, nullptr);
        const DWORD read_error = read_ok ? ERROR_SUCCESS : GetLastError();
        ObserveStage(stats.read_timing, read_started, "seek_read", stats);
        if (!read_ok) {
            if (read_error != ERROR_HANDLE_EOF) {
                fail("io_error", "ReadFile error " + std::to_string(read_error));
                break;
            }
            got = 0;
        }
        if (got > 0) last_progress_ns = os_gettime_ns();
        if (got > 0) {
            offset += static_cast<uint64_t>(got);
            stats.file_bytes = offset;
            std::vector<audio::ParsedPacket> packets;
            const uint64_t parse_started = os_gettime_ns();
            const bool parsed = parser.feed(readbuf.data(), got, packets, &err);
            ObserveStage(stats.parse_timing, parse_started, "parse_crc", stats);
            if (!parsed) {
                fail(err, err);
                break;
            }
            stats.packet_integrity_ok = true;
            stats.validation_performed = true;
            for (auto& packet : packets) {
                packet_context_valid = true;
                stats.last_packet_sequence = packet.header.sequence;
                stats.last_packet_sample = packet.header.sample_start;
                stats.last_packet_offset = packet.stream_offset;
                stats.last_packet_pts_ns = 0;
                stats.last_packet_age_ns = 0;
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
                if (test_late_pending) {
                    stats.late_packets++;
                    fail("late_budget_exceeded", "injected after first submitted packet");
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
                    stats.last_packet_pts_ns = ts;
                    stats.last_packet_age_ns = age;
                    if (first_submitted)
                        stats.steady_max_packet_age_ns = std::max(stats.steady_max_packet_age_ns, age);
                    const auto delivery = audio::ClassifyLivePacket(
                        age, d->cfg.late_budget_ns, first_submitted);
                    if (delivery != audio::LivePacketDisposition::Submit) {
                        stats.max_packet_age_ns = std::max(stats.max_packet_age_ns, age);
                        stats.late_packets++;
                        if (delivery == audio::LivePacketDisposition::SkipPreroll) {
                            /* live attach: skip pre-roll history, but keep the
                             * validated sample timeline unchanged. */
                            stats.preroll_skipped_packets++;
                            stats.preroll_skipped_frames += packet.header.sample_count;
                            stats.preroll_max_packet_age_ns = std::max(stats.preroll_max_packet_age_ns, age);
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
                    stats.last_packet_pts_ns = ts;
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
                if (!first_submitted && packet.header.sample_count > 0)
                    stats.first_submitted_age_ns = stats.last_packet_age_ns;
                ConvertAndPush(d, packet, ts, &stats);
                if (packet.header.sample_count > 0) first_submitted = true;
                // Test-only stall injection: the next packet is then observed
                // as older than late_budget_ns.  Never enabled by default.
                if (first_submitted && !test_delayed) {
                    const char* delay_text = std::getenv("A3_FRAMED_TEST_DELAY_AFTER_FIRST_MS");
                    if (delay_text && *delay_text) {
                        test_delayed = true;
                        if (std::getenv("A3_FRAMED_TEST_FORCE_LATE") != nullptr)
                            test_late_pending = true;
                        std::this_thread::sleep_for(
                            std::chrono::milliseconds(std::atoi(delay_text)));
                    }
                }
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
                ++stats.eof_reopens;
                file = open_file(true);
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
                if (audio::CaptureStalled(os_gettime_ns(), last_progress_ns)) {
                    fail("capture_stalled", "live framed file made no progress for 5s");
                    break;
                }
                const uint64_t idle_started = os_gettime_ns();
                os_sleep_ms(10);
                ObserveStage(stats.idle_timing, idle_started, "eof_sleep", stats);
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
        if (!fatal_error)
            fail(d->cfg.live ? "capture_not_ready" : "empty_stream", "no complete validated packet");
        Publish(d, stats);
        return;
    }
    const bool host_stopped = d->stopping.load(std::memory_order_acquire);
    // A recording fence consumes a validated prefix of a continuing capture.
    // Cancelling this reader is not EOF: its read-ahead buffer may legitimately
    // contain the beginning of a packet after the retained media interval.
    // CRC/stream errors discovered before cancellation remain latched below.
    if (!host_stopped && !parser.finish(&err)) {
        fail("truncated", err);
    }
    bool complete = false;
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
    if (!d->cfg.live && replay_finished && !stats.saw_last && !fatal_error)
        fail("missing_last", "replay ended without LAST");
    Publish(d, stats);
}

// The archive is an identity/evidence channel, never the live delivery clock.
// Snapshot readers own no slot locks: killing a host cannot block the producer.
void SharedMemoryReaderThread(FramedAudioData* d)
{
    FramedAudioStats stats;
    stats.created = true;
    stats.path = d->cfg.path;
    stats.mode = "live_capture";
    stats.transport = "shared_memory";
    struct Resources {
        HANDLE identity = INVALID_HANDLE_VALUE;
        HANDLE mapping = nullptr;
        HANDLE owner = nullptr;
        HANDLE mmcss = nullptr;
        A3AudioLiveShared* shared = nullptr;
        ~Resources() {
            if (mmcss) AvRevertMmThreadCharacteristics(mmcss);
            if (shared) UnmapViewOfFile(shared);
            if (mapping) CloseHandle(mapping);
            if (owner) CloseHandle(owner);
            if (identity != INVALID_HANDLE_VALUE) CloseHandle(identity);
        }
    } r;
    bool fatal = false;
    bool attached = false;
    bool context = false;
    bool test_delayed = false;
    audio::FramedStreamValidator validator;
    audio::SampleTimeline timeline;
    std::string err;
    auto fail = [&](const std::string& code, const std::string& detail) {
        if (!fatal) {
            stats.error_code = code;
            stats.error = detail;
            stats.fatal_latched = true;
            stats.fatal_has_packet = context;
            stats.fatal_sequence = stats.last_packet_sequence;
            stats.fatal_sample = stats.last_packet_sample;
            stats.fatal_now_ns = os_gettime_ns();
            stats.fatal_pts_ns = stats.last_packet_pts_ns;
            stats.fatal_age_ns = stats.fatal_now_ns > stats.fatal_pts_ns && stats.fatal_pts_ns
                ? stats.fatal_now_ns - stats.fatal_pts_ns : 0;
            stats.fatal_file_offset = stats.last_packet_offset;
            stats.fatal_read_offset = stats.file_bytes;
            const std::string stages = StageSummary("open", stats.open_timing) +
                StageSummary("snapshot", stats.read_timing) + StageSummary("parse_crc", stats.parse_timing) +
                StageSummary("obs_submit", stats.submit_timing) + StageSummary("loop_gap", stats.loop_timing) +
                StageSummary("idle", stats.idle_timing) + StageSummary("publish_lock", stats.publish_lock_timing);
            blog(LOG_ERROR, "A3_FRAMED_FATAL transport=shared_memory code='%s' detail='%s' "
                 "seq=%llu sample=%llu now_ns=%llu pts_ns=%llu age_ns=%llu "
                 "logical_offset=%llu archive_end=%llu producer_fatal=%llu stages='%s'",
                 code.c_str(), detail.c_str(), static_cast<unsigned long long>(stats.fatal_sequence),
                 static_cast<unsigned long long>(stats.fatal_sample),
                 static_cast<unsigned long long>(stats.fatal_now_ns),
                 static_cast<unsigned long long>(stats.fatal_pts_ns),
                 static_cast<unsigned long long>(stats.fatal_age_ns),
                 static_cast<unsigned long long>(stats.fatal_file_offset),
                 static_cast<unsigned long long>(stats.archive_committed_end_sample),
                 static_cast<unsigned long long>(stats.producer_fatal_code), stages.c_str());
            std::fflush(stdout);
            std::fflush(stderr);
        }
        fatal = true;
        stats.packet_integrity_ok = stats.stream_integrity_ok = stats.timeline_ok = false;
        stats.validation_performed = true;
        if (code.find("crc") != std::string::npos) ++stats.crc_errors;
        Publish(d, stats);
    };
    if (!d->cfg.live || !d->cfg.expected_producer_pid) {
        fail("live_owner_required", "shared_memory requires live capture and expected producer PID");
        return;
    }
    const uint64_t open_started = os_gettime_ns();
    r.identity = CreateFileA(d->cfg.path.c_str(), GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL, nullptr);
    audio::PacketHeader identity {};
    DWORD got = 0;
    if (r.identity == INVALID_HANDLE_VALUE ||
        !ReadFile(r.identity, &identity, sizeof(identity), &got, nullptr) || got != sizeof(identity)) {
        fail("live_identity_unavailable", "cannot read the first archive header");
        return;
    }
    stats.file_bytes = got; // Only this identity header is read from disk.
    CloseHandle(r.identity);
    r.identity = INVALID_HANDLE_VALUE;
    if (!audio::ValidateHeader(identity, &err) || !(identity.flags & audio::kFlagFirst) ||
        identity.sequence != 0 || identity.sample_start != 0) {
        fail("live_identity_invalid", err.empty() ? "archive must begin with FIRST sequence zero" : err);
        return;
    }
    char map_name[96];
    std::snprintf(map_name, sizeof(map_name), A3_AUDIO_LIVE_MAP_FORMAT,
        static_cast<unsigned long long>(identity.capture_id), identity.generation);
    stats.map_name = map_name;
    // Interlocked loads need a writable view, but the reader never acquires a
    // slot/producer lock and never changes a counter or stamp.
    r.mapping = OpenFileMappingA(FILE_MAP_READ | FILE_MAP_WRITE, FALSE, map_name);
    if (!r.mapping) { fail("live_map_missing", "shared audio mapping is unavailable"); return; }
    r.shared = static_cast<A3AudioLiveShared*>(MapViewOfFile(r.mapping,
        FILE_MAP_READ | FILE_MAP_WRITE, 0, 0, sizeof(A3AudioLiveShared)));
    if (!r.shared || InterlockedCompareExchange(&r.shared->header.initialized, 0, 0) != 1) {
        fail("live_map_uninitialized", "shared audio mapping has no committed identity"); return;
    }
    r.owner = OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, FALSE,
                          d->cfg.expected_producer_pid);
    FILETIME created {}, exited {}, kernel {}, user {};
    const bool owner_alive = r.owner && WaitForSingleObject(r.owner, 0) == WAIT_TIMEOUT;
    uint64_t creation = 0;
    if (r.owner && GetProcessTimes(r.owner, &created, &exited, &kernel, &user))
        creation = (static_cast<uint64_t>(created.dwHighDateTime) << 32) | created.dwLowDateTime;
    const char* identity_error = audio::ValidateLiveIdentity(r.shared->header, identity,
        d->cfg.expected_producer_pid, creation, owner_alive);
    if (identity_error) { fail(identity_error, "map/capture/process identity mismatch"); return; }
    stats.producer_pid = r.shared->header.producer_pid;
    stats.producer_creation_filetime = creation;
    stats.capture_id = identity.capture_id;
    stats.generation = identity.generation;
    stats.qpc_frequency = identity.qpc_frequency;
    stats.capture_epoch_qpc = identity.capture_epoch_qpc;
    audio::TimelineConfig tc;
    tc.mode = audio::TimelineMode::LiveCapture;
    tc.sample_rate = identity.sample_rate;
    tc.qpc_frequency = identity.qpc_frequency;
    tc.capture_epoch_qpc = identity.capture_epoch_qpc;
    if (!timeline.Configure(tc, &err)) { fail("timeline_config", err); return; }
    DWORD task_index = 0;
    r.mmcss = AvSetMmThreadCharacteristicsW(L"Audio", &task_index);
    if (!r.mmcss) { fail("live_mmcss_failed", "cannot register realtime reader with MMCSS Audio"); return; }
    stats.mmcss_registered = true;
    ObserveStage(stats.open_timing, open_started, "live_open", stats);
    stats.opened = true;
    Publish(d, stats);

    auto refresh_archive = [&]() -> bool {
        const LONG64 committed = InterlockedCompareExchange64(
            &r.shared->header.archive_committed_end_sample, 0, 0);
        stats.archive_committed_end_sample = committed > 0 ? static_cast<uint64_t>(committed) : 0;
        stats.archive_closed = InterlockedCompareExchange(&r.shared->header.archive_closed, 0, 0) != 0;
        stats.producer_fatal_code = InterlockedCompareExchange(&r.shared->header.fatal_code, 0, 0);
        stats.archive_lag_frames = stats.submitted_end_sample > stats.archive_committed_end_sample
            ? stats.submitted_end_sample - stats.archive_committed_end_sample : 0;
        stats.archive_lag_max_frames = std::max(stats.archive_lag_max_frames, stats.archive_lag_frames);
        if (stats.producer_fatal_code ||
            InterlockedCompareExchange(&r.shared->header.archive_failed, 0, 0)) {
            fail("live_producer_fatal", "producer/archive fatal code " + std::to_string(stats.producer_fatal_code));
            return false;
        }
        return true;
    };
    uint64_t expected = 0;
    uint64_t logical_offset = 0;
    uint64_t last_progress = os_gettime_ns();
    uint64_t last_loop = 0;
    uint64_t retry_started = 0;
    std::vector<uint8_t> snapshot(A3_AUDIO_LIVE_PACKET_BYTES);
    while (!d->stopping.load(std::memory_order_acquire) && !fatal && !stats.saw_last) {
        const uint64_t now = os_gettime_ns();
        if (last_loop) ObserveStage(stats.loop_timing, last_loop, "live_loop_gap", stats);
        last_loop = now;
        if (!refresh_archive()) break;
        const bool closed = InterlockedCompareExchange(&r.shared->header.closed, 0, 0) != 0;
        if (WaitForSingleObject(r.owner, 0) != WAIT_TIMEOUT && !closed) {
            fail("live_owner_exited", "capture producer exited without live LAST"); break;
        }
        const uint64_t published = static_cast<uint64_t>(a3_audio_live_load_sequence(r.shared));
        if (!attached) expected = std::max(expected, audio::LiveAttachSequence(published));
        if (expected >= published) {
            if (closed) { fail("live_last_missing", "closed mapping has no validated LAST"); break; }
            if (audio::CaptureStalled(now, last_progress)) {
                fail("capture_stalled", "shared capture has made no progress for 5s"); break;
            }
            const uint64_t idle = os_gettime_ns();
            Sleep(2);
            ObserveStage(stats.idle_timing, idle, "live_idle", stats);
            continue;
        }
        const uint64_t read_started = os_gettime_ns();
        uint32_t bytes = 0;
        const auto decision = audio::ReadLiveSnapshot(r.shared, expected,
            snapshot.data(), snapshot.size(), &bytes);
        ObserveStage(stats.read_timing, read_started, "live_snapshot", stats);
        if (decision == audio::LiveSnapshotDecision::Overwritten) {
            fail("live_ring_overwrite", "expected sequence was overwritten: " + std::to_string(expected)); break;
        }
        if (decision == audio::LiveSnapshotDecision::Retry) {
            ++stats.snapshot_retries;
            if (!retry_started) retry_started = now;
            if (now - retry_started > d->cfg.late_budget_ns) {
                fail("live_snapshot_stalled", "no stable expected packet within late budget"); break;
            }
            Sleep(1);
            continue;
        }
        retry_started = 0;
        if (bytes < audio::kHeaderBytes || bytes > snapshot.size()) {
            fail("live_packet_length", "stable slot has invalid packet length"); break;
        }
        audio::ParsedPacket packet;
        std::memcpy(&packet.header, snapshot.data(), sizeof(packet.header));
        packet.payload.assign(snapshot.begin() + audio::kHeaderBytes, snapshot.begin() + bytes);
        packet.stream_offset = logical_offset;
        context = true;
        stats.last_packet_sequence = packet.header.sequence;
        stats.last_packet_sample = packet.header.sample_start;
        stats.last_packet_offset = logical_offset;
        stats.last_packet_pts_ns = 0;
        const uint64_t parse_started = os_gettime_ns();
        const bool valid = audio::ValidatePacket(packet.header, packet.payload.data(), packet.payload.size(), &err);
        ObserveStage(stats.parse_timing, parse_started, "live_parse_crc", stats);
        if (!valid) { fail(err, err); break; }
        if (packet.header.sequence != expected || packet.header.capture_id != identity.capture_id ||
            packet.header.generation != identity.generation) {
            fail("live_packet_identity", "snapshot sequence/capture differs from expected identity"); break;
        }
        uint64_t ts = 0;
        if (!timeline.PlannedMatches(packet.header, &err) ||
            !timeline.LiveSampleTimeNs(packet.header.sample_start, &ts, &err)) {
            fail("timeline_live", err); break;
        }
        const uint64_t delivery_now = os_gettime_ns();
        const uint64_t age = delivery_now > ts ? delivery_now - ts : 0;
        stats.last_packet_pts_ns = ts;
        stats.last_packet_age_ns = age;
        if (!attached && age > audio::kLiveAttachMaxAgeNs) {
            // Pre-attachment history is explicitly excluded; no packet may be
            // skipped after StartAttached establishes the retained interval.
            ++expected;
            ++stats.preroll_skipped_packets;
            stats.preroll_skipped_frames += packet.header.sample_count;
            stats.preroll_max_packet_age_ns = std::max(stats.preroll_max_packet_age_ns, age);
            continue;
        }
        if (!attached) {
            audio::AttachDescriptor desc;
            desc.capture_id = identity.capture_id;
            desc.generation = identity.generation;
            desc.sequence = packet.header.sequence;
            desc.sample_start = packet.header.sample_start;
            desc.stream_offset = logical_offset;
            if (!validator.StartAttached(desc, &err)) { fail("stream_start", err); break; }
            attached = true;
            stats.preroll_skipped_packets = packet.header.sequence;
            stats.preroll_skipped_frames = packet.header.sample_start;
            stats.submitted_start_sample = packet.header.sample_start;
            stats.first_submitted_age_ns = age;
        } else {
            stats.steady_max_packet_age_ns = std::max(stats.steady_max_packet_age_ns, age);
            if (age > d->cfg.late_budget_ns) {
                fail("late_budget_exceeded", "shared packet age " + std::to_string(age) + "ns"); break;
            }
        }
        if (!validator.Accept(packet, &err)) { fail(err, err); break; }
        if (!validator.integrity_ok()) { fail("producer_error", "packet reports producer discontinuity/error"); break; }
        stats.max_packet_age_ns = std::max(stats.max_packet_age_ns, age);
        if (age) ++stats.late_packets;
        if (stats.submitted_packets && packet.header.sample_start != stats.submitted_end_sample) {
            ++stats.submitted_gaps;
            fail("submitted_gap", "shared submitted samples are not contiguous"); break;
        }
        ConvertAndPush(d, packet, ts, &stats);
        const auto& vs = validator.stats();
        stats.validated_packets = vs.validated_packets;
        stats.validated_frames = vs.validated_frames;
        stats.validated_end_sample = vs.validated_end_sample;
        stats.sequence_gaps = vs.sequence_gaps;
        stats.sample_gaps = vs.sample_gaps;
        stats.sample_overlaps = vs.sample_overlaps;
        stats.discontinuity_packets = vs.discontinuity_packets;
        stats.overflow_packets = vs.overflow_packets;
        stats.producer_error_packets = vs.producer_error_packets;
        stats.first_sample = vs.first_sample;
        stats.saw_first = validator.saw_first();
        stats.saw_last = validator.saw_last();
        stats.validation_performed = true;
        logical_offset += bytes;
        ++expected;
        last_progress = os_gettime_ns();
        if (!refresh_archive()) break;
        Publish(d, stats);
        if (!test_delayed && stats.submitted_packets) {
            test_delayed = true;
            const char* delay = std::getenv("A3_FRAMED_TEST_DELAY_AFTER_FIRST_MS");
            if (delay && *delay) std::this_thread::sleep_for(std::chrono::milliseconds(std::atoi(delay)));
        }
    }
    if (!attached && !fatal) fail("capture_not_ready", "no fresh shared-memory packet was attached");
    if (attached) {
        bool complete = false;
        if (!validator.Finish(false, &complete, &err) && !fatal) fail(err, err);
    }
    if (!fatal) {
        const uint64_t started = os_gettime_ns();
        for (;;) {
            if (!refresh_archive()) break;
            stats.archive_commit_ok = audio::ArchiveCommitted(stats.archive_committed_end_sample,
                stats.submitted_end_sample, stats.saw_last, stats.archive_closed);
            stats.archive_drain_wait_ns = os_gettime_ns() - started;
            if (stats.archive_commit_ok) break;
            if (stats.archive_drain_wait_ns >= audio::kArchiveDrainNs ||
                WaitForSingleObject(r.owner, 0) != WAIT_TIMEOUT) {
                fail("archive_not_committed", "archive did not commit retained live samples/LAST"); break;
            }
            Sleep(2);
        }
    }
    stats.stream_integrity_ok = !fatal && validator.integrity_ok();
    stats.packet_integrity_ok = stats.timeline_ok = !fatal;
    Publish(d, stats);
}

void ReaderThread(FramedAudioData* d)
{
    if (d->cfg.live && d->cfg.transport == "shared_memory") SharedMemoryReaderThread(d);
    else FileReaderThread(d);
}

const char* FramedGetName(void*)
{
    return kSourceName;
}

void FramedGetDefaults(obs_data_t* defaults)
{
    obs_data_set_default_string(defaults, "path", "");
    obs_data_set_default_bool(defaults, "live", false);
    obs_data_set_default_string(defaults, "transport", "shared_memory");
    obs_data_set_default_int(defaults, "expected_producer_pid", 0);
    obs_data_set_default_bool(defaults, "start_at_zero", true);
    obs_data_set_default_int(defaults, "replay_origin_ns", 0);
    obs_data_set_default_int(defaults, "late_budget_ns", audio::kDefaultLateBudgetNs);
}

void* FramedCreate(obs_data_t* settings, obs_source_t* source)
{
    auto* d = new FramedAudioData();
    d->source = source;
    const char* path = obs_data_get_string(settings, "path");
    d->cfg.path = path ? path : "";
    d->cfg.live = obs_data_get_bool(settings, "live");
    d->cfg.transport = obs_data_get_string(settings, "transport");
    d->cfg.expected_producer_pid = static_cast<uint32_t>(obs_data_get_int(settings, "expected_producer_pid"));
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
