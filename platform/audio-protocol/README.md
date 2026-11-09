# a3 audio protocol v1 (A0)

This directory contains the wire schema and validation core for the private SDL
capture backend. The packet format is independent of SDL, VCMI and libobs;
the optional live shared-memory transport is a Windows ABI.

## Wire format

- 128-byte little-endian header, then `payload_bytes` of interleaved PCM.
- PCM is s16le, stereo, at the header's `sample_rate`.
- `sample_start`/`sample_count` are a half-open **sample-frame** range.
- A packet may be split or coalesced on the transport; header size, payload
  size and CRC32 delimit it. Consumers must not infer packet boundaries from
  read/write calls.
- `planned_qpc` is the absolute producer deadline. `actual_start_qpc` and
  `actual_end_qpc` are observations, not idealised values.
- `capture_epoch_qpc` is the QPC at `sample_start == 0`; all sample-to-time
  conversion uses that origin, never UTC.
- `generation` increments when the SDL device/capture is reset. `sequence`
  starts at zero per generation.
- `flags` carry first/last/discontinuity/queue-overflow/device-restart/
  producer-error. A discontinuity must also set a non-empty missing interval.
- `header_crc32` covers the header with that field zeroed and includes
  `payload_crc32`; `payload_crc32` covers exactly the payload bytes.
- `reserved0/reserved1` must be zero for v1.

## Validation contract

`ValidatePacket()` rejects wrong magic, version, header size, format, unknown
flags, empty/overflowing sample ranges, mismatched payload length, missing or
bad CRCs, invalid QPC intervals, and discontinuity flag/interval inconsistency.
The relay/native source must preserve the packet header and CRC values; it may
add separate receive/flush timestamps outside the wire header.

## Live shared-memory transport

`include/a3_audio_live.h` defines `a3-audio-live/1`: a 128-byte metadata header
and 256 fixed slots containing the unchanged CRC-protected `a3-audio/1` packet.
The mapping is pagefile-backed and named from the capture ID and generation.
Producer PID and creation FILETIME bind the mapping to the owned game process.
It is a live delivery channel, not a persistent recovery file.

The single writer sets an aligned slot stamp to odd, copies packet bytes, then
publishes an even stamp followed by the global sequence. The reader snapshots
stamp/bytes/stamp and accepts only matching expected even stamps, then performs
normal packet and stream validation. Readers never hold a cross-process slot
lock. Overwrite or missing samples after attachment are fatal; restarting a
consumer creates a new retained interval and does not claim a continuous old run.

An independently bounded archive queue writes the same packets. Its committed
sample endpoint, failure state and archived LAST are separate from live progress.
The native source keeps capture QPC timestamps regardless of archive latency.
Successful archival writes acknowledge OS acceptance, not fsync or power-loss
durability. See `../sdl-capture-patch/README.md` for delivery/stop budgets.

## Source lock

The production backend must pin SDL **2.32.10** source and report the actual
loaded DLL path/hash/version plus `SDL_GetCurrentAudioDriver()`, the negotiated
audio format, QPC frequency and protocol version before any recording starts.
Setting `SDL_AUDIODRIVER` alone is not verification.

## Not here yet

The private SDL backend (`platform/sdl-capture-patch/`), relay framing, QPC
scheduler, libobs fixed-buffer experiment and recovery worker are subsequent
steps. No code in this directory changes the current recorder default.
