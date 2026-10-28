# -*- coding: utf-8 -*-
r"""T33/P-B LIVE smoke: verify the rebuilt client publishes the new hero observation fields.

LIVE/DEBUG - starts ONE real VCMI client in an isolated tree; refused unless every writable path
is redirected.  It is not part of the offline regression suite.

Checks (one new game, no gameplay beyond one end_turn + one short recording):
  P6L-1  explicit config start + contract pinned-exe match (new sha) + protocol 2
  P6L-2  raw state.json hero carries mana_max/spellbook/spells/skills/artifacts/luck/morale/
         specialty/army.creature_name, with readable names (spellbook artifact name, skill names)
  P6L-3  game_observe projects the full hero observation (lists + names)
  P6L-4  game_brief projects the compact hero view (skills/spell names/artifact names/counts)
  P6L-5  after one end_turn the recovery surface (last_action/recent_actions) works on the
         rebuilt bridge's ledger
  P6L-6  recording identity binds the NEW exe sha and health samples are usable
  P6L-7  clean stop, no leftover client process

Safety gate (executed BEFORE importing mcp_server_v2):
  * AUTO_HEROS3_LIVE_TEST=1 is required for the real body.
  * AUTO_HEROS3_STATE_DIR / SAVES_DIR / SAVE_SUBDIR / MEMORY_ROOT / RECORDINGS_DIR must be set;
    STATE/MEMORY/RECORDINGS must live under ROOT/var/test_n1/ or %TEMP%; SAVE_SUBDIR must start
    with 'agent_n1'.
  * AUTO_HEROS3_LIVE_TEST_DRYRUN=1 prints the resolved paths and exits 0 without starting a client.

Manual run (example):
    set AUTO_HEROS3_LIVE_TEST=1
    set AUTO_HEROS3_STATE_DIR=%TEMP%\p6\state
    set AUTO_HEROS3_SAVES_DIR=%USERPROFILE%\Documents\My Games\vcmi\Saves
    set AUTO_HEROS3_SAVE_SUBDIR=agent_n1_pb_manual
    set AUTO_HEROS3_MEMORY_ROOT=%TEMP%\p6\memory
    set AUTO_HEROS3_RECORDINGS_DIR=%TEMP%\p6\recordings
    .venv\Scripts\python.exe -X utf8 tools\test_p6_live_hero_fields.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    results.append((name, bool(ok), detail))
    print(('PASS' if ok else 'FAIL'), name, detail)


def _canon(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _p6_guard() -> dict:
    """Refuse to start a real client unless every writable path is isolated. Returns paths."""
    resolved = {
        'STATE_DIR': os.environ.get('AUTO_HEROS3_STATE_DIR'),
        'SAVES_DIR': os.environ.get('AUTO_HEROS3_SAVES_DIR'),
        'MEMORY_ROOT': os.environ.get('AUTO_HEROS3_MEMORY_ROOT'),
        'RECORDINGS_DIR': os.environ.get('AUTO_HEROS3_RECORDINGS_DIR'),
    }
    problems: list[str] = []
    temp_roots = [_canon(Path(tempfile.gettempdir()))]
    test_root = _canon(ROOT / 'var' / 'test_n1')
    allowed = temp_roots + [test_root]
    formal = {
        'STATE_DIR': _canon(ROOT / 'var' / 'agent_mcp_v2'),
        'SAVES_DIR': _canon(Path.home() / 'Documents' / 'My Games' / 'vcmi' / 'Saves' / 'agent'),
        'MEMORY_ROOT': _canon(ROOT / 'doc' / 'player'),
        'RECORDINGS_DIR': _canon(ROOT / 'var' / 'recordings'),
    }
    for var, raw in resolved.items():
        if not raw:
            problems.append(f'{var} is not set')
            continue
        path = _canon(Path(raw))
        if var != 'SAVES_DIR' and (path == formal[var] or formal[var].is_relative_to(path)
                                   or path.is_relative_to(formal[var])):
            problems.append(f'{var} points at the formal directory ({formal[var]})')
        if var == 'SAVES_DIR':
            # The engine always writes under <Documents>/My Games/vcmi/Saves; only accept the
            # default saves root (the test then uses a dedicated agent_n1* subdirectory).
            default_saves = _canon(Path.home() / 'Documents' / 'My Games' / 'vcmi' / 'Saves')
            if path != default_saves and not any(path == allowed_root
                                                 or path.is_relative_to(allowed_root)
                                                 for allowed_root in allowed):
                problems.append(f'{var}={path} is not the default saves root')
        elif not any(path == allowed_root or path.is_relative_to(allowed_root)
                     for allowed_root in allowed):
            problems.append(f'{var}={path} is outside the allowed test roots')
    subdir = os.environ.get('AUTO_HEROS3_SAVE_SUBDIR')
    if not subdir:
        problems.append('AUTO_HEROS3_SAVE_SUBDIR is not set (must start with agent_n1)')
    elif not re.fullmatch(r'[A-Za-z0-9_-]+', subdir) or not subdir.startswith('agent_n1'):
        problems.append(f'AUTO_HEROS3_SAVE_SUBDIR={subdir!r} must be a plain name starting '
                        'with agent_n1')
    dryrun = os.environ.get('AUTO_HEROS3_LIVE_TEST_DRYRUN') == '1'
    if not dryrun and os.environ.get('AUTO_HEROS3_LIVE_TEST') != '1':
        problems.append('AUTO_HEROS3_LIVE_TEST=1 is required to run the live smoke')
    if problems:
        print('LIVE SMOKE REFUSED: this test starts a real VCMI client.')
        for problem in problems:
            print(f'  - {problem}')
        raise SystemExit(2)
    if dryrun:
        for var, raw in resolved.items():
            print(f'DRYRUN {var}={raw}')
        print(f'DRYRUN SAVE_SUBDIR={subdir}')
        raise SystemExit(0)
    return resolved


_paths = _p6_guard()

sys.path.insert(0, str(ROOT / 'tools'))
import mcp_server_v2 as M  # noqa: E402


def state_now() -> dict:
    try:
        return M._agent().state(timeout=10) or {}
    except Exception:
        return {}


def wait_ready(timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    state: dict = {}
    while time.time() < deadline:
        state = state_now()
        if state.get('in_game') and (state.get('date') or {}).get('day') is not None:
            return state
        time.sleep(1.0)
    return state


def wait_day_after(previous_day, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    state: dict = {}
    while time.time() < deadline:
        state = state_now()
        day = (state.get('date') or {}).get('day')
        if day is not None and day != previous_day:
            return state
        time.sleep(1.0)
    return state


def main() -> int:
    started = False
    health: dict = {}
    try:
        start = M.session_start('Maps/Key to Victory', difficulty='easy', player_color='red',
                                starting_town='castle', starting_hero='random',
                                starting_bonus='random')
        started = bool(start.get('ok'))
        config = start.get('config') or {}
        check('P6L-1 start ok + explicit config recorded', started and config.get('config_match') is True,
              str(start.get('error') or config.get('config_match')))
        M.lease_acquire()
        st = wait_ready()
        day = (st.get('date') or {}).get('day')
        check('P6L-1b in_game with a day', bool(day), f'day={day}')

        status = M.game_status()
        contract = status.get('contract') or {}
        pinned = json.loads((ROOT / 'tools' / 'bridge_contract.json')
                            .read_text(encoding='utf-8'))['client']
        check('P6L-1c live client sha256 matches the newly pinned contract',
              contract.get('match') is True
              and contract.get('client_sha256') == pinned.get('sha256')
              and contract.get('protocol') == 2,
              f"match={contract.get('match')} sha={(contract.get('client_sha256') or '')[:12]} "
              f"pinned={pinned.get('sha256')[:12]} protocol={contract.get('protocol')}")

        heroes = st.get('heroes') or []
        hero = heroes[0] if heroes else {}
        check('P6L-2 raw state carries the new hero fields',
              isinstance(hero.get('mana_max'), int)
              and isinstance(hero.get('spellbook'), bool)
              and isinstance(hero.get('spells'), list)
              and isinstance(hero.get('skills'), list)
              and isinstance(hero.get('artifacts'), dict)
              and isinstance(hero.get('luck'), int)
              and isinstance(hero.get('morale'), int)
              and isinstance(hero.get('specialty'), str),
              str(sorted(hero)))
        arts = hero.get('artifacts') or {}
        worn = arts.get('worn') or []
        spellbook_art = next((a for a in worn if isinstance(a, dict) and a.get('id') == 0), None)
        check('P6L-2a the hero spellbook artifact is listed with a readable name',
              bool(spellbook_art) and bool(str(spellbook_art.get('name') or '').strip()),
              str(spellbook_art))
        skills = hero.get('skills') or []
        check('P6L-2b starting secondary skills are listed with readable names',
              bool(skills) and all(isinstance(s, dict) and s.get('name')
                                   and isinstance(s.get('level'), int) for s in skills),
              str(skills)[:220])
        spells = hero.get('spells') or []
        check('P6L-2c known spells are a structured list (names when any are known)',
              isinstance(spells, list)
              and all(isinstance(sp, dict) and sp.get('name') for sp in spells),
              f'count={len(spells)} first={spells[:2]}')
        army = hero.get('army') or []
        check('P6L-2d army stacks carry a readable creature_name',
              bool(army) and all(bool(str(a.get('creature_name') or '').strip()) for a in army),
              str([(a.get('creature'), a.get('creature_name'), a.get('count')) for a in army]))
        check('P6L-2e morale/luck/specialty/mana_max are sane',
              hero.get('luck') in (-3, -2, -1, 0, 1, 2, 3)
              and hero.get('morale') in (-3, -2, -1, 0, 1, 2, 3)
              and bool(str(hero.get('specialty') or '').strip())
              and isinstance(hero.get('mana_max'), int)
              and hero.get('mana_max') >= hero.get('mana', 0),
              f"luck={hero.get('luck')} morale={hero.get('morale')} "
              f"specialty={hero.get('specialty')} mana={hero.get('mana')}/{hero.get('mana_max')}")

        observed = M.game_observe()
        obs_hero = ((observed.get('state') or {}).get('heroes') or [{}])[0]
        obs_arts = obs_hero.get('artifacts') or {}
        check('P6L-3 game_observe projects the full hero-screen observation',
              observed.get('ok') is True
              and obs_hero.get('mana_max') == hero.get('mana_max')
              and obs_hero.get('skills') == hero.get('skills')
              and obs_hero.get('spells') == hero.get('spells')
              and (obs_arts.get('worn') or []) == worn,
              str({k: obs_hero.get(k) for k in ('mana_max', 'specialty')}))

        brief = M.game_brief()
        brief_hero = (brief.get('heroes') or [{}])[0]
        check('P6L-4 game_brief projects the compact hero view',
              brief.get('ok') is True
              and brief_hero.get('mana_max') == hero.get('mana_max')
              and brief_hero.get('skills') == hero.get('skills')
              and brief_hero.get('spell_count') == len(hero.get('spells') or [])
              and len(brief_hero.get('artifacts') or []) == len(worn)
              + len(arts.get('backpack') or []),
              str({k: brief_hero.get(k) for k in
                   ('mana_max', 'spell_count', 'artifact_count', 'specialty')}))

        end = M.game_act('end_turn', {}, thought='P6 live smoke: one turn to exercise the ledger')
        st2 = wait_day_after(day)
        day2 = (st2.get('date') or {}).get('day')
        check('P6L-5 end_turn accepted and the day advanced', day2 is not None and day2 != day,
              f'{day}->{day2} status={end.get("status")}')
        brief2 = M.game_brief()
        last = brief2.get('last_action') or {}
        check('P6L-5b recovery surface works on the rebuilt bridge ledger',
              last.get('action') == 'end_turn'
              and last.get('status') in ('completed', 'submitted')
              and isinstance(last.get('age_s'), float)
              and (brief2.get('recent_actions') or [])[:1] == [last],
              str(last))

        rec = M.recording_start(fps=15, height=768)
        check('P6L-6 recording started', rec.get('ok') is True, str(rec.get('error')))
        time.sleep(1.4)
        rec_stop = M.recording_stop()
        health = rec_stop.get('health') or {}
        identity = rec_stop.get('identity') or {}
        check('P6L-6b recording identity binds the new exe sha',
              identity.get('client_sha256') == pinned.get('sha256'),
              f"identity_sha={(identity.get('client_sha256') or '')[:12]}")
        check('P6L-6c recording health samples are usable',
              bool(health.get('samples')) and health.get('first_frame_black') is False,
              str({k: health.get(k) for k in
                   ('samples', 'first_frame_black', 'black_ratio', 'longest_black_seconds')}))
    except Exception as exc:  # noqa: BLE001 - a live failure must be reported, not hidden
        check('P6 live smoke completed without an exception', False,
              f'{type(exc).__name__}: {exc}')
    finally:
        try:
            stop = M.session_stop()
            if started:
                check('P6L-7 clean stop (no leftover client)',
                      stop.get('ok') is True and not (stop.get('leftover') or []),
                      str(stop)[:200])
        except Exception as exc:  # noqa: BLE001
            check('P6L-7 clean stop', False, f'{type(exc).__name__}: {exc}')

    failed = [item for item in results if not item[1]]
    print(f'\n{len(results) - len(failed)}/{len(results)} checks passed')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
