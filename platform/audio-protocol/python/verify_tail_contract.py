#!/usr/bin/env python3
"""Check the bounded audio tail contract for one native recording.

Rules:
  - source tail target > one AAC frame (1024 @ 44100) => source_loss;
  - last clip video/audio presentation difference > one AAC frame => tail_gap;
  - negative difference (audio longer than video) is reported as overlap.

This is an explicit packetization-boundary contract, not a claim of sample-lossless
recording.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

AAC_FRAME_MS = 1024.0 * 1000.0 / 44100.0


def probe(path: Path):
    raw = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    out = {}
    for s in json.loads(raw).get("streams", []):
        if s.get("codec_type") in ("video", "audio"):
            out[s["codec_type"]] = float(s.get("duration") or 0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--json")
    a = ap.parse_args()
    root = Path(a.dir)
    selftest = json.loads((root / "selftest.json").read_text(encoding="utf-8"))
    tail = (selftest.get("media") or {}).get("tail")
    clips = sorted(root.glob("clip-*.mp4"))
    if not clips or not tail:
        result = {"ok": False, "error": "missing_clips_or_tail"}
    else:
        last = probe(clips[-1])
        delta = (last.get("video", 0) - last.get("audio", 0)) * 1000.0
        target = int(tail.get("tail_target_samples") or 0)
        source_loss = target > 1024
        tail_gap = delta > AAC_FRAME_MS + 1.0
        overlap = delta < -1.0
        result = {
            "ok": not (source_loss or tail_gap or overlap),
            "contract": "bounded_edge_packetization",
            "aac_frame_ms": round(AAC_FRAME_MS, 3),
            "tail_source_target_samples": target,
            "last_video_s": last.get("video"),
            "last_audio_s": last.get("audio"),
            "tail_delta_ms": round(delta, 3),
            "source_loss": source_loss,
            "tail_gap": tail_gap,
            "overlap": overlap,
            "tail": tail,
        }
    text = json.dumps(result, indent=2)
    print(text)
    if a.json:
        Path(a.json).write_text(text, encoding="utf-8")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
