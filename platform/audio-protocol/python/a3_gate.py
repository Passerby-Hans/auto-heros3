#!/usr/bin/env python3
"""Analyze a3capture QPC/sample timing from the relay JSONL index."""
from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path


def analyze(index_path: Path, max_ppm: float) -> dict:
    recs = [json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines() if line]
    if len(recs) < 2:
        raise ValueError("need at least two packets")
    freq = recs[0]["qpc_frequency"]
    if not freq:
        raise ValueError("bad qpc frequency")
    first = recs[0]["actual_start_qpc"]
    xs = [r["sample_start"] / 44100.0 for r in recs]
    ys = [(r["actual_start_qpc"] - first) / freq for r in recs]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    ppm = (slope - 1.0) * 1e6
    residuals = [y - (my + slope * (x - mx)) for x, y in zip(xs, ys)]
    late_ms = sorted((r["actual_start_qpc"] - r["planned_qpc"]) * 1000.0 / freq
                     for r in recs)
    p95 = late_ms[min(len(late_ms) - 1, int(0.95 * (len(late_ms) - 1)))]
    frames = sum(r["sample_count"] for r in recs)
    # actual QPC span from the first start to the last start, not wall time.
    qpc_span = ys[-1] - ys[0]
    result = {
        "packets": n,
        "frames": frames,
        "sample_seconds": frames / 44100.0,
        "qpc_span_seconds": qpc_span,
        "slope": slope,
        "ppm": ppm,
        "residual_max_ms": max(abs(x) for x in residuals) * 1000.0,
        "residual_rms_ms": math.sqrt(sum(x * x for x in residuals) / n) * 1000.0,
        "late_median_ms": statistics.median(late_ms),
        "late_p95_ms": p95,
        "late_max_ms": late_ms[-1],
        "late_over_1ms": sum(x > 1.0 for x in late_ms),
        "late_over_5ms": sum(x > 5.0 for x in late_ms),
        "late_over_20ms": sum(x > 20.0 for x in late_ms),
        "discontinuity_packets": sum(bool(r["flags"] & 4) for r in recs),
        "overflow_packets": sum(bool(r["flags"] & 8) for r in recs),
        "first_flags": recs[0]["flags"],
        "last_flags": recs[-1]["flags"],
        "last_frames": recs[-1]["sample_count"],
        "max_ppm": max_ppm,
        "ppm_ok": abs(ppm) <= max_ppm,
        "no_discontinuity": all(not (r["flags"] & 4) for r in recs),
        "no_overflow": all(not (r["flags"] & 8) for r in recs),
    }
    result["gate_ok"] = bool(
        result["ppm_ok"] and result["no_discontinuity"] and result["no_overflow"]
        and result["late_over_20ms"] == 0
    )
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("index")
    ap.add_argument("--max-ppm", type=float, default=100.0)
    ap.add_argument("--json")
    a = ap.parse_args()
    try:
        result = analyze(Path(a.index), a.max_ppm)
    except Exception as exc:
        print(f"FAIL: {exc}")
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    if a.json:
        Path(a.json).write_text(json.dumps(result, indent=2, sort_keys=True),
                               encoding="utf-8")
    return 0 if result["gate_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
