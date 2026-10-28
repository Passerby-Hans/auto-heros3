# -*- coding: utf-8 -*-
"""Tests for the P5 short-game findings (offline; reuses the fake harness).

Covered:
  F1  move_near: exact target refused (no_path) -> nearest neighbour accepted, one command each
  F2  move_near: all candidates refused -> rejected with attempts list (no infinite retries)
  F3  transfer town->hero while the hero is NOT visiting town -> rejected BEFORE sending
  F4  transfer town->hero while the hero is in town -> command sent and verified
  F5  game_brief.pending_action ignores an OLD submitted entry (noise fix)
  F6  game_brief.pending_action reports a RECENT submitted entry with its age
  F7  confirm_deployment still in deployment -> evidence hints at battle_start()
  F8  battle_round reports accepted_orders (ok stays completed-only)

Run: python -X utf8 tools/test_p5_fixes.py
"""
from __future__ import annotations

import json
import sys
import time
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


def hero(index: int, pos, army=None) -> dict:
    return {'index': index, 'name': f'Hero{index}', 'visitable': list(pos),
            'movement': 1000, 'army': list(army or [])}


def town(index: int, garrison=None) -> dict:
    return {'index': index, 'id': 100 + index, 'name': f'Town{index}',
            'built': [], 'garrison': list(garrison or []), 'dwellings': [],
            'build_options': [], 'available_heroes': []}


def town_object(index: int, pos) -> dict:
    return {'id': 100 + index, 'kind': 'town', 'name': f'Town{index}',
            'visitable': list(pos) + [0]}


def army(count: int, slot: int = 0) -> list:
    return [{'slot': slot, 'creature': 1, 'count': count}]


def total(stacks) -> int:
    return sum(int(stack.get('count') or 0) for stack in stacks or [])


def test_move_near() -> None:
    start = T.state(heroes=[hero(0, (1, 1))])
    moved = T.state(heroes=[hero(0, (2, 0))])
    with T.Env() as env:
        fake = env.agent(T.FakeAgent([start, start, start, moved],
                                     [T.bridge_err(1, 'no_path to (2 1)'),
                                      T.bridge_ok(2, 'move_hero')]))
        res = M.move_near(0, 2, 1, radius=1)
        check('F1 move_near falls back to a neighbour', res.get('ok') is True
              and res.get('target') == [2, 0] and len(fake.sent) == 2,
              f"target={res.get('target')} sent={fake.sent}")
        targets = [attempt.get('target') for attempt in res.get('attempts') or []]
        check('F1b attempts list is complete and ordered',
              targets == [[2, 1], [2, 0]], str(targets))

    with T.Env() as env:
        fake = env.agent(T.FakeAgent([start, start], [T.bridge_err(3, 'no_path')]))
        res = M.move_near(0, 2, 1, radius=0)
        check('F2 radius 0 issues exactly one attempt',
              res.get('ok') is False and res.get('status') == 'rejected'
              and len(fake.sent) == 1 and len(res.get('attempts') or []) == 1,
              f"sent={fake.sent} attempts={res.get('attempts')}")


def test_transfer_precondition() -> None:
    outside = T.state(heroes=[hero(0, (6, 8), army(4))],
                      towns=[town(0, garrison=army(10))])
    outside['objects'] = [town_object(0, (7, 5))]
    inside = T.state(heroes=[hero(0, (7, 5), army(4))],
                     towns=[town(0, garrison=army(10))])
    inside['objects'] = [town_object(0, (7, 5))]
    after = T.state(heroes=[hero(0, (7, 5), army(14))],
                    towns=[town(0, garrison=army(0))])
    after['objects'] = [town_object(0, (7, 5))]

    transfer_args = {'src': 'town', 'src_index': 0, 'src_slot': 0,
                     'dst': 'hero', 'dst_index': 0, 'dst_slot': 1}

    with T.Env() as env:
        fake = env.agent(T.FakeAgent([outside, outside]))
        res = M.game_act('transfer', dict(transfer_args))
        check('F3 hero outside town -> rejected before sending',
              res.get('ok') is False and res.get('sent') is False
              and 'not visiting town' in str(res.get('evidence')) and fake.sent == [],
              f"evidence={res.get('evidence')} sent={fake.sent}")

    with T.Env() as env:
        fake = env.agent(T.FakeAgent([inside, after]))
        res = M.game_act('transfer', dict(transfer_args))
        check('F4 hero in town -> sent and verified',
              res.get('status') == 'completed' and len(fake.sent) == 1
              and fake.sent[0].startswith('transfer '),
              f"status={res.get('status')} sent={fake.sent}")


def write_ledger(env: T.Env, entries: list) -> None:
    path = M._dir() / 'actions.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + '\n')


def test_pending_action_noise() -> None:
    stamp_old = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(time.time() - 3600))
    stamp_new = time.strftime('%Y-%m-%dT%H:%M:%S')
    with T.Env() as env:
        env.agent(T.FakeAgent([T.state()]))
        write_ledger(env, [{'ts': stamp_old, 'action': 'confirm_deployment',
                            'status': 'submitted', 'evidence': 'old', 'action_id': 'old1'}])
        brief = M.game_brief()
        check('F5 old submitted entry is not surfaced',
              'pending_action' not in brief, str(brief.get('pending_action')))

        write_ledger(env, [{'ts': stamp_new, 'action': 'move_hero', 'status': 'submitted',
                            'evidence': 'moving', 'action_id': 'new1'}])
        brief = M.game_brief()
        pending = brief.get('pending_action') or {}
        check('F6 recent submitted entry surfaced with age',
              pending.get('action_id') == 'new1' and isinstance(pending.get('age_s'), float),
              str(pending))


def test_confirm_deployment_hint() -> None:
    deploy = T.state()
    deploy['battle'] = {'active': -1, 'my_side': 0, 'active_stack': -1,
                        'stacks': [T.make_stack(0, 0, 10, [1, 1])]}
    with T.Env() as env:
        env.agent(T.FakeAgent([deploy, deploy], [T.bridge_ok(1, 'key return')]))
        res = M.game_act('confirm_deployment')
        check('F7 deployment pending -> hint points at battle_start',
              res.get('status') == 'submitted' and 'battle_start' in str(res.get('evidence')),
              str(res.get('evidence')))


def test_battle_round_accepted_orders() -> None:
    battle = {'active': 1, 'my_side': 0, 'active_stack': 0,
              'stacks': [T.make_stack(0, 0, 10, [1, 1]), T.make_stack(3, 1, 8, [15, 2])]}
    state = T.state()
    state['battle'] = battle
    with T.Env() as env:
        env.agent(T.FakeAgent([state, state, state],
                              [T.bridge_ok(1, 'battle_attack 3 melee')]))
        res = M.battle_round(target=3)
        check('F8 battle_round exposes accepted_orders', res.get('accepted_orders') == 1
              and res.get('ok') is False and res.get('status') == 'submitted',
              f"accepted={res.get('accepted_orders')} ok={res.get('ok')} status={res.get('status')}")


def test_click_action() -> None:
    with T.Env() as env:
        fake = env.agent(T.FakeAgent([T.state()]))
        res = M.game_act('click', {'x': 601, 'y': 683})
        check('F9 click action forwarded to the bridge (UI dialogs)',
              fake.sent == ['click 601 683'] and res.get('status') in ('submitted', 'completed'),
              f"sent={fake.sent} status={res.get('status')}")
        invalid = M.game_act('click', {'x': -5, 'y': 7000})
        check('F9b click coordinates validated before sending',
              invalid.get('ok') is False and invalid.get('sent') is False
              and fake.sent == ['click 601 683'], str(invalid.get('error')))


def test_ui_tree_tool() -> None:
    tree = {'windows': [
        {'root': {'cls': 'class AdventureMapInterface', 'x': 0, 'y': 0, 'w': 966, 'h': 724,
                  'children': []}},
        {'root': {'cls': 'class CShipyardWindow', 'x': 319, 'y': 168, 'w': 329, 'h': 388,
                  'children': [
                      {'cls': 'class CButton', 'x': 543, 'y': 480, 'w': 64, 'h': 30,
                       'children': []},
                      {'cls': 'class CButton', 'x': 361, 'y': 480, 'w': 64, 'h': 30,
                       'children': []},
                  ]}},
    ]}
    with T.Env() as env:
        fake = env.agent(T.FakeAgent([T.state()]))
        (M._dir() / 'ui_tree.json').write_text(json.dumps(tree), encoding='utf-8')
        res = M.ui_tree()
        windows = {window['cls']: window for window in res.get('windows') or []}
        shipyard = windows.get('class CShipyardWindow') or {}
        buttons = [(btn['x'], btn['y']) for btn in shipyard.get('buttons') or []]
        check('F10 ui_tree returns windows and button coordinates',
              res.get('ok') is True and buttons == [(543, 480), (361, 480)]
              and fake.sent == ['ui_tree'],
              f"buttons={buttons} sent={fake.sent}")


def main() -> int:
    test_move_near()
    test_transfer_precondition()
    test_pending_action_noise()
    test_confirm_deployment_hint()
    test_battle_round_accepted_orders()
    test_click_action()
    test_ui_tree_tool()
    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
