#include "obs_probe.hpp"

#include <algorithm>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

#include "json_util.hpp"
#include "win_util.hpp"

namespace a3 {

namespace {

HMODULE g_obs = nullptr;

std::wstring get_env_wide(const wchar_t* name)
{
    DWORD needed = GetEnvironmentVariableW(name, nullptr, 0);
    if (needed == 0)
        return {};
    std::wstring value(static_cast<std::size_t>(needed), L'\0');
    DWORD written = GetEnvironmentVariableW(name, value.data(), needed);
    if (written == 0 || written >= needed)
        return {};
    value.resize(written);
    return value;
}

std::wstring parent_dir(const std::wstring& path)
{
    std::wstring clean = path;
    while (!clean.empty() && (clean.back() == L'\\' || clean.back() == L'/'))
        clean.pop_back();
    std::size_t pos = clean.find_last_of(L"\\/");
    if (pos == std::wstring::npos)
        return {};
    return clean.substr(0, pos);
}

bool ends_with_ci(const std::wstring& value, const std::wstring& suffix)
{
    if (value.size() < suffix.size())
        return false;
    return _wcsicmp(value.c_str() + value.size() - suffix.size(),
                    suffix.c_str()) == 0;
}

const wchar_t* const kObsDllName = L"obs.dll";

const char* const kProbedSymbols[] = {
    "obs_startup",
    "obs_shutdown",
    "obs_get_version",
    "obs_get_version_string",
    "obs_add_data_path",
    "obs_add_module_path",
    "obs_load_all_modules",
    "obs_post_load_modules",
    "obs_get_module",
    "obs_enum_modules",
    "obs_reset_video",
    "obs_reset_audio",
    "obs_source_create",
    "obs_scene_create",
    "obs_output_create",
    "obs_output_start",
    "obs_output_stop",
    "obs_data_create",
    "obs_data_get_json",
    "obs_frontend_get_main_window",
    "obs_start_recording",
};

} // namespace

// SEH helpers: keep them free of C++ objects so __try/__except is well-defined
// without /EHa.  A raised exception is reported as a failed call, never as a
// fabricated capability.
extern "C" {

__declspec(noinline) int a3_seh_call_version_string(void* fn, const char** out)
{
    __try {
        using Fn = const char* (*)();
        *out = reinterpret_cast<Fn>(fn)();
        return 1;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        *out = nullptr;
        return 0;
    }
}

__declspec(noinline) int a3_seh_call_version_uint(void* fn, unsigned long* out)
{
    __try {
        using Fn = unsigned long (*)();
        *out = reinterpret_cast<Fn>(fn)();
        return 1;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        *out = 0;
        return 0;
    }
}

} // extern "C"

ObsPaths resolve_obs_paths(const std::wstring& override_dir, std::string* source)
{
    ObsPaths paths;
    std::wstring base;

    if (!override_dir.empty()) {
        base = override_dir;
        if (source)
            *source = "cli:--obs-dir";
    } else {
        std::wstring env = get_env_wide(L"OBS_STUDIO_DIR");
        if (!env.empty() && dir_exists(env)) {
            base = env;
            if (source)
                *source = "env:OBS_STUDIO_DIR";
        }
    }
    if (base.empty()) {
        base = L"C:\\Program Files\\obs-studio";
        if (source)
            *source = "default";
    }

    paths.install_dir = base;
    paths.data_dir = join_path(base, L"data");
    paths.plugin_bin_dir = join_path(base, L"obs-plugins\\64bit");
    paths.plugin_data_dir = join_path(base, L"data\\obs-plugins");
    paths.data_studio_dir = join_path(base, L"data\\obs-studio");

    if (ends_with_ci(base, L".dll")) {
        paths.dll_path = base;
        paths.bin_dir = parent_dir(base);
        paths.install_dir = parent_dir(paths.bin_dir);
        paths.data_dir = join_path(paths.install_dir, L"data");
        paths.plugin_bin_dir = join_path(paths.install_dir, L"obs-plugins\\64bit");
        paths.plugin_data_dir = join_path(paths.install_dir, L"data\\obs-plugins");
        paths.data_studio_dir = join_path(paths.install_dir, L"data\\obs-studio");
        return paths;
    }

    paths.bin_dir = join_path(base, L"bin\\64bit");
    paths.dll_path = join_path(paths.bin_dir, kObsDllName);
    if (!file_exists(paths.dll_path)) {
        // Some layouts place obs.dll directly next to obs64.exe.
        std::wstring flat = join_path(base, kObsDllName);
        if (file_exists(flat)) {
            paths.bin_dir = base;
            paths.dll_path = flat;
        }
    }
    return paths;
}

SdkProbe probe_sdk(const ObsPaths& paths)
{
    SdkProbe sdk;
    const std::wstring env_sdk = get_env_wide(L"OBS_SDK_DIR");
    const std::wstring cwd = [] {
        wchar_t buffer[4096];
        DWORD len = GetCurrentDirectoryW(4096, buffer);
        return len ? std::wstring(buffer, len) : std::wstring();
    }();

    auto add_header = [&](const std::wstring& path) {
        bool exists = file_exists(path);
        sdk.header_candidates.emplace_back(wide_to_utf8(path), exists);
        sdk.headers_present = sdk.headers_present || exists;
    };
    auto add_import = [&](const std::wstring& path) {
        bool exists = file_exists(path);
        sdk.import_lib_candidates.emplace_back(wide_to_utf8(path), exists);
        sdk.import_lib_present = sdk.import_lib_present || exists;
    };

    add_header(join_path(paths.install_dir, L"include\\obs.h"));
    add_header(join_path(paths.install_dir, L"include\\obs\\obs.h"));
    add_header(join_path(paths.install_dir, L"libobs\\obs.h"));
    if (!env_sdk.empty()) {
        add_header(join_path(env_sdk, L"include\\obs.h"));
        add_header(join_path(env_sdk, L"include\\obs\\obs.h"));
        add_header(join_path(env_sdk, L"obs.h"));
    }
    if (!cwd.empty())
        add_header(join_path(cwd, L"vendor\\obs-studio\\libobs\\obs.h"));

    add_import(join_path(paths.install_dir, L"lib\\obs.lib"));
    add_import(join_path(paths.install_dir, L"bin\\64bit\\obs.lib"));
    add_import(join_path(paths.install_dir, L"obs.lib"));
    if (!env_sdk.empty()) {
        add_import(join_path(env_sdk, L"lib\\obs.lib"));
        add_import(join_path(env_sdk, L"obs.lib"));
    }
    return sdk;
}

ObsProbeResult load_and_probe_obs(const ObsPaths& paths, bool safe_calls)
{
    ObsProbeResult r;
    r.paths = paths;
    r.dll_exists = file_exists(paths.dll_path);
    r.dll_size = file_exists(paths.dll_path) ? file_size(paths.dll_path) : -1;

    FileVersionInfo fvi = get_file_version_info(paths.dll_path);
    if (fvi.ok) {
        r.file_version.ok = true;
        r.file_version.fixed_version = fvi.fixed_version;
        r.file_version.string_version = fvi.string_version;
        r.file_version.product_name = fvi.product_name;
        r.file_version.file_description = fvi.file_description;
    }

    r.sdk = probe_sdk(paths);

    r.load_mode = "LoadLibraryExW(LOAD_WITH_ALTERED_SEARCH_PATH)";
    r.load_attempted = true;
    if (!r.dll_exists) {
        r.load_error = ERROR_FILE_NOT_FOUND;
        r.load_error_text = "obs.dll not found at resolved path";
        return r;
    }

    if (!g_obs) {
        g_obs = LoadLibraryExW(paths.dll_path.c_str(), nullptr,
                               LOAD_WITH_ALTERED_SEARCH_PATH);
    }
    if (!g_obs) {
        r.load_error = GetLastError();
        r.load_error_text = format_win_error(r.load_error);
        return r;
    }
    r.loaded = true;

    r.symbols.reserve(sizeof(kProbedSymbols) / sizeof(kProbedSymbols[0]));
    for (const char* name : kProbedSymbols) {
        SymbolProbe symbol;
        symbol.name = name;
        FARPROC address = GetProcAddress(g_obs, name);
        symbol.present = address != nullptr;
        symbol.address = reinterpret_cast<std::uintptr_t>(address);
        r.symbols.push_back(symbol);
    }

    auto find_symbol = [&](const char* name) -> SymbolProbe* {
        for (SymbolProbe& symbol : r.symbols) {
            if (symbol.name == name)
                return symbol.present ? &symbol : nullptr;
        }
        return nullptr;
    };

    if (safe_calls) {
        if (SymbolProbe* symbol = find_symbol("obs_get_version_string")) {
            r.version_string_call_attempted = true;
            const char* value = nullptr;
            int ok = a3_seh_call_version_string(
                reinterpret_cast<void*>(symbol->address), &value);
            if (ok && value)
                r.version_string = value;
            else
                r.version_string_call_raised = true;
        }
        if (SymbolProbe* symbol = find_symbol("obs_get_version")) {
            r.version_uint_call_attempted = true;
            unsigned long value = 0;
            int ok = a3_seh_call_version_uint(
                reinterpret_cast<void*>(symbol->address), &value);
            if (ok)
                r.version_uint = value;
            else
                r.version_uint_call_raised = true;
        }
    }
    return r;
}

void unload_obs()
{
    if (g_obs) {
        FreeLibrary(g_obs);
        g_obs = nullptr;
    }
}

void write_obs_json(JsonWriter& w, const ObsProbeResult& r, bool include_symbols,
                    bool include_sdk)
{
    w.begin_object();
    w.key("install_dir");
    w.value_string(wide_to_utf8(r.paths.install_dir));
    w.key("bin_dir");
    w.value_string(wide_to_utf8(r.paths.bin_dir));
    w.key("dll_path");
    w.value_string(wide_to_utf8(r.paths.dll_path));
    w.key("dll_exists");
    w.value_bool(r.dll_exists);
    w.key("dll_size_bytes");
    r.dll_size >= 0 ? w.value_int(r.dll_size) : w.value_null();
    w.key("path_source");
    w.value_string(r.path_source);

    w.key("file_version");
    w.begin_object();
    w.key("ok");
    w.value_bool(r.file_version.ok);
    w.key("fixed");
    r.file_version.fixed_version.empty()
        ? w.value_null()
        : w.value_string(r.file_version.fixed_version);
    w.key("string");
    r.file_version.string_version.empty()
        ? w.value_null()
        : w.value_string(r.file_version.string_version);
    w.key("product_name");
    r.file_version.product_name.empty()
        ? w.value_null()
        : w.value_string(r.file_version.product_name);
    w.key("file_description");
    r.file_version.file_description.empty()
        ? w.value_null()
        : w.value_string(r.file_version.file_description);
    w.end_object();

    w.key("dynamic_load");
    w.begin_object();
    w.key("attempted");
    w.value_bool(r.load_attempted);
    w.key("loaded");
    w.value_bool(r.loaded);
    w.key("mode");
    w.value_string(r.load_mode);
    w.key("error_code");
    r.load_error == 0 ? w.value_null() : w.value_uint(r.load_error);
    w.key("error_text");
    r.load_error_text.empty() ? w.value_null()
                              : w.value_string(r.load_error_text);
    w.end_object();

    w.key("version_accessors");
    w.begin_object();
    w.key("get_version_string_called");
    w.value_bool(r.version_string_call_attempted);
    w.key("get_version_string_raised");
    w.value_bool(r.version_string_call_raised);
    w.key("get_version_string");
    r.version_string.empty() ? w.value_null() : w.value_string(r.version_string);
    w.key("get_version_uint_called");
    w.value_bool(r.version_uint_call_attempted);
    w.key("get_version_uint_raised");
    w.value_bool(r.version_uint_call_raised);
    w.key("get_version_uint_hex");
    if (r.version_uint_call_attempted && !r.version_uint_call_raised) {
        char buf[32];
        std::snprintf(buf, sizeof(buf), "0x%08lx", r.version_uint);
        w.value_string(buf);
    } else {
        w.value_null();
    }
    w.end_object();

    w.key("startup");
    w.begin_object();
    w.key("obs_startup_called");
    w.value_bool(r.obs_startup_called);
    w.key("obs_shutdown_called");
    w.value_bool(r.obs_shutdown_called);
    w.key("reason");
    w.value_string(
        "S0/S1a scaffold: obs_startup is not called (no SDK headers/import lib, "
        "no module/data path wiring, no video/audio/output subsystems).");
    w.end_object();

    if (include_symbols) {
        w.key("exports_probed");
        w.begin_array();
        for (const SymbolProbe& symbol : r.symbols) {
            w.begin_object();
            w.key("name");
            w.value_string(symbol.name);
            w.key("present");
            w.value_bool(symbol.present);
            w.key("address");
            symbol.present ? w.value_uintptr(symbol.address) : w.value_null();
            w.end_object();
        }
        w.end_array();
    }

    if (include_sdk) {
        w.key("sdk");
        w.begin_object();
        w.key("sdk_headers");
        w.value_bool(r.sdk.headers_present);
        w.key("sdk_import_lib");
        w.value_bool(r.sdk.import_lib_present);
        w.key("header_candidates");
        w.begin_array();
        for (const auto& candidate : r.sdk.header_candidates) {
            w.begin_object();
            w.key("path");
            w.value_string(candidate.first);
            w.key("exists");
            w.value_bool(candidate.second);
            w.end_object();
        }
        w.end_array();
        w.key("import_lib_candidates");
        w.begin_array();
        for (const auto& candidate : r.sdk.import_lib_candidates) {
            w.begin_object();
            w.key("path");
            w.value_string(candidate.first);
            w.key("exists");
            w.value_bool(candidate.second);
            w.end_object();
        }
        w.end_array();
        w.end_object();
    }
    w.end_object();
}

} // namespace a3
