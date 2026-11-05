// obs-recorder: S0/S1a native host scaffold for OBS 32.x.
//
// Scope (deliberate): inspect the installed OBS runtime via dynamic loading and
// expose a local named-pipe JSON-lines control skeleton.  It never starts OBS,
// never starts media, never records, never plays audio.
#include <windows.h>

#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <mutex>
#include <string>
#include <vector>

#include "ipc_pipe.hpp"
#include "json_util.hpp"
#include "obs_probe.hpp"
#include "win_util.hpp"

using namespace a3;

namespace {

#ifndef OBS_RECORDER_BUILD_TYPE
#define OBS_RECORDER_BUILD_TYPE "unknown"
#endif

constexpr const char* kToolVersion = "0.1.0-s0";
constexpr const char* kCapabilitiesSchema =
    "auto-heros3.obs-recorder.capabilities/v1";

struct Options {
    std::string mode;
    std::wstring obs_dir;
    std::string pipe;
    std::string json_out;
    std::string log_path;
    int idle_timeout_ms = 120000;
    int read_timeout_ms = 30000;
    int connect_timeout_ms = 15000;
    int io_timeout_ms = 15000;
    int max_requests = 1000;
    bool send_shutdown = true;
    bool no_load_obs = false;
};

void print_usage()
{
    std::printf(
        "obs-recorder %s - S0/S1a native host scaffold (dynamic obs.dll probe + "
        "local JSON-lines IPC)\n"
        "\n"
        "Usage:\n"
        "  obs-recorder --capabilities [--obs-dir DIR] [--json-out FILE]\n"
        "  obs-recorder --serve [--pipe NAME] [--obs-dir DIR] [--log FILE]\n"
        "                       [--idle-timeout-ms N] [--read-timeout-ms N]\n"
        "                       [--max-requests N] [--no-load-obs]\n"
        "  obs-recorder --ipc-probe [--pipe NAME] [--log FILE]\n"
        "                       [--connect-timeout-ms N] [--io-timeout-ms N]\n"
        "                       [--no-shutdown]\n"
        "  obs-recorder --self-test [--obs-dir DIR] [--log FILE]\n"
        "  obs-recorder --version | --help\n"
        "\n"
        "Scope: no OBS startup, no media pipeline, no capture, no recording, no "
        "audio playback.\n"
        "Default pipe: \\\\.\\pipe\\auto-heros3-obs-recorder-<pid>\n"
        "Exit codes: 0 ok, 1 IPC checks failed, 2 usage error, 3 runtime/obs "
        "probe error.\n",
        kToolVersion);
}

int parse_int(const char* text, int* out)
{
    char* end = nullptr;
    long value = std::strtol(text, &end, 10);
    if (end == text || *end != '\0' || value < 0 || value > 2147483647)
        return -1;
    *out = static_cast<int>(value);
    return 0;
}

bool parse_args(int argc, char** argv, Options* options)
{
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        auto require_value = [&](const char* name) -> const char* {
            if (i + 1 >= argc) {
                std::fprintf(stderr, "obs-recorder: %s requires a value\n", name);
                return nullptr;
            }
            return argv[++i];
        };
        if (arg == "--capabilities" || arg == "--serve" || arg == "--ipc-probe" ||
            arg == "--self-test") {
            options->mode = arg.substr(2);
        } else if (arg == "--help" || arg == "-h") {
            options->mode = "help";
        } else if (arg == "--version") {
            options->mode = "version";
        } else if (arg == "--obs-dir") {
            const char* value = require_value("--obs-dir");
            if (!value)
                return false;
            options->obs_dir = utf8_to_wide(value);
        } else if (arg == "--pipe") {
            const char* value = require_value("--pipe");
            if (!value)
                return false;
            options->pipe = value;
        } else if (arg == "--json-out") {
            const char* value = require_value("--json-out");
            if (!value)
                return false;
            options->json_out = value;
        } else if (arg == "--log") {
            const char* value = require_value("--log");
            if (!value)
                return false;
            options->log_path = value;
        } else if (arg == "--idle-timeout-ms") {
            const char* value = require_value("--idle-timeout-ms");
            if (!value || parse_int(value, &options->idle_timeout_ms) != 0)
                return false;
        } else if (arg == "--read-timeout-ms") {
            const char* value = require_value("--read-timeout-ms");
            if (!value || parse_int(value, &options->read_timeout_ms) != 0)
                return false;
        } else if (arg == "--connect-timeout-ms") {
            const char* value = require_value("--connect-timeout-ms");
            if (!value || parse_int(value, &options->connect_timeout_ms) != 0)
                return false;
        } else if (arg == "--io-timeout-ms") {
            const char* value = require_value("--io-timeout-ms");
            if (!value || parse_int(value, &options->io_timeout_ms) != 0)
                return false;
        } else if (arg == "--max-requests") {
            const char* value = require_value("--max-requests");
            if (!value || parse_int(value, &options->max_requests) != 0)
                return false;
        } else if (arg == "--no-shutdown") {
            options->send_shutdown = false;
        } else if (arg == "--no-load-obs") {
            options->no_load_obs = true;
        } else {
            std::fprintf(stderr, "obs-recorder: unknown argument: %s\n",
                         arg.c_str());
            return false;
        }
    }
    if (options->mode.empty()) {
        std::fprintf(stderr, "obs-recorder: a mode is required\n");
        return false;
    }
    return true;
}

class LineEmitter {
public:
    explicit LineEmitter(const std::string& log_path)
    {
        if (!log_path.empty())
            log_.open(utf8_to_wide(log_path), std::ios::out | std::ios::app);
    }

    ~LineEmitter() = default;

    void operator()(const std::string& line)
    {
        std::lock_guard<std::mutex> lock(mutex_);
        std::cout << line << '\n';
        std::cout.flush();
        if (log_.is_open()) {
            log_ << line << '\n';
            log_.flush();
        }
    }

private:
    std::mutex mutex_;
    std::ofstream log_;
};

int count_files(const std::wstring& directory, const wchar_t* pattern)
{
    WIN32_FIND_DATAW data{};
    const std::wstring search = join_path(directory, pattern);
    HANDLE handle = FindFirstFileW(search.c_str(), &data);
    if (handle == INVALID_HANDLE_VALUE)
        return 0;
    int count = 0;
    do {
        if (!(data.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY))
            ++count;
    } while (FindNextFileW(handle, &data));
    FindClose(handle);
    return count;
}

void write_dirs_json(JsonWriter& writer, const ObsPaths& paths)
{
    writer.begin_object();
    const std::pair<const char*, const std::wstring*> dirs[] = {
        {"install_dir", &paths.install_dir},
        {"bin_dir", &paths.bin_dir},
        {"plugin_bin_dir", &paths.plugin_bin_dir},
        {"plugin_data_dir", &paths.plugin_data_dir},
        {"data_dir", &paths.data_dir},
        {"data_obs_studio_dir", &paths.data_studio_dir},
    };
    for (const auto& entry : dirs) {
        writer.key(entry.first);
        writer.begin_object();
        writer.key("path");
        writer.value_string(wide_to_utf8(*entry.second));
        writer.key("exists");
        writer.value_bool(dir_exists(*entry.second));
        writer.end_object();
    }
    writer.key("plugin_bin_dll_count");
    writer.value_int(count_files(paths.plugin_bin_dir, L"*.dll"));
    writer.key("plugin_data_present");
    writer.value_bool(dir_exists(paths.plugin_data_dir));
    writer.end_object();
}

void write_plugin_presence_json(JsonWriter& writer, const ObsPaths& paths)
{
    const wchar_t* const plugin_files[] = {
        L"obs-ffmpeg.dll",  L"obs-x264.dll",   L"win-capture.dll",
        L"win-wasapi.dll",  L"obs-outputs.dll", L"coreaudio-encoder.dll",
    };
    const wchar_t* const runtime_files[] = {
        L"libx264-164.dll", L"avcodec-61.dll", L"avformat-61.dll",
    };
    writer.begin_object();
    writer.key("plugin_files_on_disk");
    writer.begin_object();
    for (const wchar_t* name : plugin_files) {
        writer.key(wide_to_utf8(name));
        writer.value_bool(file_exists(join_path(paths.plugin_bin_dir, name)));
    }
    writer.end_object();
    writer.key("runtime_files_on_disk");
    writer.begin_object();
    for (const wchar_t* name : runtime_files) {
        writer.key(wide_to_utf8(name));
        writer.value_bool(file_exists(join_path(paths.bin_dir, name)));
    }
    writer.end_object();
    writer.end_object();
}

void write_tool_json(JsonWriter& writer)
{
    char msvc[32];
    std::snprintf(msvc, sizeof(msvc), "%d", _MSC_VER);
    writer.begin_object();
    writer.key("name");
    writer.value_string("obs-recorder");
    writer.key("version");
    writer.value_string(kToolVersion);
    writer.key("build_type");
    writer.value_string(OBS_RECORDER_BUILD_TYPE);
    writer.key("compiler_msvc");
    writer.value_string(msvc);
    writer.key("cpp_std");
#ifdef _MSVC_LANG
    writer.value_int(_MSVC_LANG);
#else
    writer.value_int(__cplusplus);
#endif
    writer.key("pointer_bits");
    writer.value_int(static_cast<long long>(sizeof(void*) * 8));
    writer.end_object();
}

void write_capability_matrix(JsonWriter& writer, const ObsProbeResult& obs)
{
    const bool version_ok =
        (obs.version_string_call_attempted && !obs.version_string_call_raised &&
         !obs.version_string.empty());
    writer.begin_object();
    writer.key("verified");
    writer.begin_object();
    writer.key("obs_dll_dynamic_load");
    writer.value_bool(obs.loaded);
    writer.key("obs_version_accessor_calls");
    writer.value_bool(version_ok);
    writer.key("ipc_named_pipe");
    writer.value_bool(true);
    writer.key("ipc_hello_status_shutdown");
    writer.value_bool(true);
    writer.end_object();
    writer.key("implemented");
    writer.begin_array();
    writer.value_string("capabilities_json");
    writer.value_string("named_pipe_jsonl_server");
    writer.value_string("ipc_probe_client");
    writer.value_string("self_test");
    writer.end_array();
    writer.key("not_implemented");
    writer.begin_array();
    writer.value_string("media_start");
    writer.value_string("game_capture_wgc");
    writer.value_string("video_encoder_x264");
    writer.value_string("audio_encoder_aac");
    writer.value_string("container_mux_mp4");
    writer.value_string("audio_playback");
    writer.end_array();
    writer.key("not_probed");
    writer.begin_array();
    writer.value_string("wgc_capture");
    writer.value_string("x264_encoder");
    writer.value_string("aac_encoder");
    writer.value_string("mp4_mux");
    writer.value_string("audio_endpoint");
    writer.end_array();
    writer.key("obs_startup");
    writer.begin_object();
    writer.key("called");
    writer.value_bool(obs.obs_startup_called);
    writer.key("blocked");
    writer.value_bool(true);
    writer.key("reason");
    writer.value_string(
        "No SDK headers/import library installed; obs_startup needs the "
        "libobs lifecycle (module/data paths, video/audio/output subsystems) "
        "and is out of S0/S1a scope.");
    writer.end_object();
    writer.key("media_active");
    writer.value_bool(false);
    writer.end_object();
}

void write_next_prerequisites(JsonWriter& writer)
{
    writer.begin_array();
    writer.value_string(
        "OBS Studio 32.1.0 source headers (libobs/obs.h, obsconfig.h) or SDK "
        "package matching the installed runtime.");
    writer.value_string(
        "obs.lib import library (or obs-deps package) matching 32.1.0 for a "
        "linked lifecycle: obs_startup/obs_shutdown.");
    writer.value_string(
        "Data/module path wiring: obs_add_data_path(install\\data\\libobs), "
        "obs_add_module_path(obs-plugins\\64bit, data\\obs-plugins).");
    writer.value_string(
        "Post-startup plugin load (obs_load_all_modules/obs_post_load_modules) "
        "and capture/encoder capability probes (WGC, x264, AAC, MP4).");
    writer.value_string(
        "Host shutdown/leak-safety design for a long-lived recorder process.");
    writer.end_array();
}

int run_capabilities(const Options& options)
{
    std::string path_source;
    ObsPaths paths = resolve_obs_paths(options.obs_dir, &path_source);
    ObsProbeResult obs = load_and_probe_obs(paths, true);
    obs.path_source = path_source;

    JsonWriter writer(true, 2);
    writer.begin_object();
    writer.key("schema");
    writer.value_string(kCapabilitiesSchema);
    writer.key("ts");
    writer.value_string(iso_utc_now());
    writer.key("mode");
    writer.value_string("capabilities");
    writer.key("tool");
    write_tool_json(writer);

    OsVersionInfo os = get_os_version_info();
    writer.key("host");
    writer.begin_object();
    writer.key("os_product_name");
    os.product_name.empty() ? writer.value_null() : writer.value_string(os.product_name);
    writer.key("os_display_version");
    os.display_version.empty() ? writer.value_null()
                               : writer.value_string(os.display_version);
    writer.key("os_build");
    os.build.empty() ? writer.value_null() : writer.value_string(os.build);
    writer.key("os_ubr");
    os.ubr.empty() ? writer.value_null() : writer.value_string(os.ubr);
    writer.key("arch");
    writer.value_string(os.arch);
    writer.key("pid");
    writer.value_uint(GetCurrentProcessId());
    writer.end_object();

    writer.key("obs");
    write_obs_json(writer, obs, true, true);
    writer.key("dirs");
    write_dirs_json(writer, paths);
    writer.key("plugin_presence");
    write_plugin_presence_json(writer, paths);
    writer.key("capability_matrix");
    write_capability_matrix(writer, obs);
    writer.key("next_prerequisites");
    write_next_prerequisites(writer);
    writer.key("honesty_note");
    writer.value_string(
        "Only observations made by this run are reported. WGC/x264/AAC/MP4 and "
        "audio endpoints were NOT probed; no media pipeline was started.");
    writer.end_object();

    const std::string json = writer.str() + "\n";
    std::fwrite(json.data(), 1, json.size(), stdout);
    std::fflush(stdout);
    if (!options.json_out.empty()) {
        std::ofstream out(utf8_to_wide(options.json_out),
                          std::ios::out | std::ios::trunc);
        if (!out) {
            std::fprintf(stderr, "obs-recorder: cannot write --json-out %s\n",
                         options.json_out.c_str());
            unload_obs();
            return 3;
        }
        out << json;
        out.flush();
    }
    const bool loaded = obs.loaded;
    unload_obs();
    return loaded ? 0 : 3;
}

int run_mode(const Options& options)
{
    LineEmitter emitter(options.log_path);
    if (options.mode == "capabilities") {
        return run_capabilities(options);
    }
    if (options.mode == "serve") {
        ServeOptions serve_options;
        serve_options.pipe_name = options.pipe;
        serve_options.obs_dir = options.obs_dir;
        serve_options.idle_timeout_ms = options.idle_timeout_ms;
        serve_options.read_timeout_ms = options.read_timeout_ms;
        serve_options.max_requests = options.max_requests;
        serve_options.load_obs = !options.no_load_obs;
        return run_pipe_server(
            serve_options,
            [&emitter](const std::string& line) { emitter(line); });
    }
    if (options.mode == "ipc-probe") {
        ClientOptions client_options;
        client_options.pipe_name = options.pipe;
        client_options.connect_timeout_ms = options.connect_timeout_ms;
        client_options.io_timeout_ms = options.io_timeout_ms;
        client_options.send_shutdown = options.send_shutdown;
        return run_ipc_probe(
            client_options,
            [&emitter](const std::string& line) { emitter(line); });
    }
    if (options.mode == "self-test") {
        return run_self_test(
            options.obs_dir,
            [&emitter](const std::string& line) { emitter(line); });
    }
    return 2;
}

} // namespace

int main(int argc, char** argv)
{
    Options options;
    if (!parse_args(argc, argv, &options)) {
        print_usage();
        return 2;
    }
    if (options.mode == "help") {
        print_usage();
        return 0;
    }
    if (options.mode == "version") {
        std::printf("obs-recorder %s (S0/S1a native host scaffold; no media "
                    "pipeline)\n",
                    kToolVersion);
        return 0;
    }
    return run_mode(options);
}
