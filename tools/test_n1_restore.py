# -*- coding: utf-8 -*-
"""Offline regression for the N1 restore/archive transaction (plan §5.1).

No game, no client, no network: `VcmiAgent` is replaced by a stand-in that records what the
state dir looked like at start() time and can truncate the evidence like the real launcher does;
save slots/sidecars live in temp dirs.  Every negative case asserts sends / identity-commit
timing / file changes, not just an error string.

Covered:
  A  archive-before-clear ordering, captured_before_start, unique segments, no double archive
  B  cold restore: cross-run adoption, sidecar missing, file hash mismatch, missing file,
     same-run resume, replay branch, target-proof failure (no identity commit)
  C  load_game target proof: same-day unrelated change, fingerprint match, file sha mismatch,
     legacy meta fallback, stale-timestamp barrier
  D  request_id cannot cross an engine epoch

Run: python -X utf8 tools/test_n1_restore.py
Exit code 0 only when every check passes.
"""
from __future__ import annotations

import json
import sys
import time
import types
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


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def game_state(day: int = 5, gold: int = 1000, ts: float | None = None) -> dict:
    st = state(day=day, gold=gold)
    if ts is not None:
        st['ts_wall'] = int(ts * 1000)
    return st


def prep_run(env: Env, run_id: str = 'currun', branch: str = 'main',
             epoch: int = 0) -> Path:
    state_dir = env.root / 'state'
    write_json(state_dir / 'run.json',
               {'schema_version': 1, 'run_id': run_id, 'branch_id': branch, 'epoch': epoch,
                'epoch_started_at': time.time()})
    return state_dir


def write_save(env: Env, name: str = 'p2', meta: dict | None = None,
               subdir: str | None = None, data: bytes = b'fake-save-bytes') -> Path:
    subdir = subdir or M.SAVE_SUBDIR
    path = env.root / 'saves' / subdir / f'{name}.vsgm1'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    saves_path = env.root / 'state' / 'saves.json'
    saves = {}
    if saves_path.exists():
        try:
            saves = json.loads(saves_path.read_text(encoding='utf-8'))
        except ValueError:
            saves = {}
    if meta is not None:
        saves[name] = meta
    write_json(saves_path, saves)
    return path


class RestoreAgent:
    """Stand-in for VcmiAgent: records the evidence visible at start() time, then clears it."""

    instances: list['RestoreAgent'] = []

    def __init__(self, post_state: dict, dirpath=None, map_name=None, log=False,
                 clear_on_start: bool = True):
        self.post_state = dict(post_state)
        self.target = map_name
        self.started = 0
        self.stopped = 0
        self.archive_at_start: list = []
        self.results_at_start: str | None = None
        self.contract = {'match': True, 'protocol_ok': True, 'actual_sha256': 'a' * 64,
                         'built_from_commit': 'c0ffee', 'repo_head': 'c0ffee',
                         'source_matches_head': True}
        self.session = types.SimpleNamespace(
            session_id=f'sess-{len(RestoreAgent.instances)}',
            client_pid=400000 + len(RestoreAgent.instances), created_at=time.time())
        self.clear_on_start = clear_on_start
        RestoreAgent.instances.append(self)

    def start(self, timeout: float = 120.0) -> bool:
        self.started += 1
        # What does the active dir look like when the launcher is about to clear it?
        for path in (self.dir / 'archive').rglob('manifest.json') if self.dir else []:
            try:
                self.archive_at_start.append(json.loads(path.read_text(encoding='utf-8')))
            except (OSError, ValueError):
                pass
        try:
            self.results_at_start = (self.dir / 'results.log').read_text(encoding='utf-8')
        except (OSError, AttributeError):
            self.results_at_start = None
        if self.clear_on_start:
            # Real VcmiAgent.start() truncates these before/while spawning the engine.
            (self.dir / 'results.log').write_text('', encoding='utf-8')
            (self.dir / 'commands.txt').write_text('', encoding='utf-8')
            (self.dir / 'state_epoch.json').write_text(
                json.dumps({'started_at': time.time()}), encoding='utf-8')
        return True

    def wait_in_game(self, timeout: float = 240.0) -> dict:
        return dict(self.post_state)

    def state(self, timeout: float = 10.0) -> dict:
        return dict(self.post_state)

    def window_is_offscreen(self) -> bool:
        return True

    def stop(self) -> dict:
        self.stopped += 1
        return {'killed': True}


def start_session(env: Env, agent: RestoreAgent, target: str = 'Maps/Key to Victory',
                  **kwargs):
    original = M.VcmiAgent
    M.VcmiAgent = lambda *args, **kw: agent
    agent.dir = Path(env.root / 'state')
    try:
        return M.session_start(target, **kwargs)
    finally:
        M.VcmiAgent = original


# ------------------------------------------------------------------ A: archive before clear
def test_archive_before_start() -> None:
    with Env() as env:
        state_dir = prep_run(env, 'oldrun', 'main', 2)
        (state_dir / 'actions.jsonl').write_text('{"action": "old-action"}\n', encoding='utf-8')
        (state_dir / 'results.log').write_text('seq=7 OK old-reply\n', encoding='utf-8')
        (state_dir / 'state_epoch.json').write_text('{"old": true}', encoding='utf-8')
        (state_dir / 'start_options.txt').write_text('difficulty=1\n', encoding='utf-8')
        old_results = (state_dir / 'results.log').read_text(encoding='utf-8')
        old_sha = M._file_sha256(state_dir / 'results.log')
        agent = RestoreAgent(game_state(day=1), clear_on_start=True)
        res = start_session(env, agent)
        manifests = sorted((state_dir / 'archive' / 'oldrun').glob('*/manifest.json'))
        manifest = json.loads(manifests[0].read_text(encoding='utf-8')) if manifests else {}
        files = {entry.get('name'): entry for entry in manifest.get('files') or []}
        check('A1 session_start archives the outgoing segment', res.get('ok') is True
              and len(manifests) == 1, f'ok={res.get("ok")} manifests={len(manifests)}')
        check('A1b archive is captured BEFORE the launcher clears evidence',
              manifest.get('captured_before_start') is True
              and str(manifest.get('reason', '')).startswith('session_start:'),
              str(manifest.get('reason')))
        check('A1c archived results.log is the OLD content (not the cleared version)',
              files.get('results.log', {}).get('sha256') == old_sha
              and agent.results_at_start == old_results,
              f"sha_match={files.get('results.log', {}).get('sha256') == old_sha}")
        check('A1d archive includes actions/results/state_epoch/start_options',
              {'actions.jsonl', 'results.log', 'state_epoch.json',
               'start_options.txt'} <= set(files), str(sorted(files)))
        check('A1e the fake start() was called exactly once and reset the active run',
              agent.started == 1 and not (state_dir / 'actions.jsonl').exists(),
              f'started={agent.started}')
        index = read_jsonl(state_dir / 'archive' / 'index.jsonl')
        check('A1f archive/index.jsonl records the unique segment',
              len(index) == 1 and index[0].get('captured_before_start') is True
              and index[0].get('run_id') == 'oldrun'
              and index[0].get('segment_id', '').endswith('segment-1'), str(index[:1])[:240])

    # repeated rotation of the same run must never overwrite an earlier segment
    with Env() as env:
        state_dir = prep_run(env, 'samrun')
        (state_dir / 'results.log').write_text('first', encoding='utf-8')
        first = M._archive_segment_before_start('t1')
        (state_dir / 'results.log').write_text('second', encoding='utf-8')
        second = M._archive_segment_before_start('t2')
        first_manifest = json.loads(
            (state_dir / 'archive' / 'samrun' / 'segment-1' / 'manifest.json').read_text(
                encoding='utf-8'))
        first_results = (state_dir / 'archive' / 'samrun' / 'segment-1' / 'results.log'
                         ).read_text(encoding='utf-8')
        check('A2 repeated segments get unique dirs and preserve older evidence',
              first and second and first['segment_id'] != second['segment_id']
              and first_results == 'first',
              f"{first and first['segment_id']} / {second and second['segment_id']}")
        check('A2b first manifest stays untouched by the second archive',
              first_manifest.get('captured_before_start') is True, '')


# ------------------------------------------------------------------ B: cold restore
def _resume_meta(path: Path, day: int, run_id: str | None = 'oldrun',
                 actions_count: int | None = 3, fingerprint: dict | None = None,
                 sha256: str | None = '__auto__') -> dict:
    st = game_state(day=day)
    return {'slot': f'Saves/{M.SAVE_SUBDIR}/p2', 'file': str(path), 'bytes': path.stat().st_size,
            'sha256': M._file_sha256(path) if sha256 == '__auto__' else sha256,
            'day': day, 'saved_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'fingerprint': fingerprint if fingerprint is not None else M._state_fingerprint(st),
            'run_id': run_id, 'branch_id': 'main', 'epoch': 0,
            'actions_count': actions_count, 'last_action_id': 'a1', 'current_revision': 4,
            'segment_id': 'seg-1', 'session_id': 'old-session'}


def test_cross_run_resume() -> None:
    with Env() as env:
        state_dir = prep_run(env, 'currun', 'main', 0)
        path = write_save(env, 'p2', meta=None)
        meta = _resume_meta(path, day=5)
        write_json(state_dir / 'saves.json', {'p2': meta})
        agent = RestoreAgent(game_state(day=5))
        res = start_session(env, agent, target=f'Saves/{M.SAVE_SUBDIR}/p2')
        restore = res.get('restore') or {}
        run = json.loads((state_dir / 'run.json').read_text(encoding='utf-8'))
        check('B1 cross-run cold restore succeeds without pretending continuity',
              res.get('ok') is True and res.get('resumed') is True
              and restore.get('continuity') == 'cross_run_adopted',
              f"continuity={restore.get('continuity')} error={res.get('error')}")
        check('B1b a NEW run is created with the old run only as parent',
              run.get('run_id') != 'currun' and run.get('parent_run_id') == 'oldrun'
              and run.get('parent_branch_id') == 'main',
              str({key: run.get(key) for key in
                   ('run_id', 'parent_run_id', 'parent_branch_id')}))
        check('B1c restore receipt binds file hash and sidecar identity',
              restore.get('file_sha256') == meta.get('sha256')
              and restore.get('target', {}).get('run_id') == 'oldrun'
              and restore.get('target_proof') == 'fingerprint_match'
              and restore.get('sidecar_path'), str(restore.get('target_proof')))
        restores = read_jsonl(state_dir / 'restores.jsonl')
        check('B1d restores.jsonl has exactly one appended receipt',
              len(restores) == 1 and restores[0].get('slot') == meta['slot'],
              str(len(restores)))
        segments = read_jsonl(state_dir / 'segments.jsonl')
        check('B1e segments.jsonl records the restored engine segment',
              len(segments) == 1 and segments[0].get('kind') == 'restore'
              and segments[0].get('memory_branch') == run.get('branch_id'),
              str(segments[:1])[:200])
        memory = M._memory_path('game', 'current')
        check('B1f game memory now resolves under the new run/branch',
              str(run.get('run_id')) in str(memory) and str(run.get('branch_id')) in str(memory),
              str(memory))


def test_resume_sidecar_missing() -> None:
    with Env() as env:
        state_dir = prep_run(env, 'currun', 'main', 0)
        write_save(env)  # no saves.json entry at all
        agent = RestoreAgent(game_state(day=2))
        res = start_session(env, agent, target=f'Saves/{M.SAVE_SUBDIR}/p2')
        restore = res.get('restore') or {}
        run = json.loads((state_dir / 'run.json').read_text(encoding='utf-8'))
        check('B2 sidecar missing -> explicit unverified continuity, never the old run',
              res.get('ok') is True and restore.get('continuity') == 'unverified'
              and restore.get('reason') == 'sidecar_missing'
              and run.get('run_id') != 'currun',
              f"continuity={restore.get('continuity')} run={run.get('run_id')}")
        check('B2b unverified restore still records the receipt and stops no client',
              restore.get('file_sha256') and agent.stopped == 0,
              f"stopped={agent.stopped}")


def test_resume_file_hash_mismatch() -> None:
    with Env() as env:
        state_dir = prep_run(env, 'currun', 'main', 0)
        path = write_save(env)
        meta = _resume_meta(path, day=5, sha256='0' * 64)
        write_json(state_dir / 'saves.json', {'p2': meta})
        original_run = (state_dir / 'run.json').read_text(encoding='utf-8')
        agent = RestoreAgent(game_state(day=5))
        res = start_session(env, agent, target=f'Saves/{M.SAVE_SUBDIR}/p2')
        check('B3 save file / sidecar hash mismatch -> restore_conflict BEFORE starting',
              res.get('ok') is False and res.get('status') == 'restore_conflict'
              and res.get('continuity') == 'file_hash_mismatch' and agent.started == 0,
              f"status={res.get('status')} started={agent.started}")
        check('B3b a conflicting restore commits no identity',
              (state_dir / 'run.json').read_text(encoding='utf-8') == original_run, '')


def test_resume_missing_file() -> None:
    with Env() as env:
        prep_run(env, 'currun')
        agent = RestoreAgent(game_state(day=5))
        res = start_session(env, agent, target=f'Saves/{M.SAVE_SUBDIR}/nothere')
        check('B4 missing save file -> rejected without starting the client',
              res.get('ok') is False and res.get('status') == 'rejected'
              and agent.started == 0, f"status={res.get('status')}")


def test_same_run_resume_and_replay() -> None:
    # resume: the checkpoint watermark >= the current executed watermark
    with Env() as env:
        state_dir = prep_run(env, 'currun', 'main', 0)
        (state_dir / 'actions.jsonl').write_text(
            '{"action_id": "c1"}\n{"action_id": "c2"}\n', encoding='utf-8')
        path = write_save(env)
        meta = _resume_meta(path, day=5, run_id='currun', actions_count=10)
        write_json(state_dir / 'saves.json', {'p2': meta})
        agent = RestoreAgent(game_state(day=5))
        res = start_session(env, agent, target=f'Saves/{M.SAVE_SUBDIR}/p2')
        restore = res.get('restore') or {}
        run = json.loads((state_dir / 'run.json').read_text(encoding='utf-8'))
        check('B5 same-run resume starts a new epoch/branch with parent watermark',
              res.get('ok') is True and restore.get('continuity') == 'resumed'
              and run.get('epoch') == 1 and run.get('branch_id') == 'resume-1'
              and run.get('parent_epoch') == 0 and run.get('parent_watermark') == 2
              and restore.get('rollback') is False,
              str({key: run.get(key) for key in
                   ('epoch', 'branch_id', 'parent_epoch', 'parent_watermark')}))
        epochs = read_jsonl(state_dir / 'epochs.jsonl')
        check('B5b epochs.jsonl chains the parent branch/epoch/watermark',
              len(epochs) == 1 and epochs[0].get('parent_branch_id') == 'main'
              and epochs[0].get('parent_watermark') == 2,
              str(epochs[:1])[:200])

    # replay: the checkpoint watermark is older than what this timeline already executed
    with Env() as env:
        state_dir = prep_run(env, 'currun', 'main', 0)
        (state_dir / 'actions.jsonl').write_text(
            '{"action_id": "c1"}\n{"action_id": "c2"}\n{"action_id": "c3"}\n',
            encoding='utf-8')
        path = write_save(env)
        meta = _resume_meta(path, day=4, run_id='currun', actions_count=0)
        write_json(state_dir / 'saves.json', {'p2': meta})
        agent = RestoreAgent(game_state(day=4))
        res = start_session(env, agent, target=f'Saves/{M.SAVE_SUBDIR}/p2')
        restore = res.get('restore') or {}
        run = json.loads((state_dir / 'run.json').read_text(encoding='utf-8'))
        check('B6 older checkpoint -> replay branch with rollback=true',
              res.get('ok') is True and restore.get('continuity') == 'replay'
              and restore.get('rollback') is True and run.get('branch_id') == 'replay-1'
              and run.get('parent_watermark') == 3,
              str({key: run.get(key) for key in ('branch_id', 'parent_watermark', 'rollback')}))


def test_resume_target_proof_failure() -> None:
    with Env() as env:
        state_dir = prep_run(env, 'currun', 'main', 0)
        path = write_save(env)
        # sidecar says day 5 / gold 1000, but the restored game shows a different state
        meta = _resume_meta(path, day=5)
        write_json(state_dir / 'saves.json', {'p2': meta})
        original_run = (state_dir / 'run.json').read_text(encoding='utf-8')
        agent = RestoreAgent(game_state(day=5, gold=5555))
        res = start_session(env, agent, target=f'Saves/{M.SAVE_SUBDIR}/p2')
        restore = res.get('restore') or {}
        check('B7 target fingerprint mismatch -> restore_conflict, identity NOT committed',
              res.get('ok') is False and res.get('status') == 'restore_conflict'
              and restore.get('committed') is None
              and (state_dir / 'run.json').read_text(encoding='utf-8') == original_run,
              f"status={res.get('status')}")
        check('B7b the owned client is stopped and the failed proof is recorded',
              agent.stopped == 1 and restore.get('target_proof') == 'fingerprint_mismatch'
              and restore.get('problems'), f"stopped={agent.stopped}")


# ------------------------------------------------------------------ C: load_game target proof
def _load_meta(env: Env, path: Path, day: int, fingerprint: dict | None = None,
               sha256: str | None = '__auto__', name: str = 'chk') -> dict:
    st = game_state(day=day)
    meta = {'slot': f'Saves/{M.SAVE_SUBDIR}/{name}', 'file': str(path), 'day': day,
            'saved_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'run_id': 'currun', 'branch_id': 'main', 'epoch': 0,
            'operations': 1}
    if sha256 == '__auto__':
        meta['sha256'] = M._file_sha256(path)
    elif sha256 is not None:
        meta['sha256'] = sha256
    if fingerprint is not None:
        meta['fingerprint'] = fingerprint
    return meta


def test_load_same_day_unrelated_change() -> None:
    with Env() as env:
        path = env.root / 'saves' / M.SAVE_SUBDIR / 'chk.vsgm1'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'target-save')
        target_fp = M._state_fingerprint(game_state(day=3, gold=1000))
        write_json(env.root / 'state' / 'saves.json',
                   {'chk': _load_meta(env, path, day=3, fingerprint=target_fp)})
        before = game_state(day=3, gold=1000)
        after = game_state(day=3, gold=9999, ts=time.time() + 2)
        env.agent(FakeAgent([before, after], [bridge_ok(1)]))
        res = M.load_game('chk')
        run = json.loads((env.root / 'state' / 'run.json').read_text(encoding='utf-8'))
        check('C1 same-day unrelated gold change -> unknown, not completed',
              res.get('ok') is False and res.get('status') == 'unknown'
              and 'fingerprint' in str(res.get('evidence')), str(res.get('evidence'))[:200])
        check('C1b no epoch/branch is committed on an unproven load',
              run.get('epoch') == 0 and run.get('branch_id') == 'main', str(run)[:160])
        check('C1c no restore receipt is written for an unproven load',
              not (env.root / 'state' / 'restore.json').exists(), '')


def test_load_fingerprint_match() -> None:
    with Env() as env:
        path = env.root / 'saves' / M.SAVE_SUBDIR / 'chk.vsgm1'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'target-save')
        target = game_state(day=3, gold=1000, ts=time.time() + 2)
        target_fp = M._state_fingerprint(target)
        write_json(env.root / 'state' / 'saves.json',
                   {'chk': _load_meta(env, path, day=3, fingerprint=target_fp)})
        env.agent(FakeAgent([game_state(day=5, gold=100), target], [bridge_ok(1)]))
        res = M.load_game('chk')
        restore = res.get('restore') or {}
        check('C2 load completed only when the fingerprint equals the target',
              res.get('ok') is True and res.get('status') == 'completed'
              and res.get('epoch') == 1, f"status={res.get('status')}")
        check('C2b restore receipt carries target_proof=fingerprint_match and file sha',
              restore.get('target_proof') == 'fingerprint_match'
              and restore.get('file_sha256') == M._file_sha256(path)
              and restore.get('committed', {}).get('epoch') == 1, str(restore)[:200])
        check('C2c restores.jsonl appended once',
              len(read_jsonl(env.root / 'state' / 'restores.jsonl')) == 1, '')
        epochs = read_jsonl(env.root / 'state' / 'epochs.jsonl')
        check('C2d epochs.jsonl records the load parent watermark',
              len(epochs) == 1 and 'parent_branch_id' in epochs[0]
              and epochs[0].get('source') == 'load', str(epochs[:1])[:200])


def test_load_file_hash_mismatch() -> None:
    with Env() as env:
        path = env.root / 'saves' / M.SAVE_SUBDIR / 'chk.vsgm1'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'target-save')
        write_json(env.root / 'state' / 'saves.json',
                   {'chk': _load_meta(env, path, day=3, sha256='0' * 64)})
        fake = FakeAgent([game_state(day=3), game_state(day=3)])
        env.agent(fake)
        res = M.load_game('chk')
        check('C3 load file sha mismatch -> rejected and NOTHING is sent',
              res.get('ok') is False and res.get('status') == 'rejected'
              and res.get('sent') is False and fake.sent == [],
              f"status={res.get('status')} sent={fake.sent}")


def test_load_legacy_meta_fallback() -> None:
    with Env() as env:
        path = env.root / 'saves' / M.SAVE_SUBDIR / 'chk.vsgm1'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'legacy-save')
        write_json(env.root / 'state' / 'saves.json',
                   {'chk': _load_meta(env, path, day=3, sha256=None, fingerprint=None)})
        before = game_state(day=3, gold=1000)
        after = game_state(day=3, gold=2000, ts=time.time() + 2)
        env.agent(FakeAgent([before, after], [bridge_ok(1)]))
        res = M.load_game('chk')
        restore = res.get('restore') or {}
        check('C4 legacy meta (no fingerprint) keeps historical completion but labels it',
              res.get('status') == 'completed'
              and restore.get('target_proof') == 'legacy_change_only'
              and restore.get('identity_precision') == 'unknown',
              f"status={res.get('status')} proof={restore.get('target_proof')}")


def test_load_stale_timestamp_barrier() -> None:
    with Env() as env:
        path = env.root / 'saves' / M.SAVE_SUBDIR / 'chk.vsgm1'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'target-save')
        target_fp = M._state_fingerprint(game_state(day=3, gold=1000))
        write_json(env.root / 'state' / 'saves.json',
                   {'chk': _load_meta(env, path, day=3, fingerprint=target_fp)})
        stale = game_state(day=3, gold=1000, ts=time.time() - 30)
        env.agent(FakeAgent([game_state(day=5), stale], [bridge_ok(1)]))
        res = M.load_game('chk')
        check('C5 a pre-command state snapshot cannot prove the load (barrier)',
              res.get('ok') is False and res.get('status') == 'unknown'
              and 'barrier' in str(res.get('evidence')), str(res.get('evidence'))[:200])


# ------------------------------------------------------------------ D: request epoch
def test_request_epoch_boundary() -> None:
    with Env() as env:
        fake = FakeAgent([state(day=1), state(day=2)], [bridge_ok(1)])
        env.agent(fake)
        res = M.game_act('end_turn', {}, request_id='epoch-req-1')
        check('D0 request executed in the original epoch',
              res.get('status') == 'completed' and len(fake.sent) == 1,
              f"status={res.get('status')} sent={fake.sent}")
        old_action = res.get('action_id')
        M._bump_epoch('load', {'save': 'later-slot'})
        replay = M.game_act('end_turn', {}, request_id='epoch-req-1')
        check('D1 the same request_id across an epoch boundary is refused',
              replay.get('ok') is False and replay.get('error') == 'request_epoch_mismatch'
              and len(fake.sent) == 1, str(replay.get('error')))
        check('D1b the refused request did not resend/replay the action',
              len(fake.sent) == 1, str(fake.sent))
        lookup = M.game_action_status(old_action)
        check('D1c the old action is still queryable through game_action_status',
              lookup.get('ok') is True and lookup['action'].get('request_id') == 'epoch-req-1',
              str(lookup)[:160])
        fresh = M.game_act('end_turn', {}, request_id='epoch-req-2')
        check('D1d a new request_id works in the new epoch', len(fake.sent) == 2,
              f'sent={fake.sent} status={fresh.get("status")}')


def main() -> int:
    test_archive_before_start()
    test_cross_run_resume()
    test_resume_sidecar_missing()
    test_resume_file_hash_mismatch()
    test_resume_missing_file()
    test_same_run_resume_and_replay()
    test_resume_target_proof_failure()
    test_load_same_day_unrelated_change()
    test_load_fingerprint_match()
    test_load_file_hash_mismatch()
    test_load_legacy_meta_fallback()
    test_load_stale_timestamp_barrier()
    test_request_epoch_boundary()
    failed = [item for item in RESULTS if not item[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
