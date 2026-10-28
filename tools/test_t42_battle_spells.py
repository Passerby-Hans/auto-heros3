# -*- coding: utf-8 -*-
r"""T42 / F2-P2 offline contract tests: two-stage battle spells (Teleport / Sacrifice).

OFFLINE ONLY - no game client/bridge: drives mcp_server_v2 with the scripted FakeAgent from
test_action_contract (isolated temp state dir via T.Env).

Scope (the C++ side - SpellID-level refusal and battle_actions marking - is covered by
tools/test_t42_live_two_stage.py; this suite covers what the MCP layer guarantees):

  A  bridge `ERR spell_target_unsupported <name> two_stage` -> status=rejected, ok=False,
     exactly ONE bridge command (never retried), evidence/bridge_line keep the refusal,
     no unresolved gate (a deterministic rejection is not "unknown")
  B  battle_actions passthrough: the bridge's two_stage=true / can_cast=false /
     reason=two_stage_unsupported rows reach the tool unchanged; rows for normal spells are
     untouched (no key invented) - the MCP does not re-derive spell legality
  C  normal single-target battle casting is not harmed: a hex spell still verifies completed
     from the mana/cast-spells diff and a normal row has no two_stage key

Run: .venv\Scripts\python.exe -X utf8 tools\test_t42_battle_spells.py
Exit code 0 only when every check passes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))
import mcp_server_v2 as M  # noqa: E402
import test_action_contract as T  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition, detail: str = '') -> None:
    RESULTS.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


def battle_hero(mana: int = 40) -> dict:
    hero = T.make_hero(0, (2, 2))
    hero.update({'id': 7, 'mana': mana, 'mana_max': 60, 'spellbook': True,
                 'spells': [{'id': 15, 'name': 'Haste', 'cost': 3, 'adventure': False,
                             'can_cast': True},
                            {'id': 40, 'name': '牺牲', 'cost': 25, 'adventure': False,
                             'can_cast': True},
                            {'id': 63, 'name': '时空之门', 'cost': 25, 'adventure': False,
                             'can_cast': True}]})
    return hero


def battle_state(hero_mana: int = 40, cast_spells: int = 0, enemy_count: int = 6,
                 active_stack: int = 5) -> dict:
    st = T.state(heroes=[battle_hero()])
    st['battle'] = {'active': active_stack, 'my_side': 0, 'active_stack': active_stack,
                    'round': 1, 'hero_mana': hero_mana, 'hero_mana_max': 60,
                    'hero_spellbook': True, 'cast_spells': cast_spells,
                    'stacks': [T.make_stack(5, 0, 10, (1, 1)),
                               T.make_stack(9, 1, enemy_count, (9, 9))]}
    return st


def two_stage_payload() -> dict:
    """battle_actions.json shape produced by the patched bridge (T42/F2)."""
    return {
        'active': 5, 'mine': True,
        'actor': {'id': 5, 'creature': 0, 'count': 10, 'hex': [1, 1], 'shots': 0,
                  'counterattacks': 1},
        'move_hexes': [[2, 2]], 'targets': [], 'turn_order': [5, 9],
        'hero': {'mana': 40, 'mana_max': 60, 'spellbook': True, 'can_cast': True,
                 'reason': 'ok', 'casts_used': 0},
        'spells': [
            # F2/P2 additions: Teleport (63) and Sacrifice (40) are marked two-stage.
            {'id': 63, 'name': '时空之门', 'cost': 25, 'can_cast': False,
             'reason': 'two_stage_unsupported', 'target_kind': 'hex', 'two_stage': True},
            {'id': 40, 'name': '牺牲', 'cost': 25, 'can_cast': False,
             'reason': 'two_stage_unsupported', 'target_kind': 'unit', 'two_stage': True},
            # A normal single-target spell next to them: untouched, no two_stage key.
            {'id': 15, 'name': '魔法神箭', 'cost': 5, 'can_cast': True,
             'reason': '', 'target_kind': 'unit'},
            {'id': 54, 'name': '迟缓大法', 'cost': 6, 'can_cast': False,
             'reason': 'not_enough_mana', 'target_kind': 'hex'},
        ],
    }


# ------------------------------------------------------------------ A: deterministic refusal
def test_two_stage_refusal() -> None:
    st = battle_state()
    with T.Env() as env:
        fake = env.agent(T.FakeAgent(
            [st, st],
            results=[T.bridge_err(1, 'spell_target_unsupported 时空之门 two_stage')]))
        # Teleport needs a hex target; the MCP precondition only checks battle/turn/target stack.
        res = M.game_act('battle_cast_spell', {'spell': 63, 'x': 3, 'y': 4})
        check('T42-A1 bridge two-stage refusal -> status=rejected / ok=False',
              res.get('status') == 'rejected' and res.get('ok') is False,
              str(res)[:220])
        check('T42-A2 the refused cast was sent exactly once (never retried)',
              fake.sent == ['battle_cast_spell 63 hex 3 4'], str(fake.sent))
        check('T42-A3 evidence and bridge_line keep the spell_target_unsupported refusal',
              'spell_target_unsupported' in str(res.get('evidence'))
              and 'two_stage' in str(res.get('evidence'))
              and 'spell_target_unsupported' in str(res.get('bridge_line')),
              f"evidence={str(res.get('evidence'))[:160]} "
              f"line={str(res.get('bridge_line'))[:160]}")
        check('T42-A4 a deterministic refusal creates no unresolved gate',
              not (env.root / 'state' / 'unresolved.json').exists(),
              'no unresolved.json')

    with T.Env() as env:
        fake = env.agent(T.FakeAgent(
            [st, st],
            results=[T.bridge_err(1, 'spell_target_unsupported 牺牲 two_stage')]))
        res = M.game_act('battle_cast_spell', {'spell': 40, 'stack': 9})
        check('T42-A5 Sacrifice (unit target) keeps the same deterministic rejection',
              res.get('status') == 'rejected'
              and 'spell_target_unsupported' in str(res.get('bridge_line'))
              and fake.sent == ['battle_cast_spell 40 unit 9'], str(fake.sent))

    # Even a no-target-shaped request for the same spell never fabricates a completed cast.
    with T.Env() as env:
        fake = env.agent(T.FakeAgent(
            [st, st], results=[T.bridge_err(1, 'spell_target_unsupported 时空之门 two_stage')]))
        res = M.game_act('battle_cast_spell', {'spell': 63})
        check('T42-A6 a refused two-stage cast stays rejected (never completed/submitted)',
              res.get('status') == 'rejected' and fake.sent == ['battle_cast_spell 63'],
              str(res.get('evidence'))[:160])


# ------------------------------------------------------------------ B: battle_actions passthrough
def test_battle_actions_passthrough() -> None:
    payload = two_stage_payload()
    with T.Env() as env:
        def on_execute(line: str) -> None:
            if line == 'battle_actions':
                path = env.root / 'state' / 'battle_actions.json'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')

        env.agent(T.FakeAgent([battle_state()], results=[T.bridge_ok(1, 'battle_actions')],
                              on_execute=on_execute))
        res = M.battle_actions()
        rows = {int(row['id']): row for row in (res.get('spells') or [])}
        teleport = rows.get(63) or {}
        sacrifice = rows.get(40) or {}
        check('T42-B1 battle_actions returns the bridge JSON (ok=True)',
              res.get('ok') is True and res.get('hero', {}).get('mana') == 40, str(res)[:200])
        check('T42-B2 Teleport row: two_stage=true / can_cast=false / reason=two_stage_unsupported',
              teleport.get('two_stage') is True and teleport.get('can_cast') is False
              and teleport.get('reason') == 'two_stage_unsupported'
              and teleport.get('target_kind') == 'hex', str(teleport))
        check('T42-B3 Sacrifice row: two_stage=true / can_cast=false / reason=two_stage_unsupported',
              sacrifice.get('two_stage') is True and sacrifice.get('can_cast') is False
              and sacrifice.get('reason') == 'two_stage_unsupported'
              and sacrifice.get('target_kind') == 'unit', str(sacrifice))
        normal = rows.get(15) or {}
        other = rows.get(54) or {}
        check('T42-B4 a normal castable spell row is untouched (no two_stage key)',
              normal.get('two_stage') is None and normal.get('can_cast') is True
              and normal.get('reason') == '' and normal.get('target_kind') == 'unit',
              str(normal))
        check('T42-B5 a normal blocked spell keeps its own reason (no two_stage key)',
              other.get('two_stage') is None
              and other.get('reason') == 'not_enough_mana', str(other))


# ------------------------------------------------------------------ C: normal casting not harmed
def test_normal_cast_unaffected() -> None:
    before = battle_state(hero_mana=40, cast_spells=0, enemy_count=6)
    after = battle_state(hero_mana=34, cast_spells=1, enemy_count=6)
    with T.Env() as env:
        fake = env.agent(T.FakeAgent([before, after],
                                     results=[T.bridge_ok(1, 'battle_cast_spell 54 cost=6')]))
        res = M.game_act('battle_cast_spell', {'spell': 54, 'x': 5, 'y': 5})
        check('T42-C1 a normal hex spell still verifies completed from the mana diff',
              res.get('status') == 'completed'
              and 'mana' in str(res.get('evidence')).lower(),
              str(res.get('evidence'))[:200])
        check('T42-C2 the normal cast sent exactly the expected bridge line',
              fake.sent == ['battle_cast_spell 54 hex 5 5'], str(fake.sent))


def main() -> int:
    test_two_stage_refusal()
    test_battle_actions_passthrough()
    test_normal_cast_unaffected()
    failed = [item for item in RESULTS if not item[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
