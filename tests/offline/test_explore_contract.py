# -*- coding: utf-8 -*-
"""B0.7 explore operation contract: identity, wall-clock deadline, cancel, no-retry stop.

The explorer is a long-running composite; these offline tests use the shared FakeAgent/Env
fixtures (no client) to prove the MCP wrapper's operation contract, not the FogExplorer BFS
itself (that is covered by test_fog_path.py).
"""
from __future__ import annotations

import inspect
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mcp_server_v2 as M  # noqa: E402
from test_action_contract import Env, FakeAgent, state  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition, detail: str = '') -> None:
    RESULTS.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


def hero_state(pos=(1, 1, 0)) -> dict:
    return state(heroes=[{'index': 0, 'name': 'Hero', 'pos': list(pos),
                          'visitable': list(pos), 'movement': 1000, 'army': []}], towns=[])


def op_path(operation_id: str) -> Path:
    return M._dir() / 'explore_operations' / f'{operation_id}.json'


def test_interface() -> None:
    params = inspect.signature(M.explore_toward).parameters
    check('L1 explore_toward exposes operation_id/time_budget_s',
          'operation_id' in params and 'time_budget_s' in params,
          str(list(params)))
    check('L2 explore_cancel is a callable MCP tool',
          hasattr(M, 'explore_cancel') and callable(M.explore_cancel))
    bad = M.explore_toward(hero=0, x=5, y=5, operation_id='bad/../id', map_w=8, map_h=8)
    check('L3 invalid operation_id is rejected before any state access',
          bad.get('ok') is False and 'operation_id' in str(bad.get('error')),
          str(bad)[:160])


def test_reached_completion_has_operation_record() -> None:
    with Env() as env:
        env.agent(FakeAgent([hero_state()]))
        before = set(p.name for p in (M._dir() / 'explore_operations').glob('*.json')) \
            if (M._dir() / 'explore_operations').exists() else set()
        res = M.explore_toward(hero=0, x=1, y=1, z=0, map_w=8, map_h=8,
                               operation_id='reached-op', time_budget_s=30)
        check('L4 direct reached completion is reported with an operation id',
              res.get('ok') is True and res.get('status') == 'reached'
              and res.get('operation_status') == 'completed'
              and res.get('operation_id') == 'reached-op',
              str(res)[:200])
        after = op_path('reached-op')
        data = json.loads(after.read_text(encoding='utf-8')) if after.exists() else {}
        check('L5 operation record persists status/deadline/owner inputs',
              data.get('status') == 'completed' and data.get('deadline') > data.get('started_at')
              and data.get('target') == [1, 1, 0] and data.get('sent_actions') == [],
              str(data)[:220])
        reused = M.explore_toward(hero=0, x=1, y=1, z=0, map_w=8, map_h=8,
                                  operation_id='reached-op')
        check('L6 reusing a finished operation id is refused (no silent resume)',
              reused.get('ok') is False and reused.get('status') == 'operation_id_exists',
              str(reused)[:160])


def test_overall_deadline_stops_before_next_action() -> None:
    with Env() as env:
        fake = env.agent(FakeAgent([hero_state()], on_execute=lambda _line: time.sleep(0.08)))
        res = M.explore_toward(hero=0, x=6, y=6, z=0, map_w=8, map_h=8,
                               operation_id='timeout-op', time_budget_s=0.03)
        data = json.loads(op_path('timeout-op').read_text(encoding='utf-8'))
        check('L7 the overall wall-clock deadline stops with status=timed_out',
              res.get('ok') is False and res.get('status') == 'timed_out'
              and data.get('status') == 'timed_out',
              f"res={str(res)[:160]} op={str(data)[:120]}")
        check('L8 a timeout does not record or resend a probe as a game action',
              res.get('sent_actions') == [] and data.get('sent_actions') == []
              and fake.sent and all('probe' in line for line in fake.sent),
              f"sent={fake.sent}")


def test_cancel_stops_the_loop_without_new_action() -> None:
    with Env() as env:
        def cancel_during_probe(_line: str) -> None:
            M.explore_cancel('cancel-op')
        fake = env.agent(FakeAgent([hero_state()], on_execute=cancel_during_probe))
        res = M.explore_toward(hero=0, x=6, y=6, z=0, map_w=8, map_h=8,
                               operation_id='cancel-op', time_budget_s=30)
        data = json.loads(op_path('cancel-op').read_text(encoding='utf-8'))
        check('L9 explore_cancel stops before the next action',
              res.get('ok') is False and res.get('status') == 'cancelled'
              and data.get('status') == 'cancelled'
              and res.get('sent_actions') == [],
              f"res={str(res)[:180]} op={str(data)[:120]}")
        check('L10 cancel is checked before any move is issued',
              all('probe' in line for line in fake.sent), str(fake.sent))
        again = M.explore_cancel('cancel-op')
        check('L11 cancelling a finished operation is an explicit no-op error',
              again.get('ok') is False and again.get('error') == 'operation_not_running',
              str(again)[:160])


def main() -> int:
    test_interface()
    test_reached_completion_has_operation_record()
    test_overall_deadline_stops_before_next_action()
    test_cancel_stops_the_loop_without_new_action()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f'\n{passed}/{len(RESULTS)} checks passed')
    return 0 if passed == len(RESULTS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
