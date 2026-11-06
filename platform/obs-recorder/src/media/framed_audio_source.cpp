#include "framed_audio_source.hpp"

#include "a3_audio_protocol_c.h"

#include <windows.h>

#include <obs.h>
#include <obs-source.h>

#include <util/platform.h>

#include <algorithm>
#include <atomic>
#include <cstring>
#include <fstream>
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

bool ReadWholeFile(const std::string& path, std::vector<uint8_t>& out, std::string& error)
{
    std::ifstream f(path, std::ios::binary);
    if (!f) {
        error = "cannot open";
        return false;
    }
    f.seekg(0, std::ios::end);
    const std::streamoff size = f.tellg();
    f.seekg(0, std::ios::beg);
    if (size < 0) {
        error = "tellg failed";
        return false;
    }
    out.resize(static_cast<size_t>(size));
    if (size > 0) {
        f.read(reinterpret_cast<char*>(out.data()), size);
        if (!f) {
            error = "read failed";
            return false;
        }
    }
    return true;
}

void ReaderThread(FramedAudioData* d)
{
    FramedAudioStats stats;
    stats.created = true;
    stats.path = d->cfg.path;
    std::vector<uint8_t> bytes;
    while (!d->stopping.load(std::memory_order_acquire)) {
        std::string error;
        if (ReadWholeFile(d->cfg.path, bytes, error)) {
            stats.opened = true;
            stats.error.clear();
            break;
        }
        stats.error = error;
        {
            std::lock_guard<std::mutex> lock(d->stats_mutex);
            d->stats = stats;
        }
        os_sleep_ms(50);
    }
    if (!stats.opened) {
        return;
    }
    stats.file_bytes = bytes.size();

    std::vector<float> left;
    std::vector<float> right;
    size_t offset = 0;
    uint64_t first_qpc = 0;
    uint64_t qpc_freq = 0;
    uint32_t expected_sequence = 0;
    bool have_sequence = false;
    const uint64_t anchor_ns = os_gettime_ns();
    while (!d->stopping.load(std::memory_order_acquire)) {
        if (offset + sizeof(A3AudioPacketHeader) > bytes.size()) {
            break;
        }
        A3AudioPacketHeader h = {};
        std::memcpy(&h, bytes.data() + offset, sizeof(h));
        if (std::memcmp(h.magic, "A3AP", 4) != 0 || h.version != 1 ||
            h.header_bytes != A3_AUDIO_HEADER_BYTES ||
            h.sample_rate != 44100 || h.channels != 2 ||
            h.sample_format != A3_AUDIO_FORMAT_S16LE ||
            h.payload_bytes != static_cast<uint32_t>(h.sample_count) * 4 ||
            h.qpc_frequency == 0) {
            stats.error = "invalid framed header";
            break;
        }
        if (offset + sizeof(h) + h.payload_bytes > bytes.size()) {
            stats.error = "truncated framed payload";
            break;
        }
        A3AudioPacketHeader zeroed = h;
        zeroed.header_crc32 = 0;
        if (a3_audio_crc32(&zeroed, sizeof(zeroed)) != h.header_crc32 ||
            a3_audio_crc32(bytes.data() + offset + sizeof(h), h.payload_bytes) !=
                h.payload_crc32) {
            ++stats.crc_errors;
            stats.error = "CRC mismatch";
            break;
        }
        if (have_sequence && h.sequence != expected_sequence) {
            ++stats.sequence_gaps;
        }
        expected_sequence = h.sequence + 1;
        have_sequence = true;
        if (h.sequence == 0 || first_qpc == 0) {
            first_qpc = h.actual_start_qpc;
            qpc_freq = h.qpc_frequency;
            stats.capture_id = h.capture_id;
            stats.generation = h.generation;
            stats.saw_first = (h.flags & A3_AUDIO_FLAG_FIRST) != 0;
            stats.first_timestamp_ns = anchor_ns;
        }
        if (h.flags & A3_AUDIO_FLAG_DISCONTINUITY) {
            ++stats.discontinuity_packets;
        }
        const uint64_t ts = anchor_ns +
            (h.actual_start_qpc - first_qpc) * 1000000000ULL / qpc_freq;
        while (!d->stopping.load(std::memory_order_acquire)) {
            const uint64_t now = os_gettime_ns();
            if (now >= ts) {
                break;
            }
            os_sleepto_ns(std::min(ts, now + 2000000ULL));
        }
        if (d->stopping.load(std::memory_order_acquire)) {
            break;
        }
        const uint32_t frames = static_cast<uint32_t>(h.sample_count);
        left.resize(frames);
        right.resize(frames);
        const uint8_t* p = bytes.data() + offset + sizeof(h);
        for (uint32_t i = 0; i < frames; ++i) {
            int16_t l = 0;
            int16_t r = 0;
            std::memcpy(&l, p, 2);
            std::memcpy(&r, p + 2, 2);
            p += 4;
            left[i] = static_cast<float>(l) / 32768.0f;
            right[i] = static_cast<float>(r) / 32768.0f;
        }
        if (frames) {
            obs_source_audio audio = {};
            audio.data[0] = reinterpret_cast<const uint8_t*>(left.data());
            audio.data[1] = reinterpret_cast<const uint8_t*>(right.data());
            audio.frames = frames;
            audio.speakers = SPEAKERS_STEREO;
            audio.format = AUDIO_FORMAT_FLOAT_PLANAR;
            audio.samples_per_sec = 44100;
            audio.timestamp = ts;
            obs_source_output_audio(d->source, &audio);
            ++stats.pushed_packets;
            stats.pushed_frames += frames;
            stats.last_timestamp_ns = ts;
        }
        ++stats.packets;
        stats.frames += frames;
        stats.saw_last = (h.flags & A3_AUDIO_FLAG_LAST) != 0;
        {
            std::lock_guard<std::mutex> lock(d->stats_mutex);
            d->stats = stats;
        }
        offset += sizeof(h) + h.payload_bytes;
        if (stats.saw_last) {
            break;
        }
    }
    {
        std::lock_guard<std::mutex> lock(d->stats_mutex);
        d->stats = stats;
    }
}

const char* FramedGetName(void*)
{
    return kSourceName;
}

void FramedGetDefaults(obs_data_t* defaults)
{
    obs_data_set_default_string(defaults, "path", "");
}

void* FramedCreate(obs_data_t* settings, obs_source_t* source)
{
    auto* d = new FramedAudioData();
    d->source = source;
    const char* path = obs_data_get_string(settings, "path");
    d->cfg.path = path ? path : "";
    d->stats.created = true;
    d->stats.path = d->cfg.path;
    g_last_source.store(d, std::memory_order_release);
    d->thread = std::thread(ReaderThread, d);
    return d;
}

void FramedDestroy(void* ptr)
{
    auto* d = static_cast<FramedAudioData*>(ptr);
    if (!d) {
        return;
    }
    g_last_source.store(nullptr, std::memory_order_release);
    d->stopping.store(true, std::memory_order_release);
    if (d->thread.joinable()) {
        d->thread.join();
    }
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

} // namespace

void RegisterFramedAudioSource()
{
    obs_register_source_s(&kFramedAudioInfo, sizeof(kFramedAudioInfo));
}

void StopFramedAudioSourceThread()
{
    FramedAudioData* d = g_last_source.load(std::memory_order_acquire);
    if (!d) {
        return;
    }
    d->stopping.store(true, std::memory_order_release);
    if (d->thread.joinable()) {
        d->thread.join();
    }
}

FramedAudioStats GetFramedAudioStats()
{
    FramedAudioData* d = g_last_source.load(std::memory_order_acquire);
    if (!d) {
        return {};
    }
    FramedAudioStats stats = CopyStats(d);
    stats.created = true;
    return stats;
}

} // namespace a3
