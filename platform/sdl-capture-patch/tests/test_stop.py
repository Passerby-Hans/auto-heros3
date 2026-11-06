#!/usr/bin/env python3
"""Graceful-stop integration test: stop marker -> LAST/drain -> complete framed file."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", required=True)
    ap.add_argument("--dll", required=True)
    ap.add_argument("--validator", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    raw = out / "stop.raw"
    stop = out / "stop.marker"
    for p in (raw, stop):
        try:
            p.unlink()
        except OSError:
            pass
    env = dict(os.environ)
    env["A3CAPTURE_STOP_FILE"] = str(stop)
    env["A3CAPTURE_TEST_FAULT"] = "1"
    proc = subprocess.Popen([a.smoke, a.dll, str(raw), "3000"], env=env,
                            stdout=open(out / "stop.log", "wb"),
                            stderr=subprocess.STDOUT)
    try:
        time.sleep(0.8)
        stop.write_text("stop", encoding="utf-8")
        rc = proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    if rc != 0:
        raise RuntimeError(f"smoke exit={rc}")
    v = subprocess.run([sys.executable, a.validator, str(raw), "--require-last",
                        "--json", str(out / "stop.framed.json")],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, timeout=20)
    (out / "validate.log").write_text(v.stdout, encoding="utf-8")
    if v.returncode != 0:
        raise RuntimeError(f"validator exit={v.returncode}: {v.stdout}")
    print(v.stdout)
    print("GRACEFUL_STOP_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
