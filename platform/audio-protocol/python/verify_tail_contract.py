#!/usr/bin/env python3
"""Verify the fixed encoder stop fence against MP4 presentation metadata.

Uses selftest.media.files order (including across midnight), never filename
sorting, ffprobe, or media decoding. Legacy one-AAC tolerance is reported
separately and only accepted with --allow-legacy; it is never a v2 pass.
"""
from __future__ import annotations

import argparse
from fractions import Fraction
import json
from pathlib import Path

from verify_mp4_intervals import check as check_intervals

CONTRACT = "closed_gop_audio_fence_v2"


def _integer(data: dict, key: str, *, minimum: int = 0) -> int:
    value = data.get(key)
    if type(value) is not int or value < minimum:
        raise ValueError("invalid_or_missing_" + key)
    return value


def _clip_paths(root: Path, media: dict) -> list[Path]:
    files = media.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("missing_ordered_media_files")
    paths = []
    base = root.resolve()
    for item in files:
        name = (item.get("path") or item.get("name")) if isinstance(item, dict) else item
        if not isinstance(name, str) or not name:
            raise ValueError("invalid_media_file_entry")
        path = (base / name).resolve()
        if not path.is_relative_to(base) or path.suffix.lower() != ".mp4":
            raise ValueError("invalid_media_file_path")
        if path in paths:
            raise ValueError("duplicate_media_file")
        paths.append(path)
    actual = {path.resolve() for path in base.glob("clip*.mp4") if path.is_file()}
    if set(paths) != actual:
        raise ValueError("media_file_inventory_mismatch")
    return paths


def check_run(root: Path, *, allow_legacy: bool = False) -> dict:
    result = {"ok": False, "v2_ok": False, "legacy_bounded_ok": False, "errors": []}
    try:
        selftest = json.loads((root / "selftest.json").read_text(encoding="utf-8"))
        media = selftest.get("media")
        if not isinstance(media, dict) or not isinstance(media.get("tail"), dict):
            raise ValueError("missing_media_or_tail")
        tail = media["tail"]
        paths = _clip_paths(root, media)
        intervals = [check_intervals(path) for path in paths]
        failures = [row for row in intervals if not row["ok"]]
        result.update({"last_clip": paths[-1].name, "clip_count": len(paths),
                       "file_order_source": "selftest.media.files", "interval_failures": failures})
        if failures:
            raise ValueError("invalid_mp4_intervals")
        last = intervals[-1]
        scale = last["movie_timescale"]
        difference_ticks = last["video_tkhd_ticks"] - last["audio_tkhd_ticks"]
        result.update({"tail_delta_ms": last["tail_delta_ms"],
                       "movie_quantization_ms": last["quantization_ms"],
                       "last_video_s": last["video_tkhd_ms"] / 1000,
                       "last_audio_s": last["audio_tkhd_ms"] / 1000})
        version = tail.get("contract_version")
        if type(version) is not int or version != 2 or tail.get("contract") != CONTRACT:
            if version is not None or tail.get("contract") == CONTRACT:
                raise ValueError("unsupported_or_inconsistent_tail_contract")
            legacy_ok = Fraction(difference_ticks, scale) <= Fraction(1024, 44100) + Fraction(1, scale)
            result.update({"contract": "legacy_bounded_edge_packetization", "contract_version": 1,
                           "legacy_bounded_ok": legacy_ok,
                           "ok": bool(allow_legacy and legacy_ok),
                           "source_coverage_verified": False})
            # tail_target_samples is not a proof of source loss or completeness.
            result["errors"] = ([] if result["ok"] else
                                ["legacy_not_v2" if legacy_ok else "legacy_tail_gap"])
            if result["errors"]:
                result["error"] = result["errors"][0]
            return result
        result.update({"contract": CONTRACT, "contract_version": 2})
        for key, expected in (("stop_requested", True), ("fence_set", True),
                              ("complete", True), ("failed", False)):
            if tail.get(key) is not expected:
                result["errors"].append("invalid_stop_state_" + key)
        if _integer(tail, "stop_error") != 0:
            result["errors"].append("stop_error")
        fence = _integer(tail, "fence_pts_usec", minimum=1)
        video_end = _integer(tail, "video_end_pts_usec", minimum=1)
        audio_end = _integer(tail, "audio_end_pts_usec", minimum=1)
        frame_samples = _integer(tail, "audio_frame_samples", minimum=1)
        sample_rate = _integer(tail, "audio_sample_rate", minimum=1)
        stop_request_ns = _integer(tail, "stop_request_ns", minimum=1)
        fence_capture_ns = _integer(tail, "fence_capture_ns", minimum=1)
        if tail.get("fence_capture_valid") is not True:
            result["errors"].append("invalid_capture_clock_fence")
        if fence_capture_ns < stop_request_ns:
            result["errors"].append("capture_fence_precedes_stop_request")
        if sample_rate != last["audio_media_timescale"]:
            result["errors"].append("audio_sample_rate_mismatch")
        if audio_end < fence:
            result["errors"].append("encoder_audio_does_not_cover_fence")
        if abs(video_end - fence) * scale > 1000000:
            result["errors"].append("video_end_does_not_match_fence")
        # This contract uses a zero-origin encoder PTS timeline. Account for
        # rounding independently at each movie timescale, never a whole segment.
        duration = sum((Fraction(row["video_tkhd_ticks"], row["movie_timescale"])
                        for row in intervals), Fraction())
        duration_tolerance = sum((Fraction(1, row["movie_timescale"])
                                  for row in intervals), Fraction())
        duration_error = abs(duration - Fraction(fence, 1000000))
        result.update({"summed_video_duration_s": float(duration),
                       "duration_fence_error_ms": float(duration_error * 1000),
                       "duration_fence_tolerance_ms": float(duration_tolerance * 1000)})
        if duration_error > duration_tolerance:
            result["errors"].append("media_duration_does_not_cover_fence")
        if difference_ticks < 0 or difference_ticks > 1:
            result["errors"].append("v2_presentation_tail_exceeds_movie_tick")
        before, after = tail.get("source_at_request"), tail.get("source_after_drain")
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise ValueError("missing_source_stop_snapshots")
        # An immediate stop can precede the first source submission. The final
        # snapshot must still cover the fence; an initial zero is not a gap.
        before_end = _integer(before, "submitted_end_timestamp_ns")
        after_end = _integer(after, "submitted_end_timestamp_ns", minimum=1)
        if after.get("integrity_ok") is not True or _integer(after, "submitted_gaps") != 0:
            result["errors"].append("source_after_drain_integrity_failed")
        if after_end < before_end:
            result["errors"].append("source_submitted_endpoint_regressed")
        if after_end < fence_capture_ns:
            result["errors"].append("source_does_not_cover_capture_fence")
        result.update({"fence_pts_usec": fence, "video_end_pts_usec": video_end,
                       "audio_end_pts_usec": audio_end,
                       "audio_frame_samples": frame_samples, "audio_sample_rate": sample_rate,
                       "aac_frame_ms": float(Fraction(frame_samples * 1000, sample_rate)),
                       "source_submitted_end_at_request_ns": before_end,
                       "source_submitted_end_after_drain_ns": after_end,
                       "stop_request_ns": stop_request_ns,
                       "fence_capture_ns": fence_capture_ns,
                       "source_coverage_verified": not result["errors"],
                       "coverage_basis": "source submitted capture clock fence plus encoder endpoints and MP4 edits; not decoded PCM",
                       "v2_ok": not result["errors"], "ok": not result["errors"]})
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        result["errors"].append(str(exc))
    if result["errors"]:
        result["error"] = result["errors"][0]
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--json")
    ap.add_argument("--allow-legacy", action="store_true")
    args = ap.parse_args()
    result = check_run(Path(args.dir), allow_legacy=args.allow_legacy)
    text = json.dumps(result, indent=2)
    print(text)
    if args.json:
        Path(args.json).write_text(text, encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
