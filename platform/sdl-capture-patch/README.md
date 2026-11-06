# Private SDL capture backend (A2, not implemented yet)

The recorder must not modify or replace the Conan cache/system `SDL2.dll`. The
planned backend is a project-private SDL **2.32.10** build containing an
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
