// Live delivery limits are separate from producer archive queue capacity.
// No timestamp rebasing, silence insertion, or resampling is permitted here.
#pragma once

#include <cstdint>

namespace a3::audio {

constexpr uint64_t kDefaultLateBudgetNs = 500000000ULL;
constexpr uint64_t kCaptureStallNs = 5000000000ULL;

enum class LivePacketDisposition { Submit, SkipPreroll, RejectLate };

inline LivePacketDisposition ClassifyLivePacket(uint64_t packet_age_ns,
                                               uint64_t late_budget_ns,
                                               bool first_submitted)
{
    if (packet_age_ns <= late_budget_ns) return LivePacketDisposition::Submit;
    return first_submitted ? LivePacketDisposition::RejectLate
                           : LivePacketDisposition::SkipPreroll;
}

inline bool CaptureStalled(uint64_t now_ns, uint64_t last_progress_ns)
{
    return now_ns >= last_progress_ns && now_ns - last_progress_ns > kCaptureStallNs;
}

} // namespace a3::audio
