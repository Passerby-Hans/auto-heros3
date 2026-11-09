#!/usr/bin/env python3
"""Locate decoded AAC content against non-periodic source PCM across segments.

Delivery contract: MP4 presentation intervals, with their edit lists honored.
The decoder is run WITHOUT -t, -ss, atrim, resampling or synthesized PTS. Its
full PCM/frame counts are retained in the report. We independently select
the container-declared presentation interval for measurement and also locate
decoder tail samples outside that interval. Raw PCM concatenation is reported
separately; legal encoded pre-roll/padding is not presentation duplication.

No audio is played. Requires numpy and local ffmpeg/ffprobe executables.
"""
from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import subprocess

import numpy as np

from audio_locator import RATE, load_reference, locate
from verify_mp4_intervals import check as check_intervals
from verify_tail_contract import _clip_paths


def decode_full(path: Path, ffmpeg: str, ffprobe: str) -> tuple[np.ndarray, dict]:
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_frames",
         "-show_entries", "stream=sample_rate,channels,time_base:frame=pts,nb_samples",
         "-of", "json", str(path)], capture_output=True, text=True, check=True)
    info = json.loads(probe.stdout)
    streams = info.get("streams", [])
    frames = info.get("frames", [])
    if len(streams) != 1 or not frames:
        raise ValueError("missing_decoded_audio_stream_or_frames")
    stream = streams[0]
    rate, channels = int(stream["sample_rate"]), int(stream["channels"])
    if rate != RATE or channels != 2:
        raise ValueError("unsupported_audio_format_no_resampling_allowed")
    timebase = Fraction(stream["time_base"])
    first_pts = None
    previous_end = None
    total = 0
    for frame in frames:
        if "pts" not in frame or "nb_samples" not in frame:
            raise ValueError("missing_real_decoded_frame_pts")
        pts = int(frame["pts"]) * timebase * rate
        count = int(frame["nb_samples"])
        if pts.denominator != 1 or count <= 0:
            raise ValueError("non_sample_aligned_decoded_frame")
        pts = int(pts)
        if previous_end is not None and pts != previous_end:
            raise ValueError("decoded_pts_gap_or_overlap")
        if first_pts is None:
            first_pts = pts
        previous_end = pts + count
        total += count
    command = [ffmpeg, "-v", "error", "-i", str(path), "-map", "0:a:0",
               "-vn", "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"]
    decoded = subprocess.run(command, capture_output=True, check=True)
    if len(decoded.stdout) % (4 * channels):
        raise ValueError("invalid_decoded_pcm_length")
    pcm = np.frombuffer(decoded.stdout, dtype="<f4").reshape(-1, channels)
    if len(pcm) != total or not np.all(np.isfinite(pcm)):
        raise ValueError("decoder_frame_count_or_finite_pcm_mismatch")
    return pcm.mean(axis=1, dtype=np.float64), {
        "decoded_frames_raw": len(pcm), "decoded_frame_count": len(frames),
        "decoded_first_pts_sample": first_pts, "decoded_end_pts_sample": previous_end,
        "decoded_pcm_sha256": hashlib.sha256(decoded.stdout).hexdigest(),
        "decode_command": command,
        "decode_trim_applied": False, "decoder_pts_synthesized": False,
    }


def match_clip(reference: np.ndarray, decoded: np.ndarray, *,
               presentation_frames: int, first_pts: int, global_begin: int,
               base_offset: int | None, window_samples: int = 2048,
               hop_samples: int = 1024, search_radius: int = 4096,
               initial_search_samples: int = RATE * 10,
               initial_reference_start: int = 0, tolerance_samples: int = 2,
               minimum_score: float = 0.90, minimum_margin: float = 0.10) -> tuple[dict, int | None]:
    result = {"ok": False, "status": "uncertain", "errors": [], "anchors": [],
              "presentation_frames": presentation_frames, "global_begin_sample": global_begin,
              "decoded_frames_raw": len(decoded), "decoded_first_pts_sample": first_pts,
              "raw_decode_extra_head": max(0, -first_pts),
              "raw_decode_extra_tail": max(0, first_pts + len(decoded) - presentation_frames),
              "measurement_basis": "container edit-list presentation interval; full decode retained and reported"}
    start_index = -first_pts
    if (start_index < 0 or start_index + presentation_frames > len(decoded) or
            presentation_frames < window_samples or hop_samples > window_samples):
        result["errors"].append("presentation_not_fully_covered_or_too_short")
        return result, base_offset
    positions = set(range(0, presentation_frames - window_samples + 1, hop_samples))
    positions.add(presentation_frames - window_samples)
    offsets = []
    options = {"minimum_score": minimum_score, "minimum_margin": minimum_margin}
    for position in sorted(positions):
        raw_index = start_index + position
        if base_offset is None:
            lo = initial_reference_start
            hi = lo + initial_search_samples
        else:
            expected = base_offset + global_begin + position
            lo, hi = expected - search_radius, expected + search_radius
        match = locate(reference, decoded[raw_index:raw_index + window_samples], lo, hi, **options)
        match.update({"presentation_sample": position, "raw_decode_index": raw_index})
        if match["status"] == "matched":
            offset = match["source_sample"] - global_begin - position
            match["run_source_offset"] = offset
            if base_offset is None:
                base_offset = offset
            match["offset_delta_samples"] = offset - base_offset
            offsets.append(offset)
            if abs(offset - base_offset) > tolerance_samples:
                result["errors"].append("source_position_jump")
        else:
            result["errors"].append("uncertain_source_position")
            if base_offset is None:
                result["anchors"].append(match)
                break
        result["anchors"].append(match)
    result["errors"] = sorted(set(result["errors"]))
    if offsets:
        offset = int(round(float(np.median(offsets))))
        result.update({"measured_source_offset": offset,
                       "source_offset_range": [min(offsets), max(offsets)],
                       "presentation_source_start": offset + global_begin,
                       "presentation_source_end": offset + global_begin + presentation_frames,
                       "raw_decode_source_start_inferred": offset + global_begin + first_pts,
                       "raw_decode_source_end_inferred": offset + global_begin + first_pts + len(decoded)})
    extra = result["raw_decode_extra_tail"]
    if extra and base_offset is not None:
        size = min(window_samples, extra)
        raw_index = start_index + presentation_frames
        expected = base_offset + global_begin + presentation_frames
        match = locate(reference, decoded[raw_index:raw_index + size],
                       expected - search_radius, expected + search_radius, **options)
        match.update({"raw_decode_index": raw_index, "outside_presentation": True,
                      "expected_source_sample": expected})
        if match["status"] == "matched":
            match["source_offset_delta_samples"] = match["source_sample"] - expected
        result["raw_extra_tail_location"] = match
    result["ok"] = not result["errors"] and bool(result["anchors"])
    result["status"] = "pass" if result["ok"] else (
        "fail" if "source_position_jump" in result["errors"] else "uncertain")
    return result, base_offset


def boundary_checks(clips: list[dict], tolerance_samples: int) -> list[dict]:
    checks = []
    for previous, current in zip(clips, clips[1:]):
        result = {"previous": previous["clip"], "next": current["clip"], "ok": False}
        if previous.get("ok") and current.get("ok"):
            gap = current["presentation_source_start"] - previous["presentation_source_end"]
            raw_gap = current["raw_decode_source_start_inferred"] - previous["raw_decode_source_end_inferred"]
            extra = previous.get("raw_extra_tail_location", {})
            result.update({"presentation_source_gap_samples": gap,
                           "gap_meaning": "positive=missing source samples; negative=repeated presented source samples",
                           "previous_presented_source_end": previous["presentation_source_end"],
                           "next_presented_source_start": current["presentation_source_start"],
                           "unsafe_raw_pcm_concat_gap_samples": raw_gap,
                           "raw_extra_matches_next_source_start": bool(
                               extra.get("status") == "matched" and
                               abs(extra["source_sample"] - current["presentation_source_start"]) <= tolerance_samples),
                           "ok": abs(gap) <= tolerance_samples})
        else:
            result["error"] = "boundary_positions_uncertain"
        checks.append(result)
    return checks


def check_run(root: Path, reference_path: Path, args) -> dict:
    result = {"ok": False, "contract": "decoded_mp4_presentation_source_continuity/1",
              "scope": "AAC lossy source localization; not encoded-byte identity or proof for players ignoring edit lists",
              "errors": [], "clips": [], "boundaries": []}
    try:
        reference, reference_info = load_reference(reference_path)
        fixture_path = reference_path.with_suffix(reference_path.suffix + ".fixture.json")
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        if (fixture.get("schema") != "a3-pn-fixture/1" or
                fixture.get("pattern") != "continuous_bandlimited_pn" or
                fixture.get("pcm_sha256") != reference_info["pcm_sha256"] or
                fixture.get("framed_sha256") != reference_info["framed_sha256"] or
                fixture.get("frames") != reference_info["frames"] or
                fixture.get("sample_rate") != RATE or fixture.get("channels") != 2):
            raise ValueError("missing_or_mismatched_nonperiodic_fixture_manifest")
        media = json.loads((root / "selftest.json").read_text(encoding="utf-8"))["media"]
        paths = _clip_paths(root, media)
        result.update({"reference": str(reference_path.resolve()), "reference_info": reference_info,
                       "fixture": fixture, "file_order_source": "selftest.media.files",
                       "parameters": {key: getattr(args, key) for key in (
                           "window_samples", "hop_samples", "search_radius", "tolerance_samples",
                           "minimum_score", "minimum_margin", "initial_search_samples", "initial_reference_start")}})
        global_begin = 0
        base_offset = None
        for path in paths:
            intervals = check_intervals(path)
            if not intervals["ok"] or len(intervals["audio_elst"]) != 1 or intervals["audio_elst"][0][1] < 0:
                raise ValueError("invalid_or_unsupported_presentation_edits:" + path.name)
            scale = intervals["movie_timescale"]
            presentation = Fraction(intervals["audio_tkhd_ticks"] * RATE, scale)
            video_frames = Fraction(intervals["video_tkhd_ticks"] * RATE, scale)
            if presentation.denominator != 1 or video_frames.denominator != 1:
                raise ValueError("non_integral_movie_presentation_sample_grid:" + path.name)
            decoded, decode_info = decode_full(path, args.ffmpeg, args.ffprobe)
            row, base_offset = match_clip(
                reference, decoded, presentation_frames=int(presentation),
                first_pts=decode_info["decoded_first_pts_sample"], global_begin=global_begin,
                base_offset=base_offset, **result["parameters"])
            row.update(decode_info)
            row.update({"clip": path.name, "intervals": intervals})
            result["clips"].append(row)
            global_begin += int(video_frames)
        result["boundaries"] = boundary_checks(result["clips"], args.tolerance_samples)
        result["run_source_offset_samples"] = base_offset
        result["raw_decode_extra_frames"] = sum(c["raw_decode_extra_head"] + c["raw_decode_extra_tail"]
                                                  for c in result["clips"])
        result["unsafe_raw_pcm_concatenation"] = any(
            b.get("unsafe_raw_pcm_concat_gap_samples", 0) != 0 for b in result["boundaries"])
        result["presentation_ok"] = bool(result["clips"]) and all(c["ok"] for c in result["clips"]) and all(
            b["ok"] for b in result["boundaries"])
        result["ok"] = result["presentation_ok"]
        if not result["ok"]:
            result["errors"].append("presentation_content_discontinuous_or_uncertain")
    except (ValueError, KeyError, OSError, subprocess.SubprocessError, TypeError) as exc:
        result["errors"].append(str(exc))
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dir", type=Path)
    ap.add_argument("--reference", type=Path, required=True)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--ffprobe", default="ffprobe")
    ap.add_argument("--window-samples", type=int, default=2048)
    ap.add_argument("--hop-samples", type=int, default=1024)
    ap.add_argument("--search-radius", type=int, default=4096)
    ap.add_argument("--tolerance-samples", type=int, default=2)
    ap.add_argument("--minimum-score", type=float, default=0.90)
    ap.add_argument("--minimum-margin", type=float, default=0.10)
    ap.add_argument("--initial-search-samples", type=int, default=RATE * 10)
    ap.add_argument("--initial-reference-start", type=int, default=0)
    args = ap.parse_args()
    if (args.window_samples < 128 or args.hop_samples <= 0 or args.hop_samples > args.window_samples or
            args.search_radius <= args.tolerance_samples or args.tolerance_samples < 0 or
            args.initial_search_samples <= 0 or args.initial_reference_start < 0 or
            not 0 < args.minimum_score <= 1 or not 0 < args.minimum_margin <= 1):
        ap.error("invalid correlation/coverage parameters")
    result = check_run(args.dir, args.reference, args)
    if args.json:
        args.json.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key not in ("clips", "fixture")}, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
