// S1b media self-test host for auto-heros3.
//
// What this executable does (and only this):
//   1. links against libobs 32.1.0 through a generated import library
//      (obs.lib built from the installed obs.dll) and the OBS 32.1.0 source
//      headers (vendor/obs-studio/libobs + generated obsconfig.h);
//   2. obs_startup() + explicit selective module load (obs-ffmpeg.dll,
//      obs-x264.dll, obs-outputs.dll only; never obs_load_all_modules, no
//      Qt/UI/frontend plugin);
//   3. obs_reset_audio(44100 Hz stereo) + obs_reset_video(1920x1080@60, I420,
//      D3D11 graphics module from the installed OBS bin directory);
//   4. registers and creates one in-memory test source
//      ("a3_test_av_source", asynchronous video + PCM audio, see
//      gen_av_source.cpp), puts it in a scene and assigns the scene to main
//      canvas channel 0 (obs_set_output_source);
//   5. creates H.264 (obs_x264) + AAC (ffmpeg_aac) encoders and an
//      "mp4_output" (Hybrid MP4) output with split_file=true/max_time_sec=5,
//      records for the requested duration (default 17 s) and stops cleanly;
//   6. writes a machine-readable report (selftest.json) next to the produced
//      MP4s: loaded modules, output settings actually applied, output
//      signals/events, frame/byte counters, produced files, and the process
//      module list (used to show no WASAPI/MMDevAPI audio endpoint was opened).
//
// There is no screen capture, no audio capture, no audio monitoring/playback,
// no physical audio output and no OBS UI/WebSocket anywhere in this path.
#include <windows.h>

#include <tlhelp32.h>

// obs.h must come before obs-source.h: obs.h includes obs-source.h itself and
// the latter is not self-sufficient under `#pragma once` if entered first.
#include <obs.h>
#include <obs-source.h>

#include <util/platform.h>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "../json_util.hpp"
#include "gen_av_source.hpp"

namespace fs = std::filesystem;

namespace {

// ---------------------------------------------------------------------------
// small string / path helpers
// ---------------------------------------------------------------------------

std::string WideToUtf8(const std::wstring& w)
{
    if (w.empty()) {
        return {};
    }
    const int need = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), static_cast<int>(w.size()),
                                         nullptr, 0, nullptr, nullptr);
    std::string out(static_cast<size_t>(need), '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), static_cast<int>(w.size()), out.data(), need,
                        nullptr, nullptr);
    return out;
}

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

std::string JoinPath(const std::string& a, const std::string& b)
{
    if (a.empty()) {
        return b;
    }
    if (b.empty()) {
        return a;
    }
    const char last = a.back();
    if (last == '\\' || last == '/') {
        return a + b;
    }
    return a + "\\" + b;
}

bool EnsureDir(const std::string& dir)
{
    std::error_code ec;
    fs::create_directories(fs::path(Utf8ToWide(dir)), ec);
    return fs::is_directory(fs::path(Utf8ToWide(dir)), ec);
}

bool FileExists(const std::string& path)
{
    std::error_code ec;
    return fs::is_regular_file(fs::path(Utf8ToWide(path)), ec);
}

std::string UtcNowIso()
{
    std::time_t t = std::time(nullptr);
    std::tm tm {};
    gmtime_s(&tm, &t);
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%04d-%02d-%02dT%02d:%02d:%02dZ", tm.tm_year + 1900,
                  tm.tm_mon + 1, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec);
    return buf;
}

std::string LowerCopy(std::string s)
{
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

// ---------------------------------------------------------------------------
// run state
// ---------------------------------------------------------------------------

struct Options {
    std::string obs_dir = "C:\\Program Files\\obs-studio";
    std::string out_dir;
    int duration_ms = 17000;
    int max_time_sec = 5;
    std::string json_out;
};

struct Step {
    std::string name;
    bool ok = false;
    std::string detail;
};

struct Event {
    std::string name;
    std::string detail;
    uint64_t t_ns = 0;
};

struct ModuleResult {
    std::string file;
    std::string path;
    std::string data_path;
    int open_rc = -999;
    bool init_ok = false;
};

struct FileResult {
    std::string name;
    uint64_t size = 0;
    uint64_t mtime_unix = 0;
    uint64_t mtime_filetime = 0;
};

struct State {
    Options opt;
    std::vector<Step> steps;
    std::vector<ModuleResult> modules;
    std::vector<FileResult> files;
    std::vector<std::string> process_modules;

    std::string obs_version;
    uint32_t obs_api_version = 0;
    std::string graphics_module;

    bool obs_started = false;
    bool audio_reset_ok = false;
    bool video_reset_ok = false;

    obs_source_t* source = nullptr;
    obs_scene_t* scene = nullptr;
    obs_encoder_t* video_encoder = nullptr;
    obs_encoder_t* audio_encoder = nullptr;
    obs_output_t* output = nullptr;
    obs_data_t* output_settings = nullptr;
    signal_handler_t* output_signals = nullptr;

    uint64_t t_start_ns = 0;
    uint64_t t_output_active_ns = 0;
    uint64_t t_stop_request_ns = 0;
    uint64_t t_output_inactive_ns = 0;
    bool output_started = false;
    bool output_active_seen = false;
    bool output_stopped = false;
    int output_stop_code = -1;
    bool stop_signal_seen = false;

    uint32_t total_frames_drawn_at_start = 0;
    uint32_t total_frames_drawn_at_stop = 0;
    uint32_t lagged_frames_at_stop = 0;
    uint64_t output_total_bytes = 0;
    int output_total_frames = 0;
    int output_frames_dropped = 0;
    std::string output_last_error;
    std::string video_encoder_last_error;
    std::string audio_encoder_last_error;
    std::string applied_output_settings_json;
    std::string applied_video_encoder_settings_json;
    std::string applied_audio_encoder_settings_json;

    std::string monitoring_device_name;
    std::string monitoring_device_id;
    int source_monitoring_type = -1;
};

std::mutex g_events_mutex;
std::vector<Event> g_events;
std::atomic<bool> g_stop_signal_seen {false};
std::atomic<int> g_stop_code {-1};

void ResetSignalGlobals()
{
    g_stop_signal_seen.store(false);
    g_stop_code.store(-1);
}

void RecordEvent(const std::string& name, const std::string& detail)
{
    std::lock_guard<std::mutex> lock(g_events_mutex);
    g_events.push_back(Event{name, detail, os_gettime_ns()});
}

// Output signal callbacks.  libobs' output signal handler is declared in
// libobs/obs-output.c output_signals[]; "file_changed" is emitted by
// plugins/obs-outputs/mp4-output.c change_file() and is declared by this host
// (mp4-output.c signals it without a declaration).
void OnOutputStarting(void*, calldata_t*)
{
    RecordEvent("output_starting", "");
}
void OnOutputStart(void*, calldata_t*)
{
    RecordEvent("output_start", "");
}
void OnOutputStopping(void*, calldata_t*)
{
    RecordEvent("output_stopping", "");
}
void OnOutputStop(void*, calldata_t* cd)
{
    long long code = -1;
    calldata_get_int(cd, "code", &code);
    g_stop_code.store(static_cast<int>(code));
    g_stop_signal_seen.store(true);
    const char* err = calldata_string(cd, "last_error");
    RecordEvent("output_stop", "code=" + std::to_string(code) + "; last_error=" + (err ? err : "<null>"));
}
void OnOutputDeactivate(void*, calldata_t*)
{
    RecordEvent("output_deactivate", "");
}
void OnOutputFileChanged(void*, calldata_t* cd)
{
    const char* next = calldata_string(cd, "next_file");
    RecordEvent("output_file_changed", next ? next : "<null>");
}

// ---------------------------------------------------------------------------
// evidence helpers
// ---------------------------------------------------------------------------

std::vector<std::string> EnumProcessModuleNames()
{
    std::vector<std::string> names;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, GetCurrentProcessId());
    if (snap == INVALID_HANDLE_VALUE) {
        return names;
    }
    MODULEENTRY32W entry {};
    entry.dwSize = sizeof(entry);
    if (Module32FirstW(snap, &entry)) {
        do {
            names.push_back(WideToUtf8(entry.szModule));
        } while (Module32NextW(snap, &entry));
    }
    CloseHandle(snap);
    return names;
}

bool HasModule(const std::vector<std::string>& modules, const std::string& wanted)
{
    const std::string want = LowerCopy(wanted);
    for (const auto& m : modules) {
        if (LowerCopy(m) == want) {
            return true;
        }
    }
    return false;
}

std::vector<FileResult> ListMp4Files(const std::string& dir)
{
    std::vector<FileResult> out;
    const std::wstring pattern = Utf8ToWide(JoinPath(dir, "*.mp4"));
    WIN32_FIND_DATAW fd {};
    HANDLE h = FindFirstFileW(pattern.c_str(), &fd);
    if (h == INVALID_HANDLE_VALUE) {
        return out;
    }
    do {
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
            continue;
        }
        FileResult fr;
        fr.name = WideToUtf8(fd.cFileName);
        fr.size = (static_cast<uint64_t>(fd.nFileSizeHigh) << 32) | fd.nFileSizeLow;
        ULARGE_INTEGER ft;
        ft.LowPart = fd.ftLastWriteTime.dwLowDateTime;
        ft.HighPart = fd.ftLastWriteTime.dwHighDateTime;
        fr.mtime_filetime = ft.QuadPart;
        // FILETIME (100 ns units since 1601-01-01) -> unix seconds.
        constexpr uint64_t kEpochDiff100ns = 116444736000000000ULL;
        if (fr.mtime_filetime > kEpochDiff100ns) {
            fr.mtime_unix = (fr.mtime_filetime - kEpochDiff100ns) / 10000000ULL;
        }
        out.push_back(fr);
    } while (FindNextFileW(h, &fd));
    FindClose(h);

    std::sort(out.begin(), out.end(), [](const FileResult& a, const FileResult& b) {
        if (a.mtime_filetime != b.mtime_filetime) {
            return a.mtime_filetime < b.mtime_filetime;
        }
        return a.name < b.name;
    });
    return out;
}

std::string CurrentOutputLastError(obs_output_t* output)
{
    const char* err = obs_output_get_last_error(output);
    return err ? err : "";
}

std::string CurrentEncoderLastError(obs_encoder_t* encoder)
{
    const char* err = obs_encoder_get_last_error(encoder);
    return err ? err : "";
}

void AddStep(State& s, const std::string& name, bool ok, const std::string& detail = "")
{
    s.steps.push_back(Step{name, ok, detail});
    std::printf("STEP %-32s %s%s%s\n", name.c_str(), ok ? "PASS" : "FAIL",
                detail.empty() ? "" : "  ", detail.c_str());
    std::fflush(stdout);
}

bool AllStepsOk(const State& s)
{
    for (const auto& step : s.steps) {
        if (!step.ok) {
            return false;
        }
    }
    return true;
}

// ---------------------------------------------------------------------------
// obs lifecycle / pipeline
// ---------------------------------------------------------------------------
void Cleanup(State& s)
{
    auto trace = [](const char* what) {
        std::printf("CLEANUP %s\n", what);
        std::fflush(stdout);
    };
    trace("begin");

    // Safety net for error paths: make sure the synthetic media producer is
    // stopped before any libobs object is released.  On the normal path this
    // was already done right after the output drained (idempotent).
    a3::StopAvTestSourceThread();

    // Detach the scene from the main canvas channel first (the channel holds a
    // strong reference to the scene source, libobs/obs-canvas.c
    // obs_canvas_set_channel => obs_source_get_ref).
    if (s.obs_started && s.scene) {
        obs_set_output_source(0, nullptr);
        trace("detached_output_source");
    }

    if (s.output_signals) {
        signal_handler_disconnect(s.output_signals, "start", OnOutputStart, nullptr);
        signal_handler_disconnect(s.output_signals, "stop", OnOutputStop, nullptr);
        signal_handler_disconnect(s.output_signals, "starting", OnOutputStarting, nullptr);
        signal_handler_disconnect(s.output_signals, "stopping", OnOutputStopping, nullptr);
        signal_handler_disconnect(s.output_signals, "deactivate", OnOutputDeactivate, nullptr);
        signal_handler_disconnect(s.output_signals, "file_changed", OnOutputFileChanged, nullptr);
        s.output_signals = nullptr;
        trace("signals_disconnected");
    }
    if (s.output) {
        obs_output_release(s.output);
        s.output = nullptr;
        trace("output_released");
    }
    if (s.video_encoder) {
        obs_encoder_release(s.video_encoder);
        s.video_encoder = nullptr;
        trace("video_encoder_released");
    }
    if (s.audio_encoder) {
        obs_encoder_release(s.audio_encoder);
        s.audio_encoder = nullptr;
        trace("audio_encoder_released");
    }

    // Release the host references to the scene and to the test source.  The
    // scene source itself is also strongly referenced by the main canvas
    // (SCENE_REF / PROGRAM canvas flag), so scene + item + source objects are
    // finally destroyed inside obs_shutdown() when the canvas is released.
    // That is the normal OBS lifecycle; the test source's worker thread was
    // stopped above so nothing can push media into libobs during teardown.
    if (s.source) {
        obs_source_release(s.source);
        s.source = nullptr;
        trace("source_released");
    }
    if (s.scene) {
        obs_scene_release(s.scene);
        s.scene = nullptr;
        trace("scene_released");
    }
    if (s.output_settings) {
        obs_data_release(s.output_settings);
        s.output_settings = nullptr;
        trace("output_settings_released");
    }

    if (s.obs_started) {
        obs_shutdown();
        s.obs_started = false;
        trace("obs_shutdown_done");
    }
    trace("end");
}

bool LoadSelectiveModules(State& s)
{
    const std::string bin_dir = JoinPath(s.opt.obs_dir, "obs-plugins\\64bit");
    const std::string data_root = JoinPath(s.opt.obs_dir, "data\\obs-plugins");
    // Explicit whitelist only.  obs_load_all_modules() would also load the Qt
    // UI/frontend plugins and other unrelated modules; S0 already showed that
    // must be avoided for a headless host.
    const char* kFiles[] = {"obs-ffmpeg.dll", "obs-x264.dll", "obs-outputs.dll"};

    for (const char* file : kFiles) {
        ModuleResult mr;
        mr.file = file;
        mr.path = JoinPath(bin_dir, file);
        // Module data directory convention: <data>/obs-plugins/<module-name>
        // (see libobs/obs-module.c make_data_directory()/make_data_dir()).
        std::string stem = file;
        const size_t dot = stem.rfind('.');
        if (dot != std::string::npos) {
            stem.resize(dot);
        }
        mr.data_path = JoinPath(data_root, stem);

        if (!FileExists(mr.path)) {
            mr.open_rc = -404;
            s.modules.push_back(mr);
            continue;
        }

        obs_module_t* module = nullptr;
        mr.open_rc = obs_open_module(&module, mr.path.c_str(), mr.data_path.c_str());
        if (mr.open_rc == MODULE_SUCCESS && module) {
            mr.init_ok = obs_init_module(module);
            std::printf("MODULE %-20s open=ok init=%s\n", file, mr.init_ok ? "ok" : "FAILED");
        } else {
            std::printf("MODULE %-20s open=FAILED rc=%d\n", file, mr.open_rc);
        }
        std::fflush(stdout);
        s.modules.push_back(mr);
    }

    bool all_ok = true;
    for (const auto& mr : s.modules) {
        all_ok = all_ok && mr.init_ok;
    }
    if (!all_ok) {
        return false;
    }
    obs_post_load_modules();
    return true;
}

bool InitObs(State& s)
{
    // Verify the installed OBS layout we depend on before touching libobs.
    const std::string obs_dll = JoinPath(JoinPath(s.opt.obs_dir, "bin\\64bit"), "obs.dll");
    const std::string libobs_data = JoinPath(s.opt.obs_dir, "data\\libobs");
    const std::string d3d11_module = JoinPath(JoinPath(s.opt.obs_dir, "bin\\64bit"), "libobs-d3d11.dll");
    s.graphics_module = d3d11_module;
    std::replace(s.graphics_module.begin(), s.graphics_module.end(), '\\', '/');

    if (!FileExists(obs_dll) || !fs::is_directory(fs::path(Utf8ToWide(libobs_data))) ||
        !FileExists(d3d11_module)) {
        AddStep(s, "obs_install_layout", false,
                "obs_dir=" + s.opt.obs_dir + " (expected bin/64bit/obs.dll, bin/64bit/libobs-d3d11.dll, data/libobs)");
        return false;
    }
    AddStep(s, "obs_install_layout", true, obs_dll);

    if (!obs_startup("en-US", nullptr, nullptr)) {
        AddStep(s, "obs_startup", false, "obs_startup returned false");
        return false;
    }
    s.obs_started = true;
    s.obs_version = obs_get_version_string() ? obs_get_version_string() : "";
    s.obs_api_version = obs_get_version();
    AddStep(s, "obs_startup", true, "libobs " + s.obs_version);

    // libobs core data files (default.effect, format_conversion.effect, ...)
    // are required by obs_reset_video().  Source builds find them relative to
    // obs64.exe ("../../data/libobs/", libobs/obs-windows.c
    // find_libobs_data_file); a host that links libobs must register the same
    // directory explicitly.  Two details matter:
    //  * obs_add_data_path() concatenates path + file name without inserting a
    //    separator (libobs/obs-internal.h check_path) => trailing '/';
    //  * the effect preprocessor resolves '#include "color.effect"' relative to
    //    the *including file's* path by splitting on '/' (libobs/util/
    //    cf-lexer.c insert_path), so the data path must use '/' even on
    //    Windows; a backslash path makes the include lookup fail and
    //    obs_reset_video() returns OBS_VIDEO_FAIL.
    // The function is marked deprecated in 32.x but still implemented in
    // libobs/obs.c.
    std::string libobs_data_path = libobs_data;
    std::replace(libobs_data_path.begin(), libobs_data_path.end(), '\\', '/');
    if (libobs_data_path.empty() || libobs_data_path.back() != '/') {
        libobs_data_path += '/';
    }
    obs_add_data_path(libobs_data_path.c_str());

    if (!LoadSelectiveModules(s)) {
        AddStep(s, "load_selective_modules", false, "at least one whitelisted module failed to initialize");
        return false;
    }
    AddStep(s, "load_selective_modules", true, "3/3 obs-ffmpeg, obs-x264, obs-outputs");

    obs_audio_info aoi = {};
    aoi.samples_per_sec = 44100;
    aoi.speakers = SPEAKERS_STEREO;
    s.audio_reset_ok = obs_reset_audio(&aoi);
    AddStep(s, "obs_reset_audio", s.audio_reset_ok, "44100 Hz, SPEAKERS_STEREO");
    if (!s.audio_reset_ok) {
        return false;
    }

    obs_video_info ovi = {};
    ovi.graphics_module = s.graphics_module.c_str();
    ovi.fps_num = 60;
    ovi.fps_den = 1;
    ovi.base_width = 1920;
    ovi.base_height = 1080;
    ovi.output_width = 1920;
    ovi.output_height = 1080;
    ovi.output_format = VIDEO_FORMAT_I420;
    ovi.adapter = 0;
    ovi.gpu_conversion = true;
    ovi.colorspace = VIDEO_CS_709;  // OBS recording default for SDR 1080p
    ovi.range = VIDEO_RANGE_PARTIAL;
    ovi.scale_type = OBS_SCALE_BICUBIC;
    const int video_rc = obs_reset_video(&ovi);
    s.video_reset_ok = (video_rc == OBS_VIDEO_SUCCESS);
    AddStep(s, "obs_reset_video", s.video_reset_ok,
            "1920x1080@60 I420 " + std::string("graphics_module=") + s.graphics_module +
                " rc=" + std::to_string(video_rc));
    return s.video_reset_ok;
}

bool CreatePipeline(State& s, const std::string& first_file_path)
{
    a3::RegisterAvTestSource();
    AddStep(s, "register_test_source", true, "obs_register_source_s(a3_test_av_source)");

    obs_data_t* source_settings = obs_data_create();
    s.source = obs_source_create("a3_test_av_source", "S1b In-Memory Test AV", source_settings, nullptr);
    obs_data_release(source_settings);
    AddStep(s, "create_test_source", s.source != nullptr, "async video (I420 1920x1080@60) + PCM audio");
    if (!s.source) {
        return false;
    }

    s.source_monitoring_type = static_cast<int>(obs_source_get_monitoring_type(s.source));
    const uint32_t source_flags = obs_source_get_output_flags(s.source);
    AddStep(s, "source_flags", (source_flags & OBS_SOURCE_ASYNC_VIDEO) != 0 &&
                                   (source_flags & OBS_SOURCE_AUDIO) != 0,
            "output_flags=0x" + [](uint32_t v) {
                char buf[16];
                std::snprintf(buf, sizeof(buf), "%x", v);
                return std::string(buf);
            }(source_flags));

    s.scene = obs_scene_create("S1b Scene");
    AddStep(s, "create_scene", s.scene != nullptr, "obs_scene_create");
    if (!s.scene) {
        return false;
    }

    obs_sceneitem_t* item = obs_scene_add(s.scene, s.source);
    AddStep(s, "scene_add_source", item != nullptr, "obs_scene_add");
    if (!item) {
        return false;
    }

    obs_set_output_source(0, obs_scene_get_source(s.scene));
    // obs_get_output_source() returns a strong reference (libobs/obs-canvas.c
    // obs_view_get_source -> obs_source_get_ref); release it again or the
    // scene source can never be destroyed and the in-memory source stays alive
    // until obs_shutdown(), which crashed at teardown.
    obs_source_t* channel_source = obs_get_output_source(0);
    AddStep(s, "set_output_source", channel_source == obs_scene_get_source(s.scene),
            "obs_set_output_source(0, scene)");
    if (channel_source) {
        obs_source_release(channel_source);
    }

    // ---- video encoder: x264 H.264 -----------------------------------------
    obs_data_t* vsettings = obs_data_create();
    obs_data_set_string(vsettings, "rate_control", "CBR");
    obs_data_set_int(vsettings, "bitrate", 8000);
    obs_data_set_int(vsettings, "keyint_sec", 1); // 1 s GOP => split at 5 s boundaries
    obs_data_set_string(vsettings, "preset", "veryfast");
    obs_data_set_string(vsettings, "profile", "high");
    obs_data_set_string(vsettings, "x264opts", "scenecut=0");
    s.video_encoder = obs_video_encoder_create("obs_x264", "S1b H264", vsettings, nullptr);
    obs_data_release(vsettings);
    AddStep(s, "create_video_encoder", s.video_encoder != nullptr, "obs_x264 (CBR 8000 kbps, keyint=1 s)");
    if (!s.video_encoder) {
        return false;
    }
    obs_encoder_set_video(s.video_encoder, obs_get_video());

    // ---- audio encoder: FFmpeg AAC ----------------------------------------
    obs_data_t* asettings = obs_data_create();
    obs_data_set_int(asettings, "bitrate", 160);
    s.audio_encoder = obs_audio_encoder_create("ffmpeg_aac", "S1b AAC", asettings, 0, nullptr);
    obs_data_release(asettings);
    AddStep(s, "create_audio_encoder", s.audio_encoder != nullptr, "ffmpeg_aac (160 kbps, mix 0)");
    if (!s.audio_encoder) {
        return false;
    }
    obs_encoder_set_audio(s.audio_encoder, obs_get_audio());

    // ---- Hybrid MP4 output with split_file / max_time_sec ------------------
    obs_data_t* osettings = obs_data_create();
    obs_data_set_string(osettings, "path", first_file_path.c_str());
    obs_data_set_string(osettings, "directory", s.opt.out_dir.c_str());
    obs_data_set_string(osettings, "format", "clip-%hh%mm%ss");
    obs_data_set_string(osettings, "extension", "mp4");
    obs_data_set_bool(osettings, "allow_spaces", false);
    obs_data_set_bool(osettings, "split_file", true);
    obs_data_set_int(osettings, "max_time_sec", s.opt.max_time_sec);
    obs_data_set_int(osettings, "max_size_mb", 0);
    obs_data_set_bool(osettings, "allow_overwrite", true);
    obs_data_set_string(osettings, "muxer_settings", "");
    s.output_settings = osettings;
    s.output = obs_output_create("mp4_output", "S1b Split MP4", s.output_settings, nullptr);
    AddStep(s, "create_mp4_output", s.output != nullptr,
            "mp4_output split_file=true max_time_sec=" + std::to_string(s.opt.max_time_sec));
    if (!s.output) {
        return false;
    }

    obs_output_set_video_encoder(s.output, s.video_encoder);
    obs_output_set_audio_encoder(s.output, s.audio_encoder, 0);
    AddStep(s, "attach_encoders", true, "obs_output_set_video_encoder / obs_output_set_audio_encoder");

    // The Hybrid MP4 muxer emits a "file_changed" signal on split; declare it
    // locally so the callback can record the exact split file names.
    s.output_signals = obs_output_get_signal_handler(s.output);
    signal_handler_add(s.output_signals, "void file_changed(ptr output, string next_file)");
    signal_handler_connect(s.output_signals, "starting", OnOutputStarting, nullptr);
    signal_handler_connect(s.output_signals, "start", OnOutputStart, nullptr);
    signal_handler_connect(s.output_signals, "stopping", OnOutputStopping, nullptr);
    signal_handler_connect(s.output_signals, "stop", OnOutputStop, nullptr);
    signal_handler_connect(s.output_signals, "deactivate", OnOutputDeactivate, nullptr);
    signal_handler_connect(s.output_signals, "file_changed", OnOutputFileChanged, nullptr);
    AddStep(s, "connect_output_signals", true,
            "start/stop/starting/stopping/deactivate/file_changed");

    return true;
}

bool RunOutput(State& s)
{
    ResetSignalGlobals();
    s.total_frames_drawn_at_start = obs_get_total_frames();
    s.t_start_ns = os_gettime_ns();

    if (!obs_output_start(s.output)) {
        s.output_last_error = CurrentOutputLastError(s.output);
        AddStep(s, "output_start", false, "obs_output_start=false; last_error=" + s.output_last_error);
        return false;
    }
    s.output_started = true;

    // Wait for the output to become active (data capture started).
    const uint64_t active_deadline = os_gettime_ns() + 5000000000ULL;
    while (os_gettime_ns() < active_deadline) {
        if (obs_output_active(s.output)) {
            s.output_active_seen = true;
            s.t_output_active_ns = os_gettime_ns();
            break;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    AddStep(s, "output_active", s.output_active_seen,
            s.output_active_seen ? "obs_output_active()=true" : "timeout waiting for active output");
    if (!s.output_active_seen) {
        s.output_last_error = CurrentOutputLastError(s.output);
        return false;
    }

    // Record for the requested duration.  The source thread keeps pushing
    // frames/PCM for the whole time, including while the output drains.
    const uint64_t run_deadline = s.t_output_active_ns + static_cast<uint64_t>(s.opt.duration_ms) * 1000000ULL;
    while (os_gettime_ns() < run_deadline) {
        if (!obs_output_active(s.output)) {
            AddStep(s, "output_stayed_active", false,
                    "output stopped unexpectedly after " +
                        std::to_string((os_gettime_ns() - s.t_output_active_ns) / 1000000ULL) + " ms");
            return false;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    AddStep(s, "record_duration", true,
            std::to_string((os_gettime_ns() - s.t_output_active_ns) / 1000000ULL) + " ms requested=" +
                std::to_string(s.opt.duration_ms) + " ms");

    s.total_frames_drawn_at_stop = obs_get_total_frames();
    s.lagged_frames_at_stop = obs_get_lagged_frames();
    s.output_total_bytes = obs_output_get_total_bytes(s.output);
    s.output_total_frames = obs_output_get_total_frames(s.output);
    s.output_frames_dropped = obs_output_get_frames_dropped(s.output);

    // ---- stop: mp4_output finalises the current segment on the next packet --
    s.t_stop_request_ns = os_gettime_ns();
    obs_output_stop(s.output);
    AddStep(s, "output_stop_request", true, "obs_output_stop()");

    const uint64_t stop_deadline = os_gettime_ns() + 10000000000ULL;
    while (os_gettime_ns() < stop_deadline) {
        if (!obs_output_active(s.output)) {
            s.output_stopped = true;
            s.t_output_inactive_ns = os_gettime_ns();
            break;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    if (!s.output_stopped) {
        s.output_last_error = CurrentOutputLastError(s.output);
        AddStep(s, "output_stopped", false, "timeout waiting for output to stop");
        obs_output_force_stop(s.output);
        return false;
    }
    AddStep(s, "output_stopped", true,
            "drain " + std::to_string((s.t_output_inactive_ns - s.t_stop_request_ns) / 1000000ULL) + " ms");

    // The MP4 files are finalized at this point.  Stop the synthetic media
    // producer now so nothing is pushed into libobs while the context is being
    // torn down (the source object itself is released by Cleanup()).
    a3::StopAvTestSourceThread();
    AddStep(s, "source_thread_stopped", true, "in-memory AV source worker stopped after output drain");

    // stop event details (code is set from mp4_output's end_data_capture path)
    s.stop_signal_seen = g_stop_signal_seen.load();
    s.output_stop_code = g_stop_code.load();
    AddStep(s, "output_stop_signal", s.stop_signal_seen,
            "stop code=" + std::to_string(s.output_stop_code) + " (0=OBS_OUTPUT_SUCCESS)");

    s.output_last_error = CurrentOutputLastError(s.output);
    s.video_encoder_last_error = CurrentEncoderLastError(s.video_encoder);
    s.audio_encoder_last_error = CurrentEncoderLastError(s.audio_encoder);
    AddStep(s, "no_output_errors", s.output_last_error.empty() && s.video_encoder_last_error.empty() &&
                                       s.audio_encoder_last_error.empty(),
            "output='" + s.output_last_error + "' video='" + s.video_encoder_last_error + "' audio='" +
                s.audio_encoder_last_error + "'");

    // Applied settings (read back from libobs, not just our inputs).
    if (s.output) {
        obs_data_t* applied = obs_output_get_settings(s.output);
        const char* json = obs_data_get_json(applied);
        s.applied_output_settings_json = json ? json : "";
        obs_data_release(applied);
    }
    if (s.video_encoder) {
        obs_data_t* applied = obs_encoder_get_settings(s.video_encoder);
        const char* json = obs_data_get_json(applied);
        s.applied_video_encoder_settings_json = json ? json : "";
        obs_data_release(applied);
    }
    if (s.audio_encoder) {
        obs_data_t* applied = obs_encoder_get_settings(s.audio_encoder);
        const char* json = obs_data_get_json(applied);
        s.applied_audio_encoder_settings_json = json ? json : "";
        obs_data_release(applied);
    }

    s.files = ListMp4Files(s.opt.out_dir);
    AddStep(s, "split_files_ge_3", s.files.size() >= 3,
            std::to_string(s.files.size()) + " mp4 file(s) in " + s.opt.out_dir);
    return true;
}

void CollectFinalEvidence(State& s)
{
    s.process_modules = EnumProcessModuleNames();

    const char* name = nullptr;
    const char* id = nullptr;
    obs_get_audio_monitoring_device(&name, &id);
    s.monitoring_device_name = name ? name : "";
    s.monitoring_device_id = id ? id : "";

    // Default monitoring type is OBS_MONITORING_TYPE_NONE; we never call
    // obs_source_set_monitoring_type().  Recording the observed value is the
    // in-process proof that no monitoring sink was created.
    if (s.source) {
        s.source_monitoring_type = static_cast<int>(obs_source_get_monitoring_type(s.source));
    }

    const bool has_mmdevapi = HasModule(s.process_modules, "MMDevAPI.dll");
    const bool has_audioses = HasModule(s.process_modules, "audioses.dll");
    const bool has_wdmaud = HasModule(s.process_modules, "wdmaud.drv");
    const bool has_avrt = HasModule(s.process_modules, "avrt.dll");
    AddStep(s, "no_wasapi_endpoint_modules", !has_mmdevapi && !has_audioses && !has_wdmaud,
            "MMDevAPI=" + std::string(has_mmdevapi ? "loaded" : "absent") +
                " audioses=" + std::string(has_audioses ? "loaded" : "absent") +
                " wdmaud=" + std::string(has_wdmaud ? "loaded" : "absent") +
                " avrt(thread prio only)=" + std::string(has_avrt ? "loaded" : "absent"));
    AddStep(s, "monitoring_type_none", s.source_monitoring_type == OBS_MONITORING_TYPE_NONE,
            "obs_source_get_monitoring_type=" + std::to_string(s.source_monitoring_type) +
                " (NONE=" + std::to_string(OBS_MONITORING_TYPE_NONE) + ")");

    const a3::AvSourceStats stats = a3::GetAvSourceStats();
    const uint64_t expected_video_frames =
        static_cast<uint64_t>(s.opt.duration_ms) * 60ULL / 1000ULL;
    AddStep(s, "source_pushed_media", stats.created && stats.video_frames_pushed >= expected_video_frames &&
                                          stats.audio_frames_pushed >= 40000,
            "video_frames=" + std::to_string(stats.video_frames_pushed) +
                " audio_chunks=" + std::to_string(stats.audio_chunks_pushed) +
                " audio_frames=" + std::to_string(stats.audio_frames_pushed));
}

// ---------------------------------------------------------------------------
// report
// ---------------------------------------------------------------------------

void WriteReport(State& s, int exit_code)
{
    a3::JsonWriter w(true, 2);
    w.begin_object();
    w.key("tool");
    w.value_string("obs-media-selftest");
    w.key("mode");
    w.value_string("s1b-in-memory-av-to-split-mp4");
    w.key("status");
    w.value_string(exit_code == 0 ? "pass" : "fail");
    w.key("exit_code");
    w.value_int(exit_code);
    w.key("timestamp_utc");
    w.value_string(UtcNowIso());

    w.key("host");
    w.begin_object();
    {
        std::wstring exe(32768, L'\0');
        const DWORD n = GetModuleFileNameW(nullptr, exe.data(), static_cast<DWORD>(exe.size()));
        exe.resize(n);
        w.key("exe_path");
        w.value_string(WideToUtf8(exe));
        w.key("pid");
        w.value_uint(GetCurrentProcessId());
        w.key("obs_version");
        w.value_string(s.obs_version);
        w.key("obs_api_version");
        w.value_int(static_cast<long long>(s.obs_api_version));
        w.key("obs_dir");
        w.value_string(s.opt.obs_dir);
        w.key("graphics_module");
        w.value_string(s.graphics_module);
    }
    w.end_object();

    w.key("requested_config");
    w.begin_object();
    w.key("video_width");
    w.value_int(1920);
    w.key("video_height");
    w.value_int(1080);
    w.key("fps_num");
    w.value_int(60);
    w.key("fps_den");
    w.value_int(1);
    w.key("video_format");
    w.value_string("I420");
    w.key("audio_samples_per_sec");
    w.value_int(44100);
    w.key("audio_speakers");
    w.value_string("SPEAKERS_STEREO");
    w.key("video_encoder");
    w.value_string("obs_x264");
    w.key("audio_encoder");
    w.value_string("ffmpeg_aac");
    w.key("output");
    w.value_string("mp4_output (Hybrid MP4)");
    w.key("split_file");
    w.value_bool(true);
    w.key("max_time_sec");
    w.value_int(s.opt.max_time_sec);
    w.key("record_duration_ms");
    w.value_int(s.opt.duration_ms);
    w.end_object();

    w.key("modules");
    w.begin_array();
    for (const auto& mr : s.modules) {
        w.begin_object();
        w.key("file");
        w.value_string(mr.file);
        w.key("path");
        w.value_string(mr.path);
        w.key("data_path");
        w.value_string(mr.data_path);
        w.key("open_rc");
        w.value_int(mr.open_rc);
        w.key("init_ok");
        w.value_bool(mr.init_ok);
        w.end_object();
    }
    w.end_array();

    w.key("steps");
    w.begin_array();
    for (const auto& st : s.steps) {
        w.begin_object();
        w.key("name");
        w.value_string(st.name);
        w.key("ok");
        w.value_bool(st.ok);
        w.key("detail");
        w.value_string(st.detail);
        w.end_object();
    }
    w.end_array();

    w.key("output_events");
    w.begin_array();
    {
        std::lock_guard<std::mutex> lock(g_events_mutex);
        for (const auto& ev : g_events) {
            w.begin_object();
            w.key("name");
            w.value_string(ev.name);
            w.key("detail");
            w.value_string(ev.detail);
            w.key("t_ns");
            w.value_uint(ev.t_ns);
            if (s.t_output_active_ns && ev.t_ns >= s.t_output_active_ns) {
                w.key("ms_since_output_active");
                w.value_int(static_cast<long long>((ev.t_ns - s.t_output_active_ns) / 1000000ULL));
            }
            w.end_object();
        }
    }
    w.end_array();

    w.key("media");
    w.begin_object();
    w.key("output_settings_json");
    w.value_string(s.applied_output_settings_json);
    w.key("video_encoder_settings_json");
    w.value_string(s.applied_video_encoder_settings_json);
    w.key("audio_encoder_settings_json");
    w.value_string(s.applied_audio_encoder_settings_json);
    w.key("output_last_error");
    w.value_string(s.output_last_error);
    w.key("video_encoder_last_error");
    w.value_string(s.video_encoder_last_error);
    w.key("audio_encoder_last_error");
    w.value_string(s.audio_encoder_last_error);
    w.key("stop_code");
    w.value_int(s.output_stop_code);
    w.key("stop_signal_seen");
    w.value_bool(s.stop_signal_seen);
    w.key("stats");
    w.begin_object();
    w.key("total_frames_drawn_at_start");
    w.value_uint(s.total_frames_drawn_at_start);
    w.key("total_frames_drawn_at_stop");
    w.value_uint(s.total_frames_drawn_at_stop);
    w.key("lagged_frames_at_stop");
    w.value_uint(s.lagged_frames_at_stop);
    w.key("output_total_bytes");
    w.value_uint(s.output_total_bytes);
    w.key("output_total_frames");
    w.value_int(s.output_total_frames);
    w.key("output_frames_dropped");
    w.value_int(s.output_frames_dropped);
    w.key("record_ms");
    w.value_int(s.output_active_seen ? static_cast<long long>((s.t_stop_request_ns - s.t_output_active_ns) / 1000000ULL)
                                     : 0);
    w.key("drain_ms");
    w.value_int(s.output_stopped ? static_cast<long long>((s.t_output_inactive_ns - s.t_stop_request_ns) / 1000000ULL)
                                 : -1);
    w.end_object();
    w.key("audio");
    w.begin_object();
    w.key("source_monitoring_type");
    w.value_int(s.source_monitoring_type);
    w.key("monitoring_type_none_value");
    w.value_int(OBS_MONITORING_TYPE_NONE);
    w.key("default_monitoring_device_name");
    w.value_string(s.monitoring_device_name);
    w.key("default_monitoring_device_id");
    w.value_string(s.monitoring_device_id);
    w.end_object();
    w.key("source_pushed");
    w.begin_object();
    {
        const a3::AvSourceStats stats = a3::GetAvSourceStats();
        w.key("video_frames");
        w.value_uint(stats.video_frames_pushed);
        w.key("audio_chunks");
        w.value_uint(stats.audio_chunks_pushed);
        w.key("audio_frames");
        w.value_uint(stats.audio_frames_pushed);
    }
    w.end_object();
    w.key("files");
    w.begin_array();
    for (const auto& f : s.files) {
        w.begin_object();
        w.key("name");
        w.value_string(f.name);
        w.key("size_bytes");
        w.value_uint(f.size);
        w.key("mtime_unix");
        w.value_uint(f.mtime_unix);
        w.end_object();
    }
    w.end_array();
    w.end_object();

    w.key("process_modules");
    w.begin_object();
    w.key("count");
    w.value_uint(s.process_modules.size());
    w.key("has_mmdevapi");
    w.value_bool(HasModule(s.process_modules, "MMDevAPI.dll"));
    w.key("has_audioses");
    w.value_bool(HasModule(s.process_modules, "audioses.dll"));
    w.key("has_wdmaud");
    w.value_bool(HasModule(s.process_modules, "wdmaud.drv"));
    w.key("has_avrt");
    w.value_bool(HasModule(s.process_modules, "avrt.dll"));
    w.key("all");
    w.begin_array();
    for (const auto& m : s.process_modules) {
        w.value_string(m);
    }
    w.end_array();
    w.end_object();

    w.end_object();

    std::string json = w.str();
    std::string path = s.opt.json_out.empty() ? JoinPath(s.opt.out_dir, "selftest.json") : s.opt.json_out;
    FILE* f = nullptr;
    if (fopen_s(&f, path.c_str(), "wb") == 0 && f) {
        std::fwrite(json.data(), 1, json.size(), f);
        std::fclose(f);
    }
    std::printf("REPORT path=%s status=%s steps=%zu files=%zu\n", path.c_str(),
                AllStepsOk(s) ? "pass" : "fail", s.steps.size(), s.files.size());
    std::fflush(stdout);
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

void PrintUsage()
{
    std::printf("usage: obs-media-selftest.exe --out-dir DIR [options]\n"
                "  --out-dir DIR        output directory for split MP4s + selftest.json (required)\n"
                "  --obs-dir DIR        installed OBS root (default: C:\\Program Files\\obs-studio)\n"
                "  --duration-ms N      record duration in ms (default 17000)\n"
                "  --max-time-sec N     split max_time_sec (default 5)\n"
                "  --json-out FILE      report path (default: <out-dir>\\selftest.json)\n");
}

bool ParseArgs(int argc, char** argv, Options& opt, int& exit_code)
{
    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        auto next = [&](const char* name) -> std::string {
            if (i + 1 >= argc) {
                std::fprintf(stderr, "missing value for %s\n", name);
                exit_code = 2;
                return {};
            }
            return argv[++i];
        };
        if (a == "--out-dir") {
            opt.out_dir = next("--out-dir");
        } else if (a == "--obs-dir") {
            opt.obs_dir = next("--obs-dir");
        } else if (a == "--duration-ms") {
            opt.duration_ms = std::atoi(next("--duration-ms").c_str());
        } else if (a == "--max-time-sec") {
            opt.max_time_sec = std::atoi(next("--max-time-sec").c_str());
        } else if (a == "--json-out") {
            opt.json_out = next("--json-out");
        } else if (a == "--help" || a == "-h") {
            PrintUsage();
            exit_code = 0;
            return false;
        } else {
            std::fprintf(stderr, "unknown argument: %s\n", a.c_str());
            PrintUsage();
            exit_code = 2;
            return false;
        }
        if (exit_code != 0) {
            return false;
        }
    }
    if (opt.out_dir.empty()) {
        std::fprintf(stderr, "--out-dir is required\n");
        PrintUsage();
        exit_code = 2;
        return false;
    }
    if (opt.duration_ms < 1000 || opt.duration_ms > 120000) {
        std::fprintf(stderr, "--duration-ms out of range\n");
        exit_code = 2;
        return false;
    }
    if (opt.max_time_sec < 1 || opt.max_time_sec > 60) {
        std::fprintf(stderr, "--max-time-sec out of range\n");
        exit_code = 2;
        return false;
    }
    return true;
}

} // namespace

int main(int argc, char** argv)
{
    State state;
    int exit_code = 0;
    if (!ParseArgs(argc, argv, state.opt, exit_code)) {
        return exit_code;
    }

    std::printf("obs-media-selftest: libobs=linked obs_dir=%s out_dir=%s duration_ms=%d max_time_sec=%d\n",
                state.opt.obs_dir.c_str(), state.opt.out_dir.c_str(), state.opt.duration_ms,
                state.opt.max_time_sec);
    std::fflush(stdout);

    if (!EnsureDir(state.opt.out_dir)) {
        std::fprintf(stderr, "cannot create out dir: %s\n", state.opt.out_dir.c_str());
        state.steps.push_back(Step{"create_out_dir", false, state.opt.out_dir});
        WriteReport(state, 2);
        Cleanup(state);
        return 2;
    }
    state.steps.push_back(Step{"create_out_dir", true, state.opt.out_dir});

    const std::string first_file = JoinPath(state.opt.out_dir, "clip-000.mp4");

    int rc = 0;
    if (!InitObs(state)) {
        rc = 3;
    } else if (!CreatePipeline(state, first_file)) {
        rc = 4;
    } else if (!RunOutput(state)) {
        rc = 5;
    } else {
        CollectFinalEvidence(state);
        if (!AllStepsOk(state)) {
            rc = 6;
        }
    }

    WriteReport(state, rc);
    Cleanup(state);
    std::printf("SELFTEST_EXIT %d\n", rc);
    std::fflush(stdout);
    return rc;
}
