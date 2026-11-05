// Read-only probe of the installed OBS runtime (obs.dll) via dynamic loading.
//
// S0/S1a scope:
//   * resolves the install layout, opens obs.dll with LoadLibraryExW;
//   * resolves a small whitelist of documented exports with GetProcAddress;
//   * optionally calls the two pure version accessors under SEH protection.
// It never calls obs_startup/obs_shutdown and never touches video/audio/output
// subsystems: no game capture, no recording, no audio playback.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include <windows.h>

namespace a3 {

struct ObsPaths {
    std::wstring install_dir;
    std::wstring bin_dir;
    std::wstring dll_path;
    std::wstring plugin_bin_dir;
    std::wstring plugin_data_dir;
    std::wstring data_dir;
    std::wstring data_studio_dir;
};

struct SymbolProbe {
    std::string name;
    bool present = false;
    std::uintptr_t address = 0;
};

struct SdkProbe {
    bool headers_present = false;
    bool import_lib_present = false;
    std::vector<std::pair<std::string, bool>> header_candidates;
    std::vector<std::pair<std::string, bool>> import_lib_candidates;
};

struct ObsProbeResult {
    ObsPaths paths;
    std::string path_source;
    bool dll_exists = false;
    long long dll_size = -1;

    bool load_attempted = false;
    bool loaded = false;
    unsigned long load_error = 0;
    std::string load_error_text;
    std::string load_mode;

    struct FileVersion {
        bool ok = false;
        std::string fixed_version;
        std::string string_version;
        std::string product_name;
        std::string file_description;
    } file_version;

    std::vector<SymbolProbe> symbols;

    bool version_string_call_attempted = false;
    bool version_string_call_raised = false;
    std::string version_string;

    bool version_uint_call_attempted = false;
    bool version_uint_call_raised = false;
    unsigned long version_uint = 0;

    SdkProbe sdk;

    // Deliberately hard-wired: this scaffold does not call these.
    bool obs_startup_called = false;
    bool obs_shutdown_called = false;
};

ObsPaths resolve_obs_paths(const std::wstring& override_dir, std::string* source);
SdkProbe probe_sdk(const ObsPaths& paths);

// Loads obs.dll (if needed), resolves symbols and (optionally) calls the safe
// version accessors. Returns observations; caller must call unload_obs().
ObsProbeResult load_and_probe_obs(const ObsPaths& paths, bool safe_calls);
void unload_obs();

void write_obs_json(class JsonWriter& w, const ObsProbeResult& r,
                    bool include_symbols, bool include_sdk);

} // namespace a3
