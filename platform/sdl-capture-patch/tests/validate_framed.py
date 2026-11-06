#!/usr/bin/env python3
"""Validate an a3-audio/1 framed PCM stream produced by the SDL backend."""
from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from pathlib import Path

HEADER = 128
FLAG_FIRST = 1 << 0
FLAG_LAST = 1 << 1
FLAG_DISCONTINUITY = 1 << 2
FORMAT_S16LE = 1


def parse(path: Path, allow_gaps: bool) -> dict:
    data = path.read_bytes()
    if len(data) < HEADER:
        raise ValueError(f"file too short: {len(data)}")
    offset = 0
    packets = []
    total_frames = 0
    capture_id = None
    generation = None
    expected_sequence = None
    while offset < len(data):
        if offset + HEADER > len(data):
            raise ValueError(f"truncated header at {offset}")
        h = data[offset:offset + HEADER]
        (magic, version, header_bytes, flags, cap, gen, seq, rate, channels,
         sample_format, payload_bytes, sample_start, sample_count) = struct.unpack_from(
            "<4sHHIQIIIHHIQQ", h, 0)
        (planned, actual_start, actual_end, qpc_freq, capture_epoch,
         disc_start, disc_count, header_crc, payload_crc, reserved0,
         reserved1) = struct.unpack_from("<QQQQQQQIIII", h, 56)
        if magic != b"A3AP" or version != 1 or header_bytes != HEADER:
            raise ValueError(f"bad header at {offset}")
        if reserved0 or reserved1:
            raise ValueError(f"nonzero reserved at {offset}")
        if payload_bytes != sample_count * 4:
            raise ValueError(f"payload/sample mismatch at {offset}")
        header_zero = h[:112] + b"\x00\x00\x00\x00" + h[116:]
        if zlib.crc32(header_zero) & 0xFFFFFFFF != header_crc:
            raise ValueError(f"header CRC mismatch at {offset}")
        payload = data[offset + HEADER:offset + HEADER + payload_bytes]
        if len(payload) != payload_bytes:
            raise ValueError(f"truncated payload at {offset}")
        if zlib.crc32(payload) & 0xFFFFFFFF != payload_crc:
            raise ValueError(f"payload CRC mismatch at {offset}")
        if capture_id is None:
            capture_id, generation = cap, gen
        elif (cap, gen) != (capture_id, generation):
            raise ValueError("capture/generation changed mid-stream")
        if expected_sequence is None:
            expected_sequence = seq
        if seq != expected_sequence and not allow_gaps:
            raise ValueError(f"sequence gap: {expected_sequence} != {seq}")
        expected_sequence = seq + 1
        if sample_start != total_frames and not allow_gaps:
            raise ValueError(f"sample gap: {sample_start} != {total_frames}")
        if not allow_gaps and sample_count and not flags & FLAG_DISCONTINUITY:
            if sample_start != total_frames:
                raise ValueError("unexplained sample discontinuity")
        packets.append({
            "seq": seq, "flags": flags, "frames": sample_count,
            "start": sample_start, "payload_bytes": payload_bytes,
            "header_crc_ok": True, "payload_crc_ok": True,
        })
        total_frames = max(total_frames, sample_start + sample_count)
        offset += HEADER + payload_bytes
    if offset != len(data):
        raise ValueError(f"trailing bytes: {len(data) - offset}")
    if not packets:
        raise ValueError("no packets")
    if not packets[0]["flags"] & FLAG_FIRST:
        raise ValueError("first packet lacks FIRST")
    if not packets[-1]["flags"] & FLAG_LAST:
        raise ValueError("last packet lacks LAST")
    if packets[-1]["frames"] != 0:
        raise ValueError("last packet must be zero-length")
    if packets[-1]["start"] != total_frames:
        raise ValueError("last packet start mismatch")
    return {
        "packets": len(packets),
        "frames": total_frames,
        "seconds": total_frames / 44100.0,
        "capture_id": capture_id,
        "generation": generation,
        "last_flags": packets[-1]["flags"],
        "first_flags": packets[0]["flags"],
        "bytes": len(data),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--allow-gaps", action="store_true")
    ap.add_argument("--require-last", action="store_true",
                    help="accepted for explicit complete-capture checks")
    ap.add_argument("--json")
    args = ap.parse_args()
    try:
        result = parse(Path(args.path), args.allow_gaps)
    except Exception as exc:
        print(f"FAIL: {exc}")
        return 1
    print(json.dumps(result, sort_keys=True))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
