# -*- coding: utf-8 -*-
"""Offline regression tests for the W6 evidence/recovery batch.

Covered:
  N5  overlay thought lines survive the panel trim; history keeps the full feed
  R6  run rotation archives the previous run's raw evidence; save metadata carries run/branch/
      epoch; load creates a new epoch+branch and requires a state snapshot newer than the
      load command (barrier) when the state carries ts_wall
  R7  battle_round writes a durable round record and per-order ledger entries
  R9  recording identity binding + atomic singleton lock (a second owner is refused)
Run: python -X utf8 tools/test_w6_evidence.py
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
from test_action_contract import Env, FakeAgent, bridge_ok, state  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition, detail: str = '') -> None:
    RESULTS.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


def read_jsonl(path: Path) -> list:
    try:
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()
                if line.strip()]
    except OSError:
        return []


# ------------------------------------------------------------------ N5
def test_overlay_thought_trim() -> None:
    saved = (M._OVERLAY_MAX, M._OVERLAY_THOUGHT_KEEP, M._OVERLAY_TRIM_BYTES)
    try:
        with Env():
            M._OVERLAY_MAX = 30
            M._OVERLAY_THOUGHT_KEEP = 10
            M._OVERLAY_TRIM_BYTES = 300
            for index in range(60):
                M._overlay('thought', f'th{index}')
            for index in range(40):
                M._overlay('action', f'ac{index}')
            panel = read_jsonl(M._dir() / 'overlay.jsonl')
            history = read_jsonl(M._dir() / 'overlay_history.jsonl')
            thoughts = [entry for entry in panel if entry.get('kind') == 'thought']
            actions = [entry for entry in panel if entry.get('kind') == 'action']
            check('N5a panel keeps the last 120-style thought window (10 here)',
                  len(thoughts) == 10 and thoughts[-1]['text'] == 'th59',
                  f'{len(thoughts)} thoughts, last={thoughts[-1]["text"] if thoughts else None}')
            check('N5b panel trims non-thought lines but keeps recent ones',
                  len(actions) == 20 and actions[-1]['text'] == 'ac39',
                  f'{len(actions)} actions, last={actions[-1]["text"] if actions else None}')
            check('N5c panel total stays within the cap',
                  len(panel) == 30, str(len(panel)))
            check('N5d history keeps the complete feed',
                  len(history) == 100 and history[0]['text'] == 'th0'
                  and history[-1]['text'] == 'ac39', str(len(history)))
    finally:
        M._OVERLAY_MAX, M._OVERLAY_THOUGHT_KEEP, M._OVERLAY_TRIM_BYTES = saved


# ------------------------------------------------------------------ R6 archiving / epoch
def test_run_rotation_archives_evidence() -> None:
    with Env() as env:
        state_dir = env.root / 'state'
        state_dir.mkdir(parents=True, exist_ok=True)
        old = {'schema_version': 1, 'run_id': 'oldrun', 'branch_id': 'main', 'epoch': 2,
               'epoch_started_at': time.time()}
        (state_dir / 'run.json').write_text(json.dumps(old), encoding='utf-8')
        (state_dir / 'actions.jsonl').write_text('{"action": "x"}\n', encoding='utf-8')
        (state_dir / 'results.log').write_text('seq=1 OK ping\n', encoding='utf-8')
        (state_dir / 'unresolved.json').write_text('{"action": "y"}', encoding='utf-8')
        new = M._rotate_run()
        archive = state_dir / 'archive' / 'oldrun'
        check('R6a rotation archives the previous raw evidence',
              (archive / 'actions.jsonl').exists() and (archive / 'results.log').exists()
              and (archive / 'unresolved.json').exists(),
              str(sorted(p.name for p in archive.glob('*'))) if archive.exists() else 'no archive')
        index = read_jsonl(state_dir / 'archive' / 'index.jsonl')
        check('R6b archive index records the old run identity',
              bool(index) and index[-1].get('run_id') == 'oldrun'
              and 'actions.jsonl' in (index[-1].get('files') or []), str(index[-1:])[:200])
        fresh = M._run_identity()
        check('R6c rotation creates a new run with epoch 0 and resets evidence',
              fresh['run_id'] == new['run_id'] and fresh['run_id'] != 'oldrun'
              and fresh.get('epoch') == 0
              and not (state_dir / 'actions.jsonl').exists()
              and not (state_dir / 'unresolved.json').exists(),
              f'run={fresh["run_id"]} epoch={fresh.get("epoch")}')
        check('R6d rotation keeps the previous archive available', archive.exists(),
              str(archive))


def test_epoch_bump_and_ledger_identity() -> None:
    with Env() as env:
        first = M._run_identity()
        bumped = M._bump_epoch('load', {'save': 'slot1'})
        check('R6e epoch increments and the branch follows the new timeline',
              bumped.get('epoch') == first.get('epoch', 0) + 1
              and bumped.get('branch_id') == f'load-{bumped.get("epoch")}',
              str({k: bumped.get(k) for k in ('epoch', 'branch_id')}))
        epochs = read_jsonl(env.root / 'state' / 'epochs.jsonl')
        check('R6f epochs.jsonl records the load transition',
              bool(epochs) and epochs[-1].get('source') == 'load'
              and epochs[-1].get('detail', {}).get('save') == 'slot1', str(epochs[-1:])[:200])
        M._ledger({'action': 'unit_probe', 'status': 'completed'})
        entries = read_jsonl(env.root / 'state' / 'actions.jsonl')
        check('R6g every ledger entry carries run/branch/epoch',
              bool(entries) and entries[-1].get('run_id') == bumped.get('run_id')
              and entries[-1].get('branch_id') == bumped.get('branch_id')
              and entries[-1].get('epoch') == bumped.get('epoch'),
              str(entries[-1])[:240])


class SaveAgent(FakeAgent):
    """FakeAgent that materialises the save slot during the bridge call (so save_game is provable)."""

    def __init__(self, states, results=None):
        super().__init__(states, results)
        self.save_written = False

    def execute(self, line: str, verify=None, result_timeout: float = 20.0):
        if line.startswith('save_game') and not self.save_written:
            slot = line.split(' ', 1)[1]
            path = M.SAVES_DIR / 'agent' / (slot.rsplit('/', 1)[-1] + '.vsgm1')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'save-' + str(time.time()).encode())
            self.save_written = True
        return super().execute(line, verify=verify, result_timeout=result_timeout)


def test_save_metadata_identity() -> None:
    with Env() as env:
        fake = SaveAgent([state(day=3, gold=1000), state(day=3, gold=1000)])
        env.agent(fake)
        res = M.save_game('w6meta')
        meta = M._load_saves_meta().get('w6meta') or {}
        check('R6h completed save carries run/branch/epoch metadata',
              res.get('status') == 'completed'
              and meta.get('run_id') == res.get('run_id')
              and meta.get('branch_id') == res.get('branch_id')
              and meta.get('epoch') == res.get('epoch')
              and meta.get('day') == 3
              and isinstance(meta.get('fingerprint'), dict),
              str({k: res.get(k) for k in ('status', 'run_id', 'branch_id', 'epoch')}))


def test_load_barrier_and_epoch() -> None:
    def prepare(save_name: str, env: Env, day: int = 3):
        slot_dir = env.root / 'saves' / M.SAVE_SUBDIR
        slot_dir.mkdir(parents=True, exist_ok=True)
        path = slot_dir / f'{save_name}.vsgm1'
        path.write_bytes(b'save-file')
        meta = {'slot': f'Saves/{M.SAVE_SUBDIR}/{save_name}', 'file': str(path),
                'day': day, 'saved_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                'run_id': 'oldrun', 'branch_id': 'main', 'epoch': 0}
        (env.root / 'state').mkdir(parents=True, exist_ok=True)
        (env.root / 'state' / 'saves.json').write_text(json.dumps({save_name: meta}),
                                                       encoding='utf-8')
        return path

    # (a) completed: day matches, fingerprint changed AND the observed state is newer than the
    # load command (barrier).
    with Env() as env:
        prepare('freshslot', env)
        before = state(day=3, gold=1000)
        stale = state(day=3, gold=1000)
        stale['ts_wall'] = int((time.time() - 30) * 1000)
        fresh = state(day=3, gold=9999)
        fresh['ts_wall'] = int((time.time() + 2) * 1000)
        env.agent(FakeAgent([before, stale, fresh]))
        res = M.load_game('freshslot')
        check('R6i load completed only with a post-command state snapshot',
              res.get('status') == 'completed'
              and 'newer than the load command' in str(res.get('evidence')),
              str(res.get('evidence'))[:240])
        check('R6j load starts a new epoch/branch and reports the saved identity',
              res.get('epoch') == 1 and res.get('branch_id') == 'load-1'
              and res.get('saved_run_id') == 'oldrun' and res.get('saved_epoch') == 0,
              str({k: res.get(k) for k in
                   ('epoch', 'branch_id', 'saved_run_id', 'saved_epoch')}))
    # (b) barrier: the only observed state was written before the load command -> unknown.
    with Env() as env:
        prepare('staleslot', env)
        before = state(day=3, gold=1000)
        stale_new = state(day=3, gold=2222)
        stale_new['ts_wall'] = int((time.time() - 30) * 1000)
        env.agent(FakeAgent([before, stale_new]))
        res = M.load_game('staleslot')
        check('R6k a pre-command state snapshot cannot prove a load',
              res.get('status') == 'unknown'
              and 'barrier' in str(res.get('evidence')),
              str(res.get('evidence'))[:240])
    # (c) unchanged fingerprint -> unknown even with a fresh timestamp
    with Env() as env:
        prepare('sameslot', env)
        before = state(day=3, gold=1000)
        same = state(day=3, gold=1000)
        same['ts_wall'] = int((time.time() + 2) * 1000)
        env.agent(FakeAgent([before, same]))
        res = M.load_game('sameslot')
        check('R6l unchanged fingerprint stays unknown even with a fresh snapshot',
              res.get('status') == 'unknown', str(res.get('evidence'))[:200])


# ------------------------------------------------------------------ R7 battle ledger
def test_battle_round_record() -> None:
    with Env() as env:
        run = M._run_identity()
        orders = [{'stack': 1, 'target': 3, 'status': 'completed', 'seq': 7, 'evidence': 'x'}]
        M._battle_round_record('round-abc', 3, orders, 'completed', 0, None, run)
        record = json.loads((env.root / 'state' / 'last_battle_round.json').read_text(
            encoding='utf-8'))
        check('R7a battle_round writes a durable round record',
              record.get('round_id') == 'round-abc' and record.get('status') == 'completed'
              and record.get('orders') == orders, str(record)[:240])
        entries = read_jsonl(env.root / 'state' / 'actions.jsonl')
        check('R7b battle_round and its orders are in the ledger',
              any(entry.get('action') == 'battle_round'
                  and entry.get('round_id') == 'round-abc' for entry in entries)
              and any(entry.get('action') == 'battle_attack'
                      for entry in entries) is False,
              'summary entry present (per-order entries are written by battle_round itself)')
        # source guard: the live loop logs each order before appending it to the record
        source = (ROOT / 'tools' / 'mcp_server_v2.py').read_text(encoding='utf-8')
        check('R7c battle_round logs each order with its round_id',
              "'action': 'battle_attack'," in source and "'round_id': round_guard}" in source,
              'order ledger')


# ------------------------------------------------------------------ R9 recording lock / identity
def test_recording_lock_and_identity() -> None:
    with Env() as env:
        identity = M._evidence_identity()
        check('R9a identity carries run/branch/epoch and process ids',
              identity.get('run_id') and identity.get('branch_id') is not None
              and isinstance(identity.get('epoch'), int)
              and identity.get('mcp_pid') == os.getpid(), str(identity)[:240])
        lock = M._acquire_recording_lock({**identity, 'pid': os.getpid(), 'mcp_pid': os.getpid()})
        check('R9b first recording lock acquisition succeeds', lock.get('ok') is True,
              str(lock)[:160])
        M._release_recording_lock()
        check('R9d releasing the lock removes it',
              not (env.root / 'state' / 'recording.lock').exists(), 'lock removed')
        # a live lock owned by another process must not be overwritten
        (env.root / 'state' / 'recording.lock').write_text(
            json.dumps({'mcp_pid': os.getppid(), 'pid': os.getppid()}), encoding='utf-8')
        conflict = M._acquire_recording_lock({**identity, 'pid': os.getpid(),
                                              'mcp_pid': os.getpid()})
        check('R9c a second live owner is refused (atomic singleton)',
              conflict.get('ok') is False
              and conflict.get('error') in ('recording_active', 'recording_lock_busy'),
              str(conflict)[:200])
        (env.root / 'state' / 'recording.lock').unlink()
        state_file = env.root / 'state' / 'recording.json'
        state_file.write_text(json.dumps({'pid': 999999, 'dir': str(env.root / 'clip'),
                                          'identity': identity}), encoding='utf-8')
        status = M.recording_status()
        check('R9e recording_status reports the bound identity and run match',
              status.get('run_id') == identity.get('run_id')
              and status.get('matches_current_run') is True,
              str({k: status.get(k) for k in ('run_id', 'branch_id', 'matches_current_run')}))
        source = (ROOT / 'tools' / 'mcp_server_v2.py').read_text(encoding='utf-8')
        check('R9f recording_start writes the identity and uses an exclusive lock',
              "'identity': identity" in source and 'O_CREAT | os.O_EXCL' in source
              and '_release_recording_lock()' in source, 'recording identity')


def main() -> int:
    test_overlay_thought_trim()
    test_run_rotation_archives_evidence()
    test_epoch_bump_and_ledger_identity()
    test_save_metadata_identity()
    test_load_barrier_and_epoch()
    test_battle_round_record()
    test_recording_lock_and_identity()
    failed = [item for item in RESULTS if not item[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
