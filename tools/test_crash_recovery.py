# -*- coding: utf-8 -*-
"""Offline fault-injection tests for the crash window: "command sent, result not persisted".

No game, no client, no network.  A fake agent provides the bridge files (results.log offset,
seq space, session/epoch identity) and a pre-written action_index.json reservation simulates a
crashed MCP process.  The invariant under test:

    every pending request is traceable, recoverable-queryable, never reported as success or
    failure without bridge evidence, and never re-executed while unproven.

Covered:
  F1  late OK reply after crash -> recovered `submitted` (accepted, effect not verified)
  F2  late ERR reply -> `rejected` (no effect claimed)
  F3  no reply -> needs_reconciliation; nothing is sent; not success, not failure
  F4  reply BEFORE the recorded results offset is ignored (stale reply of another attempt)
  F5  bridge session changed -> needs_reconciliation even if a matching seq line exists
  F6  engine epoch advanced -> needs_reconciliation
  F7  finished request -> deduplicated stored result
  F8  same-process in-flight -> request_pending, no reconcile
  F9  game_act reserves recovery metadata (seq/offset/session/epoch) before submitting
  F10 game_act on a crashed pending request recovers instead of resending
  F11 game_reconcile never sends a command in any of the above

Run: python -X utf8 tools/test_crash_recovery.py
Exit code 0 only when every check passes.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))
import mcp_server_v2 as M  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
DEAD_PID = 99999999


def check(name: str, condition, detail: str = '') -> None:
    RESULTS.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


class FakeRecoveryAgent:
    """Stand-in for VcmiAgent: bridge results/identity playbook + 'never sent' counter."""

    def __init__(self, results=None, session_id='session-1', epoch=1000.0,
                 next_seq=1, state=None):
        self._results = list(results or [])
        self.session_id = session_id
        self.epoch = epoch
        self.next_seq = next_seq
        self.sent: list[str] = []
        self._state = state or {'in_game': True, 'date': {'day': 5},
                                'resources': {'gold': 1000}, 'heroes': [], 'towns': [],
                                'objects': [], 'query': None,
                                'battle': {'active': False}, 'status': 'INGAME'}

    def results(self):
        return list(self._results)

    def peek_next_seq(self):
        return self.next_seq

    def bridge_identity(self):
        return {'session_id': self.session_id, 'epoch_started_at': self.epoch}

    def state(self, timeout=10.0):
        return json.loads(json.dumps(self._state))

    def execute(self, line, verify=None, result_timeout=20.0):
        self.sent.append(line)
        import types
        return types.SimpleNamespace(seq=len(self.sent), status='UNKNOWN', line='',
                                     detail='fake: no reply')


CALLS: list[str] = []


class Env:
    NAMES = ('STATE_DIR', 'SAVES_DIR', '_require_lease', '_agent', '_lease_token',
             '_RECONCILE_WAIT_S', '_STALE_PENDING_S')

    def __init__(self, agent: FakeRecoveryAgent):
        self.agent = agent
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def __enter__(self):
        self._saved = {name: getattr(M, name) for name in self.NAMES}
        M.STATE_DIR = self.root / 'state'
        M.STATE_DIR.mkdir(parents=True, exist_ok=True)
        M.SAVES_DIR = self.root / 'saves'
        M._require_lease = lambda: None
        M._agent = lambda: self.agent
        M._RECONCILE_WAIT_S = 0.15          # fast tests
        M._STALE_PENDING_S = 600.0
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            setattr(M, name, value)
        self.tmp.cleanup()

    def write_pending(self, request: str, arg_key: str, **extra) -> None:
        entry = {'key': arg_key, 'state': 'pending', 'ts': time.time() - 3600,
                 'pid': DEAD_PID}
        entry.update(extra)
        M._atomic_write_json(M._action_index_path(), {request: entry})


def arg_key(action='build', args=None) -> str:
    return json.dumps({'action': action, 'args': args or {'town': 0, 'id': 8}},
                      sort_keys=True, ensure_ascii=False)


def main() -> int:
    with Env(FakeRecoveryAgent(results=['line', 'line', 'seq=3 OK build 8'])) as env:
        env.write_pending('req-late-ok', arg_key(), expected_seq=3, results_offset=2,
                          action='build', args={'town': 0, 'id': 8},
                          session_id='session-1', epoch_started_at=1000.0)
        res = M.game_reconcile('req-late-ok')
        check('F1 late OK -> recovered submitted',
              res.get('resolved') is True and res['result'].get('status') == 'submitted'
              and res['result'].get('recovered') is True and res['result'].get('accepted') is True,
              str(res.get('verdict')))
        check('F11 no command sent during reconcile', env.agent.sent == [], str(env.agent.sent))

    with Env(FakeRecoveryAgent(results=['x', 'seq=3 ERR not_my_turn'])) as env:
        env.write_pending('req-late-err', arg_key(), expected_seq=3, results_offset=1,
                          action='build', args={'town': 0, 'id': 8},
                          session_id='session-1', epoch_started_at=1000.0)
        res = M.game_reconcile('req-late-err')
        check('F2 late ERR -> rejected (no success claim)',
              res.get('resolved') is True and res['result'].get('status') == 'rejected'
              and res['result'].get('ok') is False, str(res.get('verdict')))

    with Env(FakeRecoveryAgent(results=['x'])) as env:
        env.write_pending('req-no-reply', arg_key(), expected_seq=4, results_offset=1,
                          action='build', args={'town': 0, 'id': 8},
                          session_id='session-1', epoch_started_at=1000.0)
        res = M.game_reconcile('req-no-reply')
        check('F3 no reply -> needs_reconciliation',
              res.get('resolved') is False and res.get('error') == 'needs_reconciliation',
              str(res.get('verdict')))
        retry = M.game_act('build', {'town': 0, 'id': 8}, request_id='req-no-reply')
        check('F3b game_act with same id does not resend',
              retry.get('ok') is False and retry.get('error') == 'needs_reconciliation'
              and env.agent.sent == [], f"error={retry.get('error')} sent={env.agent.sent}")

    with Env(FakeRecoveryAgent(results=['seq=3 OK build 8', 'unrelated'])) as env:
        env.write_pending('req-pre-offset', arg_key(), expected_seq=3, results_offset=1,
                          action='build', args={'town': 0, 'id': 8},
                          session_id='session-1', epoch_started_at=1000.0)
        res = M.game_reconcile('req-pre-offset')
        check('F4 reply before recorded offset is ignored',
              res.get('resolved') is False and res.get('error') == 'needs_reconciliation',
              str(res.get('verdict')))

    with Env(FakeRecoveryAgent(results=['x', 'seq=3 OK build 8'],
                               session_id='session-2')) as env:
        env.write_pending('req-session', arg_key(), expected_seq=3, results_offset=1,
                          session_id='session-1', epoch_started_at=1000.0)
        res = M.game_reconcile('req-session')
        check('F5 bridge session change -> needs_reconciliation',
              res.get('resolved') is False
              and 'session changed' in str((res.get('verdict') or {}).get('reason', '')),
              str(res.get('verdict')))

    with Env(FakeRecoveryAgent(results=['x', 'seq=3 OK build 8'], epoch=5000.0)) as env:
        env.write_pending('req-epoch', arg_key(), expected_seq=3, results_offset=1,
                          session_id='session-1', epoch_started_at=1000.0)
        res = M.game_reconcile('req-epoch')
        check('F6 engine epoch change -> needs_reconciliation',
              res.get('resolved') is False
              and 'generation changed' in str((res.get('verdict') or {}).get('reason', '')),
              str(res.get('verdict')))

    with Env(FakeRecoveryAgent()) as env:
        M._atomic_write_json(M._action_index_path(), {
            'req-done': {'key': arg_key(), 'state': 'done', 'ts': time.time(),
                         'result': {'ok': True, 'status': 'completed', 'action_id': 'abc'}}})
        res = M.game_reconcile('req-done')
        check('F7 finished request -> deduplicated result',
              res.get('resolved') is True and res.get('deduplicated') is True
              and res['result'].get('action_id') == 'abc', str(res))

    with Env(FakeRecoveryAgent()) as env:
        env.write_pending('req-inflight', arg_key(), expected_seq=1, results_offset=0,
                          action='build', args={'town': 0, 'id': 8},
                          pid=__import__('os').getpid(), ts=time.time(),
                          session_id='session-1', epoch_started_at=1000.0)
        res = M.game_reconcile('req-inflight')
        check('F8 same-process in-flight -> request_pending',
              res.get('error') == 'request_pending', str(res.get('error')))

    with Env(FakeRecoveryAgent(next_seq=7, epoch=1000.0)) as env:
        env.agent._state['towns'] = [{'index': 0, 'name': 'T', 'built': [],
                                      'build_options': [], 'garrison': [],
                                      'dwellings': [], 'available_heroes': []}]
        # a successful build needs before/after states with the building appearing
        after = json.loads(json.dumps(env.agent._state))
        after['towns'][0]['built'] = [8]
        env.agent._state = json.loads(json.dumps(env.agent._state))
        seq = env.agent.next_seq
        env.write_pending('req-meta', arg_key(), **{'x': 0})   # will be replaced by reserve
        M._atomic_write_json(M._action_index_path(), {})
        env.agent.execute = lambda line, verify=None, result_timeout=20.0: (
            env.agent.sent.append(line) or __import__('types').SimpleNamespace(
                seq=seq, status='COMPLETED', line=f'seq={seq} OK build 8', detail=''))
        env.agent._state = after
        res = M.game_act('build', {'town': 0, 'id': 8}, request_id='req-meta')
        entry = M._load_action_index().get('req-meta') or {}
        check('F9 game_act reserves recovery metadata',
              res.get('action_id') is not None and entry.get('state') == 'done'
              and entry.get('expected_seq') == seq and entry.get('results_offset') == 0
              and entry.get('session_id') == 'session-1',
              f"{entry.get('expected_seq')} {entry.get('results_offset')} {res.get('status')}")

    with Env(FakeRecoveryAgent(results=['x', 'seq=2 OK build 8'])) as env:
        env.write_pending('req-recover', arg_key(), expected_seq=2, results_offset=1,
                          action='build', args={'town': 0, 'id': 8},
                          session_id='session-1', epoch_started_at=1000.0)
        res = M.game_act('build', {'town': 0, 'id': 8}, request_id='req-recover')
        check('F10 game_act recovers crashed pending instead of resending',
              res.get('recovered') is True and res.get('status') == 'submitted'
              and env.agent.sent == [], f"status={res.get('status')} sent={env.agent.sent}")

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
