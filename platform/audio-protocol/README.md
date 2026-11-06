# a3 audio protocol v1 (A0)

This directory is the schema and validation core for the planned private SDL
capture backend. It is deliberately independent of SDL, VCMI and libobs.

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

## Source lock

The production backend must pin SDL **2.32.10** source and report the actual
loaded DLL path/hash/version plus `SDL_GetCurrentAudioDriver()`, the negotiated
audio format, QPC frequency and protocol version before any recording starts.
Setting `SDL_AUDIODRIVER` alone is not verification.

## Not here yet

The private SDL backend (`platform/sdl-capture-patch/`), relay framing, QPC
scheduler, libobs fixed-buffer experiment and recovery worker are subsequent
steps. No code in this directory changes the current recorder default.
