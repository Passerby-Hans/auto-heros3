# -*- coding: utf-8 -*-
"""Offline regression tests for the W2 battle batch (no game, no client, no network).

Covered:
  V1  battle_attack validation: optional mode auto|melee|shot; bad mode rejected before send
  V2  battle_approach is a separate explicit action (line = battle_approach <target>)
  Z1  attack verifier: target killed/count drop -> completed; battle ended -> completed;
      no kill but turn advanced -> submitted (never a fabricated completed)
  Z2  approach verifier: active stack hex changed -> completed
  R1  battle_round: a bridge REFUSAL (cannot_attack) interrupts the round, no silent approach,
      no retry loop
  A1  battle_actions() returns the read-only legal-action dump
  S1  source guards: the bridge uses engine legality queries (battleCanAttackUnit /
      battleCanAttackHex / battleCanShoot), has no silent move inside battle_attack, and
      battle_actions is registered as a read-only command
Run: python -X utf8 tools/test_w2_battle.py
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
from test_action_contract import (  # noqa: E402
    Env, FakeAgent, bridge_err, bridge_ok, state,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition, detail: str = '') -> None:
    RESULTS.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


def battle_state(active_stack=1, our_count=10, enemy_count=10, our_hex=(1, 1),
                 enemy_hex=(2, 2), active=True, our_side=0) -> dict:
    return state(battle={
        'active': active, 'active_stack': active_stack, 'my_side': our_side,
        'stacks': [
            {'id': 1, 'side': our_side, 'creature': 10, 'count': our_count,
             'hex': list(our_hex)},
            {'id': 2, 'side': 1 - our_side, 'creature': 20, 'count': enemy_count,
             'hex': list(enemy_hex)},
        ],
    })


# ------------------------------------------------------------------ V: validation / command lines
def test_validation_and_lines() -> None:
    with Env() as env:
        fake = env.agent(FakeAgent([battle_state(), battle_state(enemy_count=7)]))
        res = M.game_act('battle_attack', {'stack': 2, 'mode': 'shot'})
        check('V1a explicit shot mode reaches the bridge',
              fake.sent == ['battle_attack 2 shot'] and res.get('status') == 'completed',
              f'sent={fake.sent} status={res.get("status")}')
        check('V1a2 the bridge reply is exposed as evidence (walk/shot/from)',
              'battle_attack 2 shot' in str(res.get('bridge_line') or ''),
              str(res.get('bridge_line')))
    with Env() as env:
        fake = env.agent(FakeAgent([battle_state()]))
        res = M.game_act('battle_attack', {'stack': 2, 'mode': 'nuke'})
        check('V1b invalid attack mode rejected before send',
              res.get('ok') is False and 'must be one of' in str(res.get('error'))
              and fake.sent == [], str(res.get('error')))
    with Env() as env:
        fake = env.agent(FakeAgent([battle_state()]))
        res = M.game_act('battle_attack', {'stack': 2, 'mode': 'auto', 'extra': 1})
        check('V1c unexpected attack argument rejected', res.get('ok') is False
              and fake.sent == [], str(res.get('error')))
    with Env() as env:
        fake = env.agent(FakeAgent([battle_state(), battle_state(active_stack=3,
                                                                 our_hex=(2, 1))]))
        res = M.game_act('battle_approach', {'target': 2})
        check('V2a battle_approach is a separate explicit command',
              fake.sent == ['battle_approach 2'] and res.get('status') == 'completed',
              f'sent={fake.sent} status={res.get("status")}')
    with Env() as env:
        fake = env.agent(FakeAgent([battle_state()]))
        res = M.game_act('battle_approach', {})
        check('V2b battle_approach requires a target',
              res.get('ok') is False and 'missing args' in str(res.get('error'))
              and fake.sent == [], str(res.get('error')))


# ------------------------------------------------------------------ Z: verifiers
def test_attack_verifier() -> None:
    with Env() as env:
        env.agent(FakeAgent([battle_state(enemy_count=10), battle_state(enemy_count=7)]))
        res = M.game_act('battle_attack', {'stack': 2})
        check('Z1a kill/count drop -> completed',
              res.get('status') == 'completed'
              and '10->7' in str(res.get('evidence')), str(res.get('evidence')))
    with Env() as env:
        env.agent(FakeAgent([battle_state(), battle_state(active_stack=3)]))
        res = M.game_act('battle_attack', {'stack': 2})
        check('Z1b non-killing hit -> submitted, never completed',
              res.get('status') == 'submitted' and res.get('ok') is False
              and 'no kill' in str(res.get('evidence')), str(res.get('evidence')))
    with Env() as env:
        env.agent(FakeAgent([battle_state(), battle_state(active=False)]))
        res = M.game_act('battle_attack', {'stack': 2})
        check('Z1c battle ended -> completed',
              res.get('status') == 'completed', str(res.get('evidence')))
    with Env() as env:
        after = battle_state()
        after['battle']['stacks'] = [after['battle']['stacks'][0]]
        env.agent(FakeAgent([battle_state(), after]))
        res = M.game_act('battle_attack', {'stack': 2})
        check('Z1d target removed -> completed',
              res.get('status') == 'completed' and 'gone' in str(res.get('evidence')),
              str(res.get('evidence')))
    with Env() as env:
        env.agent(FakeAgent([battle_state(), battle_state()]))
        res = M.game_act('battle_attack', {'stack': 2})
        check('Z1e no observable change -> submitted (retry path, not completed)',
              res.get('status') == 'submitted', str(res.get('status')))


def test_approach_verifier() -> None:
    with Env() as env:
        env.agent(FakeAgent([battle_state(active_stack=1, our_hex=(1, 1)),
                             battle_state(active_stack=3, our_hex=(2, 1))]))
        res = M.game_act('battle_approach', {'target': 2})
        check('Z2a moved stack -> completed',
              res.get('status') == 'completed' and 'moved' in str(res.get('evidence')),
              str(res.get('evidence')))
    with Env() as env:
        env.agent(FakeAgent([battle_state(active_stack=1), battle_state(active_stack=3)]))
        res = M.game_act('battle_approach', {'target': 2})
        check('Z2b not moved yet -> submitted',
              res.get('status') == 'submitted', str(res.get('status')))


# ------------------------------------------------------------------ R: battle_round refusal
def test_battle_round_refusal() -> None:
    with Env() as env:
        fake = env.agent(FakeAgent([battle_state()],
                                   results=[bridge_err(1, 'cannot_attack target=2 '
                                                          'can_melee=0 walk=0 can_shoot=0')]))
        res = M.battle_round(target=2)
        check('R1a refused attack interrupts the round',
              res.get('status') == 'interrupted'
              and (res.get('interrupt') or {}).get('reason') == 'attack_not_legal',
              str(res)[:240])
        check('R1b refused attack is not retried and no approach is sent',
              fake.sent == ['battle_attack 2'], f'sent={fake.sent}')
        check('R1c interrupt lists a legal-action hint',
              'battle_actions' in str((res.get('interrupt') or {}).get('hint')),
              str((res.get('interrupt') or {}).get('hint')))


# ------------------------------------------------------------------ A: battle_actions tool
def test_battle_actions_tool() -> None:
    payload = {'active': 1, 'mine': True,
               'actor': {'id': 1, 'creature': 10, 'count': 10, 'hex': [1, 1],
                         'shots': 0, 'counterattacks': 1},
               'move_hexes': [[1, 1], [2, 1]],
               'targets': [{'id': 2, 'creature': 20, 'count': 10, 'hex': [2, 2],
                            'adjacent': False, 'can_melee': True, 'walk': True,
                            'attack_from': [2, 1], 'can_shoot': False}],
               'turn_order': [1, 2]}
    with Env() as env:
        def on_execute(_line: str) -> None:
            state_dir = env.root / 'state'
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / 'battle_actions.json').write_text(
                json.dumps(payload), encoding='utf-8')
        env.agent(FakeAgent([battle_state()], on_execute=on_execute))
        res = M.battle_actions()
        check('A1a battle_actions returns the bridge dump',
              res.get('ok') is True and res.get('targets') == payload['targets'],
              str(res)[:160])
        check('A1b legal attack-from hex is reported',
              (res.get('targets') or [{}])[0].get('attack_from') == [2, 1],
              str(res.get('targets')))
    with Env() as env:
        env.agent(FakeAgent([state()]))
        res = M.battle_actions()
        check('A1c no battle -> explicit error', res.get('ok') is False
              and 'no active battle' in str(res.get('error')), str(res.get('error')))


# ------------------------------------------------------------------ S: bridge source guards
def test_bridge_source_guards() -> None:
    text = (ROOT / 'platform' / 'vcmi-patch' / 'client' / 'AgentBridge.cpp').read_text(
        encoding='utf-8')
    start = text.index('void doBattleAttack(')
    end = text.index('void doBattleApproach(')
    attack_body = text[start:end]
    check('S1a attack uses engine legality queries',
          'battleCanAttackUnit' in text and 'battleCanAttackHex' in text
          and 'battleCanShoot(ctx.active, targetHex)' in text,
          'battleCanAttackUnit/Hex + battleCanShoot')
    check('S1b walk-and-attack uses engine attack-from hex',
          'makeMeleeAttack(ctx.active, target->getPosition(),' in text
          and 'legality.attackFrom' in attack_body,
          'makeMeleeAttack(..., attackFrom)')
    check('S1c attack never silently moves',
          'makeMove' not in attack_body and 'cannot_attack' in attack_body,
          'no makeMove inside doBattleAttack')
    check('S1d explicit approach command exists',
          'void doBattleApproach(' in text and 'makeMove(ctx.active, best)' in text,
          'doBattleApproach')
    check('S1e battle_move validates destination',
          'ERR illegal_move' in text and 'toWhichHexMove(available, ctx.active, dest)' in text,
          'illegal_move check')
    check('S1f battle_actions is a read-only command',
          'op == "battle_actions"' in text
          and 'else if (op == "battle_actions") doBattleActions();' in text,
          'readOnly + dispatch')
    check('S1g battle observation exposes ammo/counterattacks/stance',
          '\\"shots\\":{\\"available\\":' in text
          and '\\"counterattacks\\":{\\"available\\":' in text
          and '\\"waited\\":' in text and '\\"defending\\":' in text,
          'shots/counterattacks/defending/waited')


def main() -> int:
    test_validation_and_lines()
    test_attack_verifier()
    test_approach_verifier()
    test_battle_round_refusal()
    test_battle_actions_tool()
    test_bridge_source_guards()
    failed = [item for item in RESULTS if not item[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
