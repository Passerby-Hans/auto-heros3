# -*- coding: utf-8 -*-
"""Offline regression tests for the remaining W1 trust items: R2 / R4 / R5.

Covered:
  R2a unknown mutating action blocks every new action (new request_id is not a bypass)
  R2b read-only observation is still allowed and resolve_unresolved() closes the block only
      after a fresh state read + a recorded observation
  R2c move_near stops on an unknown first move instead of trying other candidates
  R2d battle_round's unknown order blocks the next action (battle helper cannot bypass)
  R2e unknown that never sent anything does NOT block later actions
  R2f save_game / load_game / battle_start honour the unresolved gate (no side door)
  R4a a valid lease is not overwritten without force=True (ledger-recorded takeover)
  R4b a stale owner cannot release another writer's lease
  R4c battle_start needs the lease (uniform admission)
  R5a load_game never turns "saved moments ago + identical state" into completed
Run: python -X utf8 tools/test_w1_trust.py
Exit code 0 only when every check passes.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))
import mcp_server_v2 as M  # noqa: E402
from test_action_contract import (  # noqa: E402
    Env, FakeAgent, bridge_ok, bridge_unknown, make_hero, make_stack, state,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition, detail: str = '') -> None:
    RESULTS.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


def battle_state() -> dict:
    return state(battle={'active': True, 'active_stack': 1, 'my_side': 0,
                         'stacks': [make_stack(1, 0, 10, (1, 1)),
                                    make_stack(2, 1, 6, (5, 5))]})


def unresolved_file(env) -> Path:
    return env.root / 'state' / 'unresolved.json'


# ------------------------------------------------------------------ R2: unresolved gate
def test_unknown_blocks_new_actions() -> None:
    with Env() as env:
        fake = env.agent(FakeAgent([state(), RuntimeError('after-state failed')]))
        res = M.game_act('center_hero', {}, request_id='req-a')
        check('R2a-1 unknown action is reported honestly',
              res.get('status') == 'unknown' and res.get('ok') is False
              and res.get('seq') is not None, str(res)[:180])
        marker = json.loads(unresolved_file(env).read_text(encoding='utf-8'))
        check('R2a-2 unresolved marker records action/action_id',
              marker.get('action') == 'center_hero'
              and marker.get('action_id') == res.get('action_id'),
              str(marker)[:180])
        sent_before = list(fake.sent)
        blocked = M.game_act('end_turn', {}, request_id='fresh-request-id')
        check('R2a-3 a new request_id cannot bypass the unresolved action',
              blocked.get('error') == 'unresolved_prior_action'
              and blocked.get('sent') is False and fake.sent == sent_before,
              str(blocked)[:200])
        replay = M.game_act('center_hero', {}, request_id='req-a')
        check('R2a-4 the same request_id is not resent',
              fake.sent == sent_before, f'sent={fake.sent}')
        hint = str((blocked.get('hint') or ''))
        check('R2a-5 the error tells the caller how to unblock',
              'resolve_unresolved' in hint and 'game_observe' in hint, hint[:160])


def test_resolve_unresolved() -> None:
    with Env() as env:
        good = state(heroes=[make_hero(0, (1, 1))])
        fake = env.agent(FakeAgent([good, RuntimeError('after-state failed'), good]))
        res = M.game_act('move_hero', {'hero': 0, 'x': 2, 'y': 1, 'z': 0})
        marker = json.loads(unresolved_file(env).read_text(encoding='utf-8'))
        check('R2b-1 unknown move created a marker',
              res.get('status') == 'unknown' and marker.get('action') == 'move_hero',
              str(res)[:160])
        bad = M.resolve_unresolved('not-the-id', 'hero did not move')
        check('R2b-2 wrong action_id is rejected',
              bad.get('ok') is False and bad.get('error') == 'action_id_mismatch', str(bad)[:160])
        bad_text = M.resolve_unresolved(marker['action_id'], 'line1\nline2')
        check('R2b-3 multi-line observation is rejected',
              bad_text.get('ok') is False and 'single-line' in str(bad_text.get('error')),
              str(bad_text)[:160])
        obs = M.game_observe()
        check('R2b-4 read-only observation works while the action is unresolved',
              obs.get('ok') is True, str(obs)[:160])
        closed = M.resolve_unresolved(marker['action_id'],
                                      'hero still at [1,1]; day 1; army 1 stack')
        check('R2b-5 explicit resolution clears the block',
              closed.get('ok') is True and closed.get('resolved') is True
              and not unresolved_file(env).exists(), str(closed)[:180])
        after = M.game_act('end_turn', {})
        check('R2b-6 actions are allowed again after resolution',
              after.get('error') != 'unresolved_prior_action'
              and fake.sent[-1] == 'end_turn', str(after.get('status')))


def test_move_near_stops_on_unknown() -> None:
    with Env() as env:
        fake = env.agent(FakeAgent([state(heroes=[make_hero(0, (1, 1))]),
                                    RuntimeError('after-state failed')]))
        res = M.move_near(hero=0, x=3, y=1, radius=1)
        check('R2c-1 move_near stops after an unknown first move',
              res.get('status') == 'unknown' and len(res.get('attempts') or []) == 1
              and fake.sent == ['move_hero 0 3 1 0'],
              f"sent={fake.sent} status={res.get('status')}")
        blocked = M.move_near(hero=0, x=4, y=1, radius=1)
        check('R2c-2 a second move_near cannot bypass the block',
              blocked.get('error') == 'unresolved_prior_action'
              and blocked.get('status') == 'blocked', str(blocked)[:180])


def test_battle_round_unknown_blocks() -> None:
    with Env() as env:
        fake = env.agent(FakeAgent([battle_state()], results=[bridge_unknown(1)]))
        res = M.battle_round(target=2)
        interrupt = res.get('interrupt') or {}
        check('R2d-1 unknown battle order interrupts the round',
              res.get('status') == 'interrupted'
              and interrupt.get('reason') == 'order_unknown', str(res)[:220])
        check('R2d-2 the unknown order is not retried',
              fake.sent == ['battle_attack 2'], f'sent={fake.sent}')
        marker = json.loads(unresolved_file(env).read_text(encoding='utf-8'))
        check('R2d-3 battle helper created the unresolved marker',
              marker.get('action') == 'battle_round'
              and marker.get('action_id') == interrupt.get('round_id'), str(marker)[:200])
        blocked = M.game_act('battle_defend', {})
        check('R2d-4 the battle helper cannot bypass the block',
              blocked.get('error') == 'unresolved_prior_action', str(blocked)[:160])
        second = M.battle_round(target=2)
        check('R2d-5 a second battle_round is blocked too',
              second.get('error') == 'unresolved_prior_action', str(second)[:160])


def test_unsent_unknown_does_not_block() -> None:
    with Env() as env:
        fake = env.agent(FakeAgent([RuntimeError('no before-state'), state(), state()]))
        first = M.game_act('end_turn', {})
        check('R2e-1 no before-state is reported as unknown/not sent',
              first.get('status') == 'unknown' and first.get('sent') is False,
              str(first)[:160])
        check('R2e-2 nothing was sent so no block is created',
              not unresolved_file(env).exists() and fake.sent == [], f'sent={fake.sent}')
        second = M.game_act('end_turn', {})
        check('R2e-3 later actions are not blocked by an unsent unknown',
              second.get('error') != 'unresolved_prior_action'
              and fake.sent == ['end_turn'], str(second.get('status')))


def test_side_doors_honour_gate() -> None:
    with Env() as env:
        env.restore_real_lease()
        M._lease_token = None
        acquired = M.lease_acquire()          # otherwise the lease check fires before the gate
        check('R2f-0 lease acquired for the side-door test', acquired.get('ok') is True)
        M._set_unresolved({'action': 'move_hero', 'args': {'hero': 0, 'x': 9, 'y': 9, 'z': 0},
                           'action_id': 'unknown-1', 'request_id': None, 'seq': 3,
                           'status': 'unknown', 'evidence': 'bridge UNKNOWN',
                           'ts': 0.0})
        env.agent(FakeAgent([battle_state()]))
        save = M.save_game('gate-test')
        check('R2f-1 save_game honours the unresolved gate',
              save.get('error') == 'unresolved_prior_action', str(save)[:160])
        start = M.battle_start()
        check('R2f-2 battle_start honours the unresolved gate',
              start.get('error') == 'unresolved_prior_action', str(start)[:160])


# ------------------------------------------------------------------ R4: lease
def test_lease_hardening() -> None:
    with Env() as env:
        env.restore_real_lease()
        M._lease_token = None
        first = M.lease_acquire()
        check('R4a-1 lease acquired', first.get('ok') is True and M._lease_token, str(first)[:120])
        token = M._lease_token
        M._lease_token = None                       # simulate a restarted MCP process
        refused = M.lease_acquire()
        check('R4a-2 a valid lease is not overwritten',
              refused.get('ok') is False and refused.get('error') == 'lease_held',
              str(refused)[:200])
        on_disk = json.loads((env.root / 'state' / 'lease.json').read_text(encoding='utf-8'))
        check('R4a-3 the held lease file is unchanged', on_disk.get('token') == token,
              on_disk.get('token')[:8])
        forced = M.lease_acquire(force=True, reason='offline test takeover')
        check('R4a-4 forced takeover is explicit and recorded',
              forced.get('ok') is True and forced.get('took_over') is True,
              str(forced)[:160])
        new_token = M._lease_token
        # a stale writer (old in-memory token) must not delete the new lease
        M._lease_token = token
        release = M.lease_release()
        check('R4b stale owner cannot release another writer lease',
              release.get('ok') is False and release.get('error') == 'lease_not_held',
              str(release)[:160])
        M._lease_token = new_token
        released = M.lease_release()
        check('R4b-2 the current holder can release',
              released.get('ok') is True and M._lease_token is None, str(released)[:120])
        # uniform admission: battle_start without a lease is refused
        env.agent(FakeAgent([battle_state()]))
        no_lease = M.battle_start()
        check('R4c battle_start needs the lease', 'no lease' in str(no_lease.get('error')),
              str(no_lease)[:160])


# ------------------------------------------------------------------ R5: load evidence
def test_load_requires_observable_change() -> None:
    with Env() as env:
        slot = env.root / 'saves' / 'agent' / 'fresh.vsgm1'
        slot.parent.mkdir(parents=True, exist_ok=True)
        slot.write_bytes(b'fake save payload')
        import datetime
        M._atomic_write_json(M._saves_meta_path(), {
            'fresh': {'slot': 'Saves/agent/fresh', 'day': 1,
                      'saved_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}})
        same = state(day=1)
        env.agent(FakeAgent([same, same]))
        res = M.load_game('fresh')
        check('R5a fresh save + identical state -> unknown (not completed)',
              res.get('ok') is False and res.get('status') == 'unknown'
              and 'no observable state change' in str(res.get('evidence'))
              and 'does not prove a load' in str(res.get('evidence')),
              str(res.get('evidence'))[:240])


def main() -> int:
    test_unknown_blocks_new_actions()
    test_resolve_unresolved()
    test_move_near_stops_on_unknown()
    test_battle_round_unknown_blocks()
    test_unsent_unknown_does_not_block()
    test_side_doors_honour_gate()
    test_lease_hardening()
    test_load_requires_observable_change()
    failed = [item for item in RESULTS if not item[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
