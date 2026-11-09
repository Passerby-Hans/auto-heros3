# Private SDL capture backend

The recorder must not modify or replace the Conan cache/system `SDL2.dll`. The
backend is a project-private SDL **2.32.10** build containing an
`a3capture` audio backend that hands real PCM to the recording pipeline and
never opens a physical playback device.

## Source lock

- Upstream source tag: `release-2.32.10`
- Upstream repository: `https://github.com/libsdl-org/SDL`
- Local target: `vendor/sdl-2.32.10/` (tracked patch/source lock only; the
  source tree itself is git-ignored)
- Build output: project-private `SDL2.dll`, staged next to the VCMI executable,
  never into `C:\Windows`, Conan's package cache, or the system PATH.
- Runtime acceptance: verify the loaded DLL path, file hash, `SDL_GetVersion()`,
  `SDL_GetCurrentAudioDriver()` and negotiated format before recording starts.

## Backend contract

- SDL audio callback receives/produces 1024 frames per callback at 44100 Hz.
- The callback records planned and actual QPC timestamps and pushes a bounded
  packet to a worker; it performs no pipe/file/log/flush I/O.
- Absolute deadline = capture epoch QPC + cumulative sample frames converted to
  QPC ticks; processing time is included in the current period, not added as a
  new full-period sleep.
- Queue overflow, late callbacks and device reopen are explicit flags in the
  protocol packet (`platform/audio-protocol/`); they are never silent.
- The backend never falls back to a physical output device. If unavailable,
  recording fails with a source error.

## Why building SDL is required

SDL audio backends are compiled into `SDL2.dll`; they are not dynamically
loadable recorder plugins. A patched backend therefore requires a pinned
source build and a local deployment, with an explicit ABI/DLL hash check.

For a reproducible delivery build, pass `-ExpectedCommit <full-40-character-id>`
and `-RequireClean` to `build-private-sdl.ps1`. `-SkipSmoke` builds without
starting the capture smoke; the existing smoke remains the default when that
switch is absent. `stage/build.json` references the original record under
`build/provenance`. Builds no longer write output hashes back to
`source-lock.json`: its old `private_dll_*` entries are historical, while the
current artifact hash comes from the build manifest. See
`../obs-recorder/NOTICE.md` for the shared provenance and distribution notes.

## Live delivery and archive queue policy

These limits protect different boundaries and must not be treated as one
latency allowance:

| Boundary | Default | Behavior |
| --- | --- | --- |
| Live packet age, from capture sample PTS to reader | 500ms | Before first submission only, old history may be skipped. After first submission, age greater than the budget fails with `late_budget_exceeded`; no PTS rebasing, dropped-block concealment, or silence injection. |
| Growing-file progress | 5s | No new bytes for more than 5s fails with `capture_stalled`. Returning stale bytes may instead trigger the packet-age check. |
| Producer archive capacity | 256 slots, including the worker's in-flight packet | At 1024 frames / 44100 Hz this stores at most 262144 frames (5.9443s) from an initially empty ring. Occupied slots reduce available capacity. Overflow marks the exact dropped sample interval and fails integrity. It does not grant 5.94s of acceptable live latency. |

The default delivery matrix is: a 300ms-old packet is eligible for submission;
a 1s-old packet after recording has started fails; a 6s return is late, while
6s without file progress fails the stall check. If a producer worker is blocked
for 6s with callbacks continuing at their nominal cadence, the 256-slot ring
also exhausts. Which error is observed first depends on the injected boundary.
Eligibility does not prove libobs output retained all samples; live source and
decoded-output tests are separate gates.

The shared pure policy is `platform/obs-recorder/src/audio/audio_delivery_policy.hpp`.
`audio-core-tests` exercises 300ms / 1s / 6s decisions and exact thresholds without
opening SDL/libobs. Those tests are not a substitute for real worker/source
stall injection or output A/V validation.

Worker logs now distinguish observed queue residence (`queue_wait_max_us`), the
header+payload write calls (`write_io_max_us`), enqueue-to-write completion
(`write_latency_max_us`), and capture-PTS-to-write completion
(`capture_to_write_max_us`). All are QPC measurements rounded up to microseconds;
`measurements` and `clock_errors` distinguish unobserved data from a measured
zero. `queue_depth_max` excludes the worker's in-flight packet; `slots_used_max`
includes it. Final/closed observations must have `measurements > 0`,
`clock_errors == 0`, and maxima within the tested scenario's limits.
Successful writes mean OS acceptance, not a flush-to-storage durability guarantee.

For a single producer-stall experiment, set
`A3CAPTURE_TEST_WORKER_STALL_ONCE_MS=300`, `1000`, or `6000`; it pauses the worker
once after one completed packet by default. Set
`A3CAPTURE_TEST_WORKER_STALL_AFTER_PACKETS` to postpone that injection. Existing
`A3CAPTURE_TEST_WORKER_DELAY_MS` delays every packet and tests sustained write
throughput, so it must not be used as evidence for an isolated stall.
