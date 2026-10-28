# -*- coding: utf-8 -*-
"""P4 hardening tests (offline): reconnect/lease, recording management, load confirmation.

Reuses the isolated fake harness from test_action_contract (imported as a module; its main()
does not run on import).  No game/client/network is touched: recording is exercised with a
fake Popen so no encoder is started.

Covered:
  P4-1  recording singleton (in-memory live recorder) -> recording_active
  P4-2  recording singleton detected from <dir>/recording.json after "MCP restart"
  P4-3  stale recording record (dead pid) does not block a new recording
  P4-4  recording_stop recovers after an MCP restart via recording.json (STOP + meta read)
  P4-5  recording_stop reports a missing output dir instead of claiming success
  P4-6  recording_status reads the persistent state file
  P4-7  battle_start refuses when there is no battle / battle already running
  P4-8  battle_start clicks the deployment button found in ui_tree and verifies the phase
  P4-9  load_game: identical state + old slot -> unknown (not completed)
  P4-10 lease token lost after restart -> action refused until lease_acquire

Run: python -X utf8 tools/test_p4_hardening.py
Exit code 0 only when every check passes.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))
import mcp_server_v2 as M  # noqa: E402
import test_action_contract as T  # noqa: E402  (fake harness only)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition, detail: str = '') -> None:
    RESULTS.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


class FakeProc:
    def __init__(self, *args, **kwargs):
        self.pid = os.getpid()
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        self._alive = False
        return 0

    def kill(self):
        self._alive = False


def bus(battle):
    st = T.state()
    st['battle'] = battle
    return st


def main() -> int:
    # mcp_server_v2 imports subprocess lazily inside functions; patch the shared module object.
    import subprocess as sp
    original_popen = sp.Popen

    # ---------------- recording singleton / recovery (fake recorder process) -------------
    with T.Env() as env:
        M.ROOT = env.root / 'root'
        (M.ROOT / 'tools').mkdir(parents=True, exist_ok=True)
        sp.Popen = lambda *a, **k: FakeProc(*a, **k)
        try:
            first = M.recording_start(fps=5, height=768)
            check('P4-1 recording_start spawns once', first.get('ok') is True, str(first))
            second = M.recording_start(fps=5, height=768)
            check('P4-1b second recording_start -> recording_active',
                  second.get('ok') is False and second.get('error') == 'recording_active',
                  str(second.get('error')))
            state = M._read_recording_state()
            check('P4-1c persistent recording.json written',
                  state.get('pid') == os.getpid() and state.get('dir') == first.get('dir'),
                  str(state))
            # simulate MCP restart: drop the in-memory handle but keep the state file
            M._recorder.update({'proc': None, 'dir': None})
            third = M.recording_start(fps=5, height=768)
            check('P4-2 restart sees live recorder -> recording_active',
                  third.get('error') == 'recording_active', str(third))
            status = M.recording_status()
            check('P4-6 recording_status reads persistent state',
                  status.get('recording') is True and status.get('state_file'), str(status))
            # P4-4 recovery: state file points at a *live* dummy recorder + pre-written meta;
            # recording_stop must finalize it even though this process lost its handle.
            out_dir = env.root / 'recover-recording'
            out_dir.mkdir(parents=True, exist_ok=True)
            dummy = original_popen([sys.executable, '-c', 'import time; time.sleep(30)'])
            try:
                M._atomic_write_json(M._recording_state_path(),
                                     {'pid': dummy.pid, 'dir': str(out_dir),
                                      'started': time.time()})
                (out_dir / 'meta.json').write_text(
                    json.dumps({'ok': True, 'frames': 42, 'bytes': 1234,
                                'video': str(out_dir / 'video.mp4'),
                                'fps_target': 5, 'canvas': '1366x768'}), encoding='utf-8')
                stopped = M.recording_stop()
                check('P4-4 recording_stop recovers via recording.json',
                      stopped.get('recovered') is True and stopped.get('frames') == 42
                      and (out_dir / 'STOP').exists(), str(stopped))
            finally:
                dummy.kill()
            check('P4-4b recording state cleared after stop',
                  not M._recording_state_path().exists(), '')
            # stale record of a dead recorder must not block a new one
            dead_dir = env.root / 'dead-recording'
            dead_dir.mkdir(parents=True, exist_ok=True)
            M._atomic_write_json(M._recording_state_path(),
                                 {'pid': 99999999, 'dir': str(dead_dir), 'started': 0})
            fresh = M.recording_start(fps=5, height=768)
            check('P4-3 stale (dead) recording record does not block',
                  fresh.get('ok') is True, str(fresh))
            M.recording_stop()
        finally:
            sp.Popen = original_popen
            M._recorder.update({'proc': None, 'dir': None})

    with T.Env() as env:
        M._atomic_write_json(M._recording_state_path(),
                             {'pid': 99999999, 'dir': str(env.root / 'missing'), 'started': 0})
        res = M.recording_stop()
        check('P4-5 missing recording dir -> explicit error',
              res.get('ok') is False and res.get('error') == 'recording_dir_missing',
              str(res.get('error')))
        check('P4-5b stuck recording state cleaned', not M._recording_state_path().exists(), '')

    # ---------------- battle_start ------------------------------------------------------
    with T.Env() as env:
        no_battle = T.FakeAgent([T.state()])
        env.agent(no_battle)
        res = M.battle_start()
        check('P4-7 no battle -> no_battle',
              res.get('ok') is False and res.get('status') == 'no_battle', str(res))
        running_battle = {'active': 2, 'my_side': 0, 'active_stack': 2,
                          'stacks': [T.make_stack(0, 0, 10, [1, 1])]}
        env.agent(T.FakeAgent([bus(running_battle)]))
        res = M.battle_start()
        check('P4-7b already running -> running (no click)',
              res.get('ok') is True and res.get('status') == 'running', str(res))

        deployment = {'active': -1, 'my_side': 0, 'active_stack': -1,
                      'stacks': [T.make_stack(0, 0, 10, [1, 1])]}
        started = {'active': 2, 'my_side': 0, 'active_stack': 2,
                   'stacks': [T.make_stack(0, 0, 10, [1, 1])]}
        fake = env.agent(T.FakeAgent([bus(deployment), bus(deployment), bus(started)]))
        tree = {'windows': [
            {'root': {'cls': 'class AdventureMapInterface', 'x': 0, 'y': 0, 'w': 966,
                      'h': 724, 'children': []}},
            {'root': {'cls': 'class BattleWindow', 'x': 83, 'y': 20, 'w': 800, 'h': 685,
                      'children': [
                          {'cls': 'class CButton', 'x': 296, 'y': 665, 'w': 198, 'h': 36,
                           'children': []},
                          {'cls': 'class CButton', 'x': 502, 'y': 665, 'w': 198, 'h': 36,
                           'children': []},
                          {'cls': 'class CLabel', 'x': 10, 'y': 10, 'w': 0, 'h': 0,
                           'children': []},
                      ]}}]}
        (M._dir() / 'ui_tree.json').write_text(json.dumps(tree), encoding='utf-8')
        res = M.battle_start()
        check('P4-8 battle_start clicks the deployment button',
              res.get('ok') is True and res.get('status') == 'completed'
              and any('click 601 683' in line for line in fake.sent),
              f"attempts={res.get('attempts')} sent={fake.sent}")

    # ---------------- load confirmation: identical state + old slot -> unknown -----------
    with T.Env() as env:
        slot = M._save_path('p4-test')
        slot.parent.mkdir(parents=True, exist_ok=True)
        slot.write_bytes(b'fake save payload' * 10)
        M._atomic_write_json(M._saves_meta_path(), {
            'p4-test': {'slot': 'Saves/agent/p4-test', 'day': 1,
                        'saved_at': '2020-01-01 00:00:00'}})
        same = T.state(day=1)
        env.agent(T.FakeAgent([same, same]))
        res = M.load_game('p4-test')
        check('P4-9 identical state + old slot -> unknown (not completed)',
              res.get('ok') is False and res.get('status') == 'unknown'
              and 'no observable state change' in str(res.get('evidence')),
              str(res.get('evidence')))

    # ---------------- reconnect: lease token lost after restart -------------------------
    with T.Env() as env:
        fake = env.agent(T.FakeAgent([T.state(), T.state()]))
        def no_lease():
            raise RuntimeError('no lease: call lease_acquire first')
        M._require_lease = no_lease
        res = M.game_act('end_turn')
        check('P4-10 action refused without a lease after restart',
              res.get('ok') is False and 'lease' in str(res.get('error')) and fake.sent == [],
              str(res.get('error')))
        M._require_lease = lambda: None
        res2 = M.game_act('end_turn')
        check('P4-10b action works after lease_acquire',
              res2.get('status') in ('completed', 'submitted'), str(res2.get('status')))

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
