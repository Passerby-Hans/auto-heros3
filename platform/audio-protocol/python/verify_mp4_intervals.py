#!/usr/bin/env python3
"""Check declared MP4 track presentation intervals.

This is a read-only structural check, not a PCM decode:
  - every audio tkhd duration must not exceed its video tkhd duration;
  - every audio edit-list media entry must be explicit;
  - report the audio/video tkhd pairs.

Usage: verify_mp4_intervals.py <dir> [--json out.json]
Exit 0 only when all clip-*.mp4 pass.
"""
from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path


def boxes(data: bytes, start: int = 0, end: int | None = None):
    end = len(data) if end is None else end
    i = start
    while i + 8 <= end:
        size = struct.unpack(">I", data[i:i + 4])[0]
        typ = data[i + 4:i + 8].decode("latin1")
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", data[i + 8:i + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - i
        if size < hdr or i + size > end:
            raise ValueError(f"bad box {typ} at {i} size={size}")
        yield typ, i, i + size
        if typ in ("moov", "trak", "mdia", "minf", "stbl", "edts"):
            yield from boxes(data, i + hdr, i + size)
        i += size


def tkhd_duration(data: bytes, off: int) -> int:
    version = data[off + 8]
    q = off + 12
    if version == 0:
        return struct.unpack(">I", data[q + 16:q + 20])[0]
    return struct.unpack(">Q", data[q + 24:q + 32])[0]


def elst_entries(data: bytes, off: int):
    version = data[off + 8]
    count = struct.unpack(">I", data[off + 12:off + 16])[0]
    q = off + 16
    out = []
    for _ in range(count):
        if version == 0:
            duration, media_time = struct.unpack(">Ii", data[q:q + 8])
            out.append((duration, media_time))
            q += 12
        else:
            duration, media_time = struct.unpack(">Qq", data[q:q + 16])
            out.append((duration, media_time))
            q += 20
    return out


def check(path: Path) -> dict:
    data = path.read_bytes()
    tracks = []
    current = None
    for typ, a, b in boxes(data):
        if typ == "trak":
            current = {"tkhd": None, "elst": [], "handler": None}
            tracks.append(current)
        elif typ == "tkhd" and current is not None:
            current["tkhd"] = tkhd_duration(data, a)
        elif typ == "elst" and current is not None:
            current["elst"].extend(elst_entries(data, a))
        elif typ == "hdlr" and current is not None:
            # hdlr payload: version/flags, pre_defined, handler_type
            handler = data[a + 16:a + 20].decode("latin1", "replace")
            current["handler"] = handler
    video = next((t for t in tracks if t.get("handler") == "vide"), None)
    audio = next((t for t in tracks if t.get("handler") == "soun"), None)
    if not video or not audio:
        return {"clip": path.name, "ok": False, "error": "missing_track_handler"}
    audio_tkhd = int(audio["tkhd"] or 0)
    video_tkhd = int(video["tkhd"] or 0)
    audio_elst = [list(x) for x in audio["elst"]]
    elst_sum = sum(int(d) for d, _ in audio_elst)
    sum_ok = bool(audio_elst) and audio_tkhd == elst_sum
    ok = bool(video_tkhd and audio_tkhd and audio_tkhd <= video_tkhd and
              audio_elst and sum_ok)
    return {"clip": path.name, "ok": ok,
            "video_tkhd_ms": video_tkhd, "audio_tkhd_ms": audio_tkhd,
            "audio_elst": audio_elst, "elst_sum_ms": elst_sum,
            "elst_sum_matches_tkhd": sum_ok,
            "overhang_ms": max(0, audio_tkhd - video_tkhd)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--json")
    a = ap.parse_args()
    root = Path(a.dir)
    results = [check(p) for p in sorted(root.glob("clip-*.mp4"))]
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
    bad = [r for r in results if not r["ok"]]
    print(json.dumps({"clips": len(results), "failed": len(bad),
                      "overhang_clips": sum(1 for r in results if r.get("overhang_ms")),
                      "elst_sum_mismatch": sum(1 for r in results
                                               if not r.get("elst_sum_matches_tkhd", True)),
                      "results": results}, indent=2))
    return 0 if results and not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
