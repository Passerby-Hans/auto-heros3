#include "audio_clock.hpp"

#include <limits>
#include <sstream>

namespace a3::audio {
namespace {

constexpr uint64_t kMaxU64 = std::numeric_limits<uint64_t>::max();
constexpr uint64_t kMaxI64 = static_cast<uint64_t>(std::numeric_limits<int64_t>::max());

}  // namespace

std::optional<uint64_t> SampleClock::deadline_qpc(uint64_t frame) const
{
    if (!valid()) {
        return std::nullopt;
    }
    const uint64_t q = frame / sample_rate_;
    const uint64_t r = frame % sample_rate_;
    if (q != 0 && qpc_frequency_ > (kMaxU64 - epoch_qpc_) / q) {
        return std::nullopt;
    }
    const uint64_t base = q * qpc_frequency_;
    uint64_t remainder = 0;
    if (r != 0) {
        if (qpc_frequency_ > kMaxU64 / r) {
            return std::nullopt;
        }
        remainder = (r * qpc_frequency_) / sample_rate_;
    }
    if (base > kMaxU64 - epoch_qpc_ ||
        remainder > kMaxU64 - epoch_qpc_ - base) {
        return std::nullopt;
    }
    return epoch_qpc_ + base + remainder;
}

int64_t SampleClock::remaining_qpc(uint64_t now_qpc, uint64_t frame) const
{
    const auto deadline = deadline_qpc(frame);
    if (!deadline.has_value()) {
        return std::numeric_limits<int64_t>::min();
    }
    if (*deadline >= now_qpc) {
        const uint64_t delta = *deadline - now_qpc;
        return delta > kMaxI64 ? std::numeric_limits<int64_t>::max()
                               : static_cast<int64_t>(delta);
    }
    const uint64_t delta = now_qpc - *deadline;
    if (delta > kMaxI64) {
        return std::numeric_limits<int64_t>::min();
    }
    return -static_cast<int64_t>(delta);
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
