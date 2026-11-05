// Local named-pipe IPC skeleton for the obs-recorder native host.
//
// Transport: one byte-mode named pipe, newline-delimited JSON (JSON Lines).
//   server -> client events are responses to request lines;
//   supported commands (S0/S1a): hello, status, shutdown.
// Default pipe: \\.\pipe\auto-heros3-obs-recorder-<pid>
// No media control, no recording, no audio playback is exposed.
#pragma once

#include <functional>
#include <string>

#include "obs_probe.hpp"

namespace a3 {

constexpr int kIpcProtocolVersion = 1;
constexpr const char* kIpcServerName = "obs-recorder";
constexpr const char* kIpcServerVersion = "0.1.0-s0";
constexpr const char* kIpcPipePrefix = "\\\\.\\pipe\\";

using JsonLineSink = std::function<void(const std::string& line)>;

std::string default_pipe_name();
bool is_valid_pipe_name(const std::string& name);

struct ServeOptions {
    std::string pipe_name;                    // empty -> default_pipe_name()
    std::wstring obs_dir;                     // empty -> resolve_obs_paths default
    int idle_timeout_ms = 120000;             // wait for a client
    int read_timeout_ms = 30000;              // per request line
    int max_requests = 1000;
    bool load_obs = true;
};

struct ClientOptions {
    std::string pipe_name;
    int connect_timeout_ms = 15000;
    int io_timeout_ms = 15000;
    bool send_shutdown = true;
};

// Runs the pipe server until shutdown/max_requests/idle timeout. Returns 0 on a
// clean shutdown; 2 on invalid arguments; 3 on pipe/runtime errors.
int run_pipe_server(const ServeOptions& options, const JsonLineSink& sink);

// Connects to a running server, sends hello/status/shutdown, prints a JSONL
// transcript through `sink`. Returns 0 when every response was ok:true.
int run_ipc_probe(const ClientOptions& options, const JsonLineSink& sink);

// In-process end-to-end test: server thread + client, default pid-based pipe.
int run_self_test(const std::wstring& obs_dir, const JsonLineSink& sink);

} // namespace a3
