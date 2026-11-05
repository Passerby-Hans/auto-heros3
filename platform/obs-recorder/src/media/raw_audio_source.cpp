// S2a raw-file audio source implementation.
//
// Design (see raw_audio_source.hpp):
//   * source flags OBS_SOURCE_AUDIO | OBS_SOURCE_ASYNC (explicit, as requested);
//   * one std::thread opens the file, positions at 0 or EOF, then every 100 ms
//     reads up to 4410 interleaved s16le stereo frames, converts to
//     FLOAT_PLANAR and calls obs_source_output_audio() with an absolute
//     os_gettime_ns() timestamp on a fixed grid;
//   * when fewer than 4410 frames are available (empty file / no growth yet /
//     EOF), a silent FLOAT_PLANAR chunk is pushed on the same grid and counted
//     in stats.silence_chunks/eof_waits (explicit no-data handling);
//   * missing file: retried every 50 ms while the source exists; the host turns
//     "never opened" into an explicit failure (exit 9);
//   * no playback, no audio device, no monitoring calls.
#include "raw_audio_source.hpp"

#include <windows.h>

#include <obs.h>
#include <obs-source.h>

#include <util/platform.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace a3 {
namespace {

constexpr char kSourceId[] = "a3_raw_audio";
constexpr char kSourceName[] = "A3 Raw VCMI Game Audio Source (S2a)";

std::wstring Utf8ToWide(const std::string& s)
{
    if (s.empty()) {
        return {};
    }
    const int need = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()), nullptr, 0);
    std::wstring out(static_cast<size_t>(need), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()), out.data(), need);
    return out;
}

std::string Win32ErrorText(DWORD code)
{
    wchar_t* buffer = nullptr;
    const DWORD n = FormatMessageW(FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM |
                                       FORMAT_MESSAGE_IGNORE_INSERTS,
                                   nullptr, code, MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT),
                                   reinterpret_cast<LPWSTR>(&buffer), 0, nullptr);
    std::string text = "win32 error " + std::to_string(code);
    if (n && buffer) {
        const int need = WideCharToMultiByte(CP_UTF8, 0, buffer, static_cast<int>(n), nullptr, 0, nullptr, nullptr);
        std::string utf8(static_cast<size_t>(need), '\0');
        WideCharToMultiByte(CP_UTF8, 0, buffer, static_cast<int>(n), utf8.data(), need, nullptr, nullptr);
        text += ": " + utf8;
    }
    LocalFree(buffer);
    return text;
}

struct RawAudioData {
    obs_source_t* source = nullptr;
    RawAudioConfig cfg;

    std::thread thread;
    std::atomic<bool> stopping {false};

    mutable std::mutex stats_mutex;
    RawAudioStats stats;

    std::vector<float> left;
    std::vector<float> right;
};

std::atomic<RawAudioData*> g_last_source {nullptr};

void SetStats(RawAudioData* d, const RawAudioStats& stats)
{
    std::lock_guard<std::mutex> lock(d->stats_mutex);
    d->stats = stats;
}

RawAudioStats CopyStats(RawAudioData* d)
{
    std::lock_guard<std::mutex> lock(d->stats_mutex);
    return d->stats;
}

void PushAudio(RawAudioData* d, bool real_data, uint64_t timestamp_ns)
{
    const uint32_t frames = d->cfg.chunk_frames;
    if (real_data) {
        // left/right already hold the converted real samples
    } else {
        std::fill(d->left.begin(), d->left.end(), 0.0f);
        std::fill(d->right.begin(), d->right.end(), 0.0f);
    }

    obs_source_audio audio = {};
    audio.data[0] = reinterpret_cast<const uint8_t*>(d->left.data());
    audio.data[1] = reinterpret_cast<const uint8_t*>(d->right.data());
    audio.frames = frames;
    audio.speakers = SPEAKERS_STEREO;
    audio.format = AUDIO_FORMAT_FLOAT_PLANAR;
    audio.samples_per_sec = d->cfg.sample_rate;
    audio.timestamp = timestamp_ns;
    obs_source_output_audio(d->source, &audio);
}

// Converts one chunk of interleaved s16le stereo bytes into the float planar
// buffers (full-scale +-1.0; 1/32768 scaling).
void ConvertChunk(RawAudioData* d, const uint8_t* bytes)
{
    const uint32_t frames = d->cfg.chunk_frames;
    const uint8_t* p = bytes;
    for (uint32_t i = 0; i < frames; ++i) {
        int16_t l = 0;
        int16_t r = 0;
        std::memcpy(&l, p, sizeof(l));
        std::memcpy(&r, p + sizeof(l), sizeof(r));
        p += 4;
        d->left[i] = static_cast<float>(l) / 32768.0f;
        d->right[i] = static_cast<float>(r) / 32768.0f;
    }
}

HANDLE TryOpen(const std::wstring& path, std::string& error)
{
    HANDLE h = CreateFileW(path.c_str(), GENERIC_READ,
                           FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
                           OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) {
        error = Win32ErrorText(GetLastError());
        return INVALID_HANDLE_VALUE;
    }
    return h;
}

bool SeekTo(HANDLE file, uint64_t offset)
{
    LARGE_INTEGER dist;
    dist.QuadPart = static_cast<LONGLONG>(offset);
    return SetFilePointerEx(file, dist, nullptr, FILE_BEGIN) != 0;
}

void ReaderThread(RawAudioData* d)
{
    std::wstring wpath = Utf8ToWide(d->cfg.path);
    HANDLE file = INVALID_HANDLE_VALUE;
    RawAudioStats stats;
    stats.created = true;
    stats.path = d->cfg.path;

    // --- open (retry while the file has not been created yet) ---------------
    while (!d->stopping.load(std::memory_order_acquire)) {
        std::string error;
        file = TryOpen(wpath, error);
        ++stats.open_attempts;
        SetStats(d, stats);
        if (file != INVALID_HANDLE_VALUE) {
            stats.opened = true;
            stats.open_error.clear();
            break;
        }
        stats.open_error = error;
        SetStats(d, stats);
        os_sleep_ms(50); // no blocking read; the host fails explicitly after its own timeout
    }
    if (file == INVALID_HANDLE_VALUE) {
        return;
    }

    // --- initial position: start_at_zero -> 0, otherwise current EOF --------
    LARGE_INTEGER size;
    if (GetFileSizeEx(file, &size)) {
        stats.file_size_at_open = static_cast<uint64_t>(size.QuadPart);
    }
    const uint64_t initial_offset = d->cfg.start_at_zero ? 0ULL : stats.file_size_at_open;
    stats.start_offset = initial_offset;
    SetStats(d, stats);
    if (!SeekTo(file, initial_offset)) {
        stats.open_error = "seek failed: " + Win32ErrorText(GetLastError());
        stats.opened = false;
        SetStats(d, stats);
        CloseHandle(file);
        return;
    }

    const uint32_t frame_bytes = 4; // s16le stereo
    const uint32_t chunk_bytes = d->cfg.chunk_frames * frame_bytes;
    const uint64_t chunk_period_ns =
        static_cast<uint64_t>(d->cfg.chunk_frames) * 1000000000ULL / d->cfg.sample_rate;

    std::vector<uint8_t> pending;
    pending.reserve(chunk_bytes * 4);
    std::vector<uint8_t> readbuf(64 * 1024);

    uint64_t next_ts = os_gettime_ns();
    while (!d->stopping.load(std::memory_order_acquire)) {
        const uint64_t now = os_gettime_ns();
        if (now < next_ts) {
            const uint64_t target = std::min(next_ts, now + 2000000ULL);
            os_sleepto_ns(target);
            continue;
        }
        if (now > next_ts + 500000000ULL) {
            // Large hiccup: resync the timestamp grid to "now" instead of
            // pushing a burst of stale samples.  For live capture this drops
            // audio that is already in the past; for a static test file the
            // remaining bytes are still read on the new grid.
            next_ts = now;
        }

        // Read more bytes if needed; never block, the file may still grow.
        if (pending.size() < chunk_bytes) {
            DWORD got = 0;
            const BOOL ok = ReadFile(file, readbuf.data(), static_cast<DWORD>(readbuf.size()), &got, nullptr);
            if (!ok) {
                stats.open_error = "read failed: " + Win32ErrorText(GetLastError());
                stats.opened = false;
                SetStats(d, stats);
                break;
            }
            if (got == 0) {
                ++stats.eof_waits;
            } else {
                pending.insert(pending.end(), readbuf.data(), readbuf.data() + got);
            }
        }

        uint64_t ts = next_ts;
        if (pending.size() >= chunk_bytes) {
            ConvertChunk(d, pending.data());
            pending.erase(pending.begin(), pending.begin() + chunk_bytes);
            PushAudio(d, true, ts);
            ++stats.chunks_pushed;
            stats.samples_pushed += d->cfg.chunk_frames;
            stats.frames_read += d->cfg.chunk_frames;
            stats.bytes_read += chunk_bytes;
        } else {
            PushAudio(d, false, ts);
            ++stats.silence_chunks;
        }
        SetStats(d, stats);
        next_ts += chunk_period_ns;
    }

    CloseHandle(file);
}

const char* RawGetName(void*)
{
    return kSourceName;
}

void RawGetDefaults(obs_data_t* defaults)
{
    obs_data_set_default_string(defaults, "path", "");
    obs_data_set_default_bool(defaults, "start_at_zero", false);
}

void* RawCreate(obs_data_t* settings, obs_source_t* source)
{
    auto* data = new RawAudioData();
    data->source = source;
    data->cfg = RawAudioConfig{};
    const char* path = obs_data_get_string(settings, "path");
    data->cfg.path = path ? path : "";
    data->cfg.start_at_zero = obs_data_get_bool(settings, "start_at_zero");
    data->left.resize(data->cfg.chunk_frames);
    data->right.resize(data->cfg.chunk_frames);

    RawAudioStats stats;
    stats.created = true;
    stats.path = data->cfg.path;
    {
        std::lock_guard<std::mutex> lock(data->stats_mutex);
        data->stats = stats;
    }

    g_last_source.store(data, std::memory_order_release);
    data->thread = std::thread(ReaderThread, data);
    return data;
}

void RawDestroy(void* ptr)
{
    auto* data = static_cast<RawAudioData*>(ptr);
    if (!data) {
        return;
    }
    g_last_source.store(nullptr, std::memory_order_release);
    data->stopping.store(true, std::memory_order_release);
    if (data->thread.joinable()) {
        data->thread.join();
    }
    delete data;
}

const obs_source_info kRawAudioInfo = [] {
    obs_source_info info = {};
    info.id = kSourceId;
    info.type = OBS_SOURCE_TYPE_INPUT;
    info.output_flags = OBS_SOURCE_AUDIO | OBS_SOURCE_ASYNC;
    info.get_name = RawGetName;
    info.create = RawCreate;
    info.destroy = RawDestroy;
    info.get_defaults = RawGetDefaults;
    return info;
}();

} // namespace

void RegisterRawAudioSource()
{
    obs_register_source_s(&kRawAudioInfo, sizeof(kRawAudioInfo));
}

void StopRawAudioSourceThread()
{
    RawAudioData* data = g_last_source.load(std::memory_order_acquire);
    if (!data) {
        return;
    }
    data->stopping.store(true, std::memory_order_release);
    if (data->thread.joinable()) {
        data->thread.join();
    }
    const RawAudioStats stats = CopyStats(data);
    std::printf("RAW_AUDIO thread_stopped opened=%d bytes_read=%llu chunks=%llu silence=%llu eof_waits=%llu samples=%llu\n",
                stats.opened ? 1 : 0, static_cast<unsigned long long>(stats.bytes_read),
                static_cast<unsigned long long>(stats.chunks_pushed),
                static_cast<unsigned long long>(stats.silence_chunks),
                static_cast<unsigned long long>(stats.eof_waits),
                static_cast<unsigned long long>(stats.samples_pushed));
    std::fflush(stdout);
}

RawAudioStats GetRawAudioStats()
{
    RawAudioStats stats;
    RawAudioData* data = g_last_source.load(std::memory_order_acquire);
    if (data) {
        stats = CopyStats(data);
        stats.created = true;
    }
    return stats;
}

} // namespace a3
