#!/usr/bin/env python3
"""Measure A/V pulse onsets in split MP4 clips using presentation PTS.

For each clip:
  - read stream start_time with ffprobe (edit lists included);
  - decode video to grayscale 60fps and audio to 44.1k mono;
  - detect every white-frame run and audible pulse onset;
  - use native video presentation PTS and the decoded audio sample grid.
delta = video_onset - audio_onset (positive: video later).
"""
from __future__ import annotations
import argparse
import json
import math
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np


def stream_start(mp4: Path) -> dict[str, float]:
    raw = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=index,codec_type,start_time", "-of", "json", str(mp4)],
        capture_output=True, text=True, check=True).stdout
    return {s["codec_type"]: float(s.get("start_time") or 0)
            for s in json.loads(raw)["streams"]}


def pulse_events(mask: np.ndarray, dt: float):
    """Return all rising-edge onsets, not just the first pulse."""
    idx = np.flatnonzero(mask)
    if len(idx) == 0:
        return []
    # 100 ms of hysteresis prevents one noisy RMS window from creating
    # multiple events inside the same pulse.
    gap = max(1, int(round(0.10 / dt)))
    out = [idx[0]]
    previous = idx[0]
    for i in idx[1:]:
        if i - previous >= gap:
            out.append(i)
        previous = i
    return [float(i * dt) for i in out]


def measure(mp4: Path) -> dict:
    starts = stream_start(mp4)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        wav = td / "a.wav"
        gray = td / "v.gray"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(mp4),
                        "-vn", "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le",
                        str(wav)], check=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(mp4),
                        "-an", "-vsync", "0", "-vf", "scale=160:90,format=gray",
                        "-f", "rawvideo", str(gray)], check=True)
        pts_raw = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(mp4)],
            capture_output=True, text=True, check=True).stdout
        video_pts = [float(x.strip().rstrip(",")) for x in pts_raw.splitlines() if x.strip().rstrip(",")]
        with wave.open(str(wav), "rb") as w:
            a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
            sr = w.getframerate()
        # The test capture is a clean tone.  Use the first finite sample
        # above threshold rather than a forward-looking RMS window, which
        # reported onsets roughly one window early.
        audio_events = pulse_events(np.abs(a) > 0.02, 1.0 / sr)
        img = np.fromfile(gray, dtype=np.uint8)
        n = len(img) // (160 * 90)
        mean = img.reshape(n, 90, 160).mean(axis=(1, 2)) / 255
        if len(video_pts) != n:
            raise ValueError(f"presentation_pts_count_mismatch: pts={len(video_pts)} frames={n}")
        if not all(math.isfinite(value) for value in video_pts):
            raise ValueError("non_finite_video_presentation_pts")
        if any(b <= a for a, b in zip(video_pts, video_pts[1:])):
            raise ValueError("non_monotonic_video_presentation_pts")
        # Detect pulse runs on the native presentation PTS grid; the previous
        # fps=60 filter invented a new time grid and shifted later events.
        gray_on = mean > 0.5
        runs = []
        start_i = None
        for i, on in enumerate(gray_on):
            if on and start_i is None:
                start_i = i
            elif not on and start_i is not None:
                runs.append(start_i)
                start_i = None
        if start_i is not None:
            runs.append(start_i)
        video_events = [video_pts[i] for i in runs]
    # ffprobe frame PTS already includes the stream presentation start.
    audio_events = [a + starts.get("audio", 0.0) for a in audio_events]
    pair_count = min(len(video_events), len(audio_events))
    event_deltas = [round((video_events[i] - audio_events[i]) * 1000, 3)
                    for i in range(pair_count)]
    video_on = video_events[0] if video_events else None
    audio_on = audio_events[0] if audio_events else None
    delta_ms = event_deltas[0] if event_deltas else None
    return {"clip": mp4.name, "duration": round(n / 60, 3),
            "video_start": starts.get("video", 0.0),
            "audio_start": starts.get("audio", 0.0),
            "video_onset": video_on, "audio_onset": audio_on,
            "video_event_count": len(video_events),
            "audio_event_count": len(audio_events),
            "event_deltas": event_deltas,
            "delta_ms": delta_ms,
            "pass_20ms": bool(event_deltas) and all(abs(v) <= 20.0 for v in event_deltas)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--json")
    a = ap.parse_args()
    root = Path(a.dir)
    results = [measure(p) for p in sorted(root.glob("clip-*.mp4"))]
    print(json.dumps(results, indent=2))
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
    bad = []
    for r in results:
        # A pulse fixture has an explicit event contract.  Missing/mismatched
        # events cannot be waived just because this happens to be the last file.
        if (r["video_event_count"] != r["audio_event_count"] or
                not r["pass_20ms"]):
            bad.append(r)
    if not results:
        bad = [{"clip": "<empty>", "error": "no clips"}]
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
