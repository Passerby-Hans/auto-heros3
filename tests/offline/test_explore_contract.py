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
import threading
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mcp_server_v2 as M  # noqa: E402
from test_action_contract import Env, FakeAgent, bridge_ok, state  # noqa: E402

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


def _base_operation(opid: str) -> dict:
    return {'operation_id': opid, 'status': 'running', 'started_at': time.time(),
            'deadline': time.time() + 60, 'time_budget_s': 60, 'hero': 0,
            'target': [8, 8, 0], 'max_steps': 8, 'max_turns': 0,
            'attempts': [], 'sent_actions': [], 'refused_actions': [],
            'inflight_actions': [], 'steps_done': 0, 'moves': 0,
            'cancel_requested': False, 'result_status': None, 'error': None}


def test_operation_transactions_do_not_lose_updates() -> None:
    """F1: cancel/terminal updates and progress writes must be a locked transaction."""
    with Env():
        M._explore_op_write(_base_operation('txn-cancel'))
        progress = _base_operation('txn-cancel')
        progress['attempts'] = [{'action': 'move_hero', 'request_id': 'req-1',
                                 'sent': True, 'status': 'completed'}]
        entered, resume = threading.Event(), threading.Event()
        real = M._explore_op_write_locked

        def gated(operation):
            if threading.current_thread().name == 'progress-writer':
                entered.set()
                if not resume.wait(5):
                    raise RuntimeError('test barrier timeout')
            return real(operation)

        response = {}
        with mock.patch.object(M, '_explore_op_write_locked', gated):
            worker = threading.Thread(target=M._explore_op_write,
                                      args=(progress,), name='progress-writer')
            worker.start()
            assert entered.wait(5)
            cancel = threading.Thread(target=lambda: response.update(
                M.explore_cancel('txn-cancel')), name='cancel-writer')
            cancel.start()
            time.sleep(0.05)
            resume.set()
            worker.join(5)
            cancel.join(5)
        final = M._explore_op_read('txn-cancel')
        check('L12 cancel survives a concurrent progress write (sent actions preserved)',
              response.get('ok') is True and final.get('cancel_requested') is True
              and len(final.get('sent_actions') or []) == 1,
              json.dumps(final, ensure_ascii=False)[:300])

    with Env():
        M._explore_op_write(_base_operation('txn-terminal'))
        completed = _base_operation('txn-terminal')
        completed['status'] = 'completed'
        completed['attempts'] = [{'action': 'move_hero', 'request_id': 'req-2',
                                  'sent': True, 'status': 'completed'}]
        entered, resume = threading.Event(), threading.Event()
        real = M._explore_op_write_locked

        def gated(operation):
            if threading.current_thread().name == 'terminal-writer':
                entered.set()
                if not resume.wait(5):
                    raise RuntimeError('test barrier timeout')
            return real(operation)

        response = {}
        with mock.patch.object(M, '_explore_op_write_locked', gated):
            worker = threading.Thread(target=M._explore_op_write,
                                      args=(completed,), name='terminal-writer')
            worker.start()
            assert entered.wait(5)
            cancel = threading.Thread(target=lambda: response.update(
                M.explore_cancel('txn-terminal')), name='cancel-writer')
            cancel.start()
            time.sleep(0.05)
            resume.set()
            worker.join(5)
            cancel.join(5)
        final = M._explore_op_read('txn-terminal')
        check('L13 cancel cannot resurrect/blank a completed operation',
              response.get('ok') is False
              and response.get('error') == 'operation_not_running'
              and final.get('status') == 'completed'
              and len(final.get('sent_actions') or []) == 1,
              json.dumps(final, ensure_ascii=False)[:300])


def test_full_explorer_cancel_waits_for_inflight_action() -> None:
    """F1 end-to-end: cancel returns after the in-flight move, and no next move is sent."""
    with Env() as env:
        class WalkingAgent(FakeAgent):
            def __init__(self):
                super().__init__([])
                self.pos = (1, 1, 0)
                self.moves = []
                self.moved = threading.Event()

            def state(self, timeout: float = 10.0):
                return hero_state(self.pos)

            def execute(self, line: str, verify=None, result_timeout: float = 20.0):
                self.sent.append(line)
                if line.startswith('probe'):
                    suffix = ('1,1,0=1 2,1,0=1 3,1,0='
                              + ('0' if self.pos[0] == 1 else '1'))
                    return bridge_ok(len(self.sent), 'probe ' + suffix)
                words = line.split()
                assert words[0] == 'move_hero', line
                self.pos = tuple(map(int, words[2:5]))
                self.moves.append({'line': line, 'ts': time.time()})
                self.moved.set()
                return bridge_ok(len(self.sent), line)

        fake = env.agent(WalkingAgent())
        response, errors = {}, []

        def explore():
            try:
                response.update(M.explore_toward(0, 3, 1, map_w=8, map_h=8,
                                                 operation_id='full-race'))
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(repr(exc))

        worker = threading.Thread(target=explore, name='explorer')
        worker.start()
        assert fake.moved.wait(10)
        cancelled = M.explore_cancel('full-race')
        worker.join(10)
        final = M._explore_op_read('full-race')
        check('L14 a real explorer cannot start a second move after cancel returns',
              not errors and cancelled.get('ok') is True and len(fake.moves) == 1
              and final.get('cancel_requested') is True
              and final.get('status') == 'cancelled'
              and response.get('operation_status') == 'cancelled',
              f'errors={errors} moves={fake.moves} cancel={cancelled} '
              f'response={str(response)[:180]} op={str(final)[:180]}')
        check('L15 the in-flight move is recorded exactly once as sent',
              len(final.get('sent_actions') or []) == 1
              and (final.get('sent_actions') or [{}])[0].get('sent') is True,
              json.dumps(final.get('sent_actions'), ensure_ascii=False)[:300])


def test_crash_leaves_a_durable_attempt_record() -> None:
    """F2: the attempt/request_id is persisted before the bridge command is sent."""
    with Env() as env:
        class SimulatedProcessExit(BaseException):
            pass

        def crash(line: str) -> None:
            if not line.startswith('probe'):
                raise SimulatedProcessExit('process died after submit before receipt')

        env.agent(FakeAgent([hero_state()],
                            results=[bridge_ok(1, 'probe 2,1,0=1 turns=0 cost=100 len=1')],
                            on_execute=crash))
        try:
            M.explore_toward(0, 2, 1, map_w=8, map_h=8, operation_id='crashed-op')
        except SimulatedProcessExit:
            pass
        operation = M._explore_op_read('crashed-op') or {}
        attempts = operation.get('attempts') or []
        move_attempts = [item for item in attempts if item.get('action') == 'move_hero']
        cancelled = M.explore_cancel('crashed-op')
        check('L16 a crash after submission leaves the request_id in the operation record',
              bool(move_attempts) and move_attempts[0].get('request_id')
              and move_attempts[0].get('sent') is None
              and move_attempts[0] in (operation.get('inflight_actions') or []),
              json.dumps(operation, ensure_ascii=False)[:400])
        check('L17 cancel reports the in-flight/pending attempt for reconciliation',
              cancelled.get('ok') is True
              and any(item.get('request_id') == move_attempts[0].get('request_id')
                      for item in cancelled.get('inflight_actions') or []),
              json.dumps(cancelled, ensure_ascii=False)[:400])


def test_persistence_failure_is_reported_not_completed() -> None:
    """F3: a failed critical write must not report completed or allow silent re-execution."""
    with Env() as env:
        path = M._explore_op_path('persist-op')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.with_suffix(path.suffix + '.tmp').mkdir(parents=True, exist_ok=True)
        fake = env.agent(FakeAgent([hero_state()]))
        first = M.explore_toward(0, 1, 1, map_w=8, map_h=8, operation_id='persist-op')
        second = M.explore_toward(0, 1, 1, map_w=8, map_h=8, operation_id='persist-op')
        check('L18 create-persist failure returns persist_error and sends no game action',
              first.get('ok') is False and first.get('status') == 'operation_persist_error'
              and fake.sent == [], str(first)[:260])
        check('L19 the same id is not silently executed a second time after a persist failure',
              second.get('ok') is False
              and second.get('status') in ('operation_persist_error',)
              and not path.exists(), str(second)[:260])


def test_not_sent_is_not_counted_as_sent_actions() -> None:
    """F4: a pre-send refusal keeps sent=false/bridge_status and stays out of sent_actions."""
    with Env() as env:
        blocked = hero_state()
        blocked['query'] = {'id': 7, 'type': 'CHestQuery'}
        env.agent(FakeAgent([hero_state(), hero_state(), blocked],
                            results=[bridge_ok(1, 'probe 2,1,0=1 turns=0 cost=100 len=1')]))
        response = M.explore_toward(0, 2, 1, map_w=8, map_h=8, operation_id='notsent-op')
        operation = M._explore_op_read('notsent-op') or {}
        refused = operation.get('refused_actions') or []
        check('L20 a NOT_SENT precondition refusal is not in sent_actions',
              operation.get('sent_actions') == [] and len(refused) == 1,
              json.dumps(operation, ensure_ascii=False)[:400])
        check('L21 the refused attempt keeps sent=false/request_id/seq/bridge_status',
              refused and refused[0].get('sent') is False
              and refused[0].get('request_id') and refused[0].get('seq') is None
              and str(refused[0].get('bridge_status')).upper() in ('NOT_SENT', ''),
              json.dumps(refused, ensure_ascii=False)[:400])
        check('L22 the wrapper reports the refusal without claiming a completed explore',
              response.get('ok') is False
              and response.get('sent_actions') == [] and refused,
              str(response)[:260])


def test_post_submit_exception_keeps_submission_unknown() -> None:
    """R1: an exception after the real submit() must not be classified as 'not sent'."""
    from auto_heros3.runtime.vcmi_agent import VcmiAgent

    with Env() as env:
        agent = VcmiAgent(M._dir(), log=False)
        real_execute = agent.execute

        def execute(line, verify=None, result_timeout=20):
            if line.startswith('probe'):
                return bridge_ok(1, 'probe 2,1,0=1')
            return real_execute(line, verify=verify, result_timeout=result_timeout)

        def wait_error(*_args, **_kwargs):
            raise PermissionError('injected results.log read failure AFTER real submit')

        agent.execute = execute
        agent.wait_seq = wait_error
        agent.state = lambda timeout=10: hero_state()
        env.agent(agent)
        response = M.explore_toward(0, 2, 1, map_w=8, map_h=8,
                                    operation_id='post-submit-error')
        operation = M._explore_op_read('post-submit-error') or {}
        commands = ((M._dir() / 'commands.txt').read_text(encoding='utf-8').splitlines()
                    if (M._dir() / 'commands.txt').exists() else [])
        unresolved = M._load_unresolved()
        move_attempts = [item for item in operation.get('attempts') or []
                         if item.get('action') == 'move_hero']
        check('L23 a post-submit exception is not classified as refused/not-sent',
              response.get('status') == 'unknown_action'
              and not operation.get('refused_actions')
              and len(move_attempts) == 1
              and move_attempts[0].get('sent') is None,
              f"commands={commands} response={str(response)[:200]} "
              f"op={str(operation)[:300]}")
        check('L24 the submitted command is persisted and the unresolved gate is established',
              any(line.startswith('move_hero ') for line in commands)
              and bool(unresolved)
              and move_attempts[0] in (operation.get('inflight_actions') or []),
              f"commands={commands} unresolved={str(unresolved)[:180]}")


def test_legacy_b07_operation_is_preserved() -> None:
    """R2: a pre-B0.7.1 record has no attempts; migrating it must not erase its history."""
    with Env():
        legacy = {
            'operation_id': 'b07-existing', 'status': 'running',
            'started_at': time.time() - 10, 'deadline': time.time() + 100,
            'hero': 0, 'target': [9, 9, 0], 'max_steps': 8, 'max_turns': 0,
            'sent_actions': [{'action': 'move_hero', 'tile': [2, 1, 0],
                              'request_id': 'explore-move-prior',
                              'action_id': 'prior-id', 'seq': 10,
                              'status': 'completed', 'ts': time.time() - 9}],
            'steps_done': 1, 'moves': 1, 'cancel_requested': False,
            'result_status': None, 'error': None,
        }
        path = M._explore_op_path('b07-existing')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(legacy), encoding='utf-8')
        response = M.explore_cancel('b07-existing')
        after = M._explore_op_read('b07-existing') or {}
        preserved = after.get('sent_actions') or []
        migrated = after.get('attempts') or []
        check('L25 cancelling a legacy operation preserves request_id/action_id/seq',
              response.get('ok') is True and preserved
              and preserved[0].get('request_id') == 'explore-move-prior'
              and preserved[0].get('action_id') == 'prior-id'
              and preserved[0].get('seq') == 10,
              json.dumps(after, ensure_ascii=False)[:400])
        check('L26 legacy entries are migrated as unknown (sent=None), not assumed sent',
              len(migrated) == 1 and migrated[0].get('sent') is None
              and migrated[0].get('legacy') is True
              and migrated[0] in (after.get('inflight_actions') or []),
              json.dumps(after, ensure_ascii=False)[:400])


def main() -> int:
    test_interface()
    test_reached_completion_has_operation_record()
    test_overall_deadline_stops_before_next_action()
    test_cancel_stops_the_loop_without_new_action()
    test_operation_transactions_do_not_lose_updates()
    test_full_explorer_cancel_waits_for_inflight_action()
    test_crash_leaves_a_durable_attempt_record()
    test_persistence_failure_is_reported_not_completed()
    test_not_sent_is_not_counted_as_sent_actions()
    test_post_submit_exception_keeps_submission_unknown()
    test_legacy_b07_operation_is_preserved()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f'\n{passed}/{len(RESULTS)} checks passed')
    return 0 if passed == len(RESULTS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
