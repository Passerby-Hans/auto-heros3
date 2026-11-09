#!/usr/bin/env python3
"""Create a deterministic a3-audio/1 pulse capture for A4/gate-2 tests."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import zlib
from pathlib import Path

HEADER = 128
FLAG_FIRST = 1
FLAG_LAST = 2


def make_packet(seq: int, start: int, frames: int, payload: bytes,
                qpc: int, flags: int, capture_id: int,
                capture_epoch_qpc: int = 1000000000) -> bytes:
    h = bytearray(HEADER)
    struct.pack_into("<4sHHIQIIIHHIQQ", h, 0, b"A3AP", 1, HEADER, flags,
                     capture_id, 1, seq, 44100, 2, 1, len(payload),
                     start, frames)
    struct.pack_into("<QQQQQQQIIII", h, 56, qpc, qpc,
                     qpc + frames * 10000000 // 44100, 10000000,
                     capture_epoch_qpc, 0, 0, 0, 0, 0, 0)
    struct.pack_into("<I", h, 116, zlib.crc32(payload) & 0xFFFFFFFF)
    struct.pack_into("<I", h, 112, 0)
    struct.pack_into("<I", h, 112, zlib.crc32(h) & 0xFFFFFFFF)
    return bytes(h) + payload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--amplitude", type=float, default=0.6)
    ap.add_argument("--pattern", choices=("pulse", "pn"), default="pulse")
    ap.add_argument("--seed", type=int, default=670887)
    a = ap.parse_args()
    out = Path(a.out)
    total = int(a.seconds * 44100)
    payload_frames = 1024
    seq = 0
    start = 0
    base_qpc = 1000000000
    capture_id = 0x1234567890ABCDEF
    pn = None
    if a.pattern == "pn":
        from audio_locator import make_pn
        pn = make_pn(total, a.amplitude, a.seed)
    pcm_digest = hashlib.sha256()
    with out.open("wb") as f:
        while start < total:
            frames = min(payload_frames, total - start)
            if pn is not None:
                pcm = pn[start:start + frames].tobytes()
            else:
                pcm = bytearray()
                for i in range(frames):
                    t = (start + i) / 44100.0
                    phase = t % 1.0
                    v = 0
                    if phase < 0.25:
                        v = int(32767 * a.amplitude * math.sin(2 * math.pi * 1000.0 * t))
                    pcm += struct.pack("<hh", v, v)
            pcm_digest.update(pcm)
            flags = FLAG_FIRST if seq == 0 else 0
            f.write(make_packet(seq, start, frames, bytes(pcm),
                                base_qpc + start * 10000000 // 44100,
                                flags, capture_id))
            start += frames
            seq += 1
        f.write(make_packet(seq, total, 0, b"", base_qpc + total * 10000000 // 44100,
                            FLAG_LAST, capture_id))
    print(f"wrote {out} packets={seq + 1} frames={total}")
    if a.pattern == "pn":
        manifest = {"schema": "a3-pn-fixture/1", "pattern": "continuous_bandlimited_pn",
                    "sample_rate": 44100, "channels": 2, "frames": total,
                    "seed": a.seed, "generator": "numpy.PCG64", "amplitude": a.amplitude,
                    "filter": "129-tap Hamming bandpass 700..5000 Hz",
                    "pcm_sha256": pcm_digest.hexdigest(),
                    "framed_sha256": hashlib.sha256(out.read_bytes()).hexdigest()}
        out.with_suffix(out.suffix + ".fixture.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
