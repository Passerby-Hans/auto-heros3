# -*- coding: utf-8 -*-
"""Unified OFFLINE test entry - explicit whitelist, never a glob.

Why: the suites live under ``tests/offline`` (29 deterministic suites) and ``tests/live``
(7 LIVE tests that start the real VCMI client and/or would write outside temporary
directories).  Globbing all of them must never be the default.  This runner executes exactly
the suites listed in ``OFFLINE_SUITES`` (each one is a plain script that prints
``N/N checks passed`` and exits non-zero on failure), and only *reports* the live suites it
skipped.  ``tools/test_*.py`` are compatibility shims for the historical commands.

B0.5 layout: canonical suites are ``tests/offline/<name>`` / ``tests/live/<name>``; the
runner resolves them there.  Adding a new offline suite is a two-line change: create the
file under ``tests/offline`` and append its name to ``OFFLINE_SUITES``.  The runner warns
when an unlisted ``test_*.py`` shows up, so a new test cannot silently escape validation.

Usage
-----
    .venv\\Scripts\\python.exe -X utf8 tools\\run_offline_suite.py
    .venv\\Scripts\\python.exe -X utf8 tools\\run_offline_suite.py --list
    .venv\\Scripts\\python.exe -X utf8 tools\\run_offline_suite.py --suite test_fog_path.py
    .venv\\Scripts\\python.exe -X utf8 tools\\run_offline_suite.py --json-out var\\reports\\offline.json

Exit code: 0 only when every selected offline suite passes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
OFFLINE_DIR = ROOT / "tests" / "offline"
LIVE_DIR = ROOT / "tests" / "live"

# Explicit whitelist.  Order mirrors the historical offline-full.log for easy diffing.
OFFLINE_SUITES: tuple[str, ...] = (
    "test_action_contract.py",
    "test_crash_recovery.py",
    "test_fog_path.py",
    "test_n0_isolation.py",
    "test_n1_restore.py",
    "test_n2_contract.py",
    "test_p4_hardening.py",
    "test_p5_fixes.py",
    "test_protocol_mock.py",
    "test_recording_health.py",
    "test_recording_manifest.py",
    "test_native_stop.py",
    "test_recording_start.py",
    "test_recording_lock_guard.py",
    "test_recording_segment.py",
    "test_short_game_tools.py",
    "test_t35_spellcasting.py",
    "test_t37_hygiene.py",
    "test_t40_bridge_contract.py",
    "test_t42_battle_spells.py",
    "test_w0_config.py",
    "test_w0_w1_fixes.py",
    "test_w1_trust.py",
    "test_w2_battle.py",
    "test_w3_economy.py",
    "test_w4_explore.py",
    "test_w4_hero_observation.py",
    "test_w4_recovery_surface.py",
    "test_w6_evidence.py",
    "test_b0_guards.py",
    "test_reliability_fixes.py",
    "test_explore_contract.py",
    "test_knowledge_sources.py",
    "test_display_lifecycle.py",
    "test_wgc_source.py",
    "test_p5_placement.py",
    "test_audio_mux.py",
    "test_native_media_contract.py",
    "test_native_promotion.py",
    "test_sdl_deployment.py",
    "test_sdl_source_lock.py",
    "test_shared_audio_delivery.py",
    "test_native_live_harness.py",
    "test_obs_wmi_apartment.py",
    "test_obs_process_ownership.py",
)

# LIVE suites: listed for transparency only; they run native capture/recording or
# the real client and must be selected explicitly in an isolated directory.
LIVE_SUITES: tuple[str, ...] = (
    "test_native_audio_delivery.py",
    "test_native_live_continuity.py",
    "test_native_recording_e2e.py",
    "test_native_default_lifecycle.py",
    "test_n1_live_restore.py",
    "test_p6_live_hero_fields.py",
    "test_session_memory.py",
    "test_t35_live_magic.py",
    "test_t39_live_artifacts.py",
    "test_t40_live_regression.py",
    "test_t42_live_two_stage.py",
)

# Test-isolation escape hatch read by the MCP server/driver at import time.  The runner must
# NOT let the caller's shell inject these into the suite subprocesses: a suite that replaces
# module.ROOT with its temp fixture can still follow an inherited path override and write
# outside its fixture.  GPT6 review P2 (2026-10-30) reproduced exactly that with
# AUTO_HEROS3_RECORDINGS_DIR: test_recording_segment 20/20 PASS but wrote 7 fake recording
# files into the inherited directory.  Suites that need redirects set these variables inside
# their own subprocess (see tools/test_session_memory.py's import guard), so stripping the
# inherited values is safe and makes the offline run deterministic.
PROJECT_ENV_PREFIX = "AUTO_HEROS3_"


def suite_environment() -> tuple[dict[str, str], list[str]]:
    """Environment for one suite subprocess + the inherited project vars that were dropped."""
    env = dict(os.environ)
    removed = sorted(name for name in env if name.startswith(PROJECT_ENV_PREFIX))
    for name in removed:
        env.pop(name, None)
    return env, removed

PASSED_RE = re.compile(r"^(\d+)/(\d+) checks passed\s*$", re.MULTILINE)


def parse_result(stdout: str) -> tuple[int, int] | None:
    matches = PASSED_RE.findall(stdout)
    if not matches:
        return None
    passed, total = matches[-1]
    return int(passed), int(total)


def run_suite(name: str, timeout: float) -> dict:
    path = OFFLINE_DIR / name
    started = time.time()
    record = {"suite": name, "path": str(path), "exit": None, "passed": None,
              "total": None, "seconds": 0.0, "output_tail": "",
              "inherited_env_removed": []}
    if not path.is_file():
        record["output_tail"] = "suite file is missing"
        return record
    env, removed = suite_environment()
    record["inherited_env_removed"] = removed
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(path)],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        record["exit"] = "timeout"
        record["output_tail"] = f"timeout after {timeout}s"
        record["seconds"] = round(time.time() - started, 1)
        record["_stdout"] = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return record
    record["exit"] = proc.returncode
    record["seconds"] = round(time.time() - started, 1)
    combined = (proc.stdout or "") + (proc.stderr or "")
    parsed = parse_result(proc.stdout or "")
    if parsed:
        record["passed"], record["total"] = parsed
    record["output_tail"] = "\n".join(combined.strip().splitlines()[-3:])
    record["_stdout"] = proc.stdout or ""
    return record


def selected_suites(argv_suites: list[str]) -> list[str]:
    if not argv_suites:
        return list(OFFLINE_SUITES)
    wanted = []
    for name in argv_suites:
        wanted.append(name if name.endswith(".py") else f"{name}.py")
    unknown = [name for name in wanted if name not in OFFLINE_SUITES]
    if unknown:
        raise SystemExit(
            "refusing to run suites that are not in the offline whitelist: "
            + ", ".join(unknown)
            + f"\nallowed: {', '.join(OFFLINE_SUITES)}"
        )
    return wanted


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the whitelisted offline auto-heros3 test suites")
    ap.add_argument("--list", action="store_true", help="list offline/live suites and exit")
    ap.add_argument("--suite", action="append", default=[], metavar="NAME",
                    help="run only NAME (repeatable; must be whitelisted)")
    ap.add_argument("--timeout", type=float, default=300.0, help="per-suite timeout seconds")
    ap.add_argument("--json-out", type=Path, default=None, help="write a JSON result file")
    a = ap.parse_args(argv)

    if a.list:
        print(f"offline ({len(OFFLINE_SUITES)}):")
        for name in OFFLINE_SUITES:
            print(f"  {name}")
        print(f"live ({len(LIVE_SUITES)}, never run by default):")
        for name in LIVE_SUITES:
            print(f"  {name}")
        return 0

    suites = selected_suites(a.suite)
    print(f"OFFLINE SUITE {time.strftime('%Y-%m-%d %H:%M:%S')}  suites={len(suites)}")
    inherited_dropped = sorted({name for name in os.environ if name.startswith(PROJECT_ENV_PREFIX)})
    if inherited_dropped:
        print("NOTICE: dropped inherited project environment for suite subprocesses: "
              + ", ".join(inherited_dropped))
    records = []
    failed = 0
    total_checks = 0
    total_passed = 0
    for name in suites:
        rec = run_suite(name, a.timeout)
        records.append(rec)
        checks = rec.get("total")
        if rec.get("passed") is not None:
            total_checks += rec["total"] or 0
            total_passed += rec["passed"] or 0
        ok = rec["exit"] == 0 and rec.get("passed") is not None \
            and rec.get("passed") == rec.get("total")
        if not ok:
            failed += 1
        shown = f"{rec.get('passed')}/{rec.get('total')}" if rec.get("total") is not None \
            else "NO RESULT"
        print(f"{name:42s} {shown:16s} exit={rec['exit']} "
              f"{'PASS' if ok else 'FAIL'} ({rec['seconds']:.1f}s)")
        if not ok:
            for line in rec["output_tail"].splitlines():
                print(f"    | {line}")

    print(f"==== TOTAL {total_passed}/{total_checks} checks; suites={len(suites)} failed={failed}")
    if not a.suite:
        present = {p.name for p in OFFLINE_DIR.glob("test_*.py")}
        unlisted = sorted(present - set(OFFLINE_SUITES))
        live_present = {p.name for p in LIVE_DIR.glob("test_*.py")}
        skipped_live = sorted(live_present & set(LIVE_SUITES))
        if skipped_live:
            print(f"skipped live suites ({len(skipped_live)}): {', '.join(skipped_live)}")
        if unlisted:
            print(f"WARNING: unlisted test_*.py not run (add to OFFLINE_SUITES or LIVE_SUITES): "
                  f"{', '.join(unlisted)}")

    if a.json_out:
        a.json_out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "python": sys.version,
            "inherited_project_env_dropped": inherited_dropped,
            "suites": [{k: v for k, v in rec.items() if k != "_stdout"} for rec in records],
            "total_passed": total_passed,
            "total_checks": total_checks,
            "failed": failed,
        }
        a.json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"json -> {a.json_out}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
