#!/usr/bin/env python3
"""A4 live-chain test: a3capture -> pipe relay -> framed file -> native source -> MP4."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    root = Path(a.root).resolve()
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    pipe = "a4live_" + str(os.getpid())
    ready = out / "relay.ready"
    framed = out / "live.a3a"
    for p in (ready, framed):
        try:
            p.unlink()
        except OSError:
            pass
    relay = subprocess.Popen(
        [sys.executable, str(root / "platform/audio-protocol/python/a3_framed.py"),
         "--pipe", pipe, "--ready", str(ready), "--framed-out", str(framed),
         "--raw-out", str(out / "relay.raw"), "--index-out", str(out / "index.jsonl"),
         "--manifest-out", str(out / "manifest.json"), "--require-last"],
        stdout=open(out / "relay.log", "wb"), stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not ready.exists():
            if relay.poll() is not None:
                raise RuntimeError("relay exited before ready")
            time.sleep(0.02)
        if not ready.exists():
            raise RuntimeError("relay not ready")
        rec = subprocess.Popen(
            [str(root / "platform/obs-recorder/build-media/bin/obs-media-selftest.exe"),
             "--out-dir", str(out), "--mode", "in-memory", "--fixed-audio",
             "--mute-synthetic-audio", "--audio-framed", str(framed),
             "--audio-framed-live", "--duration-ms", "5000", "--max-time-sec", "1",
             "--json-out", str(out / "selftest.json")],
            stdout=open(out / "rec.log", "wb"), stderr=subprocess.STDOUT)
        time.sleep(0.8)
        smoke = subprocess.run(
            [str(root / "platform/sdl-capture-patch/stage/a3capture-smoke.exe"),
             str(root / "platform/sdl-capture-patch/stage/SDL2.dll"),
             "\\\\.\\pipe\\" + pipe, "1200"],
            stdout=open(out / "smoke.log", "wb"), stderr=subprocess.STDOUT,
            timeout=30)
        rc = rec.wait(timeout=30)
        rr = relay.wait(timeout=20)
        if rc != 0 or rr != 0:
            raise RuntimeError(f"recorder={rc} relay={rr}")
    finally:
        if relay.poll() is None:
            relay.kill()
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["packets"] == 53, manifest
    assert manifest["frames"] == 53248, manifest
    assert manifest["saw_last"] is True, manifest
    print(json.dumps(manifest, sort_keys=True))
    print("LIVE_PIPE_NATIVE_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
