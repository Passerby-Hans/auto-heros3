// S1b/S1c media self-test host for auto-heros3.
//
// What this executable does (and only this):
//   1. links against libobs 32.1.0 through a generated import library
//      (obs.lib built from the installed obs.dll) and the OBS 32.1.0 source
//      headers (vendor/obs-studio/libobs + generated obsconfig.h);
//   2. obs_startup() + explicit selective module load (never
//      obs_load_all_modules, no Qt/UI/frontend plugin):
//        - always: obs-ffmpeg.dll, obs-x264.dll, obs-outputs.dll
//        - window mode only: win-capture.dll, loaded *after* obs_reset_video()
//          so the plugin detects the D3D11 graphics context and enables WGC
//          (plugins/win-capture/plugin-main.c obs_module_load:
//          graphics_uses_d3d11 + win_version_compare(win1903) -> wgc_supported);
//   3. obs_reset_audio(44100 Hz stereo) + obs_reset_video(1920x1080@60, I420,
//      D3D11 graphics module from the installed OBS bin directory);
//   4. creates one source, puts it in a scene and assigns the scene to main
//      canvas channel 0 (obs_set_output_source):
//        - default mode "in-memory": synthetic async I420 video + 440 Hz PCM
//          (S1b; src/media/gen_av_source.cpp);
//        - mode "window": real OBS "window_capture" source forced to method=2
//          (WGC), client_area=true, capture_audio=false, targeting a window
//          found with libobs' ms_find_window()/ms_get_window_*() helpers
//          (S1c). No audio capture and no audio playback.
//   5. creates H.264 (obs_x264) + AAC (ffmpeg_aac) encoders and an
//      "mp4_output" (Hybrid MP4) output with split_file=true and the requested
//      max_time_sec, records for the requested duration and stops cleanly;
//   6. writes a machine-readable report (selftest.json) next to the produced
//      MP4s: loaded modules, applied source/encoder/output settings, output and
//      source signals/events, frame/byte counters, produced files, the process
//      module list (no WASAPI/MMDevAPI endpoint) and, in window mode, the
//      actual capture method reported by the win-capture plugin log.
//
// There is no audio monitoring/playback, no physical audio output and no OBS
// UI/WebSocket anywhere in this path.
#include <windows.h>

#include <tlhelp32.h>

// obs.h must come before obs-source.h: obs.h includes obs-source.h itself and
// the latter is not self-sufficient under `#pragma once` if entered first.
#include <obs.h>
#include <obs-source.h>

#include <util/platform.h>
#include <util/windows/window-helpers.h>

#include <util/base.h>
#include <util/dstr.h>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "../json_util.hpp"
#include "gen_av_source.hpp"
#include "raw_audio_source.hpp"

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
// libobs log capture (for actual capture-method evidence)
//
// The win-capture plugin logs "[window-capture: 'name'] update settings:
// method selected: ... method chosen: ..." (plugins/win-capture/window-capture.c
// log_settings()).  We install a log handler that both forwards to the previous
// (default stdout) handler and keeps a copy in memory, so the host can record
// the *actual* method chosen by the plugin ("WGC" vs "BitBlt") in selftest.json
// instead of only grepping stdout externally.  base_set_log_handler() /
// base_get_log_handler() are exported by obs.dll (libobs/util/base.h).
// ---------------------------------------------------------------------------

std::mutex g_log_mutex;
std::vector<std::string> g_log_lines;
log_handler_t g_prev_log_handler = nullptr;
void* g_prev_log_handler_param = nullptr;

void CaptureLogHandler(int lvl, const char* msg, va_list args, void* param)
{
    {
        va_list copy;
        va_copy(copy, args);
        char buf[8192];
        const int n = std::vsnprintf(buf, sizeof(buf), msg, copy);
        va_end(copy);
        std::lock_guard<std::mutex> lock(g_log_mutex);
        if (g_log_lines.size() < 200000) {
            g_log_lines.emplace_back(n > 0 ? std::string(buf, static_cast<size_t>(std::min<int>(n, static_cast<int>(sizeof(buf)) - 1))) : std::string());
        }
    }
    if (g_prev_log_handler) {
        g_prev_log_handler(lvl, msg, args, g_prev_log_handler_param);
    }
}

void InstallLogCapture()
{
    base_get_log_handler(&g_prev_log_handler, &g_prev_log_handler_param);
    base_set_log_handler(CaptureLogHandler, nullptr);
}

void UninstallLogCapture()
{
    base_set_log_handler(g_prev_log_handler, g_prev_log_handler_param);
}

std::vector<std::string> LogSnapshot()
{
    std::lock_guard<std::mutex> lock(g_log_mutex);
    return g_log_lines;
}

std::string FindLogLineValue(const std::vector<std::string>& lines, const std::string& needle,
                             const std::string& value_prefix)
{
    for (const auto& line : lines) {
        const size_t pos = line.find(needle);
        if (pos == std::string::npos) {
            continue;
        }
        const size_t vpos = line.find(value_prefix, pos);
        if (vpos == std::string::npos) {
            continue;
        }
        std::string value = line.substr(vpos + value_prefix.size());
        const size_t end = value.find_first_of("\r\n");
        if (end != std::string::npos) {
            value.resize(end);
        }
        while (!value.empty() && (value.front() == ' ' || value.front() == '\t')) {
            value.erase(value.begin());
        }
        while (!value.empty() && (value.back() == ' ' || value.back() == '\t')) {
            value.pop_back();
        }
        return value;
    }
    return {};
}

// ---------------------------------------------------------------------------
// run state
// ---------------------------------------------------------------------------

enum class RunMode {
    InMemory, // S1b: synthetic async AV source
    WindowWgc // S1c: real OBS window_capture source, forced WGC method
};

struct Options {
    std::string obs_dir = "C:\\Program Files\\obs-studio";
    std::string out_dir;
    int duration_ms = 17000;
    int max_time_sec = 5;
    std::string json_out;
    RunMode mode = RunMode::InMemory;
    bool duration_explicit = false;
    bool max_time_explicit = false;

    // window mode target selection (S1c)
    std::string window_title = "VCMI WGC Synthetic";
    std::string window_class;
    std::string window_exe;
    int window_pid = 0;

    // S2a: raw VCMI game-audio.raw file as a native libobs audio source
    std::string audio_raw;            // empty => no raw audio source
    bool audio_start_zero = false;    // default: start at EOF (live semantics)

    // S3a: persistent named-pipe IPC server (libobs stays initialized)
    bool serve = false;
    std::string pipe_name;
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
    signal_handler_t* source_signals = nullptr; // window_capture hooked/unhooked
    obs_sceneitem_t* scene_item = nullptr;      // owned by the scene

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

    // S1c window-capture evidence
    bool win_capture_module_loaded = false;
    bool window_found = false;
    uintptr_t window_hwnd = 0;
    int window_pid_found = 0;
    std::string window_title_found;
    std::string window_class_found;
    std::string window_exe_found;
    std::string window_lookup; // how the target was resolved
    std::string applied_source_settings_json;
    std::string capture_method_selected;
    std::string capture_method_chosen;
    bool capture_signal_hooked = false;
    bool capture_proc_hooked = false;
    std::string capture_state_title;
    std::string capture_state_class;
    std::string capture_state_exe;
    uint32_t capture_width = 0;
    uint32_t capture_height = 0;
    std::vector<std::string> capture_log_lines;

    // S2a raw-file audio source evidence
    bool audio_enabled = false;
    obs_source_t* audio_source = nullptr;
    std::string audio_applied_settings_json;
    int audio_source_monitoring_type = -1;
    float audio_source_volume = 0.0f;
    a3::RawAudioStats audio_stats;

    // exit code to use when a specific step fails (0 => stage default)
    int failure_exit_code = 0;

    // S3a IPC serve mode evidence
    bool serve_mode = false;
    std::string serve_pipe;
    std::string serve_session_id;
};

// defined in the evidence/helpers section below
void AddStep(State& s, const std::string& name, bool ok, const std::string& detail = "");

std::atomic<bool> g_window_hooked {false};
std::mutex g_hook_mutex;
std::string g_hook_detail;

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

// Source signal callbacks for the win-capture "window_capture" source
// (plugins/win-capture/window-capture.c wc_create() declares
// "void hooked(ptr source, string title, string class, string executable)" and
// "void unhooked(ptr source)"; the signals fire when the capture object is
// initialised successfully).
void OnWindowHooked(void*, calldata_t* cd)
{
    const char* title = calldata_string(cd, "title");
    const char* cls = calldata_string(cd, "class");
    const char* exe = calldata_string(cd, "executable");
    std::string detail = std::string("title='") + (title ? title : "") + "' class='" +
                         (cls ? cls : "") + "' exe='" + (exe ? exe : "") + "'";
    {
        std::lock_guard<std::mutex> lock(g_hook_mutex);
        g_hook_detail = detail;
    }
    g_window_hooked.store(true);
    RecordEvent("window_capture_hooked", detail);
}

void OnWindowUnhooked(void*, calldata_t*)
{
    RecordEvent("window_capture_unhooked", "");
}

std::string WindowTitle(HWND hwnd)
{
    struct dstr str = {};
    ms_get_window_title(&str, hwnd);
    std::string out = str.array ? str.array : "";
    dstr_free(&str);
    return out;
}

std::string WindowClass(HWND hwnd)
{
    struct dstr str = {};
    ms_get_window_class(&str, hwnd);
    std::string out = str.array ? str.array : "";
    dstr_free(&str);
    return out;
}

std::string WindowExe(HWND hwnd)
{
    struct dstr str = {};
    if (!ms_get_window_exe(&str, hwnd)) {
        dstr_free(&str);
        return {};
    }
    std::string out = str.array ? str.array : "";
    dstr_free(&str);
    return out;
}

// The OBS window source settings store the target as an escaped
// "title:class:exe" string (libobs/util/windows/window-helpers.c
// ms_build_window_strings()/decode_str(): '#' -> "#22", ':' -> "#3A"; emptied
// fields are kept as empty strings).  Passing the class is mandatory for the
// WGC path: window-capture.c wc_tick() calls ms_find_window_top_level() for
// METHOD_WGC, and that helper returns NULL when class is NULL.
std::string EncodeWindowField(const std::string& value)
{
    std::string out;
    out.reserve(value.size() + 8);
    for (char c : value) {
        if (c == '#') {
            out += "#22";
        } else if (c == ':') {
            out += "#3A";
        } else {
            out += c;
        }
    }
    return out;
}

std::string BuildObsWindowString(const std::string& title, const std::string& cls, const std::string& exe)
{
    return EncodeWindowField(title) + ":" + EncodeWindowField(cls) + ":" + EncodeWindowField(exe);
}

struct PidWindowFind {
    DWORD pid = 0;
    std::string want_title;
    std::string want_class;
    HWND result = nullptr;
};

BOOL CALLBACK PidWindowFindProc(HWND hwnd, LPARAM param)
{
    auto* find = reinterpret_cast<PidWindowFind*>(param);
    DWORD pid = 0;
    GetWindowThreadProcessId(hwnd, &pid);
    if (pid != find->pid) {
        return TRUE;
    }
    if (!IsWindowVisible(hwnd) || IsIconic(hwnd)) {
        return TRUE;
    }
    if (!find->want_title.empty() && astrcmpi(WindowTitle(hwnd).c_str(), find->want_title.c_str()) != 0) {
        return TRUE;
    }
    if (!find->want_class.empty() && astrcmpi(WindowClass(hwnd).c_str(), find->want_class.c_str()) != 0) {
        return TRUE;
    }
    find->result = hwnd;
    return FALSE;
}

// Resolves the capture target through libobs' window helpers, records the
// attributes read back through ms_get_window_*() and (for window_capture
// matching) builds the escaped window string.  Returns false with an explicit
// window_not_found step when no matching window exists.
bool FindTargetWindow(State& s)
{
    HWND hwnd = nullptr;
    std::string prio_name;

    if (s.opt.window_pid > 0) {
        PidWindowFind find;
        find.pid = static_cast<DWORD>(s.opt.window_pid);
        find.want_title = s.opt.window_title;
        find.want_class = s.opt.window_class;
        EnumWindows(PidWindowFindProc, reinterpret_cast<LPARAM>(&find));
        hwnd = find.result;
        s.window_lookup = "EnumWindows(pid=" + std::to_string(s.opt.window_pid) + ", title='" +
                          s.opt.window_title + "', class='" + s.opt.window_class + "')";
    } else {
        // ms_find_window(mode, priority, class, title, exe): pick the match
        // priority from the fields the caller supplied.
        enum window_priority priority = WINDOW_PRIORITY_TITLE;
        const char* cls = nullptr;
        const char* title = nullptr;
        const char* exe = nullptr;
        if (!s.opt.window_title.empty()) {
            title = s.opt.window_title.c_str();
            priority = WINDOW_PRIORITY_TITLE;
            prio_name = "title";
        } else if (!s.opt.window_class.empty()) {
            cls = s.opt.window_class.c_str();
            priority = WINDOW_PRIORITY_CLASS;
            prio_name = "class";
        } else if (!s.opt.window_exe.empty()) {
            exe = s.opt.window_exe.c_str();
            priority = WINDOW_PRIORITY_EXE;
            prio_name = "exe";
        } else {
            s.failure_exit_code = 7;
            AddStep(s, "window_find", false,
                    "no window selector supplied (need --window-title/--window-class/--window-exe/--window-pid)");
            return false;
        }
        hwnd = ms_find_window(INCLUDE_MINIMIZED, priority, cls, title, exe);
        s.window_lookup = std::string("ms_find_window(priority=") + prio_name + ", title='" +
                          s.opt.window_title + "', class='" + s.opt.window_class + "', exe='" +
                          s.opt.window_exe + "')";
    }

    if (!hwnd) {
        s.failure_exit_code = 7;
        AddStep(s, "window_find", false, "window_not_found: " + s.window_lookup);
        std::printf("WINDOW_NOT_FOUND lookup=%s\n", s.window_lookup.c_str());
        std::fflush(stdout);
        return false;
    }

    s.window_found = true;
    s.window_hwnd = reinterpret_cast<uintptr_t>(hwnd);
    DWORD pid = 0;
    GetWindowThreadProcessId(hwnd, &pid);
    s.window_pid_found = static_cast<int>(pid);
    s.window_title_found = WindowTitle(hwnd);
    s.window_class_found = WindowClass(hwnd);
    s.window_exe_found = WindowExe(hwnd);

    bool expected_ok = true;
    std::string mismatch;
    if (!s.opt.window_title.empty() && astrcmpi(s.window_title_found.c_str(), s.opt.window_title.c_str()) != 0) {
        expected_ok = false;
        mismatch += " title expected='" + s.opt.window_title + "' actual='" + s.window_title_found + "'";
    }
    if (!s.opt.window_class.empty() && astrcmpi(s.window_class_found.c_str(), s.opt.window_class.c_str()) != 0) {
        expected_ok = false;
        mismatch += " class expected='" + s.opt.window_class + "' actual='" + s.window_class_found + "'";
    }
    if (!s.opt.window_exe.empty() && astrcmpi(s.window_exe_found.c_str(), s.opt.window_exe.c_str()) != 0) {
        expected_ok = false;
        mismatch += " exe expected='" + s.opt.window_exe + "' actual='" + s.window_exe_found + "'";
    }

    AddStep(s, "window_find", expected_ok,
            "hwnd=" + std::to_string(s.window_hwnd) + " pid=" + std::to_string(s.window_pid_found) +
                " title='" + s.window_title_found + "' class='" + s.window_class_found + "' exe='" +
                s.window_exe_found + "'" + mismatch);
    if (!expected_ok) {
        s.failure_exit_code = 7;
        return false;
    }
    return true;
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

void AddStep(State& s, const std::string& name, bool ok, const std::string& detail)
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

    // Safety net for error paths: make sure the synthetic media producer and
    // the raw-audio reader are stopped before any libobs object is released.
    // On the normal path this was already done right after the output drained
    // (idempotent).
    a3::StopAvTestSourceThread();
    a3::StopRawAudioSourceThread();

    // Detach the scene from the main canvas channel first (the channel holds a
    // strong reference to the scene source, libobs/obs-canvas.c
    // obs_canvas_set_channel => obs_source_get_ref).
    if (s.obs_started && s.scene) {
        obs_set_output_source(0, nullptr);
        trace("detached_output_source");

        // The main canvas also keeps a strong reference to scene sources
        // (PROGRAM canvas has SCENE_REF, libobs/obs-canvas.c
        // obs_canvas_insert_source); remove the scene from the canvas so it can
        // actually be destroyed before obs_shutdown().  This matters for
        // plugin-provided sources (win-capture window_capture): plugin modules
        // are unloaded inside obs_shutdown() before sources are freed, so a
        // window_capture source must be torn down while win-capture.dll code is
        // still mapped.
        obs_canvas_scene_remove(s.scene);
        trace("scene_removed_from_canvas");
    }

    if (s.source_signals) {
        signal_handler_disconnect(s.source_signals, "hooked", OnWindowHooked, nullptr);
        signal_handler_disconnect(s.source_signals, "unhooked", OnWindowUnhooked, nullptr);
        s.source_signals = nullptr;
        trace("source_signals_disconnected");
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

    // Release the host references to the source and the scene.  Because the
    // scene was removed from the canvas above, this is the last reference and
    // libobs defers the actual destroy to its destruction task thread; flush
    // that queue while video/audio/graphics are still alive so plugin source
    // destroy callbacks (window_capture queues its graphics destroy here) run
    // before obs_shutdown() unloads the plugin modules.
    if (s.source) {
        obs_source_release(s.source);
        s.source = nullptr;
        trace("source_released");
    }
    if (s.audio_source) {
        obs_source_release(s.audio_source);
        s.audio_source = nullptr;
        trace("audio_source_released");
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
        // Cascade: scene destroy -> item release -> source destroy -> (for
        // window_capture) a graphics task.  obs_wait_for_destroy_queue() flushes
        // graphics+audio task queues and the destroy queue once; a few rounds
        // are required for the cascade.  In-memory mode additionally verifies
        // its own source destroy callback ran (AvSourceData counter cleared).
        int flush_iters = 0;
        const int max_flush = (s.opt.mode == RunMode::InMemory) ? 50 : 10;
        while (flush_iters < max_flush) {
            obs_wait_for_destroy_queue();
            ++flush_iters;
            if (s.opt.mode == RunMode::InMemory && !a3::GetAvSourceStats().created) {
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(15));
        }
        std::printf("CLEANUP destroy_flush_iters=%d\n", flush_iters);
        std::fflush(stdout);
        trace("destroy_queue_flushed");

        obs_shutdown();
        s.obs_started = false;
        trace("obs_shutdown_done");
    }
    trace("end");
}

ModuleResult LoadOneModule(State& s, const char* file)
{
    const std::string bin_dir = JoinPath(s.opt.obs_dir, "obs-plugins\\64bit");
    const std::string data_root = JoinPath(s.opt.obs_dir, "data\\obs-plugins");

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
        std::printf("MODULE %-20s missing=%s\n", file, mr.path.c_str());
        std::fflush(stdout);
        s.modules.push_back(mr);
        return mr;
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
    return mr;
}

bool LoadBaseModules(State& s)
{
    // Explicit whitelist only.  obs_load_all_modules() would also load the Qt
    // UI/frontend plugins and other unrelated modules; S0 already showed that
    // must be avoided for a headless host.
    const char* kFiles[] = {"obs-ffmpeg.dll", "obs-x264.dll", "obs-outputs.dll"};
    bool all_ok = true;
    for (const char* file : kFiles) {
        const ModuleResult mr = LoadOneModule(s, file);
        all_ok = all_ok && mr.init_ok;
    }
    if (all_ok) {
        obs_post_load_modules();
    }
    return all_ok;
}

// win-capture.dll must be loaded AFTER obs_reset_video(): its obs_module_load()
// calls obs_enter_graphics()/gs_get_device_type() and only enables WGC when the
// graphics subsystem is already D3D11 (plugins/win-capture/plugin-main.c:
// graphics_uses_d3d11 + wgc_supported).  Loading it earlier permanently forces
// the BitBlt fallback for this process.
bool LoadWinCaptureModule(State& s)
{
    const ModuleResult mr = LoadOneModule(s, "win-capture.dll");
    s.win_capture_module_loaded = mr.init_ok;
    AddStep(s, "load_win_capture_module", mr.init_ok,
            std::string("win-capture.dll open_rc=") + std::to_string(mr.open_rc) +
                " (loaded after obs_reset_video so WGC is enabled)");
    if (mr.init_ok) {
        obs_post_load_modules();
    }
    return mr.init_ok;
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

    if (!LoadBaseModules(s)) {
        AddStep(s, "load_selective_modules", false, "at least one whitelisted module failed to initialize");
        s.failure_exit_code = 3;
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
    if (!s.video_reset_ok) {
        s.failure_exit_code = 3;
        return false;
    }

    if (s.opt.mode == RunMode::WindowWgc) {
        if (!LoadWinCaptureModule(s)) {
            s.failure_exit_code = 3;
            return false;
        }
    }
    return true;
}

bool CreateEncodersAndOutput(State& s, const std::string& first_file_path)
{
    const bool window_mode = (s.opt.mode == RunMode::WindowWgc);
    const std::string prefix = window_mode ? "S1c" : "S1b";

    // ---- video encoder: x264 H.264 -----------------------------------------
    obs_data_t* vsettings = obs_data_create();
    obs_data_set_string(vsettings, "rate_control", "CBR");
    obs_data_set_int(vsettings, "bitrate", 8000);
    obs_data_set_int(vsettings, "keyint_sec", 1); // 1 s GOP => splits at keyframes
    obs_data_set_string(vsettings, "preset", "veryfast");
    obs_data_set_string(vsettings, "profile", "high");
    obs_data_set_string(vsettings, "x264opts", "scenecut=0");
    s.video_encoder = obs_video_encoder_create("obs_x264", (prefix + " H264").c_str(), vsettings, nullptr);
    obs_data_release(vsettings);
    AddStep(s, "create_video_encoder", s.video_encoder != nullptr,
            "obs_x264 (CBR 8000 kbps, keyint=1 s, veryfast, high)");
    if (!s.video_encoder) {
        s.failure_exit_code = 4;
        return false;
    }
    obs_encoder_set_video(s.video_encoder, obs_get_video());

    // ---- audio encoder: FFmpeg AAC ----------------------------------------
    // In window mode the window_capture source has capture_audio=false, so this
    // track stays silent; it is kept so the produced MP4s always have the same
    // A/V container shape as the product code path.
    obs_data_t* asettings = obs_data_create();
    obs_data_set_int(asettings, "bitrate", 160);
    s.audio_encoder = obs_audio_encoder_create("ffmpeg_aac", (prefix + " AAC").c_str(), asettings, 0, nullptr);
    obs_data_release(asettings);
    AddStep(s, "create_audio_encoder", s.audio_encoder != nullptr, "ffmpeg_aac (160 kbps, mix 0)");
    if (!s.audio_encoder) {
        s.failure_exit_code = 4;
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
    s.output = obs_output_create("mp4_output", (prefix + " Split MP4").c_str(), s.output_settings, nullptr);
    AddStep(s, "create_mp4_output", s.output != nullptr,
            "mp4_output split_file=true max_time_sec=" + std::to_string(s.opt.max_time_sec));
    if (!s.output) {
        s.failure_exit_code = 4;
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

bool CreateSceneForSource(State& s, const char* scene_name)
{
    s.scene = obs_scene_create(scene_name);
    AddStep(s, "create_scene", s.scene != nullptr, std::string("obs_scene_create('") + scene_name + "')");
    if (!s.scene) {
        return false;
    }

    obs_sceneitem_t* item = obs_scene_add(s.scene, s.source);
    AddStep(s, "scene_add_source", item != nullptr, "obs_scene_add");
    if (!item) {
        return false;
    }
    s.scene_item = item;

    obs_set_output_source(0, obs_scene_get_source(s.scene));
    // obs_get_output_source() returns a strong reference (libobs/obs-canvas.c
    // obs_view_get_source -> obs_source_get_ref); release it again or the
    // scene source can never be destroyed and the source stays alive until
    // obs_shutdown(), which crashed at teardown in the pre-S1b-initial run.
    obs_source_t* channel_source = obs_get_output_source(0);
    AddStep(s, "set_output_source", channel_source == obs_scene_get_source(s.scene),
            "obs_set_output_source(0, scene)");
    if (channel_source) {
        obs_source_release(channel_source);
    }
    return true;
}

bool CreatePipeline(State& s, const std::string& first_file_path)
{
    // S1b in-memory synthetic media source (no capture at all).
    AddStep(s, "mode", true, "mode=in-memory (synthetic async I420 video + 440 Hz PCM)");
    a3::RegisterAvTestSource();
    AddStep(s, "register_test_source", true, "obs_register_source_s(a3_test_av_source)");

    obs_data_t* source_settings = obs_data_create();
    s.source = obs_source_create("a3_test_av_source", "S1b In-Memory Test AV", source_settings, nullptr);
    obs_data_release(source_settings);
    AddStep(s, "create_test_source", s.source != nullptr, "async video (I420 1920x1080@60) + PCM audio");
    if (!s.source) {
        s.failure_exit_code = 4;
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

    if (!CreateSceneForSource(s, "S1b Scene")) {
        s.failure_exit_code = 4;
        return false;
    }
    return CreateEncodersAndOutput(s, first_file_path);
}

// Polls the window_capture source until its capture object is initialised
// (window-capture.c wc_tick(): first window lookup happens after
// WC_CHECK_TIMER=1.0 s of ticks, then the WGC capture object is created and the
// source signals "hooked"), then records the observed dimensions and the actual
// capture method reported by the plugin log.
bool WaitForWindowCapture(State& s)
{
    const uint64_t start_ns = os_gettime_ns();
    const uint64_t deadline = start_ns + 12000000000ULL; // 12 s
    uint32_t width = 0;
    uint32_t height = 0;
    while (os_gettime_ns() < deadline) {
        width = obs_source_get_width(s.source);
        height = obs_source_get_height(s.source);
        // "hooked" fires when the capture object was created; the WGC frame
        // size is only known once the first frame has been delivered, so wait
        // for both the hook signal and a non-zero source size.
        if (g_window_hooked.load() && width > 0 && height > 0) {
            break;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    const uint64_t waited_ms = (os_gettime_ns() - start_ns) / 1000000ULL;

    s.capture_signal_hooked = g_window_hooked.load();
    s.capture_width = obs_source_get_width(s.source);
    s.capture_height = obs_source_get_height(s.source);

    // proc handler exposed by window-capture.c:
    //   "void get_hooked(out bool hooked, out string title, out string class,
    //                   out string executable)"
    proc_handler_t* ph = obs_source_get_proc_handler(s.source);
    calldata_t* cd = calldata_create();
    if (ph && proc_handler_call(ph, "get_hooked", cd)) {
        s.capture_proc_hooked = calldata_bool(cd, "hooked");
        const char* title = calldata_string(cd, "title");
        const char* cls = calldata_string(cd, "class");
        const char* exe = calldata_string(cd, "executable");
        s.capture_state_title = title ? title : "";
        s.capture_state_class = cls ? cls : "";
        s.capture_state_exe = exe ? exe : "";
    }
    calldata_destroy(cd);

    // Actual capture method comes from the win-capture plugin's own log
    // (window-capture.c log_settings() prints "method selected:" and
    // "method chosen:"; choose_method() falls back to BitBlt when WGC is not
    // supported, so never assume WGC without checking this).
    const std::vector<std::string> logs = LogSnapshot();
    s.capture_method_selected = FindLogLineValue(logs, "method selected:", "method selected:");
    s.capture_method_chosen = FindLogLineValue(logs, "method chosen:", "method chosen:");
    {
        std::lock_guard<std::mutex> lock(g_log_mutex);
        for (const auto& line : g_log_lines) {
            if (line.find("window-capture") != std::string::npos ||
                line.find("method selected:") != std::string::npos ||
                line.find("method chosen:") != std::string::npos ||
                line.find("WGC") != std::string::npos ||
                line.find("BitBlt") != std::string::npos ||
                line.find("libobs-winrt") != std::string::npos ||
                line.find("winrt_capture") != std::string::npos) {
                if (s.capture_log_lines.size() < 300) {
                    s.capture_log_lines.push_back(line);
                }
            }
        }
    }

    const bool hooked = s.capture_signal_hooked || s.capture_proc_hooked ||
                        (s.capture_width > 0 && s.capture_height > 0);
    AddStep(s, "window_capture_hooked", hooked,
            "signal_hooked=" + std::to_string(s.capture_signal_hooked ? 1 : 0) +
                " proc_hooked=" + std::to_string(s.capture_proc_hooked ? 1 : 0) +
                " waited_ms=" + std::to_string(waited_ms) + " title='" + s.capture_state_title + "'");
    const bool wgc = (s.capture_method_chosen == "WGC");
    AddStep(s, "capture_method_wgc", wgc,
            "method selected='" + s.capture_method_selected + "' chosen='" + s.capture_method_chosen +
                "' (from win-capture plugin log; BitBlt is a fallback, not WGC)");
    const bool dimensions_ok = s.capture_width > 0 && s.capture_height > 0;
    AddStep(s, "capture_dimensions", dimensions_ok,
            std::to_string(s.capture_width) + "x" + std::to_string(s.capture_height));
    return hooked && wgc && dimensions_ok;
}

// S2a: create the native raw-file audio source (VCMI game-audio.raw) and add it
// to the same scene as the WGC window source.  Audio-only: no monitoring, no
// audio device, no playback.  The source retries the open itself; the host waits
// here and turns "file never opened" into an explicit failure (exit 9).
bool CreateRawAudioSource(State& s)
{
    if (s.opt.audio_raw.empty()) {
        return true;
    }
    s.audio_enabled = true;
    AddStep(s, "audio_raw_mode", true,
            std::string("raw s16le/44100/2 file -> FLOAT_PLANAR async audio source; start_at=") +
                (s.opt.audio_start_zero ? "0 (test)" : "EOF (live)") + " path='" + s.opt.audio_raw + "'");

    a3::RegisterRawAudioSource();
    AddStep(s, "register_raw_audio_source", true, "obs_register_source_s(a3_raw_audio)");

    obs_data_t* settings = obs_data_create();
    obs_data_set_string(settings, "path", s.opt.audio_raw.c_str());
    obs_data_set_bool(settings, "start_at_zero", s.opt.audio_start_zero);
    s.audio_source = obs_source_create("a3_raw_audio", "S2a Raw VCMI Game Audio", settings, nullptr);
    obs_data_release(settings);
    AddStep(s, "create_raw_audio_source", s.audio_source != nullptr,
            "a3_raw_audio (OBS_SOURCE_AUDIO|OBS_SOURCE_ASYNC)");
    if (!s.audio_source) {
        s.failure_exit_code = 9;
        return false;
    }
    const uint32_t audio_flags = s.audio_source ? obs_source_get_output_flags(s.audio_source) : 0;
    AddStep(s, "raw_audio_flags",
            (audio_flags & OBS_SOURCE_AUDIO) != 0 && (audio_flags & OBS_SOURCE_ASYNC) != 0,
            "output_flags=0x" + [](uint32_t v) {
                char buf[16];
                std::snprintf(buf, sizeof(buf), "%x", v);
                return std::string(buf);
            }(audio_flags) + " (AUDIO=0x2, ASYNC=0x4)");

    obs_data_t* applied = obs_source_get_settings(s.audio_source);
    const char* applied_json = obs_data_get_json(applied);
    s.audio_applied_settings_json = applied_json ? applied_json : "";
    obs_data_release(applied);
    s.audio_source_monitoring_type = static_cast<int>(obs_source_get_monitoring_type(s.audio_source));
    s.audio_source_volume = obs_source_get_volume(s.audio_source);
    AddStep(s, "raw_audio_settings", true, s.audio_applied_settings_json);
    AddStep(s, "raw_audio_monitoring_none",
            s.audio_source_monitoring_type == OBS_MONITORING_TYPE_NONE,
            "monitoring_type=" + std::to_string(s.audio_source_monitoring_type) + " (NONE=" +
                std::to_string(OBS_MONITORING_TYPE_NONE) + ") volume=" +
                std::to_string(s.audio_source_volume) + " mixers=" +
                std::to_string(obs_source_get_audio_mixers(s.audio_source)));

    obs_sceneitem_t* item = obs_scene_add(s.scene, s.audio_source);
    AddStep(s, "scene_add_audio_source", item != nullptr, "audio source added to the same S1c scene");
    if (!item) {
        s.failure_exit_code = 9;
        return false;
    }

    // Wait for the reader thread to open the file.  Missing files are retried by
    // the source; the host fails explicitly instead of recording silent audio
    // for a wrong path.
    const uint64_t deadline = os_gettime_ns() + 3000000000ULL;
    a3::RawAudioStats stats;
    while (os_gettime_ns() < deadline) {
        stats = a3::GetRawAudioStats();
        if (stats.opened) {
            break;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    s.audio_stats = stats;
    AddStep(s, "raw_audio_opened", stats.opened,
            "path='" + s.opt.audio_raw + "' opened=" + std::to_string(stats.opened ? 1 : 0) +
                " attempts=" + std::to_string(stats.open_attempts) +
                " start_offset=" + std::to_string(stats.start_offset) +
                " error='" + stats.open_error + "'");
    if (!stats.opened) {
        s.failure_exit_code = 9;
        return false;
    }
    return true;
}

bool CreateWindowPipeline(State& s, const std::string& first_file_path)
{
    AddStep(s, "mode", true, "mode=window (win-capture window_capture forced method=WGC)");
    if (!FindTargetWindow(s)) {
        return false;
    }

    // The window source stores its target as the escaped "title:class:exe"
    // string from libobs/util/windows/window-helpers.c ms_build_window_strings().
    // All three fields are filled from the actual HWND attributes read back with
    // ms_get_window_title/class/exe so the plugin re-finds exactly this window.
    const std::string window_string =
        BuildObsWindowString(s.window_title_found, s.window_class_found, s.window_exe_found);
    obs_data_t* settings = obs_data_create();
    obs_data_set_string(settings, "window", window_string.c_str());
    obs_data_set_int(settings, "method", 2);          // METHOD_WGC
    obs_data_set_int(settings, "priority", 1);        // WINDOW_PRIORITY_TITLE
    obs_data_set_bool(settings, "client_area", true);
    obs_data_set_bool(settings, "capture_audio", false);
    obs_data_set_bool(settings, "cursor", false);
    // Force SDR: our canvas/output is SDR Rec.709.  Without this, on an HDR
    // monitor libobs-winrt requests R16G16B16A16_FLOAT frames and reports
    // GS_CS_709_EXTENDED (winrt-capture.cpp get_pixel_format /
    // winrt_capture_get_color_space), which is not what this SDR pipeline is
    // meant to exercise; force_sdr makes WGC deliver BGRA SDR frames.
    obs_data_set_bool(settings, "force_sdr", true);
    obs_data_set_bool(settings, "compatibility", false);
    s.source = obs_source_create("window_capture", "S1c WGC Window", settings, nullptr);
    obs_data_release(settings);
    AddStep(s, "create_window_capture_source", s.source != nullptr,
            "window_capture method=2 (WGC) client_area=true capture_audio=false window='" + window_string + "'");
    if (!s.source) {
        s.failure_exit_code = 8;
        return false;
    }

    // Read back the applied settings (libobs merges plugin defaults).
    obs_data_t* applied = obs_source_get_settings(s.source);
    const char* applied_json = obs_data_get_json(applied);
    s.applied_source_settings_json = applied_json ? applied_json : "";
    obs_data_release(applied);
    s.source_monitoring_type = static_cast<int>(obs_source_get_monitoring_type(s.source));

    // window_capture declares these signals in wc_create(); "hooked" fires when
    // the capture object was initialised successfully (WGC: after
    // winrt_capture_init_window()).
    s.source_signals = obs_source_get_signal_handler(s.source);
    signal_handler_connect(s.source_signals, "hooked", OnWindowHooked, nullptr);
    signal_handler_connect(s.source_signals, "unhooked", OnWindowUnhooked, nullptr);
    AddStep(s, "connect_window_capture_signals", true, "hooked / unhooked");

    if (!CreateSceneForSource(s, "S1c Scene")) {
        s.failure_exit_code = 8;
        return false;
    }
    if (!WaitForWindowCapture(s)) {
        s.failure_exit_code = 8;
        return false;
    }

    // Scale the captured window to fill the 1920x1080 canvas so nonblack /
    // frame-change validation covers the whole output frame instead of only the
    // top-left window-sized region.  This is a test-host presentation choice
    // (non-uniform stretch) and is recorded in selftest.json.
    if (s.scene_item && s.capture_width > 0 && s.capture_height > 0) {
        struct vec2 scale = {};
        scale.x = 1920.0f / static_cast<float>(s.capture_width);
        scale.y = 1080.0f / static_cast<float>(s.capture_height);
        obs_sceneitem_set_scale(s.scene_item, &scale);
        AddStep(s, "scene_item_scale_fill_canvas", true,
                "scale=" + std::to_string(scale.x) + "x" + std::to_string(scale.y) +
                    " (window " + std::to_string(s.capture_width) + "x" +
                    std::to_string(s.capture_height) + " -> canvas 1920x1080)");
    }
    if (!CreateRawAudioSource(s)) {
        return false;
    }
    return CreateEncodersAndOutput(s, first_file_path);
}

bool StartOutput(State& s)
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
    return true;
}

// CLI path: hold the output open for the requested duration.  IPC mode does not
// use this; it keeps serving requests while the output runs.
bool RecordForDuration(State& s)
{
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
    return true;
}

bool StopOutput(State& s)
{
    if (!s.output) {
        AddStep(s, "output_stop_request", false, "no output object");
        return false;
    }
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

    if (s.opt.mode == RunMode::InMemory) {
        // The MP4 files are finalized at this point.  Stop the synthetic media
        // producer now so nothing is pushed into libobs while the context is
        // being torn down (the source object itself is released by Cleanup()).
        a3::StopAvTestSourceThread();
        AddStep(s, "source_thread_stopped", true, "in-memory AV source worker stopped after output drain");
    } else {
        AddStep(s, "window_capture_after_stop", true,
                "window_capture source is a synchronous render source (no worker thread of ours)");
    }
    if (s.audio_enabled) {
        // Stop the raw-audio reader after the output drained (idempotent), then
        // snapshot its counters for the report.
        a3::StopRawAudioSourceThread();
        s.audio_stats = a3::GetRawAudioStats();
        AddStep(s, "raw_audio_after_stop", true,
                "opened=" + std::to_string(s.audio_stats.opened ? 1 : 0) +
                    " bytes_read=" + std::to_string(s.audio_stats.bytes_read) +
                    " chunks=" + std::to_string(s.audio_stats.chunks_pushed) +
                    " silence_chunks=" + std::to_string(s.audio_stats.silence_chunks) +
                    " samples=" + std::to_string(s.audio_stats.samples_pushed));
    }

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

bool RunOutput(State& s)
{
    if (!StartOutput(s)) {
        return false;
    }
    if (!RecordForDuration(s)) {
        return false;
    }
    return StopOutput(s);
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

    if (s.opt.mode == RunMode::InMemory) {
        const a3::AvSourceStats stats = a3::GetAvSourceStats();
        const uint64_t expected_video_frames =
            static_cast<uint64_t>(s.opt.duration_ms) * 60ULL / 1000ULL;
        AddStep(s, "source_pushed_media",
                stats.created && stats.video_frames_pushed >= expected_video_frames &&
                    stats.audio_frames_pushed >= 40000,
                "video_frames=" + std::to_string(stats.video_frames_pushed) +
                    " audio_chunks=" + std::to_string(stats.audio_chunks_pushed) +
                    " audio_frames=" + std::to_string(stats.audio_frames_pushed));
    } else {
        // Window mode: the source is a real OBS window_capture source.  Verify
        // the applied settings prove WGC + no audio capture, that the source is
        // still hooked, and that no WASAPI capture stack got loaded (unlike an
        // audio-capturing configuration).
        const bool settings_ok = s.applied_source_settings_json.find("\"method\":2") != std::string::npos &&
                                 s.applied_source_settings_json.find("\"capture_audio\":false") != std::string::npos &&
                                 s.applied_source_settings_json.find("\"client_area\":true") != std::string::npos;
        AddStep(s, "window_capture_settings_applied", settings_ok, s.applied_source_settings_json);
        AddStep(s, "window_capture_still_hooked", s.capture_proc_hooked,
                "proc get_hooked=" + std::to_string(s.capture_proc_hooked ? 1 : 0) + " title='" +
                    s.capture_state_title + "' class='" + s.capture_state_class + "'");
        AddStep(s, "window_capture_no_audio_capture",
                !HasModule(s.process_modules, "MMDevAPI.dll") &&
                    !HasModule(s.process_modules, "audioses.dll") &&
                    !HasModule(s.process_modules, "win-wasapi.dll"),
                "no MMDevAPI/audioses/win-wasapi module loaded (capture_audio=false)");

        if (s.audio_enabled) {
            s.audio_stats = a3::GetRawAudioStats();
            AddStep(s, "raw_audio_reader_stats",
                    s.audio_stats.opened &&
                        (s.audio_stats.samples_pushed > 0 || s.audio_stats.silence_chunks > 0),
                    "opened=" + std::to_string(s.audio_stats.opened ? 1 : 0) +
                        " start_offset=" + std::to_string(s.audio_stats.start_offset) +
                        " bytes_read=" + std::to_string(s.audio_stats.bytes_read) +
                        " frames_read=" + std::to_string(s.audio_stats.frames_read) +
                        " chunks=" + std::to_string(s.audio_stats.chunks_pushed) +
                        " silence_chunks=" + std::to_string(s.audio_stats.silence_chunks) +
                        " eof_waits=" + std::to_string(s.audio_stats.eof_waits));
            AddStep(s, "raw_audio_data_pushed_when_start_zero",
                    !s.opt.audio_start_zero || s.audio_stats.samples_pushed > 0,
                    std::string("start_at_zero=") + (s.opt.audio_start_zero ? "true" : "false") +
                        " samples_pushed=" + std::to_string(s.audio_stats.samples_pushed) +
                        " (EOF mode may legitimately be silent if the file did not grow)");
            AddStep(s, "raw_audio_monitoring_none_final",
                    s.audio_source_monitoring_type == OBS_MONITORING_TYPE_NONE,
                    "volume=" + std::to_string(s.audio_source_volume) + " mixers=" +
                        std::to_string(s.audio_source ? obs_source_get_audio_mixers(s.audio_source) : 0) +
                        " monitoring_type=" + std::to_string(s.audio_source_monitoring_type));
            AddStep(s, "raw_audio_no_playback_device",
                    !HasModule(s.process_modules, "MMDevAPI.dll") &&
                        !HasModule(s.process_modules, "audioses.dll") &&
                        !HasModule(s.process_modules, "win-wasapi.dll"),
                    "raw file audio source loaded without WASAPI/MMDevice endpoint (no playback/monitoring)");
        }
    }
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
    w.value_string(s.serve_mode
                       ? "s3a-ipc-wgc-window-plus-raw-file-audio-to-split-mp4"
                       : (s.opt.mode == RunMode::WindowWgc
                              ? (s.audio_enabled ? "s2a-wgc-window-plus-raw-file-audio-to-split-mp4"
                                                 : "s1c-wgc-window-capture-to-split-mp4")
                              : "s1b-in-memory-av-to-split-mp4"));
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
        w.key("run_mode");
        w.value_string(s.serve_mode ? "serve" : (s.opt.mode == RunMode::WindowWgc ? "window" : "in-memory"));
        w.key("win_capture_module_loaded");
        w.value_bool(s.win_capture_module_loaded);
        w.key("raw_audio_enabled");
        w.value_bool(s.audio_enabled);
        w.key("serve_mode");
        w.value_bool(s.serve_mode);
        w.key("serve_pipe");
        w.value_string(s.serve_pipe);
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
    if (s.opt.mode == RunMode::WindowWgc) {
        w.key("window_target");
        w.begin_object();
        w.key("window_title");
        w.value_string(s.opt.window_title);
        w.key("window_class");
        w.value_string(s.opt.window_class);
        w.key("window_exe");
        w.value_string(s.opt.window_exe);
        w.key("window_pid");
        w.value_int(s.opt.window_pid);
        w.end_object();
    }
    if (s.audio_enabled) {
        w.key("audio_source");
        w.begin_object();
        w.key("type");
        w.value_string("a3_raw_audio (s16le interleaved -> FLOAT_PLANAR, async audio-only)");
        w.key("path");
        w.value_string(s.opt.audio_raw);
        w.key("start_at_zero");
        w.value_bool(s.opt.audio_start_zero);
        w.key("sample_rate");
        w.value_int(44100);
        w.key("channels");
        w.value_int(2);
        w.key("chunk_frames");
        w.value_int(4410);
        w.end_object();
    }
    if (s.serve_mode) {
        w.key("ipc");
        w.begin_object();
        w.key("protocol");
        w.value_int(1);
        w.key("pipe");
        w.value_string(s.serve_pipe);
        w.key("session_id");
        w.value_string(s.serve_session_id);
        w.key("commands");
        w.begin_array();
        for (const char* c : {"hello", "capabilities", "prepare", "start", "status", "stop", "shutdown"})
            w.value_string(c);
        w.end_array();
        w.end_object();
    }
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

    // S1c window-capture evidence (window mode only).  "method_chosen" comes
    // from the win-capture plugin's own log line, which is the only reliable
    // way to distinguish WGC from the BitBlt fallback.
    w.key("window_capture");
    w.begin_object();
    w.key("enabled");
    w.value_bool(s.opt.mode == RunMode::WindowWgc);
    w.key("module_loaded");
    w.value_bool(s.win_capture_module_loaded);
    w.key("lookup");
    w.value_string(s.window_lookup);
    w.key("found");
    w.value_bool(s.window_found);
    w.key("hwnd");
    w.value_string([&s] {
        char buf[32];
        std::snprintf(buf, sizeof(buf), "0x%llx", static_cast<unsigned long long>(s.window_hwnd));
        return std::string(buf);
    }());
    w.key("pid");
    w.value_int(s.window_pid_found);
    w.key("title");
    w.value_string(s.window_title_found);
    w.key("class");
    w.value_string(s.window_class_found);
    w.key("exe");
    w.value_string(s.window_exe_found);
    w.key("source_settings_json");
    w.value_string(s.applied_source_settings_json);
    w.key("capture_width");
    w.value_uint(s.capture_width);
    w.key("capture_height");
    w.value_uint(s.capture_height);
    w.key("canvas_scale");
    w.begin_object();
    w.key("x");
    w.value_double(s.capture_width ? 1920.0 / static_cast<double>(s.capture_width) : 0.0, 4);
    w.key("y");
    w.value_double(s.capture_height ? 1080.0 / static_cast<double>(s.capture_height) : 0.0, 4);
    w.end_object();
    w.key("hooked_signal");
    w.value_bool(s.capture_signal_hooked);
    w.key("hooked_proc");
    w.value_bool(s.capture_proc_hooked);
    w.key("hook_title");
    w.value_string(s.capture_state_title);
    w.key("hook_class");
    w.value_string(s.capture_state_class);
    w.key("hook_exe");
    w.value_string(s.capture_state_exe);
    w.key("method_selected");
    w.value_string(s.capture_method_selected);
    w.key("method_chosen");
    w.value_string(s.capture_method_chosen);
    w.key("capture_audio");
    w.value_bool(false);
    w.key("client_area");
    w.value_bool(true);
    w.key("log_lines");
    w.begin_array();
    for (const auto& line : s.capture_log_lines) {
        w.value_string(line);
    }
    w.end_array();
    w.end_object();

    // S2a raw-file audio source evidence (window+audio mode only).
    w.key("raw_audio");
    w.begin_object();
    w.key("enabled");
    w.value_bool(s.audio_enabled);
    w.key("path");
    w.value_string(s.opt.audio_raw);
    w.key("start_at_zero");
    w.value_bool(s.opt.audio_start_zero);
    w.key("source_settings_json");
    w.value_string(s.audio_applied_settings_json);
    w.key("output_flags");
    w.value_int(s.audio_source ? static_cast<long long>(obs_source_get_output_flags(s.audio_source)) : 0);
    w.key("source_monitoring_type");
    w.value_int(s.audio_source_monitoring_type);
    w.key("monitoring_type_none_value");
    w.value_int(OBS_MONITORING_TYPE_NONE);
    w.key("source_volume");
    w.value_double(s.audio_source_volume, 3);
    w.key("audio_mixers");
    w.value_int(s.audio_source ? static_cast<long long>(obs_source_get_audio_mixers(s.audio_source)) : 0);
    w.key("opened");
    w.value_bool(s.audio_stats.opened);
    w.key("open_error");
    w.value_string(s.audio_stats.open_error);
    w.key("open_attempts");
    w.value_uint(s.audio_stats.open_attempts);
    w.key("start_offset");
    w.value_uint(s.audio_stats.start_offset);
    w.key("file_size_at_open");
    w.value_uint(s.audio_stats.file_size_at_open);
    w.key("bytes_read");
    w.value_uint(s.audio_stats.bytes_read);
    w.key("frames_read");
    w.value_uint(s.audio_stats.frames_read);
    w.key("chunks_pushed");
    w.value_uint(s.audio_stats.chunks_pushed);
    w.key("silence_chunks");
    w.value_uint(s.audio_stats.silence_chunks);
    w.key("eof_waits");
    w.value_uint(s.audio_stats.eof_waits);
    w.key("samples_pushed");
    w.value_uint(s.audio_stats.samples_pushed);
    w.key("bytes_per_second_expected");
    w.value_int(176400); // 44100 frames/s * 2ch * 2 bytes
    w.end_object();

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
    w.key("raw_audio_source_monitoring_type");
    w.value_int(s.audio_source_monitoring_type);
    w.key("raw_audio_source_volume");
    w.value_double(s.audio_source_volume, 3);
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
    w.key("has_win_capture");
    w.value_bool(HasModule(s.process_modules, "win-capture.dll"));
    w.key("has_libobs_winrt");
    w.value_bool(HasModule(s.process_modules, "libobs-winrt.dll"));
    w.key("has_libcurl");
    w.value_bool(HasModule(s.process_modules, "libcurl.dll"));
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
// S3a: named-pipe IPC server (libobs initialised once; one recording per process)
// ---------------------------------------------------------------------------

using a3::JsonWriter;
using a3::json_extract_bool;
using a3::json_extract_int;
using a3::json_extract_string;

enum class ServePhase { Idle, Prepared, Recording, Stopped, Failed };

const char* ServePhaseName(ServePhase phase)
{
    switch (phase) {
    case ServePhase::Idle:
        return "idle";
    case ServePhase::Prepared:
        return "prepared";
    case ServePhase::Recording:
        return "recording";
    case ServePhase::Stopped:
        return "stopped";
    case ServePhase::Failed:
        return "failed";
    }
    return "unknown";
}

struct IpcRequest {
    std::string raw;
    std::string cmd;
    bool has_request_id = false;
    bool request_id_is_int = false;
    std::string request_id;
    long long request_id_int = 0;
};

struct ServeSession {
    ServePhase phase = ServePhase::Idle;
    bool shutdown = false;
    bool ever_prepared = false;
    bool ever_started = false;
    bool checks_ok = false;
    bool auto_stopped = false;
    int report_rc = -1;
    std::string last_error_code;
    std::string last_error_message;
    std::string session_id;
    std::string pipe;
    std::string selftest_path;
    std::string manifest_path;
    std::string started_utc;
    std::string stopped_utc;
    uint64_t started_ns = 0;
    uint64_t auto_stop_deadline_ns = 0;
    uint64_t serve_started_ns = 0;
    int requests = 0;
    int seq = 0;
};

struct IpcReply {
    std::string line;
    bool shutdown = false;
};

void WriteRequestId(JsonWriter& w, const IpcRequest& req)
{
    if (!req.has_request_id) {
        w.value_null();
    } else if (req.request_id_is_int) {
        w.value_int(req.request_id_int);
    } else {
        w.value_string(req.request_id);
    }
}

bool ParseIpcRequest(const std::string& raw, IpcRequest& req, std::string& err)
{
    req = IpcRequest{};
    req.raw = raw;
    std::size_t first = raw.find_first_not_of(" \t\r\n");
    if (first == std::string::npos || raw[first] != '{') {
        err = "request is not a JSON object";
        return false;
    }
    bool found = false;
    if (!json_extract_string(raw, "cmd", &req.cmd, &found) || !found || req.cmd.empty()) {
        err = "missing/invalid \"cmd\" (string required)";
        return false;
    }
    std::string id;
    if (!json_extract_string(raw, "request_id", &id, &found)) {
        err = "invalid \"request_id\" (string)";
        return false;
    }
    if (found) {
        req.has_request_id = true;
        req.request_id = id;
    } else {
        long long id_int = 0;
        if (!json_extract_int(raw, "request_id", &id_int, &found)) {
            err = "invalid \"request_id\"";
            return false;
        }
        if (found) {
            req.has_request_id = true;
            req.request_id_is_int = true;
            req.request_id_int = id_int;
        }
    }
    return true;
}

std::string ReplyOk(ServeSession& sess, const IpcRequest& req, const char* state,
                    const std::function<void(JsonWriter&)>& fill)
{
    JsonWriter w(false);
    w.begin_object();
    w.key("ok");
    w.value_bool(true);
    w.key("request_id");
    WriteRequestId(w, req);
    w.key("cmd");
    w.value_string(req.cmd);
    w.key("state");
    w.value_string(state);
    w.key("server");
    w.value_string("obs-media-selftest");
    w.key("protocol");
    w.value_int(1);
    w.key("seq");
    w.value_int(++sess.seq);
    if (fill)
        fill(w);
    w.end_object();
    return w.str();
}

std::string ReplyError(ServeSession& sess, const IpcRequest& req, const std::string& code,
                       const std::string& message)
{
    sess.last_error_code = code;
    sess.last_error_message = message;
    JsonWriter w(false);
    w.begin_object();
    w.key("ok");
    w.value_bool(false);
    w.key("request_id");
    WriteRequestId(w, req);
    w.key("cmd");
    w.value_string(req.cmd);
    w.key("state");
    w.value_string(ServePhaseName(sess.phase));
    w.key("server");
    w.value_string("obs-media-selftest");
    w.key("protocol");
    w.value_int(1);
    w.key("seq");
    w.value_int(++sess.seq);
    w.key("error");
    w.begin_object();
    w.key("code");
    w.value_string(code);
    w.key("message");
    w.value_string(message);
    w.end_object();
    w.end_object();
    return w.str();
}

bool GetStringArg(const IpcRequest& req, const char* key, std::string* out, bool* found,
                  std::string& err)
{
    bool f = false;
    if (!json_extract_string(req.raw, key, out, &f)) {
        err = std::string("invalid \"") + key + "\" (string required)";
        return false;
    }
    if (found)
        *found = f;
    return true;
}

bool GetIntArg(const IpcRequest& req, const char* key, long long* out, bool* found,
               std::string& err)
{
    bool f = false;
    if (!json_extract_int(req.raw, key, out, &f)) {
        err = std::string("invalid \"") + key + "\" (integer required)";
        return false;
    }
    if (found)
        *found = f;
    return true;
}

void WriteWindowJson(JsonWriter& w, const State& s)
{
    w.begin_object();
    w.key("found");
    w.value_bool(s.window_found);
    w.key("hwnd");
    w.value_uintptr(s.window_hwnd);
    w.key("pid");
    w.value_int(s.window_pid_found);
    w.key("title");
    w.value_string(s.window_title_found);
    w.key("class");
    w.value_string(s.window_class_found);
    w.key("exe");
    w.value_string(s.window_exe_found);
    w.key("capture_width");
    w.value_uint(s.capture_width);
    w.key("capture_height");
    w.value_uint(s.capture_height);
    w.end_object();
}

void WriteRawAudioStatsJson(JsonWriter& w, const a3::RawAudioStats& st, bool enabled)
{
    w.begin_object();
    w.key("enabled");
    w.value_bool(enabled);
    w.key("path");
    w.value_string(st.path);
    w.key("opened");
    w.value_bool(st.opened);
    w.key("open_error");
    w.value_string(st.open_error);
    w.key("open_attempts");
    w.value_uint(st.open_attempts);
    w.key("start_offset");
    w.value_uint(st.start_offset);
    w.key("file_size_at_open");
    w.value_uint(st.file_size_at_open);
    w.key("bytes_read");
    w.value_uint(st.bytes_read);
    w.key("frames_read");
    w.value_uint(st.frames_read);
    w.key("chunks_pushed");
    w.value_uint(st.chunks_pushed);
    w.key("silence_chunks");
    w.value_uint(st.silence_chunks);
    w.key("eof_waits");
    w.value_uint(st.eof_waits);
    w.key("samples_pushed");
    w.value_uint(st.samples_pushed);
    w.end_object();
}

void WriteSegmentsJson(JsonWriter& w, const std::vector<FileResult>& files)
{
    w.begin_array();
    for (const auto& f : files) {
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
}

void WriteManifest(State& s, ServeSession& sess)
{
    JsonWriter w(true, 1);
    w.begin_object();
    w.key("tool");
    w.value_string("obs-media-selftest");
    w.key("schema");
    w.value_string("s3a-ipc-manifest/1");
    w.key("mode");
    w.value_string("s3a-ipc-wgc-window-plus-raw-file-audio-to-split-mp4");
    w.key("protocol");
    w.value_int(1);
    w.key("session_id");
    w.value_string(sess.session_id);
    w.key("pipe");
    w.value_string(sess.pipe);
    w.key("out_dir");
    w.value_string(s.opt.out_dir);
    w.key("status");
    w.value_string(sess.checks_ok ? "pass" : "fail");
    w.key("report_exit_code");
    w.value_int(sess.report_rc);
    w.key("started_utc");
    w.value_string(sess.started_utc);
    w.key("stopped_utc");
    w.value_string(sess.stopped_utc);
    w.key("auto_stopped");
    w.value_bool(sess.auto_stopped);
    w.key("requested");
    w.begin_object();
    w.key("duration_ms");
    w.value_int(s.opt.duration_ms);
    w.key("max_time_sec");
    w.value_int(s.opt.max_time_sec);
    w.key("window_pid");
    w.value_int(s.opt.window_pid);
    w.key("window_title");
    w.value_string(s.opt.window_title);
    w.key("window_class");
    w.value_string(s.opt.window_class);
    w.key("window_exe");
    w.value_string(s.opt.window_exe);
    w.key("audio_raw");
    w.value_string(s.opt.audio_raw);
    w.key("audio_start_zero");
    w.value_bool(s.opt.audio_start_zero);
    w.end_object();
    w.key("window_capture");
    WriteWindowJson(w, s);
    w.key("raw_audio");
    WriteRawAudioStatsJson(w, s.audio_stats, s.audio_enabled);
    w.key("output");
    w.begin_object();
    w.key("total_bytes");
    w.value_uint(s.output_total_bytes);
    w.key("total_frames");
    w.value_int(s.output_total_frames);
    w.key("frames_dropped");
    w.value_int(s.output_frames_dropped);
    w.key("stop_code");
    w.value_int(s.output_stop_code);
    w.key("last_error");
    w.value_string(s.output_last_error);
    w.end_object();
    w.key("files");
    WriteSegmentsJson(w, s.files);
    w.key("checks");
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
    w.key("selftest");
    w.value_string("selftest.json");
    w.end_object();
    sess.manifest_path = JoinPath(s.opt.out_dir, "manifest.json");
    FILE* f = nullptr;
    if (fopen_s(&f, sess.manifest_path.c_str(), "wb") == 0 && f) {
        std::string text = w.str() + "\n";
        std::fwrite(text.data(), 1, text.size(), f);
        std::fclose(f);
    }
    std::printf("IPC_MANIFEST path=%s files=%zu status=%s\n", sess.manifest_path.c_str(),
                s.files.size(), sess.checks_ok ? "pass" : "fail");
    std::fflush(stdout);
}

bool ServeFinishRecording(ServeSession& sess, State& s, bool auto_stop, std::string& error)
{
    if (sess.phase != ServePhase::Recording) {
        return true;
    }
    sess.stopped_utc = UtcNowIso();
    sess.auto_stopped = auto_stop;
    const bool stop_ok = StopOutput(s);
    s.files = ListMp4Files(s.opt.out_dir);
    CollectFinalEvidence(s);
    sess.checks_ok = AllStepsOk(s);
    sess.report_rc = stop_ok ? (sess.checks_ok ? 0 : 6) : 5;
    WriteReport(s, sess.report_rc);
    sess.selftest_path = JoinPath(s.opt.out_dir, "selftest.json");
    WriteManifest(s, sess);
    sess.phase = ServePhase::Stopped;
    if (!stop_ok) {
        error = "output stop/drain failed; see selftest.json";
        sess.last_error_code = "output_stop_failed";
        sess.last_error_message = error;
    }
    return stop_ok;
}

std::string ServeHello(ServeSession& sess, State& s, const IpcRequest& req)
{
    return ReplyOk(sess, req, ServePhaseName(sess.phase), [&](JsonWriter& w) {
        w.key("version");
        w.value_string("0.4.0-s3a");
        w.key("obs_version");
        w.value_string(s.obs_version);
        w.key("obs_api_version");
        w.value_int(s.obs_api_version);
        w.key("pid");
        w.value_uint(GetCurrentProcessId());
        w.key("pipe");
        w.value_string(sess.pipe);
        w.key("commands");
        w.begin_array();
        for (const char* c : {"hello", "capabilities", "prepare", "start", "status", "stop", "shutdown"})
            w.value_string(c);
        w.end_array();
        w.key("capabilities");
        w.begin_object();
        w.key("wgc_window_capture");
        w.value_bool(true);
        w.key("raw_file_audio");
        w.value_bool(true);
        w.key("x264");
        w.value_bool(true);
        w.key("aac");
        w.value_bool(true);
        w.key("hybrid_mp4");
        w.value_bool(true);
        w.key("split_file");
        w.value_bool(true);
        w.key("auto_stop_duration_ms");
        w.value_bool(true);
        w.end_object();
        w.key("constraints");
        w.begin_object();
        w.key("no_obs_ui");
        w.value_bool(true);
        w.key("no_websocket");
        w.value_bool(true);
        w.key("no_audio_device");
        w.value_bool(true);
        w.key("no_playback");
        w.value_bool(true);
        w.key("single_recording_per_process");
        w.value_bool(true);
        w.end_object();
        w.key("win_capture_module_loaded");
        w.value_bool(s.win_capture_module_loaded);
    });
}

std::string ServeStatus(ServeSession& sess, State& s, const IpcRequest& req)
{
    // If the output died without an explicit stop, finalise now (explicit error).
    if (sess.phase == ServePhase::Recording && s.output && !obs_output_active(s.output)) {
        std::string err = "output became inactive without a stop request";
        sess.last_error_code = "output_stopped_unexpectedly";
        sess.last_error_message = err;
        ServeFinishRecording(sess, s, false, err);
    }
    if (!s.opt.out_dir.empty() && fs::is_directory(Utf8ToWide(s.opt.out_dir))) {
        s.files = ListMp4Files(s.opt.out_dir);
    }
    const bool active =
        sess.phase == ServePhase::Recording && s.output && obs_output_active(s.output);
    return ReplyOk(sess, req, ServePhaseName(sess.phase), [&](JsonWriter& w) {
        w.key("active");
        w.value_bool(active);
        w.key("uptime_ms");
        w.value_uint((os_gettime_ns() - sess.serve_started_ns) / 1000000ULL);
        w.key("requests");
        w.value_int(sess.requests);
        w.key("session_id");
        w.value_string(sess.session_id);
        w.key("out_dir");
        w.value_string(s.opt.out_dir);
        w.key("window");
        WriteWindowJson(w, s);
        w.key("audio_raw");
        w.begin_object();
        w.key("path");
        w.value_string(s.opt.audio_raw);
        w.key("start_at_zero");
        w.value_bool(s.opt.audio_start_zero);
        w.key("max_time_sec");
        w.value_int(s.opt.max_time_sec);
        w.key("duration_ms");
        w.value_int(s.opt.duration_ms);
        w.end_object();
        w.key("recording_elapsed_ms");
        w.value_uint(sess.started_ns ? (os_gettime_ns() - sess.started_ns) / 1000000ULL : 0);
        w.key("auto_stop_in_ms");
        if (sess.auto_stop_deadline_ns && sess.phase == ServePhase::Recording) {
            const uint64_t now = os_gettime_ns();
            const uint64_t remaining =
                sess.auto_stop_deadline_ns > now ? sess.auto_stop_deadline_ns - now : 0;
            w.value_int(static_cast<long long>(remaining / 1000000ULL));
        } else {
            w.value_null();
        }
        w.key("segments");
        WriteSegmentsJson(w, s.files);
        w.key("segment_count");
        w.value_uint(s.files.size());
        w.key("output");
        w.begin_object();
        w.key("total_bytes");
        w.value_uint(s.output ? obs_output_get_total_bytes(s.output) : 0);
        w.key("total_frames");
        w.value_int(s.output ? obs_output_get_total_frames(s.output) : 0);
        w.key("frames_dropped");
        w.value_int(s.output ? obs_output_get_frames_dropped(s.output) : 0);
        w.key("last_error");
        w.value_string(s.output_last_error);
        w.end_object();
        w.key("raw_audio");
        const a3::RawAudioStats stats = a3::GetRawAudioStats();
        WriteRawAudioStatsJson(w, stats, s.audio_enabled);
        w.key("result");
        w.begin_object();
        w.key("available");
        w.value_bool(sess.report_rc >= 0);
        w.key("report_exit_code");
        w.value_int(sess.report_rc);
        w.key("checks_ok");
        w.value_bool(sess.checks_ok);
        w.key("auto_stopped");
        w.value_bool(sess.auto_stopped);
        w.key("selftest");
        w.value_string(sess.selftest_path);
        w.key("manifest");
        w.value_string(sess.manifest_path);
        w.end_object();
        w.key("last_error");
        w.begin_object();
        w.key("code");
        w.value_string(sess.last_error_code);
        w.key("message");
        w.value_string(sess.last_error_message);
        w.end_object();
    });
}

std::string ServePrepare(ServeSession& sess, State& s, const IpcRequest& req)
{
    if (sess.phase != ServePhase::Idle) {
        return ReplyError(sess, req, sess.phase == ServePhase::Prepared ? "already_prepared"
                                                                       : "session_finished",
                          std::string("prepare is only allowed once per process; current state=") +
                              ServePhaseName(sess.phase));
    }
    std::string err;
    std::string out_dir;
    bool found = false;
    if (!GetStringArg(req, "out_dir", &out_dir, &found, err) || !found || out_dir.empty()) {
        return ReplyError(sess, req, "invalid_args",
                          found ? "\"out_dir\" is required" : (err.empty() ? "\"out_dir\" is required" : err));
    }
    std::string audio_raw;
    found = false;
    if (!GetStringArg(req, "audio_raw", &audio_raw, &found, err) || !found || audio_raw.empty()) {
        return ReplyError(sess, req, "invalid_args",
                          found ? "\"audio_raw\" is required" : (err.empty() ? "\"audio_raw\" is required" : err));
    }
    std::string title, cls, exe;
    bool f = false;
    if (!GetStringArg(req, "window_title", &title, &f, err))
        return ReplyError(sess, req, "invalid_args", err);
    if (!GetStringArg(req, "window_class", &cls, &f, err))
        return ReplyError(sess, req, "invalid_args", err);
    if (!GetStringArg(req, "window_exe", &exe, &f, err))
        return ReplyError(sess, req, "invalid_args", err);
    long long pid = 0;
    found = false;
    if (!GetIntArg(req, "window_pid", &pid, &found, err))
        return ReplyError(sess, req, "invalid_args", err);
    if (pid < 0)
        return ReplyError(sess, req, "invalid_args", "\"window_pid\" must be >= 0");
    long long max_time = 3;
    found = false;
    if (!GetIntArg(req, "max_time_sec", &max_time, &found, err))
        return ReplyError(sess, req, "invalid_args", err);
    if (found && (max_time < 1 || max_time > 3600))
        return ReplyError(sess, req, "invalid_args", "\"max_time_sec\" must be 1..3600");
    long long duration_ms = 0;
    found = false;
    if (!GetIntArg(req, "duration_ms", &duration_ms, &found, err))
        return ReplyError(sess, req, "invalid_args", err);
    if (found && (duration_ms < 0 || duration_ms > 86400000LL))
        return ReplyError(sess, req, "invalid_args", "\"duration_ms\" must be 0..86400000 (0=manual stop)");
    if (pid <= 0 && title.empty() && cls.empty() && exe.empty())
        return ReplyError(sess, req, "invalid_args",
                          "a window target is required (window_pid or window_title/class/exe)");
    if (!EnsureDir(out_dir))
        return ReplyError(sess, req, "invalid_out_dir", "cannot create out_dir: " + out_dir);
    if (!FileExists(audio_raw))
        return ReplyError(sess, req, "audio_raw_not_found",
                          "audio_raw file does not exist: " + audio_raw);
    std::error_code ec;
    const uintmax_t audio_size = fs::file_size(Utf8ToWide(audio_raw), ec);
    if (ec)
        return ReplyError(sess, req, "audio_raw_unreadable",
                          "cannot stat audio_raw: " + audio_raw + " (" + ec.message() + ")");

    // Fresh per-recording evidence.
    s.steps.clear();
    {
        std::lock_guard<std::mutex> lock(g_events_mutex);
        g_events.clear();
    }
    s.files.clear();
    s.failure_exit_code = 0;
    s.window_found = false;
    s.window_hwnd = 0;
    s.window_pid_found = 0;
    s.window_title_found.clear();
    s.window_class_found.clear();
    s.window_exe_found.clear();
    s.window_lookup.clear();
    s.capture_width = 0;
    s.capture_height = 0;
    s.capture_proc_hooked = false;
    s.capture_signal_hooked = false;
    s.audio_enabled = false;
    s.audio_stats = a3::RawAudioStats{};

    s.opt.out_dir = out_dir;
    s.opt.audio_raw = audio_raw;
    s.opt.audio_start_zero = false; // live semantics: default EOF
    s.opt.window_title = title;
    s.opt.window_class = cls;
    s.opt.window_exe = exe;
    s.opt.window_pid = static_cast<int>(pid);
    s.opt.max_time_sec = static_cast<int>(max_time);
    s.opt.duration_ms = static_cast<int>(duration_ms);
    s.opt.json_out.clear();
    s.opt.mode = RunMode::WindowWgc;

    if (!FindTargetWindow(s)) {
        return ReplyError(sess, req, "window_not_found", s.window_lookup);
    }

    char stamp[64];
    std::time_t now = std::time(nullptr);
    std::tm tm_now = {};
    localtime_s(&tm_now, &now);
    std::strftime(stamp, sizeof(stamp), "%Y%m%d-%H%M%S", &tm_now);
    sess.session_id = std::string("s3a-") + stamp + "-" + std::to_string(GetCurrentProcessId());
    s.serve_session_id = sess.session_id;
    sess.ever_prepared = true;
    sess.phase = ServePhase::Prepared;
    sess.last_error_code.clear();
    sess.last_error_message.clear();

    return ReplyOk(sess, req, "prepared", [&](JsonWriter& w) {
        w.key("prepared");
        w.value_bool(true);
        w.key("session_id");
        w.value_string(sess.session_id);
        w.key("out_dir");
        w.value_string(s.opt.out_dir);
        w.key("window");
        WriteWindowJson(w, s);
        w.key("audio_raw");
        w.begin_object();
        w.key("path");
        w.value_string(audio_raw);
        w.key("size_bytes");
        w.value_uint(audio_size);
        w.key("start_at_zero");
        w.value_bool(false);
        w.end_object();
        w.key("max_time_sec");
        w.value_int(s.opt.max_time_sec);
        w.key("duration_ms");
        w.value_int(s.opt.duration_ms);
        w.key("auto_stop");
        w.value_bool(s.opt.duration_ms > 0);
        w.key("note");
        w.value_string("start not called yet; window/raw were validated now and will be re-checked at start");
    });
}

std::string ServeStart(ServeSession& sess, State& s, const IpcRequest& req)
{
    if (sess.phase == ServePhase::Recording) {
        return ReplyOk(sess, req, "recording", [&](JsonWriter& w) {
            w.key("already_running");
            w.value_bool(true);
            w.key("active");
            w.value_bool(true);
            w.key("session_id");
            w.value_string(sess.session_id);
            w.key("out_dir");
            w.value_string(s.opt.out_dir);
            w.key("recording_elapsed_ms");
            w.value_uint((os_gettime_ns() - sess.started_ns) / 1000000ULL);
        });
    }
    if (sess.phase != ServePhase::Prepared) {
        return ReplyError(sess, req, sess.phase == ServePhase::Idle ? "not_prepared" : "session_finished",
                          std::string("start needs a prepared session; current state=") +
                              ServePhaseName(sess.phase));
    }
    const std::string first_file = JoinPath(s.opt.out_dir, "clip-000.mp4");
    if (!CreateWindowPipeline(s, first_file)) {
        sess.phase = ServePhase::Failed;
        const int rc = s.failure_exit_code;
        std::string code = "pipeline_failed";
        if (rc == 7)
            code = "window_not_found";
        else if (rc == 8)
            code = "capture_failed";
        else if (rc == 9)
            code = "audio_source_failed";
        std::string msg = "failed to create the WGC+raw-audio pipeline (see server stdout)";
        for (const auto& st : s.steps) {
            if (!st.ok) {
                msg = st.name + ": " + st.detail;
                break;
            }
        }
        return ReplyError(sess, req, code, msg);
    }
    if (!StartOutput(s)) {
        sess.phase = ServePhase::Failed;
        return ReplyError(sess, req, "output_start_failed", s.output_last_error.empty()
                                                                  ? "obs_output_start/active failed"
                                                                  : s.output_last_error);
    }
    sess.ever_started = true;
    sess.phase = ServePhase::Recording;
    sess.last_error_code.clear();
    sess.last_error_message.clear();
    sess.started_ns = os_gettime_ns();
    sess.started_utc = UtcNowIso();
    sess.auto_stop_deadline_ns =
        s.opt.duration_ms > 0 ? sess.started_ns + static_cast<uint64_t>(s.opt.duration_ms) * 1000000ULL : 0;
    AddStep(s, "ipc_start", true,
            "started via named-pipe IPC session=" + sess.session_id + " auto_stop=" +
                (s.opt.duration_ms > 0 ? std::to_string(s.opt.duration_ms) + " ms" : "manual"));
    return ReplyOk(sess, req, "recording", [&](JsonWriter& w) {
        w.key("already_running");
        w.value_bool(false);
        w.key("active");
        w.value_bool(true);
        w.key("session_id");
        w.value_string(sess.session_id);
        w.key("out_dir");
        w.value_string(s.opt.out_dir);
        w.key("window");
        WriteWindowJson(w, s);
        w.key("audio_raw");
        w.begin_object();
        w.key("path");
        w.value_string(s.opt.audio_raw);
        w.key("start_at_zero");
        w.value_bool(false);
        w.end_object();
        w.key("max_time_sec");
        w.value_int(s.opt.max_time_sec);
        w.key("duration_ms");
        w.value_int(s.opt.duration_ms);
        w.key("auto_stop");
        w.value_bool(s.opt.duration_ms > 0);
        w.key("recording_started_utc");
        w.value_string(sess.started_utc);
    });
}

std::string ServeStop(ServeSession& sess, State& s, const IpcRequest& req)
{
    if (sess.phase == ServePhase::Recording) {
        std::string error;
        const bool stop_ok = ServeFinishRecording(sess, s, false, error);
        if (!stop_ok) {
            return ReplyError(sess, req, "output_stop_failed", error);
        }
        sess.last_error_code.clear();
        sess.last_error_message.clear();
        return ReplyOk(sess, req, "stopped", [&](JsonWriter& w) {
            w.key("already_stopped");
            w.value_bool(false);
            w.key("active");
            w.value_bool(false);
            w.key("session_id");
            w.value_string(sess.session_id);
            w.key("out_dir");
            w.value_string(s.opt.out_dir);
            w.key("report_exit_code");
            w.value_int(sess.report_rc);
            w.key("checks_ok");
            w.value_bool(sess.checks_ok);
            w.key("auto_stopped");
            w.value_bool(sess.auto_stopped);
            w.key("selftest");
            w.value_string(sess.selftest_path);
            w.key("manifest");
            w.value_string(sess.manifest_path);
            w.key("files");
            WriteSegmentsJson(w, s.files);
            if (!stop_ok) {
                w.key("stop_error");
                w.value_string(error);
            }
        });
    }
    if (sess.phase == ServePhase::Prepared) {
        sess.phase = ServePhase::Stopped;
        sess.checks_ok = false;
        sess.report_rc = -1;
        sess.stopped_utc = UtcNowIso();
        WriteManifest(s, sess);
        sess.selftest_path.clear();
    }
    sess.last_error_code.clear();
    sess.last_error_message.clear();
    return ReplyOk(sess, req, ServePhaseName(sess.phase), [&](JsonWriter& w) {
        w.key("already_stopped");
        w.value_bool(true);
        w.key("active");
        w.value_bool(false);
        w.key("session_id");
        w.value_string(sess.session_id);
        w.key("out_dir");
        w.value_string(s.opt.out_dir);
        w.key("report_exit_code");
        w.value_int(sess.report_rc);
        w.key("checks_ok");
        w.value_bool(sess.checks_ok);
        w.key("selftest");
        w.value_string(sess.selftest_path);
        w.key("manifest");
        w.value_string(sess.manifest_path);
        w.key("files");
        WriteSegmentsJson(w, s.files);
    });
}

std::string ServeShutdown(ServeSession& sess, State& s, const IpcRequest& req)
{
    if (sess.phase == ServePhase::Recording) {
        std::string error;
        ServeFinishRecording(sess, s, false, error);
    } else if (sess.phase == ServePhase::Prepared) {
        sess.phase = ServePhase::Stopped;
        sess.stopped_utc = UtcNowIso();
        WriteManifest(s, sess);
    }
    sess.last_error_code.clear();
    sess.last_error_message.clear();
    std::string line = ReplyOk(sess, req, "shutdown", [&](JsonWriter& w) {
        w.key("shutting_down");
        w.value_bool(true);
        w.key("session_id");
        w.value_string(sess.session_id);
        w.key("report_exit_code");
        w.value_int(sess.report_rc);
        w.key("checks_ok");
        w.value_bool(sess.checks_ok);
        w.key("selftest");
        w.value_string(sess.selftest_path);
        w.key("manifest");
        w.value_string(sess.manifest_path);
        w.key("files");
        WriteSegmentsJson(w, s.files);
    });
    sess.shutdown = true;
    return line;
}

IpcReply HandleIpcLine(ServeSession& sess, State& s, const std::string& line)
{
    IpcReply reply;
    IpcRequest req;
    std::string err;
    if (!ParseIpcRequest(line, req, err)) {
        req.raw = line;
        req.cmd = "";
        reply.line = ReplyError(sess, req, "invalid_json", err);
        return reply;
    }
    ++sess.requests;
    std::string id_text = "<none>";
    if (req.has_request_id) {
        id_text = req.request_id_is_int ? std::to_string(req.request_id_int) : req.request_id;
    }
    std::printf("IPC_REQ seq=%d cmd=%s request_id=%s\n", sess.requests, req.cmd.c_str(),
                id_text.c_str());
    std::fflush(stdout);
    if (req.cmd == "hello" || req.cmd == "capabilities") {
        reply.line = ServeHello(sess, s, req);
    } else if (req.cmd == "prepare") {
        reply.line = ServePrepare(sess, s, req);
    } else if (req.cmd == "start") {
        reply.line = ServeStart(sess, s, req);
    } else if (req.cmd == "status") {
        reply.line = ServeStatus(sess, s, req);
    } else if (req.cmd == "stop") {
        reply.line = ServeStop(sess, s, req);
    } else if (req.cmd == "shutdown") {
        reply.line = ServeShutdown(sess, s, req);
        reply.shutdown = true;
    } else {
        reply.line = ReplyError(sess, req, "unsupported_command",
                                "unknown command '" + req.cmd +
                                    "'; supported: hello, capabilities, prepare, start, status, stop, shutdown");
    }
    std::printf("IPC_RESP seq=%d cmd=%s state=%s shutdown=%d\n", sess.seq, req.cmd.c_str(),
                ServePhaseName(sess.phase), reply.shutdown ? 1 : 0);
    std::fflush(stdout);
    return reply;
}

bool PipeWrite(HANDLE pipe, const std::string& text)
{
    std::string payload = text + "\n";
    DWORD written = 0;
    return WriteFile(pipe, payload.data(), static_cast<DWORD>(payload.size()), &written, nullptr) &&
           written == payload.size();
}

std::string NormalizePipeName(const std::string& requested)
{
    std::string name = requested;
    if (name.empty()) {
        name = "auto-heros3-media-" + std::to_string(GetCurrentProcessId());
    }
    const std::string prefix = "\\\\.\\pipe\\";
    std::string leaf = name;
    if (leaf.rfind(prefix, 0) == 0) {
        leaf = leaf.substr(prefix.size());
    }
    for (char c : leaf) {
        const unsigned char uc = static_cast<unsigned char>(c);
        if (!(std::isalnum(uc) || c == '-' || c == '_' || c == '.')) {
            return "";
        }
    }
    return leaf.empty() ? "" : prefix + leaf;
}

int RunServeMode(State& s, const std::string& requested_pipe)
{
    const std::string pipe_path = NormalizePipeName(requested_pipe);
    if (pipe_path.empty()) {
        std::fprintf(stderr, "invalid pipe name: %s\n", requested_pipe.c_str());
        return 2;
    }
    ServeSession sess;
    sess.pipe = pipe_path;
    sess.serve_started_ns = os_gettime_ns();
    s.serve_mode = true;
    s.serve_pipe = pipe_path;
    s.opt.serve = true;
    a3::RegisterRawAudioSource();

    const std::wstring wpipe = Utf8ToWide(pipe_path);
    HANDLE pipe = CreateNamedPipeW(wpipe.c_str(), PIPE_ACCESS_DUPLEX,
                                   PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT, 1, 65536,
                                   65536, 0, nullptr);
    if (pipe == INVALID_HANDLE_VALUE) {
        std::fprintf(stderr, "CreateNamedPipeW failed: %s (win32 %lu)\n", pipe_path.c_str(),
                     GetLastError());
        return 3;
    }
    std::printf("SERVE_READY pipe=%s pid=%lu protocol=1 obs=%s session_scope=single-recording\n",
                pipe_path.c_str(), GetCurrentProcessId(), s.obs_version.c_str());
    std::printf("SERVE_COMMANDS hello capabilities prepare start status stop shutdown\n");
    std::fflush(stdout);

    int exit_rc = 0;
    bool stop_serving = false;
    while (!stop_serving) {
        const BOOL connected = ConnectNamedPipe(pipe, nullptr);
        if (!connected) {
            const DWORD err = GetLastError();
            if (err != ERROR_PIPE_CONNECTED) {
                std::printf("SERVE_CLIENT connect_error win32=%lu\n", err);
                std::fflush(stdout);
                continue;
            }
        }
        std::printf("SERVE_CLIENT connected requests_so_far=%d\n", sess.requests);
        std::fflush(stdout);

        std::string rx;
        bool client_gone = false;
        while (!stop_serving && !client_gone) {
            // Auto-stop timer is serviced even while no request is in flight.
            if (sess.phase == ServePhase::Recording && sess.auto_stop_deadline_ns &&
                os_gettime_ns() >= sess.auto_stop_deadline_ns) {
                std::string error;
                ServeFinishRecording(sess, s, true, error);
                std::printf("IPC_EVENT auto_stop state=%s report_rc=%d checks_ok=%d files=%zu\n",
                            ServePhaseName(sess.phase), sess.report_rc, sess.checks_ok ? 1 : 0,
                            s.files.size());
                std::fflush(stdout);
                JsonWriter ev(false);
                ev.begin_object();
                ev.key("event");
                ev.value_string("auto_stop");
                ev.key("ok");
                ev.value_bool(sess.report_rc == 0);
                ev.key("state");
                ev.value_string(ServePhaseName(sess.phase));
                ev.key("session_id");
                ev.value_string(sess.session_id);
                ev.key("report_exit_code");
                ev.value_int(sess.report_rc);
                ev.key("checks_ok");
                ev.value_bool(sess.checks_ok);
                ev.key("selftest");
                ev.value_string(sess.selftest_path);
                ev.key("manifest");
                ev.value_string(sess.manifest_path);
                ev.key("files");
                WriteSegmentsJson(ev, s.files);
                ev.end_object();
                PipeWrite(pipe, ev.str());
            }

            DWORD avail = 0;
            if (!PeekNamedPipe(pipe, nullptr, 0, nullptr, &avail, nullptr)) {
                const DWORD err = GetLastError();
                if (err == ERROR_BROKEN_PIPE || err == ERROR_PIPE_NOT_CONNECTED ||
                    err == ERROR_NO_DATA) {
                    std::printf("SERVE_CLIENT disconnected win32=%lu\n", err);
                    std::fflush(stdout);
                    client_gone = true;
                    break;
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
                continue;
            }
            if (avail == 0) {
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
                continue;
            }
            char buf[4096];
            DWORD got = 0;
            if (!ReadFile(pipe, buf, sizeof(buf), &got, nullptr)) {
                std::printf("SERVE_CLIENT read_error win32=%lu\n", GetLastError());
                std::fflush(stdout);
                client_gone = true;
                break;
            }
            rx.append(buf, got);
            if (rx.size() > 1000000) {
                PipeWrite(pipe, R"({"ok":false,"request_id":null,"cmd":"","error":{"code":"line_too_long","message":"request line exceeds 1 MB"}})");
                rx.clear();
                continue;
            }
            std::size_t pos = 0;
            while ((pos = rx.find('\n')) != std::string::npos) {
                std::string line = rx.substr(0, pos);
                rx.erase(0, pos + 1);
                while (!line.empty() && (line.back() == '\r' || line.back() == ' '))
                    line.pop_back();
                if (line.empty())
                    continue;
                IpcReply reply = HandleIpcLine(sess, s, line);
                if (!reply.line.empty()) {
                    if (!PipeWrite(pipe, reply.line)) {
                        client_gone = true;
                        break;
                    }
                }
                if (reply.shutdown) {
                    stop_serving = true;
                    exit_rc = 0;
                    break;
                }
            }
        }
        if (client_gone && !stop_serving && sess.phase == ServePhase::Recording) {
            std::string error;
            sess.last_error_code = "client_disconnected";
            sess.last_error_message = "IPC client disconnected while recording; recording was stopped";
            ServeFinishRecording(sess, s, false, error);
            std::printf("IPC_EVENT client_disconnected_finalized report_rc=%d\n", sess.report_rc);
            std::fflush(stdout);
        }
        DisconnectNamedPipe(pipe);
    }
    CloseHandle(pipe);
    std::printf("SERVE_EXIT rc=%d requests=%d state=%s\n", exit_rc, sess.requests,
                ServePhaseName(sess.phase));
    std::fflush(stdout);
    return exit_rc;
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

void PrintUsage()
{
    std::printf("usage: obs-media-selftest.exe --out-dir DIR [options]\n"
                "  --out-dir DIR        output directory for split MP4s + selftest.json (required)\n"
                "  --obs-dir DIR        installed OBS root (default: C:\\Program Files\\obs-studio)\n"
                "  --mode MODE          in-memory (default, S1b synthetic AV) | window (S1c WGC)\n"
                "  --window-source      alias for --mode window\n"
                "  --window-title STR   window mode: target window title (default 'VCMI WGC Synthetic')\n"
                "  --window-class STR   window mode: target window class (optional)\n"
                "  --window-exe STR     window mode: target executable name (optional)\n"
                "  --window-pid N       window mode: restrict to a process id (optional)\n"
                "  --audio-raw PATH      also add native raw-file audio source (s16le/44100/2)\n"
                "  --audio-start-zero    raw audio: read from byte 0 instead of EOF (tests)\n"
                "  --duration-ms N      record duration in ms (default 17000; window mode 8000)\n"
                "  --max-time-sec N     split max_time_sec (default 5; window mode 3)\n"
                "  --json-out FILE      report path (default: <out-dir>\\selftest.json)\n"
                "  --serve              S3a: run the named-pipe IPC server (libobs stays initialized)\n"
                "  --pipe NAME          S3a: pipe name (default auto-heros3-media-<pid>)\n"
                "exit codes: 0 ok, 2 usage, 3 obs/module init, 4 pipeline, 5 output run,\n"
                "            6 checks, 7 window_not_found, 8 capture/not-WGC failure,\n"
                "            9 raw audio source failure\n");
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
        } else if (a == "--mode") {
            const std::string value = next("--mode");
            if (value == "in-memory" || value == "memory" || value == "s1b") {
                opt.mode = RunMode::InMemory;
            } else if (value == "window" || value == "wgc" || value == "s1c") {
                opt.mode = RunMode::WindowWgc;
            } else {
                std::fprintf(stderr, "invalid --mode: %s (in-memory|window)\n", value.c_str());
                exit_code = 2;
                return false;
            }
        } else if (a == "--window-source") {
            opt.mode = RunMode::WindowWgc;
        } else if (a == "--window-title") {
            opt.window_title = next("--window-title");
        } else if (a == "--window-class") {
            opt.window_class = next("--window-class");
        } else if (a == "--window-exe") {
            opt.window_exe = next("--window-exe");
        } else if (a == "--window-pid") {
            opt.window_pid = std::atoi(next("--window-pid").c_str());
        } else if (a == "--audio-raw") {
            opt.audio_raw = next("--audio-raw");
        } else if (a == "--audio-start-zero") {
            opt.audio_start_zero = true;
        } else if (a == "--duration-ms") {
            opt.duration_ms = std::atoi(next("--duration-ms").c_str());
            opt.duration_explicit = true;
        } else if (a == "--max-time-sec") {
            opt.max_time_sec = std::atoi(next("--max-time-sec").c_str());
            opt.max_time_explicit = true;
        } else if (a == "--json-out") {
            opt.json_out = next("--json-out");
        } else if (a == "--serve") {
            opt.serve = true;
        } else if (a == "--pipe") {
            opt.pipe_name = next("--pipe");
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
    if (opt.serve) {
        // S3a: one process initialises libobs once and serves the single-recording
        // lifecycle over a named pipe.  out_dir/window/audio come from `prepare`.
        opt.mode = RunMode::WindowWgc;
        if (!opt.duration_explicit) {
            opt.duration_ms = 8000;
        }
        if (!opt.max_time_explicit) {
            opt.max_time_sec = 3;
        }
        if (!opt.audio_raw.empty()) {
            std::fprintf(stderr, "--audio-raw is supplied by the IPC prepare command, not on the CLI in --serve mode\n");
            exit_code = 2;
            return false;
        }
        return true;
    }
    if (opt.mode == RunMode::WindowWgc) {
        if (!opt.duration_explicit) {
            opt.duration_ms = 8000;
        }
        if (!opt.max_time_explicit) {
            opt.max_time_sec = 3;
        }
        if (opt.window_pid <= 0 && opt.window_title.empty() && opt.window_class.empty() &&
            opt.window_exe.empty()) {
            std::fprintf(stderr, "window mode needs --window-title/--window-class/--window-exe/--window-pid\n");
            exit_code = 2;
            return false;
        }
    }
    if (opt.window_pid < 0) {
        std::fprintf(stderr, "--window-pid out of range\n");
        exit_code = 2;
        return false;
    }
    if (!opt.audio_raw.empty() && opt.mode != RunMode::WindowWgc) {
        std::fprintf(stderr, "--audio-raw is only supported together with --mode window\n");
        exit_code = 2;
        return false;
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
    // Match OBS' own DPI awareness (obs64.exe manifest: Per-Monitor-V2) so the
    // win-capture window_capture client-area cropping math uses physical pixel
    // coordinates like the WGC frame does.  Without this the host process is
    // DPI-unaware and client_area=true can leave the window frame/title bar in
    // the captured texture (observed on this host with a DPI-unaware Tk window).
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);

    State state;
    int exit_code = 0;
    if (!ParseArgs(argc, argv, state.opt, exit_code)) {
        return exit_code;
    }

    // Keep a copy of libobs log lines in memory (also forwarded to stdout) so
    // the report can record the win-capture plugin's actual "method chosen"
    // line instead of only relying on external stdout grep.
    InstallLogCapture();

    std::printf("obs-media-selftest: libobs=linked mode=%s obs_dir=%s out_dir=%s duration_ms=%d max_time_sec=%d\n",
                state.opt.serve ? "serve" : (state.opt.mode == RunMode::WindowWgc ? "window" : "in-memory"),
                state.opt.obs_dir.c_str(), state.opt.out_dir.c_str(), state.opt.duration_ms,
                state.opt.max_time_sec);
    std::fflush(stdout);

    // S3a: persistent IPC server.  libobs is initialised exactly once here and
    // torn down only by Cleanup() after the shutdown command / server exit.
    if (state.opt.serve) {
        state.serve_mode = true;
        state.opt.mode = RunMode::WindowWgc;
        int rc = 0;
        if (!InitObs(state)) {
            rc = state.failure_exit_code ? state.failure_exit_code : 3;
        } else {
            rc = RunServeMode(state, state.opt.pipe_name);
        }
        Cleanup(state);
        UninstallLogCapture();
        std::printf("SERVE_EXIT_CODE %d\n", rc);
        std::fflush(stdout);
        return rc;
    }

    if (!EnsureDir(state.opt.out_dir)) {
        std::fprintf(stderr, "cannot create out dir: %s\n", state.opt.out_dir.c_str());
        state.steps.push_back(Step{"create_out_dir", false, state.opt.out_dir});
        WriteReport(state, 2);
        Cleanup(state);
        UninstallLogCapture();
        return 2;
    }
    state.steps.push_back(Step{"create_out_dir", true, state.opt.out_dir});

    const std::string first_file = JoinPath(state.opt.out_dir, "clip-000.mp4");

    int rc = 0;
    if (!InitObs(state)) {
        rc = state.failure_exit_code ? state.failure_exit_code : 3;
    } else if (state.opt.mode == RunMode::WindowWgc) {
        if (!CreateWindowPipeline(state, first_file)) {
            rc = state.failure_exit_code ? state.failure_exit_code : 4;
        } else if (!RunOutput(state)) {
            rc = state.failure_exit_code ? state.failure_exit_code : 5;
        } else {
            CollectFinalEvidence(state);
            if (!AllStepsOk(state)) {
                rc = state.failure_exit_code ? state.failure_exit_code : 6;
            }
        }
    } else {
        if (!CreatePipeline(state, first_file)) {
            rc = state.failure_exit_code ? state.failure_exit_code : 4;
        } else if (!RunOutput(state)) {
            rc = state.failure_exit_code ? state.failure_exit_code : 5;
        } else {
            CollectFinalEvidence(state);
            if (!AllStepsOk(state)) {
                rc = state.failure_exit_code ? state.failure_exit_code : 6;
            }
        }
    }

    WriteReport(state, rc);
    Cleanup(state);
    UninstallLogCapture();
    std::printf("SELFTEST_EXIT %d\n", rc);
    std::fflush(stdout);
    return rc;
}
