# -*- coding: utf-8 -*-
"""T35 offline tests: hero magic (battle + adventure casting, spell costs, cheats stay test-only).

No game, no client, no network: reuses the fake harness from test_action_contract (Env fixes
STATE_DIR/SAVES_DIR to a temp dir and replaces _agent()/_require_lease()).  The C++ side is
covered by the isolated live smoke; this suite covers the MCP contract:

  A  argument validation + bridge command shapes (target all-or-nothing, unit vs hex, no-target)
  B  preconditions before sending (spellbook, mana, battle active/deployment/target stack)
  C  honest verifiers: adventure cast (mana drop / query), battle cast (mane drop, cast_spells,
     target loss, battle end, no-change -> submitted, bridge ERR -> rejected, UNKNOWN blocks)
  D  battle_actions passthrough of the bridge's hero/spells block
  E  spell observation fields (cost/adventure/can_cast) in digest + brief

Run: python -X utf8 tools/test_t35_spellcasting.py
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


def make_hero(mana: int = 20) -> dict:
    hero = T.make_hero(0, (2, 2))
    hero.update({'id': 7, 'mana': mana, 'mana_max': 40, 'spellbook': True,
                 'spells': [{'id': 15, 'name': 'Haste', 'cost': 3, 'adventure': False,
                             'can_cast': True},
                            {'id': 20, 'name': 'View Air', 'cost': 2, 'adventure': True,
                             'can_cast': True},
                            {'id': 30, 'name': 'Dimension Door', 'cost': 5,
                             'adventure': True, 'can_cast': True}]})
    return hero


def adventure_state(mana: int = 20) -> dict:
    return T.state(heroes=[make_hero(mana)])


def battle_state(hero_mana: int = 20, cast_spells: int = 0, enemy_count: int = 6,
                 active_stack: int = 5, battle_active: bool = True) -> dict:
    st = T.state(heroes=[make_hero()])
    if battle_active:
        st['battle'] = {'active': active_stack, 'my_side': 0, 'active_stack': active_stack,
                        'round': 1, 'hero_mana': hero_mana, 'hero_mana_max': 40,
                        'hero_spellbook': True, 'cast_spells': cast_spells,
                        'stacks': [T.make_stack(5, 0, 10, (1, 1)),
                                   T.make_stack(9, 1, enemy_count, (9, 9))]}
    else:
        st['battle'] = {'active': False}
    return st


# ------------------------------------------------------------------ A: validation + bridge lines
def test_validation_and_lines() -> None:
    with T.Env() as env:
        fake = env.agent(T.FakeAgent([adventure_state()]))
        res = M.game_act('cast_spell', {'hero': 0, 'spell': 20})
        check('A1 no-target adventure cast is sent as "<cmd> <hero> <spell>"',
              fake.sent == ['cast_spell 0 20'] and res.get('sent') is not False,
              str(fake.sent))
    with T.Env() as env:
        fake = env.agent(T.FakeAgent([adventure_state()]))
        res = M.game_act('cast_spell', {'hero': 0, 'spell': 30, 'x': 5, 'y': 6, 'z': 0})
        check('A1b ranged adventure cast appends x y z',
              fake.sent == ['cast_spell 0 30 5 6 0'], str(fake.sent))
    with T.Env() as env:
        fake = env.agent(T.FakeAgent([adventure_state()]))
        res = M.game_act('cast_spell', {'hero': 0, 'spell': 30, 'x': 5})
        check('A2 a partial adventure target is rejected before sending',
              res.get('ok') is False and res.get('sent') is False
              and 'all of x,y,z' in str(res.get('error')) and fake.sent == [],
              f"error={res.get('error')} sent={fake.sent}")
        check('A2b rejected target never read the game state',
              fake.state_calls == 0, f'state_calls={fake.state_calls}')
    with T.Env() as env:
        fake = env.agent(T.FakeAgent([battle_state()]))
        res = M.game_act('battle_cast_spell', {'spell': 15, 'stack': 9})
        check('A3 unit-target battle cast is sent as "unit <stackId>"',
              fake.sent == ['battle_cast_spell 15 unit 9'], str(fake.sent))
    with T.Env() as env:
        fake = env.agent(T.FakeAgent([battle_state()]))
        res = M.game_act('battle_cast_spell', {'spell': 15, 'x': 3, 'y': 4})
        check('A4 hex-target battle cast is sent as "hex <x> <y>"',
              fake.sent == ['battle_cast_spell 15 hex 3 4'], str(fake.sent))
    with T.Env() as env:
        fake = env.agent(T.FakeAgent([battle_state()]))
        res = M.game_act('battle_cast_spell', {'spell': 15})
        check('A5 no-target battle cast sends only the spell id',
              fake.sent == ['battle_cast_spell 15'], str(fake.sent))
    with T.Env() as env:
        fake = env.agent(T.FakeAgent([battle_state()]))
        res = M.game_act('battle_cast_spell', {'spell': 15, 'stack': 9, 'x': 1, 'y': 2})
        check('A6 unit+hex targets together are rejected',
              res.get('ok') is False and res.get('sent') is False
              and 'not both' in str(res.get('error')) and fake.sent == [],
              str(res.get('error')))
        res = M.game_act('battle_cast_spell', {'spell': 15, 'x': 1})
        check('A6b hex target without y is rejected',
              res.get('ok') is False and 'both x and y' in str(res.get('error')),
              str(res.get('error')))
        res = M.game_act('battle_cast_spell', {'spell': 'fireball', 'stack': 9})
        check('A6c non-integer spell id is rejected before sending',
              res.get('ok') is False and res.get('sent') is False, str(res.get('error')))


# ------------------------------------------------------------------ B: preconditions
def test_preconditions() -> None:
    with T.Env() as env:
        fake = env.agent(T.FakeAgent([adventure_state()]))
        res = M.game_act('cast_spell', {'hero': 0, 'spell': 99})
        check('B1 unknown spell is rejected before the bridge is touched',
              res.get('status') == 'rejected' and res.get('sent') is False
              and 'not in hero' in str(res.get('evidence')) and fake.sent == [],
              f"evidence={res.get('evidence')} sent={fake.sent}")
    with T.Env() as env:
        fake = env.agent(T.FakeAgent([adventure_state(mana=1)]))
        res = M.game_act('cast_spell', {'hero': 0, 'spell': 20})
        check('B2 not enough mana is rejected before sending',
              res.get('status') == 'rejected' and res.get('sent') is False
              and 'not enough mana' in str(res.get('evidence')) and fake.sent == [],
              str(res.get('evidence')))
    with T.Env() as env:
        fake = env.agent(T.FakeAgent([adventure_state(mana=1)]))
        res = M.game_act('cast_spell', {'hero': 3, 'spell': 20})
        check('B2b unknown hero index is rejected before sending',
              res.get('status') == 'rejected' and res.get('sent') is False,
              str(res.get('evidence')))
    with T.Env() as env:
        fake = env.agent(T.FakeAgent([adventure_state()]))
        res = M.game_act('battle_cast_spell', {'spell': 15, 'stack': 9})
        check('B3 battle cast outside a battle is rejected before sending',
              res.get('status') == 'rejected' and res.get('sent') is False
              and 'no active battle' in str(res.get('evidence')) and fake.sent == [],
              str(res.get('evidence')))
    with T.Env() as env:
        fake = env.agent(T.FakeAgent([battle_state(active_stack=-1)]))
        res = M.game_act('battle_cast_spell', {'spell': 15, 'stack': 9})
        check('B3b battle cast in the deployment phase is rejected with a hint',
              res.get('status') == 'rejected' and res.get('sent') is False
              and 'battle_start' in str(res.get('evidence')), str(res.get('evidence')))
    with T.Env() as env:
        fake = env.agent(T.FakeAgent([battle_state()]))
        res = M.game_act('battle_cast_spell', {'spell': 15, 'stack': 77})
        check('B3c unknown battle target stack is rejected before sending',
              res.get('status') == 'rejected' and res.get('sent') is False
              and 'not visible' in str(res.get('evidence')) and fake.sent == [],
              str(res.get('evidence')))


# ------------------------------------------------------------------ C: verifiers
def test_adventure_verifier() -> None:
    before = adventure_state(mana=20)
    after = adventure_state(mana=18)              # View Air costs 2
    with T.Env() as env:
        env.agent(T.FakeAgent([before, after], [T.bridge_ok(1, 'cast_spell')]))
        res = M.game_act('cast_spell', {'hero': 0, 'spell': 20})
        check('C1 mana drop after an adventure cast -> completed',
              res.get('status') == 'completed' and 'mana 20->18' in str(res.get('evidence')),
              str(res.get('evidence')))
    with T.Env() as env:
        env.agent(T.FakeAgent([before, adventure_state(mana=20)],
                              [T.bridge_ok(1, 'cast_spell')]))
        res = M.game_act('cast_spell', {'hero': 0, 'spell': 20})
        check('C1b no observable mana change -> submitted, never completed',
              res.get('status') == 'submitted' and res.get('ok') is False,
              str(res.get('evidence')))
    query_after = adventure_state(mana=20)
    query_after['query'] = {'id': 4, 'type': 'select', 'text': 'choose a town',
                            'can_cancel': False,
                            'options': [{'index': 0, 'type': 'RESOURCE'}]}
    with T.Env() as env:
        env.agent(T.FakeAgent([before, query_after], [T.bridge_ok(1, 'cast_spell')]))
        res = M.game_act('cast_spell', {'hero': 0, 'spell': 20})
        check('C1c a query opened by the spell counts as completed with an answer hint',
              res.get('status') == 'completed' and 'query' in str(res.get('evidence')),
              str(res.get('evidence')))


def test_battle_verifier() -> None:
    before = battle_state(hero_mana=20, cast_spells=0, enemy_count=6)
    mana_after = battle_state(hero_mana=17, cast_spells=1, enemy_count=4)
    with T.Env() as env:
        env.agent(T.FakeAgent([before, mana_after], [T.bridge_ok(1, 'battle_cast_spell')]))
        res = M.game_act('battle_cast_spell', {'spell': 15, 'stack': 9})
        check('C2 battle mana drop -> completed',
              res.get('status') == 'completed' and 'mana 20->17' in str(res.get('evidence')),
              str(res.get('evidence')))
    same_mana_damage = battle_state(hero_mana=17, cast_spells=1, enemy_count=4)
    with T.Env() as env:
        env.agent(T.FakeAgent([battle_state(hero_mana=17, cast_spells=1, enemy_count=6),
                               same_mana_damage], [T.bridge_ok(1, 'battle_cast_spell')]))
        res = M.game_act('battle_cast_spell', {'spell': 15, 'stack': 9})
        check('C3 target count drop -> completed even without a mana read',
              res.get('status') == 'completed' and 'count 6->4' in str(res.get('evidence')),
              str(res.get('evidence')))
    with T.Env() as env:
        env.agent(T.FakeAgent([before, battle_state(hero_mana=20, cast_spells=1)],
                              [T.bridge_ok(1, 'battle_cast_spell')]))
        res = M.game_act('battle_cast_spell', {'spell': 15})
        check('C3b cast_spells counter rise -> completed',
              res.get('status') == 'completed' and 'cast_spells 0->1' in str(res.get('evidence')),
              str(res.get('evidence')))
    ended = battle_state(battle_active=False)
    with T.Env() as env:
        env.agent(T.FakeAgent([before, ended], [T.bridge_ok(1, 'battle_cast_spell')]))
        res = M.game_act('battle_cast_spell', {'spell': 15})
        check('C3c battle ending after the spell -> completed',
              res.get('status') == 'completed' and 'battle ended' in str(res.get('evidence')),
              str(res.get('evidence')))
    with T.Env() as env:
        env.agent(T.FakeAgent([before, before], [T.bridge_err(1, 'spell_cannot_be_cast')]))
        res = M.game_act('battle_cast_spell', {'spell': 15, 'stack': 9})
        check('C4 bridge ERR is terminal rejected (no verifier upgrade)',
              res.get('status') == 'rejected' and res.get('accepted') is False
              and 'bridge rejected' in str(res.get('evidence')), str(res.get('evidence')))
    with T.Env() as env:
        env.agent(T.FakeAgent([before, before], [T.bridge_unknown(1)]))
        res = M.game_act('battle_cast_spell', {'spell': 15, 'stack': 9})
        check('C5 UNKNOWN battle cast freezes further mutations',
              res.get('status') == 'unknown' and res.get('ok') is False,
              str(res.get('evidence')))
        blocked = M.game_act('battle_defend', {})
        check('C5b the next mutation is blocked until resolve_unresolved',
              blocked.get('ok') is False
              and blocked.get('error') in ('unresolved_prior_action', 'request_pending'),
              str(blocked.get('error')))


# ------------------------------------------------------------------ D: battle_actions passthrough
def test_battle_actions_passthrough() -> None:
    payload = {
        'active': 5, 'mine': True, 'can_shoot_in_principle': False,
        'actor': {'id': 5, 'creature': 0, 'count': 10, 'hex': [1, 1], 'shots': 0,
                  'counterattacks': 1},
        'move_hexes': [[2, 2]], 'targets': [], 'turn_order': [5, 9],
        'hero': {'mana': 20, 'mana_max': 40, 'spellbook': True, 'can_cast': True,
                 'reason': 'ok', 'casts_used': 0},
        'spells': [{'id': 15, 'name': 'Haste', 'cost': 3, 'can_cast': True, 'reason': '',
                    'target_kind': 'unit'},
                   {'id': 45, 'name': 'Magic Arrow', 'cost': 5, 'can_cast': True,
                    'reason': '', 'target_kind': 'unit'},
                   {'id': 40, 'name': 'Lightning Bolt', 'cost': 10, 'can_cast': False,
                    'reason': 'not_enough_mana', 'target_kind': 'hex'},
                   {'id': 50, 'name': 'Death Ripple', 'cost': 12, 'can_cast': True,
                    'reason': '', 'target_kind': 'none'}],
    }

    def on_execute(line: str) -> None:
        (M._dir() / 'battle_actions.json').write_text(json.dumps(payload), encoding='utf-8')

    with T.Env() as env:
        fake = env.agent(T.FakeAgent([battle_state()], [T.bridge_ok(1, 'battle_actions')]))
        fake.on_execute = on_execute
        res = M.battle_actions()
        check('D1 battle_actions exposes the hero spellcasting block',
              res.get('ok') is True
              and (res.get('hero') or {}).get('mana') == 20
              and (res.get('hero') or {}).get('can_cast') is True,
              str(res.get('hero')))
        rows = res.get('spells') or []
        check('D2 spells carry id/name/cost/can_cast/target_kind from the bridge',
              [row.get('id') for row in rows] == [15, 45, 40, 50]
              and rows[1].get('target_kind') == 'unit'
              and rows[2].get('reason') == 'not_enough_mana'
              and rows[3].get('target_kind') == 'none', str(rows))
        check('D3 the pre-existing battle_actions fields are unchanged',
              res.get('active') == 5 and res.get('mine') is True
              and res.get('turn_order') == [5, 9], str({k: res.get(k) for k in
                                                        ('active', 'mine', 'turn_order')}))


# ------------------------------------------------------------------ E: observation fields
def test_spell_observation() -> None:
    st = adventure_state()
    digest = M._digest(st)
    hero = (digest.get('heroes') or [{}])[0]
    spells = hero.get('spells') or []
    check('E1 digest passes spell cost/adventure/can_cast through',
          spells[1].get('cost') == 2 and spells[1].get('adventure') is True
          and spells[1].get('can_cast') is True, str(spells))
    with T.Env() as env:
        env.agent(T.FakeAgent([st]))
        brief = M.game_brief()
        bhero = (brief.get('heroes') or [{}])[0]
        brows = bhero.get('spells') or []
        check('E2 game_brief shows spell id/name/cost/can_cast/adventure',
              brows[1].get('id') == 20 and brows[1].get('cost') == 2
              and brows[1].get('can_cast') is True
              and brows[1].get('adventure') is True, str(brows))
        check('E2b brief keeps spell_count as the complete total',
              bhero.get('spell_count') == 3, str(bhero.get('spell_count')))
    old = T.state(heroes=[dict(T.make_hero(0, (1, 1)),
                               spells=[{'id': 15, 'name': 'Haste'}])])
    old_hero = (M._digest(old).get('heroes') or [{}])[0]
    check('E3 an old-style spell entry without cost stays readable',
          old_hero.get('spells') == [{'id': 15, 'name': 'Haste'}], str(old_hero.get('spells')))
    check('E4 cheat spells are not exposed as MCP actions',
          'learnspell' not in M.ACTIONS and 'allspells' not in M.ACTIONS
          and 'mana' not in M.ACTIONS, str(sorted(M.ACTIONS)))


def main() -> int:
    test_validation_and_lines()
    test_preconditions()
    test_adventure_verifier()
    test_battle_verifier()
    test_battle_actions_passthrough()
    test_spell_observation()
    failed = [item for item in RESULTS if not item[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
