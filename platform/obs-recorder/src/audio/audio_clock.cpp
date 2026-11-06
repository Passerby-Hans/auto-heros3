#include "audio_clock.hpp"
#include "audio_clock_c.h"

#include <limits>
#include <sstream>

namespace a3::audio {
namespace {

a3_qpc_clock ToC(const SampleClock& clock)
{
    return a3_qpc_clock{clock.epoch_qpc(), clock.qpc_frequency(), clock.sample_rate()};
}

}  // namespace

std::optional<uint64_t> SampleClock::deadline_qpc(uint64_t frame) const
{
    if (!valid()) {
        return std::nullopt;
    }
    const a3_qpc_clock c = ToC(*this);
    uint64_t deadline = 0;
    if (!a3_qpc_deadline(&c, frame, &deadline)) {
        return std::nullopt;
    }
    return deadline;
}

int64_t SampleClock::remaining_qpc(uint64_t now_qpc, uint64_t frame) const
{
    if (!valid()) {
        return std::numeric_limits<int64_t>::min();
    }
    const a3_qpc_clock c = ToC(*this);
    return a3_qpc_remaining(&c, now_qpc, frame);
}

int64_t FrameScheduler::remaining_qpc(uint64_t now_qpc) const
{
    return clock_.remaining_qpc(now_qpc, next_frame_);
}

bool FrameScheduler::within_catchup(uint64_t now_qpc, uint64_t max_catchup_qpc) const
{
    const auto deadline = clock_.deadline_qpc(next_frame_);
    if (!deadline.has_value()) {
        return false;
    }
    return now_qpc <= *deadline || now_qpc - *deadline <= max_catchup_qpc;
}

std::string FrameScheduler::describe() const
{
    std::ostringstream os;
    os << "sample_clock epoch_qpc=" << clock_.epoch_qpc()
       << " qpc_frequency=" << clock_.qpc_frequency()
       << " sample_rate=" << clock_.sample_rate()
       << " next_frame=" << next_frame_;
    return os.str();
}

}  // namespace a3::audio
