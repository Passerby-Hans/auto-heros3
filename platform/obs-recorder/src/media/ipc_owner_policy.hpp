#pragma once
#include <cstdint>

namespace a3 {
enum class OwnerConnectDecision { Wait, Accept, OwnerUnavailable, Timeout, WrongClient };

// Pure first-connection policy. Native code supplies observations from a pinned
// process HANDLE and the named pipe; no PID lookup can replace the owner handle.
inline OwnerConnectDecision DecideOwnerConnection(bool owner_alive, uint64_t elapsed_ms,
                                                   bool connected, uint32_t peer_pid,
                                                   uint32_t owner_pid)
{
    if (!owner_alive || owner_pid == 0) return OwnerConnectDecision::OwnerUnavailable;
    if (elapsed_ms >= 30000) return OwnerConnectDecision::Timeout;
    if (!connected) return OwnerConnectDecision::Wait;
    if (peer_pid == 0 || peer_pid != owner_pid) return OwnerConnectDecision::WrongClient;
    return OwnerConnectDecision::Accept;
}
} // namespace a3
