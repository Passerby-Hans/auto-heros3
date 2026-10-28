# -*- coding: utf-8 -*-
r"""T40 LIVE regression: recruit_hero determinism + anchor/tile coordinates + open_hero.

LIVE/DEBUG - starts ONE real VCMI client in an isolated tree; refused unless every writable path
is redirected (same N0 guard as test_t35_live_magic.py, imported here).  Not part of the offline
regression suite.

Flow (one isolated new game, Key to Victory / easy / castle):
  1  explicit config start + pinned exe match + isolated paths (formal dirs untouched)
  2  anchor/tile: hero + town + town object carry pos/visitable AND anchor/tile with
     anchor==pos, tile==visitable; game_brief passes the compact tile through
  3  move the hero onto the town's interaction tile (town gains a visiting hero)
  4  recruit_hero determinism: the raw bridge answers ERR recruit_hero_refused visiting_hero
     (C8 root cause) and the MCP action reports rejected without changing gold/heroes
  5  move the hero off the town tile, then recruit_hero succeeds: +1 hero and -HERO_GOLD_COST
     (2500) gold observed in state
  6  open_hero: formal MCP action opens the real CHeroWindow (verified through the read-only
     ui_tree dump), the window is LEFT OPEN (demo requirement), then close_window pops it
     (CHeroWindow on the bridge's safe list) and ui_tree no longer shows it
  7  clean stop; the isolated paths are asserted (formal STATE_DIR/formal saves subdir untouched)

Manual run (example; DRYRUN=1 prints paths and exits):
    set AUTO_HEROS3_LIVE_TEST=1
    set AUTO_HEROS3_STATE_DIR=%TEMP%\t40\state
    set AUTO_HEROS3_SAVES_DIR=%USERPROFILE%\Documents\My Games\vcmi\Saves
    set AUTO_HEROS3_SAVE_SUBDIR=agent_n1_t40
    set AUTO_HEROS3_MEMORY_ROOT=%TEMP%\t40\memory
    set AUTO_HEROS3_RECORDINGS_DIR=%TEMP%\t40\recordings
    .venv\Scripts\python.exe -X utf8 tools\test_t40_live_regression.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import test_t35_live_magic as T  # noqa: E402  (imports the N0 live guard first)
import mcp_server_v2 as M  # noqa: E402

results: list[tuple[str, bool, str]] = []
HERO_GOLD_COST = 2500
MAP = 'Maps/Key to Victory'


def check(name: str, ok: bool, detail: str = '') -> None:
    results.append((name, bool(ok), detail))
    print(('PASS' if ok else 'FAIL'), name, detail)


def state_now() -> dict:
    try:
        return M._agent().state(timeout=10) or {}
    except Exception:  # noqa: BLE001 - observation helper
        return {}


def wait_until(predicate, timeout: float = 30.0, interval: float = 0.5):
    deadline = time.time() + timeout
    value = None
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return value


def tile_of(entry: dict):
    """Interaction tile of a hero/town/object entry (canonical tile, then visitable, then pos)."""
    for key in ('tile', 'visitable', 'pos'):
        value = entry.get(key)
        if isinstance(value, list) and len(value) >= 3:
            return list(value[:3])
    return None


def anchor_of(entry: dict):
    for key in ('anchor', 'pos'):
        value = entry.get(key)
        if isinstance(value, list) and len(value) >= 3:
            return list(value[:3])
    return None


def hero0(st: dict) -> dict:
    heroes = st.get('heroes') or []
    return heroes[0] if heroes else {}


def town0(st: dict) -> dict:
    towns = st.get('towns') or []
    return towns[0] if towns else {}


def town_object(st: dict, town: dict):
    for obj in st.get('objects') or []:
        if not isinstance(obj, dict) or obj.get('kind') != 'town':
            continue
        if town.get('id') is not None and obj.get('id') == town.get('id'):
            return obj
    return None


def hero_visiting(st: dict, hero_tile, town_tile) -> bool:
    town = town0(st)
    visiting = town.get('hero_visiting')
    if isinstance(visiting, bool):
        return visiting
    return bool(hero_tile and town_tile and list(hero_tile) == list(town_tile))


def clear_queries(max_rounds: int = 4) -> list:
    """Answer/close any server query the previous action opened (keeps the action gates happy)."""
    handled: list = []
    for _ in range(max_rounds):
        st = state_now()
        query = st.get('query')
        if not query:
            return handled
        kind = query.get('type') if isinstance(query, dict) else str(query)
        handled.append(kind)
        if kind in ('yes_no', 'select', 'level_up', 'garrison', 'teleport'):
            M.game_act('answer', {'value': 1}, thought='T40 live: answer the pending query')
        else:
            M.game_act('close_window', {}, thought='T40 live: dismiss the pending dialog')
        time.sleep(0.8)
    return handled


def move_hero_to(target, thought: str) -> dict:
    target = [int(target[0]), int(target[1]), int(target[2])]
    res = M.game_act('move', {'hero': 0, 'x': target[0], 'y': target[1], 'z': target[2]},
                     thought=thought)
    arrived = wait_until(lambda: tile_of(hero0(state_now())) == target, timeout=25.0)
    return {'result': res, 'arrived': bool(arrived), 'target': target}


def path_ok(target) -> dict:
    try:
        return M.path_preview(hero=0, x=int(target[0]), y=int(target[1]), z=int(target[2])) or {}
    except Exception as exc:  # noqa: BLE001
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}


def step_off_candidates(town_tile):
    x, y, z = int(town_tile[0]), int(town_tile[1]), int(town_tile[2])
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
        yield [x + dx, y + dy, z]


def main() -> int:
    started = False
    try:
        pinned = json.loads((ROOT / 'tools' / 'bridge_contract.json')
                            .read_text(encoding='utf-8'))['client']
        contract = M._agent().contract_status()
        check('T40L-1a isolated paths are in effect (formal STATE_DIR not used)',
              Path(M.STATE_DIR).resolve() != (ROOT / 'var' / 'agent_mcp_v2').resolve()
              and M.SAVE_SUBDIR.startswith('agent_n1'), f'STATE_DIR={M.STATE_DIR} '
              f'SAVE_SUBDIR={M.SAVE_SUBDIR}')
        check('T40L-1b the rebuilt client matches the pinned contract (protocol 2)',
              contract.get('match') is True and contract.get('pinned_protocol') == 2
              and contract.get('actual_sha256') == pinned.get('sha256'),
              str(contract)[:240])

        start = M.session_start(MAP, difficulty='easy', player_color='red',
                                starting_town='castle', starting_hero='random',
                                starting_bonus='random')
        started = bool(start.get('ok'))
        check('T40L-2 explicit config start', started,
              str(start.get('error') or (start.get('config') or {}).get('config_match')))
        M.lease_acquire()
        st = T.wait_ready()
        day = (st.get('date') or {}).get('day')
        hero = hero0(st)
        town = town0(st)
        check('T40L-3 new game with a hero and a town',
              bool(day) and bool(hero) and bool(town),
              f'day={day} heroes={len(st.get("heroes") or [])} towns={len(st.get("towns") or [])}')

        # ---- 1) anchor/tile coordinate contract ------------------------------------------
        hero_tile = tile_of(hero)
        hero_anchor = anchor_of(hero)
        check('T40L-4a hero JSON carries anchor and tile next to pos/visitable',
              isinstance(hero.get('anchor'), list) and isinstance(hero.get('tile'), list)
              and isinstance(hero.get('pos'), list) and isinstance(hero.get('visitable'), list),
              str({k: hero.get(k) for k in ('pos', 'visitable', 'anchor', 'tile')}))
        check('T40L-4b anchor==pos and tile==visitable on the hero',
              hero_anchor == list(hero.get('pos') or [])[:3]
              and hero_tile == list(hero.get('visitable') or [])[:3]
              and hero_tile is not None,
              f'anchor={hero_anchor} pos={hero.get("pos")} tile={hero_tile} '
              f'visitable={hero.get("visitable")}')
        town_tile = tile_of(town)
        town_anchor = anchor_of(town)
        check('T40L-4c town JSON carries anchor and tile next to pos',
              isinstance(town.get('anchor'), list) and isinstance(town.get('tile'), list)
              and town_anchor == list(town.get('pos') or [])[:3] and town_tile is not None,
              str({k: town.get(k) for k in ('pos', 'anchor', 'tile')}))
        town_obj = town_object(st, town)
        check('T40L-4d the town object carries anchor/tile and matches the town tile',
              town_obj is not None
              and anchor_of(town_obj) == list(town_obj.get('pos') or [])[:3]
              and tile_of(town_obj) == list(town_obj.get('visitable') or [])[:3]
              and tile_of(town_obj) == town_tile,
              str({k: (town_obj or {}).get(k) for k in ('pos', 'visitable', 'anchor', 'tile')}))
        brief = M.game_brief()
        brief_hero = (brief.get('heroes') or [{}])[0]
        brief_town = (brief.get('towns') or [{}])[0]
        check('T40L-4e game_brief passes the compact tile through',
              brief_hero.get('tile') == hero_tile and brief_town.get('tile') == town_tile,
              f'hero_tile={brief_hero.get("tile")} town_tile={brief_town.get("tile")}')

        # ---- 2) gold for the hire (test-only debug channel; isolated run) ------------------
        before_res = (st.get('resources') or {})
        cheat = M.debug_resources({'gold': 12000})
        check('T40L-5 debug gold available for the hire (test-only channel)',
              cheat.get('ok') is True or int((state_now().get('resources') or {}).get('gold') or 0)
              >= HERO_GOLD_COST,
              str(cheat.get('error') or cheat.get('resources'))[:200])
        st = state_now()

        # ---- 3) make the town have a visiting hero: move the hero onto the town tile ------
        hero_tile = tile_of(hero0(st))
        town_tile = tile_of(town0(st)) or town_tile
        original_tile = hero_tile
        already_visiting = hero_tile == town_tile
        moved_on = {'arrived': already_visiting, 'result': None, 'target': town_tile}
        if not already_visiting:
            preview = path_ok(town_tile)
            for _ in range(3):
                if preview.get('reachable') and preview.get('turns') in (0, None):
                    break
                M.game_act('end_turn', {}, thought='T40 live: wait a turn to reach the town tile')
                time.sleep(1.0)
                T.wait_ready()
                preview = path_ok(town_tile)
            check('T40L-6a the town interaction tile is reachable from the hero',
                  bool(preview.get('reachable')), str(preview)[:200])
            moved_on = move_hero_to(town_tile, 'T40 live: move onto the town tile to become the '
                                               'visiting hero (reproduce the C8 refusal case)')
            check('T40L-6b hero stands on the town interaction tile',
                  moved_on['arrived'],
                  f"target={town_tile} tile={tile_of(hero0(state_now()))} "
                  f"status={(moved_on['result'] or {}).get('status')}")
        clear_queries()
        visiting = wait_until(
            lambda: hero_tile == town_tile or hero_visiting(state_now(), tile_of(hero0(state_now())),
                                                            tile_of(town0(state_now()))),
            timeout=30.0)
        st = state_now()
        check('T40L-6c the town reports a visiting hero',
              bool(visiting) or hero_visiting(st, tile_of(hero0(st)), tile_of(town0(st))),
              f"hero_tile={tile_of(hero0(st))} town_tile={tile_of(town0(st))} "
              f"hero_visiting={town0(st).get('hero_visiting')}")

        # ---- 4) recruit_hero must be a deterministic refusal while a hero visits ----------
        gold_before = int((st.get('resources') or {}).get('gold') or 0)
        heroes_before = len(st.get('heroes') or [])
        raw = None
        try:
            raw = M._agent().execute('recruit_hero 0 0', verify=None, result_timeout=15)
        except Exception as exc:  # noqa: BLE001
            raw = None
            check('T40L-7a raw bridge command executed', False, f'{type(exc).__name__}: {exc}')
        raw_line = str(getattr(raw, 'line', '') or '')
        raw_status = str(getattr(raw, 'status', '') or '')
        check('T40L-7a raw bridge refuses with ERR recruit_hero_refused visiting_hero',
              'ERR recruit_hero_refused visiting_hero' in raw_line
              and raw_status.upper() == 'REJECTED',
              f'status={raw_status} line={raw_line[:160]}')
        refused = M.game_act('recruit_hero', {'town': 0, 'index': 0},
                             thought='T40 live: the town has a visiting hero - the hire must be '
                                     'refused deterministically, not answered OK')
        check('T40L-7b MCP recruit_hero reports rejected (no false OK)',
              refused.get('status') == 'rejected',
              f"status={refused.get('status')} sent={refused.get('sent')} "
              f"evidence={str(refused.get('evidence'))[:180]}")
        st = state_now()
        check('T40L-7c the refused hire changed neither gold nor the hero count',
              int((st.get('resources') or {}).get('gold') or 0) == gold_before
              and len(st.get('heroes') or []) == heroes_before,
              f"gold {gold_before}->{(st.get('resources') or {}).get('gold')} "
              f"heroes {heroes_before}->{len(st.get('heroes') or [])}")

        # ---- 5) move the hero off the town tile, then the hire succeeds -------------------
        town_tile = tile_of(town0(st)) or town_tile
        target = None
        if original_tile and original_tile != town_tile:
            target = original_tile                    # the tile the hero came from: known walkable
        else:
            for candidate in step_off_candidates(town_tile):
                preview = path_ok(candidate)
                if preview.get('reachable') and preview.get('turns') in (0, None):
                    target = candidate
                    break
        check('T40L-8a found a walkable tile next to the town to step off to', target is not None,
              f'target={target} town_tile={town_tile}')
        moved_off = move_hero_to(target, 'T40 live: step off the town tile so the tavern hire is '
                                        'allowed again') if target else {'arrived': False}
        clear_queries()
        stepped_off = wait_until(
            lambda: tile_of(hero0(state_now())) != town_tile
            and not hero_visiting(state_now(), tile_of(hero0(state_now())),
                                  tile_of(town0(state_now()))),
            timeout=30.0)
        st = state_now()
        check('T40L-8b hero left the town tile and the visiting flag cleared',
              bool(stepped_off),
              f"arrived={moved_off.get('arrived')} hero_tile={tile_of(hero0(st))} "
              f"town_tile={tile_of(town0(st))} hero_visiting={town0(st).get('hero_visiting')}")

        # The MCP precondition reads its own fresh before-state: wait a moment for the 2 s town
        # cache so the bridge and the MCP see the same "no visiting hero".
        time.sleep(2.2)
        clear_queries()
        st = state_now()
        gold_before = int((st.get('resources') or {}).get('gold') or 0)
        heroes_before = len(st.get('heroes') or [])
        recruit = M.game_act('recruit_hero', {'town': 0, 'index': 0},
                             thought='T40 live: no visiting hero now - hire the tavern hero')
        hire_landed = wait_until(
            lambda: len((state_now().get('heroes') or [])) > heroes_before
            or int((state_now().get('resources') or {}).get('gold') or gold_before) < gold_before,
            timeout=25.0)
        st = state_now()
        gold_after = int((st.get('resources') or {}).get('gold') or 0)
        heroes_after = len(st.get('heroes') or [])
        check('T40L-9a recruit_hero accepted once the visiting hero is gone',
              recruit.get('status') in ('completed', 'submitted'),
              f"status={recruit.get('status')} evidence={str(recruit.get('evidence'))[:180]}")
        check('T40L-9b a new hero appeared and the hire cost was paid',
              bool(hire_landed) and heroes_after == heroes_before + 1
              and gold_before - gold_after >= HERO_GOLD_COST,
              f'heroes {heroes_before}->{heroes_after} gold {gold_before}->{gold_after} '
              f'(expected -{HERO_GOLD_COST})')
        check('T40L-9c the bridge OK line reports cost/gold_before',
              'cost=2500' in str(recruit.get('bridge_line') or ''),
              str(recruit.get('bridge_line'))[:180])

        # ---- 6) open_hero: real CHeroWindow through the formal MCP action ----------------
        time.sleep(1.0)
        clear_queries()
        opened = M.game_act('open_hero', {'hero': 0},
                            thought='T40 live: open the real hero screen for the demo')
        check('T40L-10a open_hero verified CHeroWindow -> completed',
              opened.get('status') == 'completed'
              and 'CHeroWindow' in str(opened.get('evidence')),
              f"status={opened.get('status')} evidence={str(opened.get('evidence'))[:200]}")
        tree = M.ui_tree()
        window_classes = [str(w.get('cls') or '') for w in tree.get('windows') or []]
        check('T40L-10b ui_tree shows the open CHeroWindow',
              tree.get('ok') is True and any('CHeroWindow' in cls for cls in window_classes),
              str(window_classes)[:220])
        tree2 = M.ui_tree()
        window_classes2 = [str(w.get('cls') or '') for w in tree2.get('windows') or []]
        check('T40L-10c the verifier leaves the hero window open (demo requirement)',
              any('CHeroWindow' in cls for cls in window_classes2), str(window_classes2)[:220])
        try:
            shot_path = Path(M.STATE_DIR) / 'open_hero.png'
            shot = M.game_screenshot(path=str(shot_path))
            shot_bytes = shot_path.stat().st_size if shot_path.exists() else 0
            # The MCP returns image content when the SDK supports it, otherwise a dict.
            shot_ok = shot.get('ok') is True if isinstance(shot, dict) else shot is not None
            check('T40L-10d screenshot evidence of the open hero screen',
                  shot_ok and shot_bytes > 1000,
                  f'bytes={shot_bytes} result={type(shot).__name__}')
        except Exception as exc:  # noqa: BLE001
            check('T40L-10d screenshot evidence of the open hero screen', False,
                  f'{type(exc).__name__}: {exc}')

        closed = M.game_act('close_window', {},
                            thought='T40 live: close the hero screen (CHeroWindow safe list)')
        time.sleep(0.8)
        tree3 = M.ui_tree()
        window_classes3 = [str(w.get('cls') or '') for w in tree3.get('windows') or []]
        check('T40L-11a close_window pops CHeroWindow',
              'OK close_window_safe' in str(closed.get('bridge_line') or '')
              or closed.get('status') in ('completed', 'submitted'),
              f"status={closed.get('status')} line={str(closed.get('bridge_line'))[:160]}")
        check('T40L-11b ui_tree no longer shows a CHeroWindow',
              not any('CHeroWindow' in cls for cls in window_classes3),
              str(window_classes3)[:220])
    except Exception as exc:  # noqa: BLE001 - live failures must be reported, not hidden
        check('T40 live regression completed without an exception', False,
              f'{type(exc).__name__}: {exc}')
    finally:
        try:
            stop = M.session_stop()
            if started:
                check('T40L-12 clean stop (no leftover client)',
                      stop.get('ok') is True and not (stop.get('leftover') or []),
                      str(stop)[:240])
        except Exception as exc:  # noqa: BLE001
            check('T40L-12 clean stop', False, f'{type(exc).__name__}: {exc}')

    failed = [item for item in results if not item[1]]
    print(f'\n{len(results) - len(failed)}/{len(results)} checks passed')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
