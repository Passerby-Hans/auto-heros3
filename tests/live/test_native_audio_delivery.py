"""Explicit native media acceptance (no game, endpoint, or audio playback).

Runs only when selected explicitly. Artifacts go in a fresh requested directory.
The SDL case uses the private a3capture driver and real one-shot writer stalls;
the boundary case uses timestamped framed pulses, not the raw/resampling path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "platform/audio-protocol/python"
sys.path.insert(0, str(PROTOCOL))
from verify_mp4_intervals import check as check_interval


def sha(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("A3_", "A3CAPTURE_", "AUTO_HEROS3_", "SDL_AUDIO"))}


def stop_owned(proc: subprocess.Popen | None) -> None:
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def framed_failure(report: dict) -> dict:
    """Read the two actual source gates, never incidental log substrings."""
    names = {"framed_audio_after_stop", "framed_audio_consumed"}
    steps = [row for row in report.get("steps", []) if row.get("name") in names]
    codes = []
    for step in steps:
        match = re.search(r"(?:^| )error_code='([a-z0-9_]+)'(?: |$)", step.get("detail", ""))
        if step.get("ok") is not False or match is None:
            return {}
        codes.append(match.group(1))
    if len(steps) != 2 or {row["name"] for row in steps} != names or len(set(codes)) != 1:
        return {}
    return {"error_code": codes[0], "origin": "failed_named_source_gates"}


def expected_stall_failure(report: dict, native_exit: int, stall_ms: int,
                           transport: str = "file", reader_stall_ms: int = 0) -> bool:
    allowed = ({"late_budget_exceeded"} if stall_ms == 1000 else
               {"capture_stalled", "late_budget_exceeded", "producer_error"}
               if stall_ms == 6000 else set())
    if transport == "shared_memory":
        allowed = {"live_producer_fatal"} if stall_ms == 6000 else set()
    if reader_stall_ms >= 750:
        allowed = {"late_budget_exceeded"}
    if transport == "shared_memory" and stall_ms == 6000 and not reader_stall_ms:
        source = ((report.get("media") or {}).get("tail") or {}).get("source_after_drain") or {}
        # A six-second archive stall must exhaust this queue. An unrelated
        # clock failure, write error or producer exit is not the expected test.
        if (source.get("reader_diagnostics") or {}).get("producer_fatal_code") != 2:
            return False
    return (native_exit == 6 and report.get("exit_code") == 6 and
            report.get("status") == "fail" and framed_failure(report).get("error_code") in allowed)


def shared_delivery_checks(report: dict) -> dict:
    source = ((report.get("media") or {}).get("tail") or {}).get("source_after_drain") or {}
    d = source.get("reader_diagnostics") or {}
    return {
        "shared_transport": d.get("transport") == "shared_memory",
        "identity_header_only": d.get("read_offset") == 128 and d.get("eof_reopens") == 0,
        "mmcss": d.get("mmcss_registered") is True,
        "fresh_attach": type(d.get("first_submitted_age_ns")) is int and
                         0 <= d["first_submitted_age_ns"] <= 70000000,
        "archive_committed": d.get("archive_commit_ok") is True and
                             type(d.get("archive_committed_end_sample")) is int and
                             d["archive_committed_end_sample"] >= source.get("submitted_end_sample", 0),
        "no_producer_fatal": d.get("producer_fatal_code") == 0,
        "no_reader_fatal": (d.get("fatal") or {}).get("latched") is False,
        "no_submitted_gap": source.get("submitted_gaps") == 0,
    }


def expected_missing_map(report: dict, native_exit: int) -> bool:
    rows = [s for s in report.get("steps", []) if s.get("name") == "framed_audio_opened"]
    return (native_exit == 9 and report.get("exit_code") == 9 and report.get("status") == "fail" and
            len(rows) == 1 and rows[0].get("ok") is False and
            "error_code='live_map_missing'" in rows[0].get("detail", ""))


def producer_metrics(text: str) -> dict:
    # Require the completed close record; an earlier heartbeat is insufficient.
    rows = re.findall(r"a3capture latency: phase=closed ([^\r\n]*)", text)
    if len(rows) != 1:
        return {}
    return {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", rows[0])}


def producer_diagnostics(text: str, stall_ms: int) -> dict:
    """Validate emitted worker telemetry, not only the final recording gate.

    A category-filtered or malformed diagnostic stream must fail acceptance.
    The one-shot fixture stalls the worker after 100 completed packets, so
    sequence 100 is the exact sample interval that must carry the observation.
    """
    marker = "a3capture_write_diag "
    lines = [(number, line.split(marker, 1)[1].strip())
             for number, line in enumerate(text.splitlines(), 1) if marker in line]
    rows, errors = [], []
    required_ints = {
        "capture_id", "generation", "sequence", "sample_start", "sample_end", "qpc_frequency",
        "planned_qpc", "actual_start_qpc", "actual_end_qpc", "enqueue_qpc", "write_start_qpc",
        "write_end_qpc", "clock_valid", "write_ok", "failed", "write_latency_us", "queue_wait_us",
        "write_io_us", "capture_to_write_us", "write_latency_max_us", "queue_wait_max_us",
        "write_io_max_us", "capture_to_write_max_us", "measurements", "clock_errors",
        "queue_depth_waiting", "slots_used_including_writer", "queue_depth_max", "slots_used_max",
        "generated_frames", "written_frames", "dropped_frames",
    }
    valid_schema = valid_timing = True
    for line_number, payload in lines:
        try:
            row = json.loads(payload)
        except (ValueError, TypeError) as exc:
            errors.append({"line": line_number, "error": f"invalid_json: {exc}"})
            continue
        rows.append(row)
        schema_ok = (isinstance(row, dict) and row.get("schema") == "a3-worker-diag/1" and
                     isinstance(row.get("event"), str) and
                     row["event"] in {"health", "slow_write", "clock_error", "write_error"} and
                     all(type(row.get(k)) is int and row[k] >= 0 for k in required_ints))
        if not schema_ok:
            valid_schema = False
            errors.append({"line": line_number, "error": "invalid_schema"})
            continue
        frequency = row["qpc_frequency"]
        timing_ok = (frequency > 0 and row["clock_valid"] == 1 and row["write_ok"] == 1 and
                     row["sample_end"] - row["sample_start"] == 1024 and
                     row["actual_start_qpc"] <= row["actual_end_qpc"] and
                     row["enqueue_qpc"] <= row["write_start_qpc"] <= row["write_end_qpc"] and
                     row["planned_qpc"] <= row["write_end_qpc"] and row["clock_errors"] == 0)
        for field, begin, end in (
            ("write_latency_us", "enqueue_qpc", "write_end_qpc"),
            ("queue_wait_us", "enqueue_qpc", "write_start_qpc"),
            ("write_io_us", "write_start_qpc", "write_end_qpc"),
            ("capture_to_write_us", "planned_qpc", "write_end_qpc"),
        ):
            if frequency <= 0 or row[end] < row[begin]:
                timing_ok = False
            else:
                expected = ((row[end] - row[begin]) * 1000000 + frequency - 1) // frequency
                timing_ok = timing_ok and row[field] == expected
        if not timing_ok:
            valid_timing = False
            errors.append({"line": line_number, "error": "qpc_latency_mismatch"})
    typed_rows = [row for row in rows if isinstance(row, dict)]
    initial = any(row.get("event") == "health" and row.get("sequence") == 0 for row in typed_rows)
    stall_rows = [row for row in typed_rows if row.get("event") == "slow_write" and
                  row.get("sequence") == 100 and row.get("sample_start") == 100 * 1024 and
                  type(row.get("write_latency_us")) is int and
                  row["write_latency_us"] >= stall_ms * 1000 and
                  type(row.get("queue_wait_us")) is int and row["queue_wait_us"] >= stall_ms * 1000]
    checks = {"present": bool(lines), "valid_json": len(rows) == len(lines),
              "valid_schema": valid_schema, "qpc_latency_consistent": valid_timing,
              "initial_health_sequence_zero": initial,
              "stall_sequence_100": stall_ms == 0 or len(stall_rows) == 1}
    return {"ok": all(checks.values()), "checks": checks, "errors": errors, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out", type=Path)
    ap.add_argument("--seconds", type=int, default=8)
    ap.add_argument("--split", type=int, default=3)
    ap.add_argument("--stall-ms", type=int, help="Use live SDL producer instead of pulse replay")
    ap.add_argument("--transport", choices=("shared_memory", "file"), default="shared_memory")
    ap.add_argument("--reader-stall-ms", type=int, default=0, choices=(0, 750))
    ap.add_argument("--missing-map", action="store_true", help="Require fail-closed with valid identity but no map")
    ap.add_argument("--decode", action="store_true")
    ap.add_argument("--av", action="store_true", help="Measure every synthetic pulse (requires replay)")
    ap.add_argument("--pattern", choices=("pulse", "pn"), default="pulse")
    args = ap.parse_args()
    if args.seconds < 1 or args.seconds > 600 or not 1 <= args.split <= 60:
        ap.error("seconds must be 1..600; split must be 1..60")
    if args.stall_ms is not None and args.stall_ms not in (0, 300, 1000, 6000):
        ap.error("stall-ms must be 0, 300, 1000 or 6000")
    if args.av and (args.pattern != "pulse" or args.stall_ms is not None):
        ap.error("--av requires a replayed pulse fixture")
    if args.reader_stall_ms and args.stall_ms is None:
        ap.error("reader stall requires a live producer (--stall-ms 0)")
    if args.missing_map and (args.stall_ms is not None or args.av or args.transport != "shared_memory"):
        ap.error("missing-map requires shared_memory without a producer or A/V measurement")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    exe = ROOT / "platform/obs-recorder/build-media/bin/auto-heros3-recorder.exe"
    module = ROOT / "platform/obs-recorder/bin/a3-obs-outputs.dll"
    sdl = ROOT / "platform/sdl-capture-patch/stage/SDL2.dll"
    smoke = ROOT / "platform/sdl-capture-patch/stage/a3capture-smoke.exe"
    source = out / "capture.a3a"
    artifacts = {str(p.relative_to(ROOT)): sha(p) for p in (exe, module, sdl)}
    evidence: dict = {"artifacts_before": artifacts, "requested": vars(args) | {"out": str(out)}}
    command = [str(exe), "--out-dir", str(out), "--mode", "in-memory",
               "--allow-single-segment",
               "--fixed-audio", "--mute-synthetic-audio", "--audio-framed", str(source),
               "--outputs-module", str(module), "--duration-ms", str(args.seconds * 1000),
               "--max-time-sec", str(args.split), "--json-out", str(out / "selftest.json")]
    env = clean_env()
    producer = consumer = None
    producer_log = None
    try:
        if args.stall_ms is None:
            subprocess.run([sys.executable, str(PROTOCOL / "make_test_framed.py"), str(source),
                            "--seconds", str(args.seconds + 6), "--pattern", args.pattern],
                           check=True, cwd=ROOT, env=env)
            # Pulse tests need a future shared origin. Continuous PN starts
            # immediately so the recording's first sample is also measurable.
            command += ["--video-pulse", "--av-origin-delay-ms",
                        "1000" if args.pattern == "pulse" else "0"]
            if args.missing_map:
                command += ["--audio-framed-live", "--audio-framed-transport", "shared_memory",
                            "--audio-capture-pid", str(os.getpid())]
            else:
                command += ["--audio-framed-transport", "file"]
        else:
            producer_env = dict(env)
            producer_env["A3CAPTURE_TEST_WORKER_STALL_ONCE_MS"] = str(args.stall_ms)
            producer_env["A3CAPTURE_TEST_WORKER_STALL_AFTER_PACKETS"] = "100"
            if args.stall_ms >= 1000:
                producer_env["A3CAPTURE_TEST_FAULT"] = "1"
            producer_log = (out / "producer.log").open("w", encoding="utf-8")
            producer = subprocess.Popen([str(smoke), str(sdl), str(source),
                                         str((args.seconds + 6) * 1000)],
                                        stdout=producer_log, stderr=subprocess.STDOUT,
                                        cwd=ROOT, env=producer_env)
            deadline = time.monotonic() + 10
            while not source.exists() or source.stat().st_size < 128:
                if producer.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("private SDL producer did not publish its first packet")
                time.sleep(.02)
            command += ["--audio-framed-live", "--audio-framed-transport", args.transport,
                        "--audio-capture-pid", str(producer.pid)]
            if args.reader_stall_ms:
                env["A3_FRAMED_TEST_DELAY_AFTER_FIRST_MS"] = str(args.reader_stall_ms)
        evidence["command"] = command
        with (out / "native.log").open("w", encoding="utf-8") as log:
            consumer = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT, env=env)
            evidence["native_exit"] = consumer.wait(timeout=args.seconds + 35)
        if producer is not None:
            evidence["producer_exit"] = producer.wait(timeout=20)
            producer_log.close()
            producer_log = None
        report = json.loads((out / "selftest.json").read_text(encoding="utf-8-sig"))
        evidence["native_status"] = report.get("status")
        framed = (report.get("media") or {}).get("framed_audio") or report.get("framed_audio") or {}
        # raw_audio is the unrelated legacy reader, not framed source telemetry.
        if not framed:
            framed = framed_failure(report)
        evidence["framed"] = framed
        expected_failure = (bool(args.reader_stall_ms) or args.stall_ms == 6000 or
                            args.transport == "file" and args.stall_ms == 1000)
        if args.missing_map:
            ok = expected_missing_map(report, evidence["native_exit"])
        elif expected_failure:
            code = framed_failure(report).get("error_code")
            evidence["observed_failures"] = [code] if code else []
            ok = expected_stall_failure(report, evidence["native_exit"], args.stall_ms,
                                        args.transport, args.reader_stall_ms)
        else:
            ok = evidence["native_exit"] == 0 and report.get("status") == "pass"
            if args.stall_ms is not None and args.transport == "shared_memory":
                evidence["shared_delivery_checks"] = shared_delivery_checks(report)
                ok = ok and all(evidence["shared_delivery_checks"].values())
            clips = list(out.glob("clip-*.mp4"))
            evidence["intervals"] = [check_interval(p) for p in clips]
            # One-second GOP quantization may extend each requested split.
            minimum_clips = max(1, args.seconds // (args.split + 1))
            evidence["minimum_clips"] = minimum_clips
            ok = ok and len(clips) >= minimum_clips and all(r["ok"] for r in evidence["intervals"])
            tail = subprocess.run([sys.executable, str(PROTOCOL / "verify_tail_contract.py"),
                                   str(out), "--json", str(out / "tail.json")],
                                  capture_output=True, text=True, cwd=ROOT)
            evidence["tail_exit"] = tail.returncode
            evidence["tail_output"] = tail.stdout + tail.stderr
            ok = ok and tail.returncode == 0
            if args.decode:
                decode = []
                for clip in clips:
                    r = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(clip),
                                        "-f", "null", "-"], capture_output=True, text=True)
                    decode.append({"clip": clip.name, "exit": r.returncode, "error": r.stderr})
                evidence["decode"] = decode
                ok = ok and all(r["exit"] == 0 for r in decode)
            if args.av:
                r = subprocess.run([sys.executable, str(PROTOCOL / "measure_av_pulses.py"),
                                    str(out), "--json", str(out / "av.json")],
                                   capture_output=True, text=True, cwd=ROOT)
                evidence["av_exit"] = r.returncode
                evidence["av_output"] = r.stdout + r.stderr
                ok = ok and r.returncode == 0
        if args.stall_ms is not None:
            producer_text = (out / "producer.log").read_text(encoding="utf-8", errors="replace")
            metrics = producer_metrics(producer_text)
            evidence["producer_metrics"] = metrics
            diagnostics = producer_diagnostics(producer_text, args.stall_ms)
            evidence["producer_diagnostics"] = diagnostics
            ok = (ok and metrics.get("write_latency_max_us", 0) >= max(1, args.stall_ms * 1000) and
                  metrics.get("clock_errors", -1) == 0 and metrics.get("measurements", 0) > 0 and
                  diagnostics["ok"] and
                  evidence.get("producer_exit") == 0)
        evidence["artifacts_after"] = {name: sha(ROOT / name) for name in artifacts}
        evidence["ok"] = bool(ok and evidence["artifacts_after"] == artifacts)
    except Exception as exc:
        evidence.update(ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        stop_owned(consumer)
        stop_owned(producer)
        if producer_log:
            producer_log.close()
        (out / "acceptance.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps({"ok": evidence["ok"], "out": str(out), "error": evidence.get("error")}, indent=2))
    return 0 if evidence["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
