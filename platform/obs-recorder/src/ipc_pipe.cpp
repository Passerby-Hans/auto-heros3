#include "ipc_pipe.hpp"

#include <algorithm>
#include <chrono>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

#include "json_util.hpp"
#include "win_util.hpp"

namespace a3 {

namespace {

constexpr DWORD kPipeBufferSize = 64 * 1024;
constexpr std::size_t kMaxLineBytes = 1024 * 1024;

// ---------------------------------------------------------------------------
// JSON-lines helpers
// ---------------------------------------------------------------------------

std::string trim(const std::string& value)
{
    std::size_t begin = 0;
    std::size_t end = value.size();
    while (begin < end && std::isspace(static_cast<unsigned char>(value[begin])))
        ++begin;
    while (end > begin &&
           std::isspace(static_cast<unsigned char>(value[end - 1])))
        --end;
    return value.substr(begin, end - begin);
}

std::string jsonl_event(const char* event,
                        const std::function<void(JsonWriter&)>& fill)
{
    JsonWriter writer(false);
    writer.begin_object();
    writer.key("ts");
    writer.value_string(iso_utc_now());
    writer.key("event");
    writer.value_string(event);
    fill(writer);
    writer.end_object();
    return writer.str();
}

// Tx/Rx lines embed an already-valid JSON payload unescaped so human readers
// can inspect the wire format directly.
std::string jsonl_io(const char* event, const char* direction,
                     const std::string& payload)
{
    return std::string("{\"ts\":\"") + iso_utc_now() + "\",\"event\":\"" +
           event + "\",\"dir\":\"" + direction + "\",\"payload\":" + payload +
           "}";
}

bool extract_json_string(const std::string& line, const char* key,
                         std::string* out, bool* found)
{
    *found = false;
    const std::string needle = std::string("\"") + key + "\"";
    std::size_t pos = line.find(needle);
    if (pos == std::string::npos)
        return true;
    pos += needle.size();
    while (pos < line.size() &&
           std::isspace(static_cast<unsigned char>(line[pos])))
        ++pos;
    if (pos >= line.size() || line[pos] != ':')
        return false;
    ++pos;
    while (pos < line.size() &&
           std::isspace(static_cast<unsigned char>(line[pos])))
        ++pos;
    if (pos >= line.size() || line[pos] != '"')
        return false;
    ++pos;
    std::string value;
    while (pos < line.size()) {
        char c = line[pos++];
        if (c == '\\') {
            if (pos >= line.size())
                return false;
            char escaped = line[pos++];
            switch (escaped) {
            case '"':
                value += '"';
                break;
            case '\\':
                value += '\\';
                break;
            case '/':
                value += '/';
                break;
            case 'n':
                value += '\n';
                break;
            case 'r':
                value += '\r';
                break;
            case 't':
                value += '\t';
                break;
            case 'b':
                value += '\b';
                break;
            case 'f':
                value += '\f';
                break;
            case 'u':
                // Enough for the skeleton: keep the escape visible instead of
                // silently inventing a decoded value.
                if (pos + 4 > line.size())
                    return false;
                value += "\\u";
                value.append(line, pos, 4);
                pos += 4;
                break;
            default:
                return false;
            }
            continue;
        }
        if (c == '"') {
            *out = value;
            *found = true;
            return true;
        }
        value += c;
    }
    return false;
}

bool extract_json_int(const std::string& line, const char* key, long long* out,
                      bool* found)
{
    *found = false;
    const std::string needle = std::string("\"") + key + "\"";
    std::size_t pos = line.find(needle);
    if (pos == std::string::npos)
        return true;
    pos += needle.size();
    while (pos < line.size() &&
           std::isspace(static_cast<unsigned char>(line[pos])))
        ++pos;
    if (pos >= line.size() || line[pos] != ':')
        return false;
    ++pos;
    while (pos < line.size() &&
           std::isspace(static_cast<unsigned char>(line[pos])))
        ++pos;
    if (pos >= line.size())
        return false;
    if (line[pos] == '"')
        return false;
    char* end = nullptr;
    long long value = std::strtoll(line.c_str() + pos, &end, 10);
    if (end == line.c_str() + pos)
        return false;
    *out = value;
    *found = true;
    return true;
}

bool extract_json_bool(const std::string& line, const char* key, bool* out,
                       bool* found)
{
    *found = false;
    const std::string needle = std::string("\"") + key + "\"";
    std::size_t pos = line.find(needle);
    if (pos == std::string::npos)
        return true;
    pos += needle.size();
    while (pos < line.size() &&
           std::isspace(static_cast<unsigned char>(line[pos])))
        ++pos;
    if (pos >= line.size() || line[pos] != ':')
        return false;
    ++pos;
    while (pos < line.size() &&
           std::isspace(static_cast<unsigned char>(line[pos])))
        ++pos;
    if (line.compare(pos, 4, "true") == 0) {
        *out = true;
        *found = true;
        return true;
    }
    if (line.compare(pos, 5, "false") == 0) {
        *out = false;
        *found = true;
        return true;
    }
    return false;
}

// ---------------------------------------------------------------------------
// Named-pipe I/O helpers (overlapped so every wait has a timeout)
// ---------------------------------------------------------------------------

enum class IoResult { Ok, Timeout, Closed, Error };

IoResult map_pipe_error(DWORD error)
{
    switch (error) {
    case ERROR_BROKEN_PIPE:
    case ERROR_PIPE_NOT_CONNECTED:
    case ERROR_NO_DATA:
    case ERROR_INVALID_HANDLE:
        return IoResult::Closed;
    default:
        return IoResult::Error;
    }
}

IoResult connect_with_timeout(HANDLE pipe, HANDLE event, DWORD timeout_ms)
{
    OVERLAPPED overlapped{};
    overlapped.hEvent = event;
    ResetEvent(event);
    if (ConnectNamedPipe(pipe, &overlapped))
        return IoResult::Ok;
    DWORD error = GetLastError();
    if (error == ERROR_PIPE_CONNECTED)
        return IoResult::Ok;
    if (error != ERROR_IO_PENDING)
        return map_pipe_error(error);
    DWORD wait = WaitForSingleObject(event, timeout_ms);
    if (wait != WAIT_OBJECT_0) {
        CancelIoEx(pipe, &overlapped);
        return wait == WAIT_TIMEOUT ? IoResult::Timeout : IoResult::Error;
    }
    DWORD transferred = 0;
    if (!GetOverlappedResult(pipe, &overlapped, &transferred, FALSE))
        return map_pipe_error(GetLastError());
    return IoResult::Ok;
}

IoResult write_all(HANDLE pipe, HANDLE event, DWORD timeout_ms,
                   const std::string& data)
{
    std::size_t offset = 0;
    while (offset < data.size()) {
        DWORD chunk = static_cast<DWORD>(
            std::min<std::size_t>(data.size() - offset, 64 * 1024));
        OVERLAPPED overlapped{};
        overlapped.hEvent = event;
        ResetEvent(event);
        DWORD written = 0;
        BOOL ok = WriteFile(pipe, data.data() + offset, chunk, &written,
                            &overlapped);
        if (!ok) {
            DWORD error = GetLastError();
            if (error != ERROR_IO_PENDING)
                return map_pipe_error(error);
            DWORD wait = WaitForSingleObject(event, timeout_ms);
            if (wait != WAIT_OBJECT_0) {
                CancelIoEx(pipe, &overlapped);
                return wait == WAIT_TIMEOUT ? IoResult::Timeout
                                            : IoResult::Error;
            }
            if (!GetOverlappedResult(pipe, &overlapped, &written, FALSE))
                return map_pipe_error(GetLastError());
        }
        if (written == 0)
            return IoResult::Closed;
        offset += written;
    }
    return IoResult::Ok;
}

IoResult read_line(HANDLE pipe, HANDLE event, DWORD timeout_ms,
                   std::string* line)
{
    line->clear();
    for (;;) {
        char chunk[4096];
        OVERLAPPED overlapped{};
        overlapped.hEvent = event;
        ResetEvent(event);
        DWORD received = 0;
        BOOL ok = ReadFile(pipe, chunk, sizeof(chunk), &received, &overlapped);
        if (!ok) {
            DWORD error = GetLastError();
            if (error != ERROR_IO_PENDING)
                return map_pipe_error(error);
            DWORD wait = WaitForSingleObject(event, timeout_ms);
            if (wait != WAIT_OBJECT_0) {
                CancelIoEx(pipe, &overlapped);
                return wait == WAIT_TIMEOUT ? IoResult::Timeout
                                            : IoResult::Error;
            }
            if (!GetOverlappedResult(pipe, &overlapped, &received, FALSE))
                return map_pipe_error(GetLastError());
        }
        if (received == 0)
            return IoResult::Closed;
        line->append(chunk, received);
        std::size_t newline = line->find('\n');
        if (newline != std::string::npos) {
            std::string trimmed = line->substr(0, newline);
            if (!trimmed.empty() && trimmed.back() == '\r')
                trimmed.pop_back();
            *line = trimmed;
            return IoResult::Ok;
        }
        if (line->size() > kMaxLineBytes)
            return IoResult::Error;
    }
}

std::wstring pipe_name_wide(const std::string& name)
{
    return utf8_to_wide(name);
}

// ---------------------------------------------------------------------------
// Server request handling
// ---------------------------------------------------------------------------

struct RequestInfo {
    bool has_id = false;
    long long id = 0;
    bool has_cmd = false;
    std::string cmd;
};

struct RequestOutcome {
    bool shutdown = false;
    std::string response;
    std::string error_code;
    bool ok = false;
};

bool parse_request(const std::string& line, RequestInfo* info,
                   std::string* error_code)
{
    const std::string trimmed = trim(line);
    if (trimmed.empty()) {
        *error_code = "empty_request";
        return false;
    }
    if (trimmed.front() != '{' || trimmed.back() != '}') {
        *error_code = "not_a_json_object";
        return false;
    }
    if (!extract_json_int(trimmed, "id", &info->id, &info->has_id)) {
        *error_code = "bad_id";
        return false;
    }
    if (!extract_json_string(trimmed, "cmd", &info->cmd, &info->has_cmd)) {
        *error_code = "bad_cmd";
        return false;
    }
    if (!info->has_cmd) {
        *error_code = "missing_cmd";
        return false;
    }
    if (info->cmd.empty()) {
        *error_code = "empty_cmd";
        return false;
    }
    return true;
}

std::string error_response(const RequestInfo& info, const std::string& code,
                           const std::string& message)
{
    JsonWriter writer(false);
    writer.begin_object();
    writer.key("id");
    info.has_id ? writer.value_int(info.id) : writer.value_null();
    writer.key("ok");
    writer.value_bool(false);
    writer.key("cmd");
    info.has_cmd ? writer.value_string(info.cmd) : writer.value_null();
    writer.key("error");
    writer.value_string(code);
    writer.key("message");
    writer.value_string(message);
    writer.key("ts");
    writer.value_string(iso_utc_now());
    writer.end_object();
    return writer.str();
}

void write_obs_short(JsonWriter& writer, const ObsProbeResult& obs)
{
    writer.begin_object();
    writer.key("dll_loaded");
    writer.value_bool(obs.loaded);
    writer.key("dll_path");
    writer.value_string(wide_to_utf8(obs.paths.dll_path));
    writer.key("file_version");
    if (!obs.file_version.string_version.empty())
        writer.value_string(obs.file_version.string_version);
    else if (!obs.file_version.fixed_version.empty())
        writer.value_string(obs.file_version.fixed_version);
    else
        writer.value_null();
    writer.key("version_string");
    obs.version_string.empty() ? writer.value_null()
                               : writer.value_string(obs.version_string);
    writer.key("sdk_headers");
    writer.value_bool(obs.sdk.headers_present);
    writer.key("obs_startup_called");
    writer.value_bool(obs.obs_startup_called);
    writer.key("load_error");
    obs.load_error_text.empty() ? writer.value_null()
                                : writer.value_string(obs.load_error_text);
    writer.end_object();
}

void write_media_state(JsonWriter& writer)
{
    writer.begin_object();
    writer.key("media_active");
    writer.value_bool(false);
    writer.key("recording_started");
    writer.value_bool(false);
    writer.key("audio_playback_active");
    writer.value_bool(false);
    writer.key("not_probed_in_s0");
    writer.begin_array();
    writer.value_string("wgc_capture");
    writer.value_string("x264_encoder");
    writer.value_string("aac_encoder");
    writer.value_string("mp4_mux");
    writer.value_string("audio_endpoint");
    writer.end_array();
    writer.end_object();
}

RequestOutcome handle_request(const std::string& line,
                              const ObsProbeResult& obs,
                              const std::string& pipe_name,
                              int requests_handled, long long uptime_ms)
{
    RequestOutcome outcome;
    RequestInfo info;
    std::string error_code;
    if (!parse_request(line, &info, &error_code)) {
        outcome.response = error_response(info, error_code,
                                          "request is not a valid command line");
        outcome.error_code = error_code;
        return outcome;
    }

    if (info.cmd == "hello") {
        long long proto = 0;
        bool has_proto = false;
        if (!extract_json_int(line, "proto", &proto, &has_proto)) {
            outcome.response = error_response(info, "bad_proto", "proto must be an integer");
            outcome.error_code = "bad_proto";
            return outcome;
        }
        if (has_proto && proto != kIpcProtocolVersion) {
            outcome.response = error_response(info, "protocol_mismatch",
                                              "supported proto=" +
                                                  std::to_string(kIpcProtocolVersion));
            outcome.error_code = "protocol_mismatch";
            return outcome;
        }
        JsonWriter writer(false);
        writer.begin_object();
        writer.key("id");
        info.has_id ? writer.value_int(info.id) : writer.value_null();
        writer.key("ok");
        writer.value_bool(true);
        writer.key("cmd");
        writer.value_string("hello");
        writer.key("server");
        writer.value_string(kIpcServerName);
        writer.key("version");
        writer.value_string(kIpcServerVersion);
        writer.key("proto");
        writer.value_int(kIpcProtocolVersion);
        writer.key("pid");
        writer.value_uint(GetCurrentProcessId());
        writer.key("pipe");
        writer.value_string(pipe_name);
        writer.key("state");
        writer.value_string("idle");
        writer.key("uptime_ms");
        writer.value_int(uptime_ms);
        writer.key("obs");
        write_obs_short(writer, obs);
        writer.key("media");
        write_media_state(writer);
        writer.key("ts");
        writer.value_string(iso_utc_now());
        writer.end_object();
        outcome.response = writer.str();
        outcome.ok = true;
        return outcome;
    }

    if (info.cmd == "status") {
        JsonWriter writer(false);
        writer.begin_object();
        writer.key("id");
        info.has_id ? writer.value_int(info.id) : writer.value_null();
        writer.key("ok");
        writer.value_bool(true);
        writer.key("cmd");
        writer.value_string("status");
        writer.key("state");
        writer.value_string("idle");
        writer.key("pid");
        writer.value_uint(GetCurrentProcessId());
        writer.key("pipe");
        writer.value_string(pipe_name);
        writer.key("uptime_ms");
        writer.value_int(uptime_ms);
        writer.key("requests_handled");
        writer.value_int(requests_handled);
        writer.key("proto");
        writer.value_int(kIpcProtocolVersion);
        writer.key("obs");
        write_obs_short(writer, obs);
        writer.key("media");
        write_media_state(writer);
        writer.key("ts");
        writer.value_string(iso_utc_now());
        writer.end_object();
        outcome.response = writer.str();
        outcome.ok = true;
        return outcome;
    }

    if (info.cmd == "shutdown") {
        JsonWriter writer(false);
        writer.begin_object();
        writer.key("id");
        info.has_id ? writer.value_int(info.id) : writer.value_null();
        writer.key("ok");
        writer.value_bool(true);
        writer.key("cmd");
        writer.value_string("shutdown");
        writer.key("state");
        writer.value_string("shutting_down");
        writer.key("requests_handled");
        writer.value_int(requests_handled);
        writer.key("ts");
        writer.value_string(iso_utc_now());
        writer.end_object();
        outcome.response = writer.str();
        outcome.ok = true;
        outcome.shutdown = true;
        return outcome;
    }

    outcome.response = error_response(
        info, "unknown_cmd",
        "supported commands in S0/S1a: hello, status, shutdown");
    outcome.error_code = "unknown_cmd";
    return outcome;
}

// ---------------------------------------------------------------------------
// Client helpers
// ---------------------------------------------------------------------------

HANDLE connect_to_pipe(const std::string& pipe_name, int timeout_ms,
                       const JsonLineSink& sink)
{
    const std::wstring wide = pipe_name_wide(pipe_name);
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::milliseconds(timeout_ms);
    int attempt = 0;
    for (;;) {
        ++attempt;
        HANDLE pipe = CreateFileW(wide.c_str(), GENERIC_READ | GENERIC_WRITE, 0,
                                  nullptr, OPEN_EXISTING,
                                  FILE_FLAG_OVERLAPPED, nullptr);
        if (pipe != INVALID_HANDLE_VALUE)
            return pipe;
        DWORD error = GetLastError();
        if (error == ERROR_PIPE_BUSY) {
            WaitNamedPipeW(wide.c_str(), 500);
        } else if (error != ERROR_FILE_NOT_FOUND) {
            sink(jsonl_event("connect_error", [&](JsonWriter& writer) {
                writer.key("pipe");
                writer.value_string(pipe_name);
                writer.key("win_error");
                writer.value_uint(error);
                writer.key("win_error_text");
                writer.value_string(format_win_error(error));
            }));
            return INVALID_HANDLE_VALUE;
        } else {
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
        if (std::chrono::steady_clock::now() >= deadline) {
            sink(jsonl_event("connect_timeout", [&](JsonWriter& writer) {
                writer.key("pipe");
                writer.value_string(pipe_name);
                writer.key("timeout_ms");
                writer.value_int(timeout_ms);
                writer.key("attempts");
                writer.value_int(attempt);
            }));
            return INVALID_HANDLE_VALUE;
        }
    }
}

struct ProbeExchange {
    bool ok = false;
    std::string error;
};

ProbeExchange exchange(HANDLE pipe, HANDLE event, int io_timeout_ms,
                       const std::string& request, const JsonLineSink& sink)
{
    ProbeExchange result;
    sink(jsonl_io("tx", "client->server", request));
    IoResult write_result =
        write_all(pipe, event, static_cast<DWORD>(io_timeout_ms), request + "\n");
    if (write_result != IoResult::Ok) {
        result.error = write_result == IoResult::Timeout ? "write_timeout"
                                                         : "write_failed";
        return result;
    }
    std::string response;
    IoResult read_result =
        read_line(pipe, event, static_cast<DWORD>(io_timeout_ms), &response);
    if (read_result != IoResult::Ok) {
        result.error = read_result == IoResult::Timeout ? "read_timeout"
                                                        : "read_failed";
        return result;
    }
    sink(jsonl_io("rx", "server->client", response));

    bool found_ok = false;
    bool ok_value = false;
    if (!extract_json_bool(response, "ok", &ok_value, &found_ok) || !found_ok) {
        result.error = "response_without_ok";
        return result;
    }
    if (!ok_value) {
        std::string error_code;
        bool found_error = false;
        extract_json_string(response, "error", &error_code, &found_error);
        result.error = found_error ? error_code : "not_ok";
        return result;
    }
    result.ok = true;
    return result;
}

std::string request_line(int id, const char* cmd, bool include_proto)
{
    JsonWriter writer(false);
    writer.begin_object();
    writer.key("id");
    writer.value_int(id);
    writer.key("cmd");
    writer.value_string(cmd);
    if (include_proto) {
        writer.key("proto");
        writer.value_int(kIpcProtocolVersion);
    }
    writer.key("client");
    writer.value_string("obs-recorder-probe");
    writer.end_object();
    return writer.str();
}

} // namespace

// ---------------------------------------------------------------------------
// Public entry points
// ---------------------------------------------------------------------------

std::string default_pipe_name()
{
    return std::string(kIpcPipePrefix) + "auto-heros3-obs-recorder-" +
           std::to_string(GetCurrentProcessId());
}

bool is_valid_pipe_name(const std::string& name)
{
    return name.rfind(kIpcPipePrefix, 0) == 0 && name.size() > 9 &&
           name.size() < 240;
}

int run_pipe_server(const ServeOptions& options, const JsonLineSink& sink)
{
    const std::string pipe_name =
        options.pipe_name.empty() ? default_pipe_name() : options.pipe_name;
    if (!is_valid_pipe_name(pipe_name)) {
        sink(jsonl_event("error", [&](JsonWriter& writer) {
            writer.key("reason");
            writer.value_string("invalid_pipe_name");
            writer.key("pipe");
            writer.value_string(pipe_name);
            writer.key("expected_prefix");
            writer.value_string(kIpcPipePrefix);
        }));
        return 2;
    }

    ObsProbeResult obs;
    if (options.load_obs) {
        std::string path_source;
        ObsPaths paths = resolve_obs_paths(options.obs_dir, &path_source);
        obs = load_and_probe_obs(paths, true);
        obs.path_source = path_source;
    }

    const std::wstring wide_pipe = pipe_name_wide(pipe_name);
    DWORD flags = PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED |
                  FILE_FLAG_FIRST_PIPE_INSTANCE;
    HANDLE pipe = CreateNamedPipeW(
        wide_pipe.c_str(), flags,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT |
            PIPE_REJECT_REMOTE_CLIENTS,
        1, kPipeBufferSize, kPipeBufferSize, 0, nullptr);
    bool first_instance = true;
    if (pipe == INVALID_HANDLE_VALUE && GetLastError() == ERROR_ACCESS_DENIED) {
        // A stale/existing instance with our name is still a local pipe; attach
        // as a non-first instance instead of failing the scaffold.
        pipe = CreateNamedPipeW(
            wide_pipe.c_str(), PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT |
                PIPE_REJECT_REMOTE_CLIENTS,
            1, kPipeBufferSize, kPipeBufferSize, 0, nullptr);
        first_instance = false;
    }
    if (pipe == INVALID_HANDLE_VALUE) {
        DWORD error = GetLastError();
        sink(jsonl_event("error", [&](JsonWriter& writer) {
            writer.key("reason");
            writer.value_string("create_named_pipe_failed");
            writer.key("pipe");
            writer.value_string(pipe_name);
            writer.key("win_error");
            writer.value_uint(error);
            writer.key("win_error_text");
            writer.value_string(format_win_error(error));
        }));
        return 3;
    }

    HANDLE event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!event) {
        CloseHandle(pipe);
        sink(jsonl_event("error", [&](JsonWriter& writer) {
            writer.key("reason");
            writer.value_string("create_event_failed");
        }));
        return 3;
    }

    sink(jsonl_event("listening", [&](JsonWriter& writer) {
        writer.key("pipe");
        writer.value_string(pipe_name);
        writer.key("server");
        writer.value_string(kIpcServerName);
        writer.key("version");
        writer.value_string(kIpcServerVersion);
        writer.key("proto");
        writer.value_int(kIpcProtocolVersion);
        writer.key("pid");
        writer.value_uint(GetCurrentProcessId());
        writer.key("first_pipe_instance");
        writer.value_bool(first_instance);
        writer.key("media_active");
        writer.value_bool(false);
        writer.key("obs_dll_loaded");
        writer.value_bool(obs.loaded);
        writer.key("obs_startup_called");
        writer.value_bool(obs.obs_startup_called);
    }));

    int requests_handled = 0;
    int exit_code = 0;
    bool stop = false;
    const auto server_start = std::chrono::steady_clock::now();

    while (!stop) {
        IoResult connect_result = connect_with_timeout(
            pipe, event, static_cast<DWORD>(options.idle_timeout_ms));
        if (connect_result == IoResult::Timeout) {
            sink(jsonl_event("idle_timeout", [&](JsonWriter& writer) {
                writer.key("pipe");
                writer.value_string(pipe_name);
                writer.key("idle_timeout_ms");
                writer.value_int(options.idle_timeout_ms);
            }));
            break;
        }
        if (connect_result != IoResult::Ok) {
            sink(jsonl_event("error", [&](JsonWriter& writer) {
                writer.key("reason");
                writer.value_string("connect_failed");
                writer.key("pipe");
                writer.value_string(pipe_name);
            }));
            exit_code = 3;
            break;
        }

        sink(jsonl_event("client_connected", [&](JsonWriter& writer) {
            writer.key("pipe");
            writer.value_string(pipe_name);
            writer.key("requests_handled");
            writer.value_int(requests_handled);
        }));

        while (!stop) {
            std::string line;
            IoResult read_result = read_line(
                pipe, event, static_cast<DWORD>(options.read_timeout_ms), &line);
            if (read_result == IoResult::Closed) {
                sink(jsonl_event("client_disconnected", [&](JsonWriter&) {}));
                break;
            }
            if (read_result == IoResult::Timeout) {
                sink(jsonl_event("read_timeout", [&](JsonWriter& writer) {
                    writer.key("read_timeout_ms");
                    writer.value_int(options.read_timeout_ms);
                }));
                break;
            }
            if (read_result != IoResult::Ok) {
                sink(jsonl_event("error", [&](JsonWriter& writer) {
                    writer.key("reason");
                    writer.value_string("read_failed");
                }));
                break;
            }

            ++requests_handled;
            const long long uptime_ms = static_cast<long long>(
                std::chrono::duration_cast<std::chrono::milliseconds>(
                    std::chrono::steady_clock::now() - server_start)
                    .count());
            RequestOutcome outcome = handle_request(line, obs, pipe_name,
                                                    requests_handled,
                                                    uptime_ms);
            sink(jsonl_event("request", [&](JsonWriter& writer) {
                writer.key("requests_handled");
                writer.value_int(requests_handled);
                writer.key("line");
                writer.value_string(line);
            }));
            IoResult write_result = write_all(
                pipe, event, static_cast<DWORD>(options.read_timeout_ms),
                outcome.response + "\n");
            if (write_result != IoResult::Ok) {
                sink(jsonl_event("error", [&](JsonWriter& writer) {
                    writer.key("reason");
                    writer.value_string("write_failed");
                }));
                exit_code = 3;
                stop = true;
                break;
            }
            sink(jsonl_event("response", [&](JsonWriter& writer) {
                writer.key("ok");
                writer.value_bool(outcome.ok);
                writer.key("error");
                outcome.error_code.empty() ? writer.value_null()
                                           : writer.value_string(outcome.error_code);
                writer.key("shutdown");
                writer.value_bool(outcome.shutdown);
            }));

            if (outcome.shutdown) {
                stop = true;
                break;
            }
            if (requests_handled >= options.max_requests) {
                sink(jsonl_event("max_requests_reached", [&](JsonWriter& writer) {
                    writer.key("max_requests");
                    writer.value_int(options.max_requests);
                }));
                stop = true;
                break;
            }
        }
        DisconnectNamedPipe(pipe);
    }

    sink(jsonl_event("stopping", [&](JsonWriter& writer) {
        writer.key("pipe");
        writer.value_string(pipe_name);
        writer.key("requests_handled");
        writer.value_int(requests_handled);
        writer.key("exit_code");
        writer.value_int(exit_code);
    }));

    CloseHandle(event);
    CloseHandle(pipe);
    unload_obs();
    return exit_code;
}

int run_ipc_probe(const ClientOptions& options, const JsonLineSink& sink)
{
    const std::string pipe_name = options.pipe_name.empty()
                                      ? default_pipe_name()
                                      : options.pipe_name;
    if (!is_valid_pipe_name(pipe_name)) {
        sink(jsonl_event("error", [&](JsonWriter& writer) {
            writer.key("reason");
            writer.value_string("invalid_pipe_name");
            writer.key("pipe");
            writer.value_string(pipe_name);
        }));
        return 2;
    }

    sink(jsonl_event("connecting", [&](JsonWriter& writer) {
        writer.key("pipe");
        writer.value_string(pipe_name);
        writer.key("connect_timeout_ms");
        writer.value_int(options.connect_timeout_ms);
        writer.key("proto");
        writer.value_int(kIpcProtocolVersion);
    }));

    HANDLE pipe = connect_to_pipe(pipe_name, options.connect_timeout_ms, sink);
    if (pipe == INVALID_HANDLE_VALUE)
        return 3;

    HANDLE event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!event) {
        CloseHandle(pipe);
        return 3;
    }

    sink(jsonl_event("connected", [&](JsonWriter& writer) {
        writer.key("pipe");
        writer.value_string(pipe_name);
    }));

    struct Step {
        const char* cmd;
        bool include_proto;
    };
    const Step steps[] = {
        {"hello", true}, {"status", false}, {"shutdown", false},
    };

    int responses_ok = 0;
    int failures = 0;
    int id = 0;
    for (const Step& step : steps) {
        if (step.cmd == std::string("shutdown") && !options.send_shutdown)
            break;
        ++id;
        ProbeExchange exchange_result =
            exchange(pipe, event, options.io_timeout_ms,
                     request_line(id, step.cmd, step.include_proto), sink);
        if (exchange_result.ok) {
            ++responses_ok;
        } else {
            ++failures;
            sink(jsonl_event("step_failed", [&](JsonWriter& writer) {
                writer.key("cmd");
                writer.value_string(step.cmd);
                writer.key("error");
                writer.value_string(exchange_result.error);
            }));
            if (step.cmd == std::string("shutdown"))
                break;
        }
    }

    CloseHandle(event);
    CloseHandle(pipe);

    const bool ok = failures == 0 && responses_ok == 3;
    sink(jsonl_event("probe_summary", [&](JsonWriter& writer) {
        writer.key("ok");
        writer.value_bool(ok);
        writer.key("pipe");
        writer.value_string(pipe_name);
        writer.key("responses_ok");
        writer.value_int(responses_ok);
        writer.key("failures");
        writer.value_int(failures);
    }));
    return ok ? 0 : 1;
}

int run_self_test(const std::wstring& obs_dir, const JsonLineSink& sink)
{
    const std::string pipe_name = default_pipe_name();
    ServeOptions serve_options;
    serve_options.pipe_name = pipe_name;
    serve_options.obs_dir = obs_dir;
    serve_options.idle_timeout_ms = 15000;
    serve_options.read_timeout_ms = 10000;
    serve_options.load_obs = true;

    int server_exit = -1;
    std::thread server_thread([&]() {
        server_exit = run_pipe_server(serve_options, sink);
    });

    ClientOptions client_options;
    client_options.pipe_name = pipe_name;
    client_options.connect_timeout_ms = 10000;
    client_options.io_timeout_ms = 10000;
    client_options.send_shutdown = true;
    int probe_exit = run_ipc_probe(client_options, sink);

    server_thread.join();
    const bool ok = probe_exit == 0 && server_exit == 0;
    sink(jsonl_event("selftest_result", [&](JsonWriter& writer) {
        writer.key("ok");
        writer.value_bool(ok);
        writer.key("pipe");
        writer.value_string(pipe_name);
        writer.key("server_exit");
        writer.value_int(server_exit);
        writer.key("probe_exit");
        writer.value_int(probe_exit);
        writer.key("media_active");
        writer.value_bool(false);
    }));
    return ok ? 0 : 1;
}

} // namespace a3
