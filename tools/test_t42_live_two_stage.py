# -*- coding: utf-8 -*-
r"""T42 LIVE regression: two-stage battle spells (Teleport / Sacrifice) are refused explicitly.

LIVE/DEBUG - starts ONE real VCMI client in an isolated tree; refused unless every writable path
is redirected (N0 guard imported from test_t35_live_magic, which runs BEFORE mcp_server_v2 is
imported).  Not part of the offline regression suite.

Flow (one isolated new game, Key to Victory / easy / red / castle):
  1  explicit config start; the engine reports the freshly rebuilt T42 exe sha (the pinned
     contract is deliberately NOT updated in this batch - mismatch is expected and reported)
  2  debug channel (test-only, env-gated): learn every spell + set mana
  3  walk to the nearest visible neutral stack and enter a battle
  4  battle_actions: Teleport (id 63) / Sacrifice (id 40) rows carry can_cast=false,
     reason=two_stage_unsupported and the additive two_stage=true; no other row gets the flag
  5  battle_cast_spell on both -> status=rejected, bridge line contains
     `spell_target_unsupported <name> two_stage`; hero mana / cast counters unchanged
  6  control: a normal single-target spell still casts successfully (or its can_cast semantics
     stay correct), i.e. the refusal is not over-broad
  7  clean stop; no leftover client

Manual run (example; DRYRUN=1 prints the paths and exits):
    set AUTO_HEROS3_LIVE_TEST=1
    set AUTO_HEROS3_STATE_DIR=%TEMP%\t42\state
    set AUTO_HEROS3_SAVES_DIR=%USERPROFILE%\Documents\My Games\vcmi\Saves
    set AUTO_HEROS3_SAVE_SUBDIR=agent_n1_t42
    set AUTO_HEROS3_MEMORY_ROOT=%TEMP%\t42\memory
    set AUTO_HEROS3_RECORDINGS_DIR=%TEMP%\t42\recordings
    .venv\Scripts\python.exe -X utf8 tools\test_t42_live_two_stage.py
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import test_t35_live_magic as T  # noqa: E402  (N0 live guard runs at import)
import mcp_server_v2 as M  # noqa: E402

results: list[tuple[str, bool, str]] = []
MAP = 'Maps/Key to Victory'
EXE = ROOT / 'vendor' / 'vcmi-src' / 'build' / 'bin' / 'VCMI_client.exe'
TELEPORT_ID = 63      # SpellID::TELEPORT (lib/constants/EntityIdentifiers.h)
SACRIFICE_ID = 40     # SpellID::SACRIFICE


def check(name: str, ok: bool, detail: str = '') -> None:
    results.append((name, bool(ok), detail))
    print(('PASS' if ok else 'FAIL'), name, detail)


def state_now() -> dict:
    return T.state_now()


def spells_by_id(rows: list) -> dict:
    out = {}
    for row in rows or []:
        if not isinstance(row, dict) or row.get('id') is None:
            continue
        try:
            out[int(row['id'])] = row
        except (TypeError, ValueError):
            continue
    return out


def enter_battle() -> tuple[bool, dict]:
    """Walk to the nearest visible neutral stack and enter the battle (T35 flow)."""
    st = state_now()
    hero = T.hero_of(st)
    creatures = T.visible_creatures(st, hero)
    check('T42L-3a at least one visible neutral stack to fight', bool(creatures),
          f'visible={len(creatures)}')
    if not creatures:
        return False, st
    creatures.sort(key=lambda item: (T.hover_rank(item[1]), item[0]))
    target_obj = creatures[0][1]
    target_pos = list(target_obj.get('visitable') or target_obj.get('pos') or [0, 0, 0])
    move = M.move_near(0, target_pos[0], target_pos[1], target_pos[2], radius=1)
    deadline = time.time() + 30.0
    while time.time() < deadline:
        st = state_now()
        battle = st.get('battle') or {}
        if battle.get('active'):
            break
        query = st.get('query')
        if query:
            M.game_act('answer', {'value': 1},
                       thought='T42 live: answer the guard prompt to start the test battle')
            time.sleep(1.0)
            continue
        if move.get('status') == 'rejected' and 'no_path' in str(move.get('evidence')):
            break
        time.sleep(0.6)
    st = state_now()
    battle = st.get('battle') or {}
    if battle.get('active') and battle.get('active_stack') == -1:
        M.battle_start()
        time.sleep(1.0)
        st = state_now()
        battle = st.get('battle') or {}
    check('T42L-3b battle reached via move/guard answer',
          bool(battle.get('active')),
          f"move={move.get('status')} active={battle.get('active')} "
          f"evidence={str(move.get('evidence'))[:120]}")
    return bool(battle.get('active')), st


def cast_control(rows: list, battle: dict) -> dict:
    """Cast one normal (non-two-stage) spell; returns the accepted/failed attempt details."""
    stacks = [s for s in battle.get('stacks') or [] if isinstance(s, dict)]
    my_side = battle.get('my_side')
    own = [s for s in stacks if s.get('side') == my_side]
    enemy = [s for s in stacks if s.get('side') != my_side]
    candidates = [row for row in rows
                  if row.get('can_cast') is True and not row.get('two_stage')]
    attempts: list = []
    for row in candidates[:6]:
        kind = row.get('target_kind')
        args = {'spell': int(row['id'])}
        if kind == 'unit':
            target = (own or enemy or [{}])[0].get('id')
            if target is None:
                continue
            args['stack'] = int(target)
        elif kind == 'hex':
            hexes = [(s.get('hex') or [None, None]) for s in (enemy or own)]
            hexes = [h for h in hexes if isinstance(h, list) and len(h) == 2
                     and isinstance(h[0], int) and isinstance(h[1], int)]
            if not hexes:
                continue
            args['x'], args['y'] = hexes[0]
        res = M.game_act('battle_cast_spell', args,
                         thought=f'T42 live: control cast of a normal spell '
                                 f'({row.get("name")})')
        attempts.append({'spell': row.get('name'), 'id': row.get('id'), 'args': args,
                         'status': res.get('status'),
                         'line': str(res.get('bridge_line') or '')[:160]})
        if res.get('status') in ('completed', 'submitted'):
            return {'ok': True, 'row': row, 'result': res, 'attempts': attempts}
        if 'OK battle_cast_spell' in str(res.get('bridge_line') or ''):
            return {'ok': True, 'row': row, 'result': res, 'attempts': attempts}
    return {'ok': False, 'attempts': attempts}


def main() -> int:
    started = False
    try:
        if not EXE.exists():
            check('T42L-1 the rebuilt client exe exists', False, str(EXE))
            return 1
        local_sha = hashlib.sha256(EXE.read_bytes()).hexdigest()
        start = M.session_start(MAP, difficulty='easy', player_color='red',
                                starting_town='castle', starting_hero='random',
                                starting_bonus='random')
        started = bool(start.get('ok'))
        check('T42L-1 explicit config start', started,
              str(start.get('error') or (start.get('config') or {}).get('config_match')))
        M.lease_acquire()
        engine = ((start.get('config') or {}).get('engine') or {})
        contract = M._agent().contract_status()
        # The batch deliberately does NOT update tools/bridge_contract.json (Lead pins it), so the
        # contract hash mismatch and exe_sha256_match=False are the EXPECTED state here; what must
        # hold is that the running engine is exactly the freshly built T42 binary.
        check('T42L-1b the live engine runs the freshly rebuilt T42 exe '
              '(contract pin intentionally stale until Lead pins)',
              engine.get('exe_sha256') == local_sha and engine.get('protocol') == 2,
              f"local={local_sha[:12]} engine={str(engine.get('exe_sha256'))[:12]} "
              f"exe_sha256_match={engine.get('exe_sha256_match')} "
              f"contract_match={contract.get('match')} (expected False: not pinned yet)")

        st = T.wait_ready()
        before_spells = len(T.spells_of(st))
        T.write_cheat(['allspells all', 'mana all 120'])
        learned = T.wait_for(lambda: len(T.spells_of(state_now())) > before_spells,
                             timeout=25.0)
        st = state_now()
        spell_ids = {int(sp.get('id')) for sp in T.spells_of(st)
                     if isinstance(sp, dict) and sp.get('id') is not None}
        check('T42L-2 debug channel learned the full spellbook (test-only, env-gated)',
              learned and len(spell_ids) > before_spells, f'{before_spells}->{len(spell_ids)}')
        check('T42L-2b Teleport/Sacrifice are in the isolated hero spellbook',
              TELEPORT_ID in spell_ids and SACRIFICE_ID in spell_ids,
              f'teleport={TELEPORT_ID in spell_ids} sacrifice={SACRIFICE_ID in spell_ids} '
              f'(ids={sorted(spell_ids)[:12]}...)')
        hero = T.hero_of(st)
        check('T42L-2c debug channel raised mana for the casts',
              int(hero.get('mana') or 0) >= 40, f"mana={hero.get('mana')}/{hero.get('mana_max')}")

        battle_active, st = enter_battle()
        if not battle_active:
            # Minimal fallback per task: if no battle can be created, the bridge refusal path is
            # still exercised at the command level (no_battle is returned before the two-stage
            # check, so record the honest limitation instead of fabricating a PASS).
            raw = M._agent().execute(f'battle_cast_spell {TELEPORT_ID}', verify=None,
                                     result_timeout=10)
            check('T42L-3c fallback: no battle could be entered; raw bridge answer recorded',
                  'no_battle' in str(getattr(raw, 'line', '')),
                  f"line={str(getattr(raw, 'line', ''))[:160]}")
            check('T42L-4 two-stage battle marking verified', False,
                  'no battle: battle_actions/refusal checks not observable')
            check('T42L-5 two-stage spell refusal verified', False, 'no battle')
            check('T42L-6 control single-target cast', False, 'no battle')
        else:
            # ---- 4) battle_actions marking ------------------------------------------------
            actions = M.battle_actions()
            rows = [row for row in (actions.get('spells') or []) if isinstance(row, dict)]
            by_id = spells_by_id(rows)
            teleport = by_id.get(TELEPORT_ID)
            sacrifice = by_id.get(SACRIFICE_ID)
            hero_block = actions.get('hero') or {}
            check('T42L-4a battle_actions lists Teleport and Sacrifice for the hero',
                  teleport is not None and sacrifice is not None,
                  f'teleport={teleport is not None} sacrifice={sacrifice is not None} '
                  f'rows={len(rows)}')
            check('T42L-4b Teleport row: two_stage=true / can_cast=false / '
                  'reason=two_stage_unsupported',
                  bool(teleport) and teleport.get('two_stage') is True
                  and teleport.get('can_cast') is False
                  and teleport.get('reason') == 'two_stage_unsupported',
                  str(teleport))
            check('T42L-4c Sacrifice row: two_stage=true / can_cast=false / '
                  'reason=two_stage_unsupported',
                  bool(sacrifice) and sacrifice.get('two_stage') is True
                  and sacrifice.get('can_cast') is False
                  and sacrifice.get('reason') == 'two_stage_unsupported',
                  str(sacrifice))
            others = [row for row in rows if row.get('id') not in (TELEPORT_ID, SACRIFICE_ID)]
            check('T42L-4d no other spell is marked two_stage (no over-marking)',
                  all('two_stage' not in row for row in others),
                  str([row.get('name') for row in others if row.get('two_stage') is True]))
            check('T42L-4e the normal single-target spells keep their own legality fields',
                  bool(others) and all(('can_cast' in row and 'reason' in row
                                        and 'target_kind' in row) for row in others),
                  str(others[:2]))

            # ---- 5) deterministic two-stage refusal ---------------------------------------
            T.wait_for_my_turn(deadline=30.0)
            st = state_now()
            battle_before = st.get('battle') or {}
            mana_before = battle_before.get('hero_mana')
            casts_before = battle_before.get('cast_spells')
            tp_res = M.game_act('battle_cast_spell', {'spell': TELEPORT_ID, 'x': 3, 'y': 4},
                                thought='T42 live: Teleport is two-stage - the bridge must refuse')
            tp_line = str(tp_res.get('bridge_line') or '')
            check('T42L-5a Teleport cast -> rejected with spell_target_unsupported ... two_stage',
                  tp_res.get('status') == 'rejected' and tp_res.get('ok') is False
                  and 'spell_target_unsupported' in tp_line and 'two_stage' in tp_line,
                  f"status={tp_res.get('status')} line={tp_line[:180]}")
            st = state_now()
            battle_now = st.get('battle') or {}
            stacks = [s for s in battle_now.get('stacks') or [] if isinstance(s, dict)]
            own = [s for s in stacks if s.get('side') == battle_now.get('my_side')]
            target_stack = (own or stacks or [{}])[0].get('id')
            sac_args = {'spell': SACRIFICE_ID}
            if target_stack is not None:
                sac_args['stack'] = int(target_stack)
            sac_res = M.game_act('battle_cast_spell', sac_args,
                                 thought='T42 live: Sacrifice is two-stage - the bridge must refuse')
            sac_line = str(sac_res.get('bridge_line') or '')
            check('T42L-5b Sacrifice cast -> rejected with spell_target_unsupported ... two_stage',
                  sac_res.get('status') == 'rejected' and sac_res.get('ok') is False
                  and 'spell_target_unsupported' in sac_line and 'two_stage' in sac_line,
                  f"status={sac_res.get('status')} line={sac_line[:180]}")
            st = state_now()
            battle_after = st.get('battle') or {}
            mana_after = battle_after.get('hero_mana')
            casts_after = battle_after.get('cast_spells')
            check('T42L-5c both refused casts left hero mana and cast counters unchanged',
                  mana_after == mana_before and casts_after == casts_before,
                  f'mana {mana_before}->{mana_after} casts {casts_before}->{casts_after}')

            # ---- 6) control: normal single-target casting is not harmed -------------------
            control = cast_control(rows, battle_after)
            if control.get('ok'):
                deadline = time.time() + 10.0
                mana_moved = casts_moved = False
                while time.time() < deadline:
                    battle_ctl = (state_now().get('battle') or {})
                    try:
                        if (int(battle_ctl.get('hero_mana') or 0) < int(mana_after or 0)
                                or int(battle_ctl.get('cast_spells') or 0)
                                > int(casts_after or 0)):
                            mana_moved = casts_moved = True
                            break
                    except (TypeError, ValueError):
                        pass
                    time.sleep(0.4)
                check('T42L-6a a normal single-target spell still casts successfully '
                      '(refusal is not over-broad)',
                      control['ok'] and (mana_moved or control['result'].get('status')
                                         == 'completed'),
                      f"spell={control['row'].get('name')} "
                      f"status={control['result'].get('status')} "
                      f"mana_moved={mana_moved} attempts={control['attempts']}")
            else:
                # Fallback allowed by the task: the legality semantics stay correct even if the
                # battle simply has no castable single-target spell this turn.
                normal_rows = [row for row in rows if not row.get('two_stage')]
                check('T42L-6a no normal spell could be cast; its can_cast semantics stay correct',
                      bool(normal_rows)
                      and all(row.get('reason') != 'two_stage_unsupported'
                              for row in normal_rows),
                      str(control.get('attempts')))
            casts_now = int((state_now().get('battle') or {}).get('cast_spells') or 0)
            if control.get('ok'):
                check('T42L-6b only the control cast advanced cast_spells '
                      '(the two-stage refusals never counted as casts)',
                      casts_now == int(casts_before or 0) + 1,
                      f'cast_spells {casts_before}->{casts_now}')
            else:
                check('T42L-6b cast_spells stayed unchanged (no successful cast this turn)',
                      casts_now == int(casts_before or 0),
                      f'cast_spells {casts_before}->{casts_now}')
    except Exception as exc:  # noqa: BLE001 - live failures must be reported, not hidden
        check('T42 live regression completed without an exception', False,
              f'{type(exc).__name__}: {exc}')
    finally:
        try:
            stop = M.session_stop()
            if started:
                check('T42L-7 clean stop (no leftover client)',
                      stop.get('ok') is True and not (stop.get('leftover') or []),
                      str(stop)[:240])
        except Exception as exc:  # noqa: BLE001
            check('T42L-7 clean stop', False, f'{type(exc).__name__}: {exc}')

    failed = [item for item in results if not item[1]]
    print(f'\n{len(results) - len(failed)}/{len(results)} checks passed')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
