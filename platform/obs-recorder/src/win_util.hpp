// Windows helpers for obs-recorder (paths, version resources, OS info).
#pragma once

#include <string>
#include <windows.h>

namespace a3 {

std::string wide_to_utf8(const std::wstring& w);
std::wstring utf8_to_wide(const std::string& s);

bool file_exists(const std::wstring& path);
bool dir_exists(const std::wstring& path);
long long file_size(const std::wstring& path);

std::wstring join_path(const std::wstring& a, const std::wstring& b);
std::string format_win_error(unsigned long code);

struct FileVersionInfo {
    bool ok = false;
    std::string fixed_version;   // e.g. "32.1.0.0"
    std::string string_version;  // e.g. "32.1.0"
    std::string product_name;
    std::string file_description;
};

FileVersionInfo get_file_version_info(const std::wstring& path);

struct OsVersionInfo {
    std::string product_name;
    std::string display_version;
    std::string build;
    std::string ubr;
    std::string arch;
};

OsVersionInfo get_os_version_info();

// Current UTC time, e.g. "2026-11-05T07:00:00Z".
std::string iso_utc_now();

} // namespace a3
