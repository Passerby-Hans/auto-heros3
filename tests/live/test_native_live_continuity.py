"""Explicit live SDL/shared-memory PN test; no physical playback or endpoints.

The SDL smoke reads a preloaded PCM fixture. We require its complete archive to
equal the original fixture prefix before treating that archive as a reference.
Decoded AAC is then located against that independent non-periodic reference.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "platform/audio-protocol/python"
sys.path.insert(0, str(PROTOCOL))
from audio_locator import RATE, load_reference, make_pn
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_native_audio_delivery import shared_delivery_checks


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out", type=Path)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--split", type=int, default=10)
    ap.add_argument("--archive-stall-ms", type=int, default=1000)
    args = ap.parse_args()
    if not args.execute:
        ap.error("live test requires --execute")
    if not 8 <= args.seconds <= 600 or not 1 <= args.split <= args.seconds:
        ap.error("invalid duration/split")
    if not 0 <= args.archive_stall_ms <= 3000:
        ap.error("this positive continuity test allows only bounded 0..3000ms archive stalls")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    exe = ROOT / "platform/obs-recorder/build-media/bin/auto-heros3-recorder.exe"
    outputs = ROOT / "platform/obs-recorder/bin/a3-obs-outputs.dll"
    sdl = ROOT / "platform/sdl-capture-patch/stage/SDL2.dll"
    smoke = ROOT / "platform/sdl-capture-patch/stage/a3capture-smoke.exe"
    artifacts = {str(p.relative_to(ROOT)): digest(p) for p in (exe, outputs, sdl, smoke)}
    result = {"schema": "a3-live-pn-acceptance/1", "ok": False,
              "artifacts_before": artifacts, "source_commit": subprocess.check_output(
                  ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "requested_seconds": args.seconds, "archive_stall_ms": args.archive_stall_ms,
              "physical_playback_requested": False, "live_transport": "shared_memory"}
    producer = consumer = None
    try:
        pcm = make_pn((args.seconds + 15) * RATE)
        pcm_path = out / "input-pn.s16le"
        pcm_path.write_bytes(pcm.tobytes())
        source = out / "capture.a3a"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("A3_", "A3CAPTURE_", "AUTO_HEROS3_", "SDL_AUDIO"))}
        producer_env = dict(env, A3_SMOKE_PCM_FILE=str(pcm_path),
                            A3CAPTURE_TEST_WORKER_STALL_ONCE_MS=str(args.archive_stall_ms),
                            A3CAPTURE_TEST_WORKER_STALL_AFTER_PACKETS="100")
        with (out / "producer.log").open("w", encoding="utf-8") as producer_log:
            producer = subprocess.Popen([str(smoke), str(sdl), str(source),
                                         str((args.seconds + 8) * 1000)], cwd=ROOT,
                                        env=producer_env, stdout=producer_log,
                                        stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 10
            while not source.exists() or source.stat().st_size < 128:
                if producer.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("producer_did_not_publish_first_packet")
                time.sleep(.02)
            command = [str(exe), "--out-dir", str(out), "--mode", "in-memory",
                       "--allow-single-segment", "--fixed-audio", "--mute-synthetic-audio",
                       "--audio-framed", str(source), "--audio-framed-live",
                       "--audio-framed-transport", "shared_memory",
                       "--audio-capture-pid", str(producer.pid),
                       "--outputs-module", str(outputs), "--duration-ms", str(args.seconds * 1000),
                       "--max-time-sec", str(args.split), "--json-out", str(out / "selftest.json")]
            result["command"] = command
            with (out / "native.log").open("w", encoding="utf-8") as log:
                consumer = subprocess.Popen(command, cwd=ROOT, env=env,
                                            stdout=log, stderr=subprocess.STDOUT)
                result["native_exit"] = consumer.wait(timeout=args.seconds + 40)
            result["producer_exit"] = producer.wait(timeout=25)
        _, reference = load_reference(source)
        expected = pcm[:reference["frames"]].tobytes()
        result["reference"] = reference
        result["archive_equals_input_prefix"] = (
            0 < reference["frames"] <= len(pcm) and
            hashlib.sha256(expected).hexdigest() == reference["pcm_sha256"])
        if not result["archive_equals_input_prefix"]:
            raise RuntimeError("capture_archive_does_not_equal_preloaded_nonperiodic_fixture")
        fixture = dict(reference, schema="a3-pn-fixture/1", pattern="continuous_bandlimited_pn",
                       channels=2, seed=670887, generator="numpy.PCG64",
                       filter="129-tap Hamming bandpass 700..5000 Hz",
                       original_input_pcm_sha256=digest(pcm_path),
                       verification="complete captured PCM equals preloaded input prefix")
        source.with_suffix(source.suffix + ".fixture.json").write_text(json.dumps(fixture, indent=2), "utf-8")
        native = json.loads((out / "selftest.json").read_text("utf-8-sig"))
        result["native_status"] = native.get("status")
        result["shared_delivery"] = shared_delivery_checks(native)
        for name, script, extra in (
            ("decoded_continuity", "verify_decoded_continuity.py", ["--reference", str(source)]),
            ("tail", "verify_tail_contract.py", []),
        ):
            completed = subprocess.run([sys.executable, str(PROTOCOL / script), str(out),
                                        *extra, "--json", str(out / (name + ".json"))],
                                       capture_output=True, text=True, cwd=ROOT, timeout=180)
            result[name + "_exit"] = completed.returncode
            (out / (name + ".log")).write_text(completed.stdout + completed.stderr, "utf-8")
        worker = []
        for line in (out / "producer.log").read_text("utf-8", errors="replace").splitlines():
            if "a3capture_write_diag {" in line:
                worker.append(json.loads(line.split("a3capture_write_diag ", 1)[1]))
        result["archive_stall_observed"] = args.archive_stall_ms == 0 or any(
            r.get("sequence") == 100 and r.get("queue_wait_us", 0) >= args.archive_stall_ms * 1000
            for r in worker)
        result["artifacts_after"] = {name: digest(ROOT / name) for name in artifacts}
        result["ok"] = bool(result["native_exit"] == 0 and result["producer_exit"] == 0 and
                            result["native_status"] == "pass" and result["decoded_continuity_exit"] == 0 and
                            result["tail_exit"] == 0 and result["archive_stall_observed"] and
                            all(result["shared_delivery"].values()) and
                            result["artifacts_after"] == artifacts)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for process in (consumer, producer):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        (out / "acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps({"ok": result["ok"], "evidence": str(out / "acceptance.json"),
                      "error": result.get("error")}, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
