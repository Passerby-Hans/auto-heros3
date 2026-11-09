"""Synthetic MP4 metadata and stop-contract regressions; no media processes."""
from __future__ import annotations

import copy
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "platform" / "audio-protocol" / "python"))
from verify_mp4_intervals import check as verify_interval  # noqa: E402
from verify_tail_contract import check_run  # noqa: E402
import measure_av_pulses as av  # noqa: E402
from test_decoded_continuity import DecodedContinuityTests  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "delivery_harness", ROOT / "tests/live/test_native_audio_delivery.py")
delivery = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(delivery)
_spec = importlib.util.spec_from_file_location(
    "recording_e2e_harness", ROOT / "tests/live/test_native_recording_e2e.py")
e2e = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e2e)

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))


def box(kind, payload, *, extended=False):
    return (struct.pack(">I4sQ", 1, kind, len(payload) + 16) if extended else
            struct.pack(">I4s", len(payload) + 8, kind)) + payload


def clock_box(kind, scale, duration, version):
    body = bytearray(20 if version == 0 else 32)
    body[0] = version
    struct.pack_into(">II" if version == 0 else ">IQ", body,
                     12 if version == 0 else 20, scale, duration)
    return box(kind, body)


def track(kind, duration, scale, media_duration, edits, version=0, omit_tkhd=False):
    header = bytearray(24 if version == 0 else 36)
    header[0] = version
    struct.pack_into(">I" if version == 0 else ">Q", header,
                     20 if version == 0 else 28, duration)
    edit_box = b""
    if edits is not None:
        entries = bytes([version, 0, 0, 0]) + struct.pack(">I", len(edits))
        entries += b"".join(struct.pack(">Iihh" if version == 0 else ">Qqhh", d, t, 1, 0)
                            for d, t in edits)
        edit_box = box(b"edts", box(b"elst", entries))
    return box(b"trak", (b"" if omit_tkhd else box(b"tkhd", header)) + edit_box +
               box(b"mdia", clock_box(b"mdhd", scale, media_duration, version) +
                   box(b"hdlr", bytes(8) + kind + bytes(12))))


def fixture(*, movie_scale=1000, video_duration=10000, audio_duration=10000,
            audio_edits=None, audio_media_duration=None, audio_rate=44100,
            version=0, include_audio=True, omit_audio_tkhd=False,
            video_offset=0, extended=False):
    if audio_edits is None:
        audio_edits = [(audio_duration, 1024)]
    if audio_media_duration is None:
        audio_media_duration = 1024 + (audio_duration * audio_rate + movie_scale - 1) // movie_scale
    video = track(b"vide", video_duration, 60, max(1, video_duration * 60 // movie_scale),
                  [(video_duration, video_offset)] if video_offset else None, version)
    audio = track(b"soun", audio_duration, audio_rate, audio_media_duration, audio_edits,
                  version, omit_audio_tkhd) if include_audio else b""
    return box(b"moov", clock_box(b"mvhd", movie_scale, max(video_duration, audio_duration, 1), version)
               + video + audio, extended=extended) + box(b"mdat", b"synthetic")


def stop_tail():
    return {"contract": "closed_gop_audio_fence_v2", "contract_version": 2,
            "stop_requested": True, "fence_set": True, "complete": True, "failed": False,
            "stop_error": 0, "fence_pts_usec": 10000000,
            "video_end_pts_usec": 10000000, "audio_end_pts_usec": 10010000,
            "audio_frame_samples": 1024, "audio_sample_rate": 44100,
            "stop_request_ns": 100000000000,
            "fence_capture_ns": 100500000000, "fence_capture_valid": True,
            "source_at_request": {"submitted_end_timestamp_ns": 100000000000},
            "source_after_drain": {"submitted_end_timestamp_ns": 101000000000,
                                   "integrity_ok": True, "submitted_gaps": 0}}


def main():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        clip = root / "clip-000.mp4"

        def interval_case(name, contents, expected, error=None):
            clip.write_bytes(contents)
            result = verify_interval(clip)
            check(name, result["ok"] is expected and
                  (error is None or error in result.get("errors", [])), result)
            return result

        interval_case("valid_v0", fixture(), True)
        interval_case("valid_v1_extended_boxes", fixture(version=1, extended=True), True)
        scaled = interval_case("non_millisecond_movie_timescale",
                               fixture(movie_scale=48000, video_duration=480000,
                                       audio_duration=480000), True)
        check("timescale_converted_to_ms", scaled["audio_tkhd_ms"] == 10000)
        interval_case("old_priming_double_subtraction_counterexample",
                      fixture(video_duration=11000, audio_duration=11000,
                              audio_edits=[(10977, 1024)]), False, "audio_elst_tkhd_mismatch")
        interval_case("old_one_ms_overhang_counterexample",
                      fixture(audio_duration=10001), False, "audio_exceeds_video")
        interval_case("missing_audio_track", fixture(include_audio=False), False)
        interval_case("missing_audio_tkhd", fixture(omit_audio_tkhd=True), False)
        interval_case("zero_audio_duration", fixture(audio_duration=0), False)
        interval_case("zero_media_timescale", fixture(audio_rate=0, audio_media_duration=100), False)
        interval_case("empty_edit_list", fixture(audio_edits=[]), False)
        interval_case("illegal_negative_media_time", fixture(audio_edits=[(10000, -2)]), False)
        interval_case("empty_edit_is_not_audio", fixture(audio_edits=[(10000, -1)]), False)
        interval_case("valid_leading_empty_edit",
                      fixture(audio_edits=[(10, -1), (9990, 1024)]), True)
        interval_case("range_accepts_half_movie_tick",
                      fixture(audio_media_duration=442002), True)
        interval_case("range_rejects_more_than_half_movie_tick",
                      fixture(audio_media_duration=442001), False, "audio_edit_outside_media")
        interval_case("media_start_past_end",
                      fixture(audio_edits=[(10000, 500000)]), False, "audio_edit_outside_media")
        interval_case("video_b_frame_offset_not_bounded_by_decode_duration",
                      fixture(video_offset=2), True)
        interval_case("truncated_box", fixture()[:-1], False)
        interval_case("trailing_truncated_header", fixture() + b"abc", False)
        interval_case("truncated_extended_header", struct.pack(">I4s", 1, b"moov"), False)
        interval_case("duplicate_moov", fixture() + fixture(), False)

        # A virtual 1.5GB mdat ensures the verifier only reads metadata/headers.
        metadata = fixture()[:-17]  # remove the synthetic mdat box
        payload_bytes = 1_500_000_000
        mdat_header = struct.pack(">I4sQ", 1, b"mdat", payload_bytes + 16)
        suffix_offset = len(mdat_header) + payload_bytes

        class SparseRead(io.RawIOBase):
            def __init__(self):
                self.offset = 0
                self.read_bytes = 0

            def tell(self):
                return self.offset

            def seek(self, offset, whence=0):
                self.offset = offset + (self.offset if whence == 1 else
                                        suffix_offset + len(metadata) if whence == 2 else 0)
                return self.offset

            def read(self, count=-1):
                if count < 0 or count > 1024 * 1024:
                    raise AssertionError("attempted unbounded media read")
                self.read_bytes += count
                if self.offset < len(mdat_header):
                    output = mdat_header[self.offset:self.offset + count]
                elif self.offset >= suffix_offset:
                    start = self.offset - suffix_offset
                    output = metadata[start:start + count]
                else:
                    raise AssertionError("attempted mdat payload read")
                self.offset += len(output)
                return output

        stream = SparseRead()
        with patch.object(Path, "open", return_value=stream):
            sparse_result = verify_interval(clip)
        check("large_mdat_seek_only", sparse_result["ok"] and stream.read_bytes < 10000, sparse_result)

        clip.write_bytes(fixture())

        def tail_case(name, tail, expected, error=None, files=None, allow_legacy=False):
            (root / "selftest.json").write_text(json.dumps({"media": {
                "tail": tail, "files": files if files is not None else [{"name": clip.name}]}}))
            result = check_run(root, allow_legacy=allow_legacy)
            check(name, result["ok"] is expected and
                  (error is None or error in result.get("errors", [])), result)
            return result

        valid = stop_tail()
        result = tail_case("v2_valid_fence_and_intervals", valid, True)
        check("v2_flag_is_explicit", result["v2_ok"] is True and not result["legacy_bounded_ok"])
        for flag in ("stop_requested", "fence_set", "complete", "failed"):
            bad = copy.deepcopy(valid)
            bad[flag] = not valid[flag]
            tail_case("v2_reject_" + flag, bad, False, "invalid_stop_state_" + flag)
        for key in ("fence_pts_usec", "audio_frame_samples", "audio_sample_rate", "stop_error",
                    "stop_request_ns", "fence_capture_ns", "fence_capture_valid"):
            bad = copy.deepcopy(valid)
            del bad[key]
            tail_case("v2_reject_missing_" + key, bad, False)
        bad = copy.deepcopy(valid)
        bad["stop_error"] = 1
        tail_case("v2_reject_nonzero_stop_error_code", bad, False, "stop_error")
        bad = copy.deepcopy(valid)
        bad["contract_version"] = 2.0
        tail_case("v2_requires_integer_version", bad, False,
                  "unsupported_or_inconsistent_tail_contract")
        bad = copy.deepcopy(valid)
        bad["audio_end_pts_usec"] = bad["fence_pts_usec"] - 1
        tail_case("v2_encoder_short_of_fence", bad, False, "encoder_audio_does_not_cover_fence")
        bad = copy.deepcopy(valid)
        bad["video_end_pts_usec"] += 1001
        tail_case("v2_video_does_not_reach_fixed_fence", bad, False, "video_end_does_not_match_fence")
        bad = copy.deepcopy(valid)
        bad["source_after_drain"]["integrity_ok"] = False
        tail_case("v2_source_integrity_failure", bad, False, "source_after_drain_integrity_failed")
        bad = copy.deepcopy(valid)
        bad["source_after_drain"]["submitted_gaps"] = 1
        tail_case("v2_source_gap", bad, False, "source_after_drain_integrity_failed")
        bad = copy.deepcopy(valid)
        bad["source_after_drain"]["submitted_end_timestamp_ns"] = 1
        tail_case("v2_source_endpoint_regression", bad, False, "source_submitted_endpoint_regressed")
        bad = copy.deepcopy(valid)
        bad["source_after_drain"]["submitted_end_timestamp_ns"] = bad["fence_capture_ns"] - 1
        tail_case("v2_source_misses_capture_fence_by_one_ns", bad, False,
                  "source_does_not_cover_capture_fence")
        bad = copy.deepcopy(valid)
        bad["source_after_drain"]["submitted_end_timestamp_ns"] = bad["fence_capture_ns"]
        covered = tail_case("v2_source_exactly_covers_capture_fence", bad, True)
        check("v2_capture_clock_coverage_explicit", covered["source_coverage_verified"])
        bad = copy.deepcopy(valid)
        bad["source_at_request"]["submitted_end_timestamp_ns"] = 0
        tail_case("v2_immediate_stop_may_precede_first_source_submission", bad, True)
        bad = copy.deepcopy(valid)
        bad["fence_capture_ns"] = bad["stop_request_ns"] - 1
        tail_case("v2_capture_fence_precedes_request", bad, False,
                  "capture_fence_precedes_stop_request")
        bad = copy.deepcopy(valid)
        bad["fence_capture_valid"] = False
        tail_case("v2_invalid_capture_clock_mapping", bad, False, "invalid_capture_clock_fence")
        bad = copy.deepcopy(valid)
        bad["tail_target_samples"] = 999999  # old derived metric must not decide v2.
        tail_case("v2_ignores_obsolete_tail_target", bad, True)
        clip.write_bytes(fixture(audio_duration=9999))
        tail_case("v2_one_movie_tick_allowed", valid, True)
        clip.write_bytes(fixture(audio_duration=9998))
        tail_case("v2_two_movie_ticks_rejected", valid, False, "v2_presentation_tail_exceeds_movie_tick")
        clip.write_bytes(fixture(audio_duration=9980))
        legacy = tail_case("legacy_does_not_pass_v2", {"tail_target_samples": 0}, False, "legacy_not_v2")
        check("legacy_bounded_reported_separately", legacy["legacy_bounded_ok"] and not legacy["v2_ok"])
        tail_case("legacy_requires_explicit_opt_in", {}, True, allow_legacy=True)
        clip.write_bytes(fixture(audio_duration=9973))
        tail_case("legacy_old_27ms_failure", {}, False, "legacy_tail_gap", allow_legacy=True)
        before_midnight, after_midnight = root / "clip-235959.mp4", root / "clip-000001.mp4"
        clip.unlink()
        before_midnight.write_bytes(fixture(audio_duration=9980))
        after_midnight.write_bytes(fixture())
        two_segments = copy.deepcopy(valid)
        two_segments.update(fence_pts_usec=20000000, video_end_pts_usec=20000000,
                            audio_end_pts_usec=20010000)
        across = tail_case("tail_selection_uses_manifest_order_across_midnight", two_segments, True,
                           files=[{"name": before_midnight.name}, {"name": after_midnight.name}])
        check("actual_tail_after_midnight", across["last_clip"] == after_midnight.name)
        tail_case("omitted_actual_tail_cannot_pass", valid, False,
                  "media_file_inventory_mismatch", files=[{"name": before_midnight.name}])
        tail_case("all_files_but_wrong_duration_cannot_pass", valid, False,
                  "media_duration_does_not_cover_fence",
                  files=[{"name": before_midnight.name}, {"name": after_midnight.name}])
        rounded = copy.deepcopy(two_segments)
        rounded["fence_pts_usec"] += 2000
        rounded["video_end_pts_usec"] += 2000
        tail_case("duration_allows_one_tick_per_segment", rounded, True,
                  files=[{"name": before_midnight.name}, {"name": after_midnight.name}])
        rounded["fence_pts_usec"] += 1
        rounded["video_end_pts_usec"] += 1
        tail_case("duration_rejects_beyond_accumulated_ticks", rounded, False,
                  "media_duration_does_not_cover_fence",
                  files=[{"name": before_midnight.name}, {"name": after_midnight.name}])
        tail_case("missing_order_not_filename_fallback", valid, False, files=[])
        tail_case("reject_duplicate_manifest_file", valid, False,
                  files=[{"name": after_midnight.name}, {"name": after_midnight.name}])
        tail_case("missing_named_tail_fails", valid, False, files=[{"name": "missing.mp4"}])
        after_midnight.write_bytes(fixture(include_audio=False))
        tail_case("v2_missing_audio_cannot_pass_as_zero_duration", valid, False,
                  files=[{"name": after_midnight.name}])

        def failed_report(code):
            return {"status": "fail", "exit_code": 6, "steps": [
                {"name": name, "ok": False, "detail": "error_code='" + code + "' error='test'"}
                for name in ("framed_audio_after_stop", "framed_audio_consumed")]}

        failure = failed_report("late_budget_exceeded")
        check("stall_exact_1s_source_failure", delivery.expected_stall_failure(failure, 6, 1000))
        check("stall_exact_6s_source_failure", delivery.expected_stall_failure(
            failed_report("capture_stalled"), 6, 6000))
        check("stall_wrong_1s_error_rejected", not delivery.expected_stall_failure(
            failed_report("capture_stalled"), 6, 1000))
        check("stall_unrelated_exit_rejected", not delivery.expected_stall_failure(failure, 1, 1000))
        loose = {"status": "fail", "exit_code": 6, "steps": [
            {"name": "open", "ok": False, "detail": "overflow=0 producer_error=0"}]}
        check("stall_zero_overflow_text_never_passes", not delivery.expected_stall_failure(loose, 6, 6000))
        failure["steps"][1]["ok"] = True
        check("stall_successful_source_gate_rejected", not delivery.expected_stall_failure(failure, 6, 1000))
        check("stall_incomplete_producer_metrics_rejected", not delivery.producer_metrics(
            "a3capture latency: phase=closing write_latency_max_us=1000000 measurements=10"))
        check("stall_final_metrics_not_incidental_text", delivery.producer_metrics(
            "write_latency_max_us=1\na3capture latency: phase=closed "
            "write_latency_max_us=1000001 measurements=10 clock_errors=0\n") ==
            {"write_latency_max_us": 1000001, "measurements": 10, "clock_errors": 0})

        endpoint_good = {"ok": True, "owned_physical_sessions": []}
        endpoint_changed = {"ok": False, "owned_physical_sessions": [],
                            "endpoint_controls_unchanged": False}
        physical = {"ok": False, "owned_physical_sessions": [{"pid": 123}]}
        check("e2e_default_strict_keeps_endpoint_abort", e2e.observation_requires_abort(endpoint_changed))
        check("e2e_observe_environment_change_continues", not e2e.observation_requires_abort(endpoint_changed, True))
        check("e2e_observe_physical_session_still_aborts", e2e.observation_requires_abort(physical, True))
        full = {"run_session_completed": True, "artifact_hashes_unchanged": True,
                "settings_restored_by_product": True, "settings_bytes_restored": True,
                "runtime_sdl_bytes_restored": True, "session_stop": {"ok": True},
                "owned_game_left_alive": False, "owned_recorder_left_alive": False,
                "remaining_vcmi_processes": [], "tail_verifier": {"v2_ok": True},
                "recording_stop": [{"ok": True, "checks_ok": True, "native_checks_ok": True}],
                "endpoint_samples": [{"comparison": endpoint_good}],
                "endpoint_after_comparison": endpoint_good}
        check("e2e_all_checks_pass_exit0", e2e.final_verdict(full) == {
            "ok": True, "media_checks_ok": True, "endpoint_checks_ok": True, "exit_code": 0})
        transient = copy.deepcopy(full)
        transient["endpoint_samples"].insert(0, {"comparison": endpoint_changed})
        check("e2e_transient_environment_not_hidden_by_restoration", e2e.final_verdict(transient) == {
            "ok": False, "media_checks_ok": True, "endpoint_checks_ok": False, "exit_code": 2})
        changed_after = copy.deepcopy(full)
        changed_after["endpoint_after_comparison"] = endpoint_changed
        check("e2e_media_only_pass_exit2", e2e.final_verdict(changed_after)["exit_code"] == 2)
        unsafe = copy.deepcopy(full)
        unsafe["endpoint_samples"].append({"comparison": physical})
        check("e2e_physical_audio_cannot_be_media_pass", e2e.final_verdict(unsafe)["exit_code"] == 1 and
              not e2e.final_verdict(unsafe)["media_checks_ok"])
        aborted = copy.deepcopy(full)
        aborted["run_session_completed"] = False
        aborted["cleanup_recording_stop"] = aborted.pop("recording_stop")[0]
        check("e2e_cleanup_stop_cannot_replace_formal_stop", e2e.final_verdict(aborted)["exit_code"] == 1)
        for field in ("artifact_hashes_unchanged", "settings_restored_by_product",
                      "settings_bytes_restored", "runtime_sdl_bytes_restored"):
            failed = copy.deepcopy(full)
            failed[field] = False
            check("e2e_reject_" + field, not e2e.final_verdict(failed)["media_checks_ok"])

        # Mock decoder/probe results. The subprocess function never launches a
        # process: it writes tiny deterministic fixture arrays in the temp dir.
        def fake_decode(pts):
            def run(command, **kwargs):
                if command[0] == "ffprobe":
                    return SimpleNamespace(stdout="\n".join(str(x) for x in pts))
                if command[0] != "ffmpeg":
                    raise AssertionError("unexpected process request")
                destination = Path(command[-1])
                if "-vn" in command:
                    samples = av.np.zeros(4410, dtype="<i2")
                    samples[2205:2646] = 10000  # onset at 0.05s in decoded audio
                    with wave.open(str(destination), "wb") as wav:
                        wav.setnchannels(1)
                        wav.setsampwidth(2)
                        wav.setframerate(44100)
                        wav.writeframes(samples.tobytes())
                else:
                    frames = av.np.zeros((6, 90, 160), dtype=av.np.uint8)
                    frames[3] = 255
                    destination.write_bytes(frames.tobytes())
                return SimpleNamespace(returncode=0)
            return run

        pts = [0.5 + i / 60 for i in range(6)]
        with patch.object(av, "stream_start", return_value={"audio": 0.5, "video": 0.5}), \
                patch.object(av.subprocess, "run", side_effect=fake_decode(pts)):
            measured = av.measure(clip)
        check("av_nonzero_video_start_not_added_twice",
              measured["video_onset"] == 0.55 and measured["pass_20ms"], measured)
        for name, corrupt_pts, expected_error in (
                ("av_missing_pts_never_fabricated", pts[:-1], "presentation_pts_count_mismatch"),
                ("av_duplicate_pts_rejected", [pts[0]] * 6, "non_monotonic_video_presentation_pts"),
                ("av_nan_outside_event_rejected", [float("nan")] + pts[1:], "non_finite_video_presentation_pts")):
            with patch.object(av, "stream_start", return_value={"audio": 0.5, "video": 0.5}), \
                    patch.object(av.subprocess, "run", side_effect=fake_decode(corrupt_pts)):
                try:
                    av.measure(clip)
                    check(name, False)
                except ValueError as exc:
                    check(name, expected_error in str(exc), str(exc))
        # CLI must reject a bad final file even if earlier event pairs pass.
        for name, row in (
                ("av_empty_tail_not_waived", {"video_event_count": 0, "audio_event_count": 0,
                                             "pass_20ms": False}),
                ("av_extra_tail_audio_not_waived", {"video_event_count": 1, "audio_event_count": 2,
                                                   "pass_20ms": True}),
                ("av_late_tail_pair_not_waived", {"video_event_count": 1, "audio_event_count": 1,
                                                 "pass_20ms": False})):
            with patch.object(sys, "argv", ["measure_av_pulses.py", str(root)]), \
                    patch.object(av, "measure", return_value=row), patch("builtins.print"):
                check(name, av.main() == 1)

    decoded_suite = unittest.defaultTestLoader.loadTestsFromTestCase(DecodedContinuityTests)
    decoded_output = io.StringIO()
    decoded_result = unittest.TextTestRunner(stream=decoded_output).run(decoded_suite)
    check("decoded_continuity_failure_injection_7_cases",
          decoded_result.testsRun == 7 and decoded_result.wasSuccessful(), decoded_output.getvalue())

    for name, ok, detail in RESULTS:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            print(detail)
    passed = sum(ok for _, ok, _ in RESULTS)
    print(f"{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
