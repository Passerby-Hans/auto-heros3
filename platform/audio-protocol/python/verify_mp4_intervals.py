#!/usr/bin/env python3
"""Read-only MP4 presentation checks; skips mdat and never decodes media.

Checks tkhd/edit-list agreement, timescales/durations, audio media ranges and
audio <= video. Media-range rounding permits half a movie tick. Video B-frame
edit offsets are not bounded by decode-duration mdhd.
"""
from __future__ import annotations

import argparse
from fractions import Fraction
import json
import struct
from pathlib import Path

MAX_MOOV_BYTES = 64 * 1024 * 1024


def _children(data: bytes):
    offset = 0
    while offset < len(data):
        if len(data) - offset < 8:
            raise ValueError("truncated_box_header")
        size, kind = struct.unpack_from(">I4s", data, offset)
        header = 8
        if size == 1:
            if len(data) - offset < 16:
                raise ValueError("truncated_extended_box_header")
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header = 16
        elif size == 0:
            size = len(data) - offset
        if size < header or size > len(data) - offset:
            raise ValueError("invalid_box_size")
        yield kind, data[offset + header:offset + size]
        offset += size


def _one(items, kind: bytes, *, required: bool = True):
    found = [payload for tag, payload in items if tag == kind]
    if len(found) > 1 or (required and not found):
        raise ValueError("missing_or_duplicate_" + kind.decode("ascii"))
    return found[0] if found else None


def _moov(path: Path) -> bytes:
    """Validate top-level extents, reading only headers and bounded metadata."""
    with path.open("rb") as stream:
        stream.seek(0, 2)
        file_end = stream.tell()
        stream.seek(0)
        found = None
        while stream.tell() < file_end:
            offset = stream.tell()
            header_data = stream.read(8)
            if len(header_data) != 8:
                raise ValueError("truncated_top_level_header")
            size, kind = struct.unpack(">I4s", header_data)
            header = 8
            if size == 1:
                extended = stream.read(8)
                if len(extended) != 8:
                    raise ValueError("truncated_extended_top_level_header")
                size = struct.unpack(">Q", extended)[0]
                header = 16
            elif size == 0:
                size = file_end - offset
            if size < header or size > file_end - offset:
                raise ValueError("invalid_top_level_extent")
            if kind == b"moov":
                if found is not None:
                    raise ValueError("duplicate_moov")
                if size - header > MAX_MOOV_BYTES:
                    raise ValueError("moov_metadata_limit")
                found = stream.read(size - header)
                if len(found) != size - header:
                    raise ValueError("truncated_moov")
            stream.seek(offset + size)
        if found is None:
            raise ValueError("missing_moov")
        return found


def _version(payload: bytes, field: str) -> int:
    if len(payload) < 4 or payload[0] not in (0, 1):
        raise ValueError("invalid_" + field + "_version")
    return payload[0]


def _clock(payload: bytes, field: str) -> tuple[int, int]:
    version = _version(payload, field)
    if version == 0:
        scale, duration = struct.unpack_from(">II", payload, 12)
        unknown = 0xFFFFFFFF
    else:
        scale, duration = struct.unpack_from(">IQ", payload, 20)
        unknown = 0xFFFFFFFFFFFFFFFF
    if not scale or not duration or duration == unknown:
        raise ValueError("invalid_" + field + "_clock_or_duration")
    return scale, duration


def _track_duration(payload: bytes) -> int:
    version = _version(payload, "tkhd")
    duration = struct.unpack_from(">I" if version == 0 else ">Q",
                                  payload, 20 if version == 0 else 28)[0]
    if not duration or duration == (0xFFFFFFFF if version == 0 else 0xFFFFFFFFFFFFFFFF):
        raise ValueError("invalid_tkhd_duration")
    return duration


def _edits(payload: bytes) -> list[tuple[int, int]]:
    version = _version(payload, "elst")
    count = struct.unpack_from(">I", payload, 4)[0]
    stride = 12 if version == 0 else 20
    if not count or len(payload) != 8 + count * stride:
        raise ValueError("invalid_elst_entry_length")
    entries = []
    for offset in range(8, len(payload), stride):
        duration, media_time = struct.unpack_from(">Ii" if version == 0 else ">Qq",
                                                 payload, offset)
        rate = struct.unpack_from(">hh", payload, offset + stride - 4)
        if not duration or media_time < -1 or rate != (1, 0):
            raise ValueError("invalid_elst_media_time_duration_or_rate")
        entries.append((duration, media_time))
    return entries


def _track(payload: bytes) -> dict:
    fields = list(_children(payload))
    duration = _track_duration(_one(fields, b"tkhd"))
    mdia = list(_children(_one(fields, b"mdia")))
    timescale, media_duration = _clock(_one(mdia, b"mdhd"), "mdhd")
    hdlr = _one(mdia, b"hdlr")
    if len(hdlr) < 12:
        raise ValueError("truncated_hdlr")
    edit_box = _one(fields, b"edts", required=False)
    edits = _edits(_one(list(_children(edit_box)), b"elst")) if edit_box is not None else []
    return {"handler": hdlr[8:12], "duration": duration, "timescale": timescale,
            "media_duration": media_duration, "edits": edits}


def check(path: Path) -> dict:
    result = {"clip": path.name, "path": str(path), "ok": False, "errors": []}
    try:
        fields = list(_children(_moov(path)))
        movie_scale, movie_duration = _clock(_one(fields, b"mvhd"), "mvhd")
        tracks = [_track(payload) for kind, payload in fields if kind == b"trak"]
        videos = [track for track in tracks if track["handler"] == b"vide"]
        audios = [track for track in tracks if track["handler"] == b"soun"]
        if len(videos) != 1 or len(audios) != 1:
            raise ValueError("missing_or_ambiguous_audio_video_tracks")
        video, audio = videos[0], audios[0]
        errors = result["errors"]
        elst_sum = sum(duration for duration, _ in audio["edits"])
        sum_ok = bool(audio["edits"]) and audio["duration"] == elst_sum
        if not sum_ok:
            errors.append("audio_elst_tkhd_mismatch")
        if audio["duration"] > video["duration"]:
            errors.append("audio_exceeds_video")
        if max(audio["duration"], video["duration"]) > movie_duration:
            errors.append("track_exceeds_movie")
        if video["edits"] and sum(duration for duration, _ in video["edits"]) != video["duration"]:
            errors.append("video_elst_tkhd_mismatch")
        media_entries = [(duration, start) for duration, start in audio["edits"] if start >= 0]
        if not media_entries:
            errors.append("missing_audio_media_edit")
        # Round-to-nearest movie duration can advertise at most half a movie
        # tick beyond the exact sample end. Rationals prevent float slack.
        tolerance = Fraction(audio["timescale"], 2 * movie_scale)
        range_ok = bool(media_entries)
        for duration, start in media_entries:
            end = start + Fraction(duration * audio["timescale"], movie_scale)
            if start >= audio["media_duration"] or end > audio["media_duration"] + tolerance:
                range_ok = False
        if not range_ok:
            errors.append("audio_edit_outside_media")
        to_ms = lambda value: float(Fraction(value * 1000, movie_scale))
        result.update({
            "movie_timescale": movie_scale, "movie_duration_ticks": movie_duration,
            "video_tkhd_ticks": video["duration"], "audio_tkhd_ticks": audio["duration"],
            "video_tkhd_ms": to_ms(video["duration"]), "audio_tkhd_ms": to_ms(audio["duration"]),
            "audio_elst": [list(entry) for entry in audio["edits"]],
            "elst_sum_ms": to_ms(elst_sum), "elst_sum_matches_tkhd": sum_ok,
            "audio_media_timescale": audio["timescale"], "audio_media_duration": audio["media_duration"],
            "audio_media_range_ok": range_ok,
            "overhang_ms": to_ms(max(0, audio["duration"] - video["duration"])),
            "tail_delta_ms": to_ms(video["duration"] - audio["duration"]),
            "quantization_ms": to_ms(1), "ok": not errors,
        })
    except (OSError, ValueError, struct.error) as exc:
        result["errors"].append(str(exc))
    if result["errors"]:
        result["error"] = result["errors"][0]
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--json")
    args = ap.parse_args()
    results = [check(path) for path in sorted(Path(args.dir).glob("clip-*.mp4"))]
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
    bad = [row for row in results if not row["ok"]]
    print(json.dumps({"clips": len(results), "failed": len(bad),
                      "overhang_clips": sum(bool(row.get("overhang_ms")) for row in results),
                      "elst_sum_mismatch": sum(not row.get("elst_sum_matches_tkhd", True) for row in results),
                      "results": results}, indent=2))
    return 0 if results and not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
