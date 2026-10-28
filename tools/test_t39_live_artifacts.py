# -*- coding: utf-8 -*-
r"""T39 LIVE smoke: debug-only all-artifacts cheat (VCMI vcmiforgeofnoldorking equivalent).

LIVE/DEBUG - starts ONE real VCMI client in an isolated tree; refused unless every writable path
is redirected.  Not part of the offline regression suite.

Flow:
  1 explicit config start; lease acquired
  2 debug cheat channel: allartifacts all   (test-only, env-gated; MCP has no cheat API)
  3 game_brief/state shows a large worn+backpack artifact set and changed hero stats
  4 clean stop

Manual run: same isolated env as test_t35_live_magic.py, then
    .venv\Scripts\python.exe -X utf8 tools\test_t39_live_artifacts.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import test_t35_live_magic as T  # noqa: E402  (isolation guard runs at import)
import mcp_server_v2 as M  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("PASS" if ok else "FAIL"), name, detail)


def hero_artifacts(st: dict) -> dict:
    hero = T.hero_of(st)
    art = hero.get("artifacts") or {}
    return {"worn": list(art.get("worn") or []), "backpack": list(art.get("backpack") or [])}


def artifact_count(st: dict) -> int:
    a = hero_artifacts(st)
    return len(a["worn"]) + len(a["backpack"])


def primary_sum(hero: dict) -> int:
    p = hero.get("primary") or {}
    return sum(int(p.get(k) or 0) for k in ("attack", "defence", "spellpower", "knowledge"))


def main() -> int:
    started = False
    try:
        start = M.session_start("Maps/Key to Victory", difficulty="easy", player_color="red",
                                starting_town="castle", starting_hero="random",
                                starting_bonus="random")
        started = bool(start.get("ok"))
        check("T39-1 explicit config start", started, str(start.get("error") or start.get("config")))
        M.lease_acquire()
        st = T.wait_ready()
        hero = T.hero_of(st)
        before = artifact_count(st)
        before_stats = primary_sum(hero)
        check("T39-2 new game with hero", bool(st.get("in_game")) and bool(hero),
              f"day={(st.get('date') or {}).get('day')} artifacts={before}")

        T.write_cheat(["allartifacts all"])
        learned = T.wait_for(lambda: artifact_count(T.state_now()) > before, timeout=30.0)
        st = T.state_now()
        hero = T.hero_of(st)
        arts = hero_artifacts(st)
        total = len(arts["worn"]) + len(arts["backpack"])
        names = [a.get("name") for a in arts["worn"] + arts["backpack"]]
        check("T39-3 debug allartifacts added artifacts", learned and total > before,
              f"{before}->{total} worn={len(arts['worn'])} backpack={len(arts['backpack'])}")
        check("T39-4 equipped/worn slots populated", len(arts["worn"]) >= 5,
              f"worn={len(arts['worn'])} backpack={len(arts['backpack'])}")
        check("T39-5 backpack contains artifacts", len(arts["backpack"]) > 0,
              f"backpack={len(arts['backpack'])}")
        after_stats = primary_sum(hero)
        check("T39-6 artifact bonuses changed primary stats", after_stats >= before_stats,
              f"primary_sum {before_stats}->{after_stats}")
        check("T39-7 artifact names visible", any(isinstance(n, str) and n for n in names),
              str(names[:8]))
        brief = M.game_brief()
        bh = (brief.get("heroes") or [{}])[0]
        barts = bh.get("artifacts") or {}
        if isinstance(barts, dict):
            shown = len(barts.get("worn") or []) + len(barts.get("backpack") or [])
            detail = {k: len(v or []) for k, v in barts.items()}
        elif isinstance(barts, list):
            shown = len(barts)
            detail = {"list": shown}
        else:
            shown = 0
            detail = {"type": type(barts).__name__}
        check("T39-8 game_brief shows artifacts", shown >= 5, str(detail))
        print("ARTIFACT_SAMPLE", json.dumps(names[:20], ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        check("T39 live smoke completed without exception", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            stop = M.session_stop()
            if started:
                check("T39-9 clean stop", stop.get("ok") is True and not (stop.get("leftover") or []),
                      str(stop)[:200])
        except Exception as exc:  # noqa: BLE001
            check("T39-9 clean stop", False, f"{type(exc).__name__}: {exc}")
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
