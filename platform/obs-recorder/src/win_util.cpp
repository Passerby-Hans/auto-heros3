#include "win_util.hpp"

#include <cstdio>
#include <vector>

namespace a3 {

std::string wide_to_utf8(const std::wstring& w)
{
    if (w.empty())
        return {};
    int len = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), static_cast<int>(w.size()),
                                  nullptr, 0, nullptr, nullptr);
    if (len <= 0)
        return {};
    std::string out(static_cast<std::size_t>(len), '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), static_cast<int>(w.size()),
                        out.data(), len, nullptr, nullptr);
    return out;
}

std::wstring utf8_to_wide(const std::string& s)
{
    if (s.empty())
        return {};
    int len = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()),
                                  nullptr, 0);
    if (len <= 0)
        return {};
    std::wstring out(static_cast<std::size_t>(len), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()),
                        out.data(), len);
    return out;
}

bool file_exists(const std::wstring& path)
{
    DWORD attrs = GetFileAttributesW(path.c_str());
    return attrs != INVALID_FILE_ATTRIBUTES && !(attrs & FILE_ATTRIBUTE_DIRECTORY);
}

bool dir_exists(const std::wstring& path)
{
    DWORD attrs = GetFileAttributesW(path.c_str());
    return attrs != INVALID_FILE_ATTRIBUTES && (attrs & FILE_ATTRIBUTE_DIRECTORY);
}

long long file_size(const std::wstring& path)
{
    WIN32_FILE_ATTRIBUTE_DATA data{};
    if (!GetFileAttributesExW(path.c_str(), GetFileExInfoStandard, &data))
        return -1;
    return (static_cast<long long>(data.nFileSizeHigh) << 32) |
           static_cast<long long>(data.nFileSizeLow);
}

std::wstring join_path(const std::wstring& a, const std::wstring& b)
{
    if (a.empty())
        return b;
    if (b.empty())
        return a;
    wchar_t last = a.back();
    if (last == L'\\' || last == L'/')
        return a + b;
    return a + L"\\" + b;
}

std::string format_win_error(unsigned long code)
{
    char* buffer = nullptr;
    DWORD len = FormatMessageA(
        FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM |
            FORMAT_MESSAGE_IGNORE_INSERTS,
        nullptr, code, MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT),
        reinterpret_cast<char*>(&buffer), 0, nullptr);
    std::string message;
    if (len && buffer) {
        message.assign(buffer, len);
        while (!message.empty() &&
               (message.back() == '\r' || message.back() == '\n'))
            message.pop_back();
    }
    if (buffer)
        LocalFree(buffer);
    char prefix[32];
    std::snprintf(prefix, sizeof(prefix), "%lu", code);
    return message.empty() ? std::string(prefix) : std::string(prefix) + ": " + message;
}

namespace {

std::string query_string_value(const std::vector<char>& block,
                               const std::wstring& sub_block)
{
    struct LANGANDCODEPAGE {
        WORD language;
        WORD codepage;
    };
    void* raw = nullptr;
    UINT raw_size = 0;
    if (!VerQueryValueW(block.data(), L"\\VarFileInfo\\Translation", &raw,
                        &raw_size) ||
        !raw || raw_size < sizeof(LANGANDCODEPAGE)) {
        return {};
    }
    const LANGANDCODEPAGE* translations =
        static_cast<const LANGANDCODEPAGE*>(raw);
    wchar_t path[256];
    std::swprintf(path, sizeof(path) / sizeof(path[0]),
                  L"\\StringFileInfo\\%04x%04x\\%s", translations[0].language,
                  translations[0].codepage, sub_block.c_str());
    void* value = nullptr;
    UINT value_size = 0;
    if (!VerQueryValueW(block.data(), path, &value, &value_size) || !value)
        return {};
    const wchar_t* w = static_cast<const wchar_t*>(value);
    std::wstring result(w, wcslen(w));
    return wide_to_utf8(result);
}

} // namespace

FileVersionInfo get_file_version_info(const std::wstring& path)
{
    FileVersionInfo info;
    DWORD ignored = 0;
    DWORD size = GetFileVersionInfoSizeW(path.c_str(), &ignored);
    if (size == 0)
        return info;
    std::vector<char> block(size);
    if (!GetFileVersionInfoW(path.c_str(), 0, size, block.data()))
        return info;
    info.ok = true;

    VS_FIXEDFILEINFO* fixed = nullptr;
    UINT fixed_size = 0;
    {
        void* raw = nullptr;
        UINT raw_size = 0;
        if (VerQueryValueW(block.data(), L"\\", &raw, &raw_size) && raw &&
            raw_size >= sizeof(VS_FIXEDFILEINFO)) {
            fixed = static_cast<VS_FIXEDFILEINFO*>(raw);
            fixed_size = raw_size;
        }
    }
    if (fixed && fixed_size >= sizeof(VS_FIXEDFILEINFO)) {
        char buf[64];
        std::snprintf(buf, sizeof(buf), "%u.%u.%u.%u",
                      HIWORD(fixed->dwFileVersionMS), LOWORD(fixed->dwFileVersionMS),
                      HIWORD(fixed->dwFileVersionLS), LOWORD(fixed->dwFileVersionLS));
        info.fixed_version = buf;
    }
    info.string_version = query_string_value(block, L"FileVersion");
    info.product_name = query_string_value(block, L"ProductName");
    info.file_description = query_string_value(block, L"FileDescription");
    return info;
}

OsVersionInfo get_os_version_info()
{
    OsVersionInfo info;
    HKEY key = nullptr;
    if (RegOpenKeyExW(HKEY_LOCAL_MACHINE,
                      L"SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion", 0,
                      KEY_QUERY_VALUE, &key) == ERROR_SUCCESS) {
        auto read_string = [&](const wchar_t* name) {
            wchar_t buffer[512];
            DWORD size = sizeof(buffer);
            DWORD type = 0;
            if (RegQueryValueExW(key, name, nullptr, &type,
                                 reinterpret_cast<BYTE*>(buffer),
                                 &size) == ERROR_SUCCESS &&
                (type == REG_SZ || type == REG_EXPAND_SZ)) {
                return wide_to_utf8(std::wstring(buffer));
            }
            return std::string();
        };
        info.product_name = read_string(L"ProductName");
        info.display_version = read_string(L"DisplayVersion");
        info.build = read_string(L"CurrentBuildNumber");

        DWORD ubr = 0;
        DWORD size = sizeof(ubr);
        DWORD type = 0;
        if (RegQueryValueExW(key, L"UBR", nullptr, &type,
                             reinterpret_cast<BYTE*>(&ubr), &size) == ERROR_SUCCESS &&
            type == REG_DWORD) {
            info.ubr = std::to_string(ubr);
        }
        RegCloseKey(key);
    }

    SYSTEM_INFO si{};
    GetNativeSystemInfo(&si);
    switch (si.wProcessorArchitecture) {
    case PROCESSOR_ARCHITECTURE_AMD64:
        info.arch = "x64";
        break;
    case PROCESSOR_ARCHITECTURE_ARM64:
        info.arch = "arm64";
        break;
    case PROCESSOR_ARCHITECTURE_INTEL:
        info.arch = "x86";
        break;
    default:
        info.arch = "unknown";
        break;
    }
    return info;
}

std::string iso_utc_now()
{
    SYSTEMTIME st{};
    GetSystemTime(&st);
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%04u-%02u-%02uT%02u:%02u:%02uZ",
                  static_cast<unsigned>(st.wYear), static_cast<unsigned>(st.wMonth),
                  static_cast<unsigned>(st.wDay), static_cast<unsigned>(st.wHour),
                  static_cast<unsigned>(st.wMinute),
                  static_cast<unsigned>(st.wSecond));
    return buf;
}

} // namespace a3
