# -*- coding: utf-8 -*-
"""Offline regression for N2: observation passthrough, transfer/merge verification, battle_assess.

No game/client/network: Env/FakeAgent from test_action_contract patch the module state/agent.

Covered:
  A  _digest / game_brief pass through existing hero fields (id/level/exp/mana/primary) and battle
     stack facts (shots/counterattacks/defending/waited/double_wide); older states stay compatible
  B  transfer (slot swap / single-stack move) and merge (same-creature) completion rules:
     equal swap, different-creature swap, move into empty slot, same-creature merge, wrong slot,
     unrelated count change, non-conservation
  C  battle_assess is a post-contact counts summary, not a pre-battle risk assessment;
     the disclaimer is present and no permission-style field is returned

Run: python -X utf8 tools/test_n2_contract.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))
import mcp_server_v2 as M  # noqa: E402
from test_action_contract import (Env, FakeAgent, bridge_ok, make_hero, make_stack,  # noqa: E402
                                  make_town, state)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition, detail: str = '') -> None:
    RESULTS.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


def slot(number: int, creature, count: int) -> dict:
    return {'slot': number, 'creature': creature, 'count': count}


def containers(hero_army: list, town_garrison: list) -> dict:
    return state(heroes=[make_hero(0, (1, 1), hero_army)],
                 towns=[make_town(0, garrison=town_garrison)])


TRANSFER_ARGS = {'src': 'town', 'src_index': 0, 'src_slot': 0,
                 'dst': 'hero', 'dst_index': 0, 'dst_slot': 0}


def act_transfer(before: dict, after: dict, args: dict | None = None) -> dict:
    with Env() as env:
        env.agent(FakeAgent([before, after], [bridge_ok(1)]))
        return M.game_act('transfer', dict(args or TRANSFER_ARGS))


def act_merge(before: dict, after: dict) -> dict:
    args = {'src': 'town', 'src_index': 0, 'src_slot': 0,
            'dst': 'hero', 'dst_index': 0, 'dst_slot': 0}
    with Env() as env:
        env.agent(FakeAgent([before, after], [bridge_ok(1)]))
        return M.game_act('merge', args)


# ------------------------------------------------------------------ A: observation passthrough
def test_digest_passthrough() -> None:
    rich_hero = make_hero(0, (2, 3), [{'slot': 0, 'creature': 1, 'count': 7}])
    rich_hero.update({'id': 42, 'level': 6, 'exp': 1234, 'mana': 33,
                      'primary': {'attack': 2, 'defence': 3, 'spellpower': 4,
                                  'knowledge': 5}})
    battle = {'active': 2, 'my_side': 0, 'active_stack': 2,
              'stacks': [dict(make_stack(1, 0, 10, (1, 1)),
                              shots={'available': 5, 'total': 5, 'limited': True},
                              counterattacks={'available': 1, 'total': 1},
                              defending=False, waited=True, double_wide=True)]}
    rich = state(heroes=[rich_hero], battle=battle, towns=[make_town(0)])
    digest = M._digest(rich)
    hero = (digest.get('heroes') or [{}])[0]
    stack = ((digest.get('battle') or {}).get('stacks') or [{}])[0]
    check('A1 digest passes hero id/level/exp/mana/primary',
          hero.get('id') == 42 and hero.get('level') == 6 and hero.get('exp') == 1234
          and hero.get('mana') == 33
          and hero.get('primary') == {'attack': 2, 'defence': 3, 'spellpower': 4,
                                      'knowledge': 5}, str(hero))
    check('A1b digest keeps the old hero fields',
          hero.get('i') == 0 and hero.get('name') == 'Hero0' and hero.get('movement') == 1000
          and hero.get('army') == [{'slot': 0, 'creature': 1, 'count': 7}], str(hero))
    check('A1c digest passes battle shots/counterattacks/defending/waited/double_wide',
          stack.get('shots') == {'available': 5, 'total': 5, 'limited': True}
          and stack.get('counterattacks') == {'available': 1, 'total': 1}
          and stack.get('defending') is False and stack.get('waited') is True
          and stack.get('double_wide') is True, str(stack))
    old = state(heroes=[make_hero(1, (4, 4))])
    old_digest = M._digest(old)
    old_hero = (old_digest.get('heroes') or [{}])[0]
    check('A1d an old/partial state keeps the previous digest shape (no KeyError)',
          'level' not in old_hero and 'primary' not in old_hero and old_hero.get('i') == 1,
          str(old_hero))

    with Env() as env:
        env.agent(FakeAgent([rich]))
        brief = M.game_brief()
        brief_hero = (brief.get('heroes') or [{}])[0]
        check('A2 game_brief exposes the same hero fields as game_observe',
              brief.get('ok') is True and brief_hero.get('level') == 6
              and brief_hero.get('primary') is not None
              and brief_hero.get('mana') == 33, str(brief_hero))


# ------------------------------------------------------------------ B: transfer / merge semantics
def test_transfer_swap() -> None:
    before = containers([slot(0, 2, 5)], [slot(0, 1, 10)])       # different creatures
    after = containers([slot(0, 1, 10)], [slot(0, 2, 5)])       # exact swap
    res = act_transfer(before, after)
    check('B1 different-creature swap -> completed with slot_swap proof',
          res.get('status') == 'completed' and 'slot_swap' in str(res.get('evidence')),
          str(res.get('evidence'))[:200])


def test_transfer_move_into_empty_slot() -> None:
    before = containers([], [slot(0, 1, 10)])
    after = containers([slot(1, 1, 10)], [])                     # moved into hero slot 1
    args = dict(TRANSFER_ARGS, dst_slot=1)
    res = act_transfer(before, after, args)
    check('B2 whole-stack move into an empty destination slot -> completed',
          res.get('status') == 'completed'
          and ('single_slot_move' in str(res.get('evidence'))
               or 'slot_swap' in str(res.get('evidence'))), str(res.get('evidence'))[:200])


def test_transfer_same_creature_partial_move() -> None:
    before = containers([slot(0, 1, 10)], [slot(0, 1, 30)])
    after = containers([slot(0, 1, 15)], [slot(0, 1, 25)])
    res = act_transfer(before, after)
    check('B3 same-creature count move between the requested slots -> completed',
          res.get('status') == 'completed' and 'single_slot_move' in str(res.get('evidence')),
          str(res.get('evidence'))[:200])
    check('B3b evidence lists before/after slot creature+count',
          'creature=1 count=30->creature=1 count=25' in str(res.get('evidence'))
          and 'creature=1 count=10->creature=1 count=15' in str(res.get('evidence')),
          str(res.get('evidence'))[:220])


def test_transfer_wrong_slot_and_unrelated_change() -> None:
    before = containers([slot(0, 1, 10)], [slot(0, 1, 4)])
    after = containers([slot(0, 1, 14)], [])                     # slot 0 moved, not slot 5
    args = dict(TRANSFER_ARGS, src_slot=5)
    res = act_transfer(before, after, args)
    check('B4 wrong requested slot -> not completed',
          res.get('status') in ('submitted', 'unknown') and res.get('ok') is False,
          f"status={res.get('status')} evidence={str(res.get('evidence'))[:160]}")
    unrelated = containers([slot(0, 1, 5)], [slot(0, 1, 4)])     # totals not conserved
    res2 = act_transfer(before, unrelated)
    check('B4b unrelated count change -> submitted, never completed',
          res2.get('status') == 'submitted' and res2.get('ok') is False,
          f"status={res2.get('status')} evidence={str(res2.get('evidence'))[:160]}")


def test_merge_same_creature() -> None:
    before = containers([slot(0, 1, 10)], [slot(0, 1, 30)])
    after = containers([slot(0, 1, 40)], [])
    res = act_merge(before, after)
    check('B5 same-creature merge -> completed with counts summed',
          res.get('status') == 'completed' and 'same_creature_merge' in str(res.get('evidence'))
          and 'count=30->empty' in str(res.get('evidence')),
          str(res.get('evidence'))[:220])


def test_merge_creature_mismatch_and_non_conservation() -> None:
    before = containers([slot(0, 2, 10)], [slot(0, 1, 30)])
    after = containers([slot(0, 2, 40)], [])                     # totals conserved, types wrong
    res = act_merge(before, after)
    check('B6 merge creature mismatch -> submitted, not completed',
          res.get('status') == 'submitted' and res.get('ok') is False,
          f"status={res.get('status')} evidence={str(res.get('evidence'))[:180]}")
    before2 = containers([slot(0, 1, 10)], [slot(0, 1, 30)])
    after2 = containers([slot(0, 1, 35)], [])                    # not conserved (45 vs 35)
    res2 = act_merge(before2, after2)
    check('B6b merge non-conservation -> submitted',
          res2.get('status') == 'submitted'
          and 'not conserved' in str(res2.get('evidence')),
          str(res2.get('evidence'))[:180])


# ------------------------------------------------------------------ C: battle_assess semantics
PERMISSION_KEYS = {'allowed', 'permission', 'may_engage', 'can_engage', 'go_ahead', 'approve',
                   'engage', 'engage_allowed'}


def test_battle_assess_semantics() -> None:
    with Env() as env:
        env.agent(FakeAgent([state()]))
        res = M.battle_assess()
        check('C1 adventure map -> explicit not-a-pre-battle-assessment error',
              res.get('ok') is False
              and res.get('error') == ('battle_assess only works after a battle has started; '
                                       'it is not a pre-battle risk assessment'),
              str(res.get('error')))
        check('C1b no active battle -> ok=False, no permission-like key',
              not (set(res) & PERMISSION_KEYS), str(sorted(set(res) & PERMISSION_KEYS)))

    battle = {'active': 2, 'my_side': 0, 'active_stack': 2,
              'stacks': [make_stack(1, 0, 10, (1, 1)), make_stack(2, 1, 5, (4, 4))]}
    with Env() as env:
        env.agent(FakeAgent([state(battle=battle)]))
        res = M.battle_assess()
        check('C2 active battle -> counts summary with disclaimer',
              res.get('ok') is True and 'disclaimer' in res
              and '不得作为接战许可' in str(res.get('disclaimer'))
              and res.get('risk') == 'advantage', str(res.get('disclaimer'))[:160])
        check('C2b no permission-style field is returned',
              not (set(res) & PERMISSION_KEYS), str(sorted(set(res) & PERMISSION_KEYS)))
        overlay = (env.root / 'state' / 'overlay.jsonl').read_text(encoding='utf-8')
        check('C2c overlay says post-contact summary, not a pre-battle assessment',
              '入战后数量摘要' in overlay and '非战力评估' in overlay
              and 'advantage' in overlay, overlay.splitlines()[-1][:160])


def main() -> int:
    test_digest_passthrough()
    test_transfer_swap()
    test_transfer_move_into_empty_slot()
    test_transfer_same_creature_partial_move()
    test_transfer_wrong_slot_and_unrelated_change()
    test_merge_same_creature()
    test_merge_creature_mismatch_and_non_conservation()
    test_battle_assess_semantics()
    failed = [item for item in RESULTS if not item[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
