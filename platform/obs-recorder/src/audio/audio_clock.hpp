// Absolute QPC scheduling core for the planned SDL capture backend.
//
// This module is deliberately pure: it does not sleep, open devices or know
// about libobs.  Tests inject fake QPC values.  The producer uses the same
// epoch/frequency from SDL's device callback and records both planned and
// actual callback times.
#pragma once

#include <cstdint>
#include <limits>
#include <optional>
#include <string>

namespace a3::audio {

// Maps a cumulative sample-frame counter to an absolute QPC deadline:
//
//   deadline(frame) = epoch_qpc + floor(frame * qpc_frequency / sample_rate)
//
// The quotient/remainder split avoids repeatedly rounding a per-block period,
// so the error does not accumulate over long recordings.
class SampleClock {
public:
    SampleClock() = default;
    SampleClock(uint64_t epoch_qpc, uint64_t qpc_frequency, uint32_t sample_rate)
        : epoch_qpc_(epoch_qpc), qpc_frequency_(qpc_frequency), sample_rate_(sample_rate)
    {
    }

    bool valid() const
    {
        return qpc_frequency_ != 0 && sample_rate_ != 0;
    }

    uint64_t epoch_qpc() const { return epoch_qpc_; }
    uint64_t qpc_frequency() const { return qpc_frequency_; }
    uint32_t sample_rate() const { return sample_rate_; }

    // Returns std::nullopt if the exact integer deadline would overflow.
    std::optional<uint64_t> deadline_qpc(uint64_t frame) const;

    // Signed distance from now to the deadline; negative means late.
    // Callers must already have validated the clock and deadline.
    int64_t remaining_qpc(uint64_t now_qpc, uint64_t frame) const;

private:
    uint64_t epoch_qpc_ = 0;
    uint64_t qpc_frequency_ = 0;
    uint32_t sample_rate_ = 0;
};

// Stateful frame cursor for one callback stream.  `advance()` is called only
// after the real samples have been queued; processing time never changes the
// next absolute deadline.
class FrameScheduler {
public:
    FrameScheduler() = default;
    FrameScheduler(SampleClock clock, uint64_t first_frame = 0)
        : clock_(clock), next_frame_(first_frame)
    {
    }

    bool valid() const { return clock_.valid(); }
    const SampleClock& clock() const { return clock_; }
    uint64_t next_frame() const { return next_frame_; }

    std::optional<uint64_t> next_deadline_qpc() const
    {
        return clock_.deadline_qpc(next_frame_);
    }

    // Positive = wake later; negative = late.  Saturates to int64 bounds.
    int64_t remaining_qpc(uint64_t now_qpc) const;

    // Advances by exactly the frames actually accepted from the callback.
    // Returns false on uint64_t cursor overflow; callers must fail the capture
    // rather than wrap the sample timeline.
    bool advance(uint32_t frames)
    {
        if (frames > std::numeric_limits<uint64_t>::max() - next_frame_) {
            return false;
        }
        next_frame_ += frames;
        return true;
    }

    // A small bounded catch-up policy for the producer, not a hidden rate
    // correction.  Returns false when the late interval exceeds
    // max_catchup_qpc and the caller must emit a discontinuity/error.
    bool within_catchup(uint64_t now_qpc, uint64_t max_catchup_qpc) const;

    // Human-readable validation for startup/evidence.
    std::string describe() const;

private:
    SampleClock clock_;
    uint64_t next_frame_ = 0;
};

}  // namespace a3::audio
