#!/usr/bin/env python3
"""Integration test: a3capture -> named pipe -> a3_framed relay -> raw/index."""
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
    ap.add_argument("--relay", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--duration-ms", type=int, default=1200)
    a = ap.parse_args()
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    pipe = "a3it_" + str(os.getpid()) + "_" + str(time.time_ns())[-6:]
    ready = out / "relay.ready"
    manifest = out / "manifest.json"
    relay = subprocess.Popen(
        [sys.executable, a.relay, "--pipe", pipe, "--ready", str(ready),
         "--framed-out", str(out / "capture.a3a"),
         "--raw-out", str(out / "audio.raw"),
         "--index-out", str(out / "index.jsonl"),
         "--manifest-out", str(manifest), "--require-last"],
        stdout=open(out / "relay.log", "wb"),
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not ready.exists():
            if relay.poll() is not None:
                raise RuntimeError("relay exited before ready")
            time.sleep(0.02)
        if not ready.exists():
            raise RuntimeError("relay ready timeout")
        env = dict(os.environ)
        env["A3CAPTURE_TEST_PIPE"] = "1"
        smoke = subprocess.run(
            [a.smoke, a.dll, "\\\\.\\pipe\\" + pipe, str(a.duration_ms)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=30,
        )
        (out / "smoke.log").write_bytes(smoke.stdout)
        if smoke.returncode != 0:
            raise RuntimeError(f"smoke exit={smoke.returncode}")
        if relay.wait(timeout=20) != 0:
            raise RuntimeError(f"relay exit={relay.returncode}")
    finally:
        if relay.poll() is None:
            relay.kill()
    import json
    m = json.loads(manifest.read_text(encoding="utf-8"))
    assert m["packets"] == 53, m
    assert m["frames"] == 53248, m
    assert m["saw_first"] and m["saw_last"], m
    assert m["partial_tail"] is False, m
    assert (out / "audio.raw").stat().st_size == 53248 * 4
    print(json.dumps(m, sort_keys=True))
    print("PIPE_RELAY_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
