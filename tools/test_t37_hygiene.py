# -*- coding: utf-8 -*-
"""T37 offline regression: 5B evidence-chain hygiene + 5D Python backlog.

Covered (no game / client / recorder: everything is a temp STATE_DIR fixture + fake agent):
  5B  a new game archives AND resets the per-run identity chain (segments.jsonl, epochs.jsonl,
      restores.jsonl, restore.json, recording_segments.jsonl) under archive/<old_run>/chain-<n>/,
      so the new run's first recording segment has parent=None/restore_ref=None (ISSUE-01);
      a resume within the same run keeps its chain, and cross-run leftovers are ignored.
  5D-1 save_game never accepts a transient 0-byte save: completion requires a non-empty file
      whose signature stayed stable; a file that stays 0B / keeps growing returns submitted with
      an explicit reason.
  5D-2 game_screenshot's default target is the untracked var/shots/current.png, never the tracked
      var/shots/mcp_v2.png; explicit paths still work.
  5D-3 D-014 gate: concurrent mutating calls are refused with error=in_flight (thread test), the
      slot is released after success and after an exception, nested composite calls
      (move_near -> game_act) are allowed, and a new mutating action is refused with
      error=query_pending while a UI query is open (answer/close_window stay allowed).
  5D-4 recruit_hero: the known OK-but-no-op town hire (visiting hero / no available hero) is
      rejected before it is sent.

Run: python -X utf8 tools/test_t37_hygiene.py
Exit code 0 only when every check passes.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))
import mcp_server_v2 as M  # noqa: E402
from test_action_contract import Env, FakeAgent, bridge_ok, make_hero, make_town, state  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition, detail: str = '') -> None:
    RESULTS.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def read_jsonl(path: Path) -> list:
    try:
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()
                if line.strip()]
    except OSError:
        return []


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


# =====================================================================================
# 5B: evidence-chain hygiene (ISSUE-01)
# =====================================================================================
def _old_chain_fixture(state_dir: Path) -> dict:
    """Old run with a full identity chain; returns {file: sha256} of what was written."""
    state_dir.mkdir(parents=True, exist_ok=True)
    write_json(state_dir / 'run.json',
               {'schema_version': 1, 'run_id': 'oldrun', 'branch_id': 'replay-2', 'epoch': 2,
                'epoch_started_at': time.time(), 'segments': 2})
    payloads = {
        'segments.jsonl': '\n'.join(json.dumps(item) for item in
                                    [{'segment_id': 'oldrun/segment-1', 'run_id': 'oldrun',
                                      'kind': 'new_game', 'parent': None},
                                     {'segment_id': 'oldrun/segment-2', 'run_id': 'oldrun',
                                      'kind': 'restore',
                                      'parent': {'parent_run_id': 'oldrun'}}]) + '\n',
        'epochs.jsonl': json.dumps({'epoch': 1, 'source': 'restore', 'run_id': 'oldrun'}) + '\n',
        'restores.jsonl': json.dumps({'kind': 'session_start_resume',
                                      'slot': 'Saves/agent/older',
                                      'committed': {'run_id': 'oldrun'}}) + '\n',
        'restore.json': json.dumps({'kind': 'session_start_resume', 'slot': 'Saves/agent/older',
                                    'continuity': 'replay',
                                    'committed': {'run_id': 'oldrun', 'branch_id': 'replay-2',
                                                  'epoch': 2}}),
        'recording_segments.jsonl': '\n'.join(json.dumps(item) for item in
                                              [{'segment_id': 'oldrun/rec-1',
                                                'run_id': 'oldrun', 'branch_id': 'main',
                                                'epoch': 0},
                                               {'segment_id': 'oldrun/rec-2',
                                                'run_id': 'oldrun',
                                                'branch_id': 'replay-2', 'epoch': 2,
                                                'parent_segment': 'oldrun/rec-1'}]) + '\n',
        'actions.jsonl': json.dumps({'action': 'old_action', 'status': 'completed'}) + '\n',
        'results.log': 'seq=1 OK old\n',
    }
    shas = {}
    for name, text in payloads.items():
        (state_dir / name).write_text(text, encoding='utf-8')
        shas[name] = M._file_sha256(state_dir / name)
    return shas


CHAIN_NAMES = ('segments.jsonl', 'epochs.jsonl', 'restores.jsonl', 'restore.json',
               'recording_segments.jsonl')


def test_rotate_archives_and_resets_chain() -> None:
    with Env() as env:
        state_dir = env.root / 'state'
        shas = _old_chain_fixture(state_dir)
        new = M._rotate_run()   # legacy direct caller: archive=True
        manifest_paths = sorted((state_dir / 'archive' / 'oldrun').glob('chain-*/manifest.json'))
        check('5B-1 rotate archives the outgoing identity chain once',
              len(manifest_paths) == 1, str(manifest_paths))
        manifest = read_json(manifest_paths[0]) if manifest_paths else {}
        files = {entry.get('name'): entry for entry in manifest.get('files') or []}
        check('5B-2 every chain file is in the archive manifest',
              set(files) == set(CHAIN_NAMES), f'files={sorted(files)}')
        check('5B-3 archived chain copies match the pre-rotate sha256',
              all(files.get(name, {}).get('sha256') == shas[name] for name in CHAIN_NAMES),
              str({name: files.get(name, {}).get('sha256') for name in CHAIN_NAMES}))
        check('5B-4 archive manifest records the old run identity',
              manifest.get('run_id') == 'oldrun' and manifest.get('branch_id') == 'replay-2'
              and manifest.get('epoch') == 2 and manifest.get('kind') == 'chain_reset',
              str({key: manifest.get(key) for key in ('run_id', 'branch_id', 'epoch', 'kind')}))
        index = read_jsonl(state_dir / 'archive' / 'index.jsonl')
        check('5B-5 archive index records the chain_reset entry',
              any(entry.get('kind') == 'chain_reset' and entry.get('run_id') == 'oldrun'
                  for entry in index), str(index[-1:])[:200])
        check('5B-6 the active chain files were reset',
              all(not (state_dir / name).exists() for name in CHAIN_NAMES),
              str([name for name in CHAIN_NAMES if (state_dir / name).exists()]))
        run = M._run_identity()
        chain_reset = run.get('chain_reset') or {}
        check('5B-7 the new run identity points at the archived chain',
              run.get('run_id') != 'oldrun' and run.get('epoch') == 0
              and chain_reset.get('dir') == manifest.get('dir')
              and {item.get('name') for item in chain_reset.get('files') or []}
              == set(CHAIN_NAMES), str(chain_reset)[:240])

        # a new-game engine segment starts a clean segments.jsonl
        M._record_segment({'kind': 'new_game', 'parent': None, 'started_at': time.time()})
        segments = read_jsonl(state_dir / 'segments.jsonl')
        check('5B-8 segments.jsonl only contains the new run after rotation',
              len(segments) == 1 and segments[0].get('run_id') == run.get('run_id'),
              str(segments))

        # first clip of the new run: no parent, no old restore_ref
        clip = env.root / 'clip1'
        clip.mkdir(parents=True, exist_ok=True)
        (clip / 'video.mp4').write_bytes(b'clip-bytes')
        segment = M.finalize_recording_segment(
            clip, identity={'segment_id': f'{run.get("run_id")}/rec-1'},
            started_at=time.time(), meta={})
        check('5B-9 the new run first recording segment has no parent',
              segment.get('parent') is None and segment.get('parent_segment') is None,
              f"parent={segment.get('parent')} parent_segment={segment.get('parent_segment')}")
        check('5B-10 the new run first recording segment has no old restore_ref',
              segment.get('restore_ref') is None, str(segment.get('restore_ref'))[:160])
        check('5B-11 the new run first recording segment is bound to the new run',
              segment.get('run_id') == run.get('run_id')
              and segment.get('epoch') == 0, str(segment.get('run_id')))

        # a within-run restore keeps the parent chain + restore_ref (resume semantics untouched)
        write_json(state_dir / 'restore.json',
                   {'kind': 'session_start_resume', 'slot': 'Saves/agent/checkpoint',
                    'continuity': 'resumed', 'target_proof': 'fingerprint_match',
                    'committed': {'run_id': run.get('run_id'),
                                  'branch_id': 'resume-1', 'epoch': 1}})
        clip2 = env.root / 'clip2'
        clip2.mkdir(parents=True, exist_ok=True)
        second = M.finalize_recording_segment(
            clip2, identity={'segment_id': f'{run.get("run_id")}/rec-2'},
            started_at=time.time(), meta={})
        check('5B-12 a within-run clip keeps the parent chain',
              second.get('parent_segment') == f'{run.get("run_id")}/rec-1',
              str(second.get('parent_segment')))
        check('5B-13 a within-run clip keeps the restore_ref evidence',
              (second.get('restore_ref') or {}).get('slot') == 'Saves/agent/checkpoint',
              str(second.get('restore_ref'))[:160])


def test_rotate_never_deletes_unarchived_chain() -> None:
    """If the chain copy fails, the old evidence is renamed aside, never deleted."""
    with Env() as env:
        state_dir = env.root / 'state'
        shas = _old_chain_fixture(state_dir)
        original = M._archive_chain_files
        M._archive_chain_files = lambda old_run: None
        try:
            run = M._rotate_run(archive=False)
        finally:
            M._archive_chain_files = original
        stale = sorted(state_dir.glob('*.stale-*'))
        check('5B-21 an archive failure renames the chain out of the way (no data deleted)',
              len(stale) == len(CHAIN_NAMES)
              and all(M._file_sha256(path) == shas[path.name.split('.stale-')[0]]
                      for path in stale),
              str([path.name for path in stale]))
        check('5B-22 the new run still starts with a clean chain after the fallback',
              all(not (state_dir / name).exists() for name in CHAIN_NAMES)
              and run.get('chain_reset') is None, str(run.get('chain_reset'))[:120])


def test_session_start_new_game_ordering() -> None:
    """session_start archives the outgoing segment BEFORE start; rotate(archive=False) resets."""
    with Env() as env:
        state_dir = env.root / 'state'
        shas = _old_chain_fixture(state_dir)
        first = M._archive_segment_before_start('session_start:new_game')
        archived = {entry.get('name'): entry for entry in (first or {}).get('files') or []}
        check('5B-14 the pre-start archive already captures the chain files',
              set(CHAIN_NAMES) <= set(archived)
              and all(archived[name].get('sha256') == shas[name] for name in CHAIN_NAMES),
              str(sorted(archived)))
        run = M._rotate_run(archive=False)
        chain_dirs = sorted((state_dir / 'archive' / 'oldrun').glob('chain-*'))
        check('5B-15 rotate(archive=False) still archives the chain separately',
              bool(chain_dirs)
              and (chain_dirs[0] / 'manifest.json').exists(), str(chain_dirs))
        check('5B-16 rotate(archive=False) still resets the active chain',
              all(not (state_dir / name).exists() for name in CHAIN_NAMES)
              and run.get('chain_reset') is not None, str(run.get('chain_reset'))[:160])


def test_cross_run_leftovers_are_ignored() -> None:
    with Env() as env:
        state_dir = env.root / 'state'
        state_dir.mkdir(parents=True, exist_ok=True)
        run = M._run_identity()
        (state_dir / M._RECORDING_SEGMENTS_FILE).write_text(
            json.dumps({'segment_id': 'oldrun/rec-9', 'run_id': 'oldrun'}) + '\n',
            encoding='utf-8')
        check('5B-17 a cross-run recording line is not returned as the parent',
              M._last_recording_segment() is None, str(M._last_recording_segment()))
        with open(state_dir / M._RECORDING_SEGMENTS_FILE, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps({'segment_id': f'{run.get("run_id")}/rec-10',
                                     'run_id': run.get('run_id')}) + '\n')
        found = M._last_recording_segment()
        check('5B-18 a same-run recording line is returned',
              (found or {}).get('segment_id') == f'{run.get("run_id")}/rec-10',
              str(found))
        write_json(state_dir / 'restore.json',
                   {'kind': 'session_start_resume', 'slot': 'Saves/agent/old',
                    'committed': {'run_id': 'oldrun'}})
        check('5B-19 a cross-run restore receipt is ignored',
              M._latest_restore_ref() is None, str(M._latest_restore_ref()))
        write_json(state_dir / 'restore.json',
                   {'kind': 'session_start_resume', 'slot': 'Saves/agent/new',
                    'target_proof': 'fingerprint_match',
                    'committed': {'run_id': run.get('run_id')}})
        ref = M._latest_restore_ref() or {}
        check('5B-20 a same-run restore receipt is returned',
              ref.get('slot') == 'Saves/agent/new', str(ref)[:160])


# =====================================================================================
# 5D-1: save_game never accepts a transient 0-byte / unstable save file
# =====================================================================================
def _short_settle(wait: float = 1.0) -> None:
    M._SAVE_WAIT_SECONDS = wait
    M._SAVE_POLL_SECONDS = 0.05
    M._SAVE_STABLE_SECONDS = 0.12


def _save_writer(path: Path, payload: bytes | None, delay: float, initial: bytes = b''):
    """on_execute hook: create the save path now (initial bytes) and write payload after delay."""
    def hook(_line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(initial)
        if payload is not None:
            def fill() -> None:
                time.sleep(delay)
                path.write_bytes(payload)
            threading.Thread(target=fill, daemon=True).start()
    return hook


def test_save_zero_then_real() -> None:
    with Env() as env:
        _short_settle(1.5)
        path = M._save_path('zero-then-real')
        payload = b'x' * 4096
        env.agent(FakeAgent([state(day=3), state(day=3)], [bridge_ok(1)],
                            on_execute=_save_writer(path, payload, 0.25)))
        res = M.save_game('zero-then-real')
        check('5D1-1 a save that is briefly 0 bytes then settles is completed',
              res.get('status') == 'completed' and res.get('ok') is True,
              f"status={res.get('status')} evidence={res.get('evidence')}")
        check('5D1-2 the completed result reports the real non-zero byte size',
              res.get('bytes') == len(payload) and res.get('file_verified') is True,
              f"bytes={res.get('bytes')} file_verified={res.get('file_verified')}")
        check('5D1-3 the evidence states the file was non-empty and stable',
              'non-empty and stable' in str(res.get('evidence')),
              str(res.get('evidence'))[:200])
        meta = M._load_saves_meta().get('zero-then-real') or {}
        check('5D1-4 the checkpoint metadata records a full sha256 of the real file',
              meta.get('bytes') == len(payload) and isinstance(meta.get('sha256'), str)
              and path.read_bytes() == payload, str(meta.get('bytes')))


def test_save_stays_zero() -> None:
    with Env() as env:
        _short_settle(0.4)
        path = M._save_path('zero-forever')
        env.agent(FakeAgent([state(day=3), state(day=3)], [bridge_ok(1)],
                            on_execute=_save_writer(path, None, 0.0)))
        res = M.save_game('zero-forever')
        check('5D1-5 a save that stays 0 bytes is submitted, never completed',
              res.get('status') == 'submitted' and res.get('ok') is False,
              f"status={res.get('status')} evidence={res.get('evidence')}")
        check('5D1-6 the 0-byte failure carries an explicit reason',
              '0 bytes' in str(res.get('reason')), str(res.get('reason')))
        check('5D1-7 a 0-byte save writes no checkpoint metadata',
              'zero-forever' not in M._load_saves_meta(),
              str(list(M._load_saves_meta()))[:120])


def test_save_never_settles() -> None:
    with Env() as env:
        _short_settle(0.5)
        path = M._save_path('growing')
        stop = threading.Event()

        def churn(_line: str) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'seed')

            def loop() -> None:
                while not stop.wait(0.04):
                    with open(path, 'ab') as handle:
                        handle.write(b'chunk')
            threading.Thread(target=loop, daemon=True).start()
        try:
            env.agent(FakeAgent([state(day=3), state(day=3)], [bridge_ok(1)],
                                on_execute=churn))
            res = M.save_game('growing')
        finally:
            stop.set()
            time.sleep(0.1)
        check('5D1-8 a still-growing save file is submitted, never completed',
              res.get('status') == 'submitted' and res.get('ok') is False,
              f"status={res.get('status')} evidence={res.get('evidence')}")
        check('5D1-9 the unstable file carries an explicit reason',
              'stable' in str(res.get('reason')) or '0 bytes' in str(res.get('reason')),
              str(res.get('reason')))


def test_save_missing_and_unchanged() -> None:
    with Env() as env:
        _short_settle(0.4)
        env.agent(FakeAgent([state(day=3), state(day=3)], [bridge_ok(1)]))
        res = M.save_game('never-written')
        check('5D1-10 a missing save file is submitted with a reason',
              res.get('status') == 'submitted' and 'missing' in str(res.get('reason')),
              f"status={res.get('status')} reason={res.get('reason')}")
    with Env() as env:
        _short_settle(0.4)
        path = M._save_path('unchanged')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'old-checkpoint')
        env.agent(FakeAgent([state(day=3), state(day=3)], [bridge_ok(1)]))
        res = M.save_game('unchanged')
        check('5D1-11 an unchanged existing save file is still not attributed',
              res.get('status') == 'submitted' and 'not changed' in str(res.get('evidence')),
              f"status={res.get('status')} evidence={res.get('evidence')}")


# =====================================================================================
# 5D-2: game_screenshot default path is untracked
# =====================================================================================
class ShotAgent:
    def __init__(self) -> None:
        self.targets: list[Path] = []

    def shot(self, path=None):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'fake-png')
        self.targets.append(target)
        return target


def test_screenshot_default_path() -> None:
    source = (ROOT / 'tools' / 'mcp_server_v2.py').read_text(encoding='utf-8')
    check('5D2-1 the tracked mcp_v2.png is no longer the implicit screenshot target',
          "ROOT / 'var' / 'shots' / 'mcp_v2.png'" not in source
          and "ROOT / 'var' / 'shots' / 'current.png'" in source, 'source default')
    with Env() as env:
        original_root, original_image = M.ROOT, M.Image
        M.ROOT = env.root / 'root'
        M.Image = None
        fake = ShotAgent()
        env.agent(fake)
        try:
            res = M.game_screenshot()
            expected = env.root / 'root' / 'var' / 'shots' / 'current.png'
            check('5D2-2 the default screenshot lands on the untracked current.png path',
                  isinstance(res, dict) and Path(res.get('path')) == expected
                  and 'mcp_v2.png' not in str(res.get('path')), str(res)[:200])
            explicit = env.root / 'shots' / 'final.png'
            res2 = M.game_screenshot(str(explicit))
            check('5D2-3 an explicit screenshot path is still honoured',
                  isinstance(res2, dict) and Path(res2.get('path')) == explicit
                  and explicit.exists(), str(res2)[:160])
        finally:
            M.ROOT, M.Image = original_root, original_image


# =====================================================================================
# 5D-3: D-014 mutation serialization + query gate
# =====================================================================================
class BlockingAgent(FakeAgent):
    def __init__(self, states, entered: threading.Event, release: threading.Event):
        super().__init__(states)
        self.entered = entered
        self.release = release

    def execute(self, line: str, verify=None, result_timeout: float = 20.0):
        if line.startswith('end_turn'):
            self.entered.set()
            self.release.wait(timeout=5)
        return super().execute(line, verify=verify, result_timeout=result_timeout)


def test_in_flight_rejection() -> None:
    with Env() as env:
        entered, release = threading.Event(), threading.Event()
        env.agent(BlockingAgent([state(day=1), state(day=2)], entered, release))
        holder: dict = {}

        def run_holder() -> None:
            holder['res'] = M.game_act('end_turn')
        thread = threading.Thread(target=run_holder)
        thread.start()
        try:
            started = entered.wait(2)
            check('5D3-1 the first mutating call reached the bridge', started, '')
            second = M.game_act('end_turn')
            check('5D3-2 a concurrent mutating call is refused with error=in_flight',
                  second.get('ok') is False and second.get('error') == 'in_flight'
                  and second.get('sent') is False, str(second)[:200])
            busy = second.get('in_flight') or {}
            check('5D3-3 the in_flight error names the running tool',
                  busy.get('tool') == 'game_act' and busy.get('pid') == __import__('os').getpid(),
                  str(busy)[:200])
        finally:
            release.set()
            thread.join(5)
        check('5D3-4 the original mutating call still completes',
              (holder.get('res') or {}).get('status') == 'completed',
              str(holder.get('res'))[:160])
        check('5D3-5 the slot is released after the call',
              M._mutation_owner_snapshot() is None, str(M._mutation_owner_snapshot()))
        entries = read_jsonl(env.root / 'state' / 'actions.jsonl')
        check('5D3-6 the refused concurrent call is recorded in the ledger',
              any(entry.get('error') == 'in_flight' for entry in entries),
              str([entry.get('error') for entry in entries]))
        env.agent(FakeAgent([state(day=2), state(day=3)]))
        after = M.game_act('end_turn')
        check('5D3-7 a serial call works again after the slot is free',
              after.get('status') == 'completed', str(after.get('status')))


def test_slot_released_on_exception() -> None:
    with Env():
        def boom() -> None:
            raise RuntimeError('boom')
        guarded = M._serialized_mutation('unit_probe')(boom)
        raised = False
        try:
            guarded()
        except RuntimeError:
            raised = True
        check('5D3-8 an exception inside a mutating tool propagates', raised, '')
        check('5D3-9 the slot is released even when the tool raises',
              M._mutation_owner_snapshot() is None, str(M._mutation_owner_snapshot()))


def test_nested_composite_call() -> None:
    with Env() as env:
        before = state(heroes=[make_hero(0, (2, 2))])
        after = state(heroes=[{'index': 0, 'name': 'Hero0', 'pos': [6, 5, 0],
                               'visitable': [5, 5, 0], 'movement': 900, 'army': []}])
        env.agent(FakeAgent([before, after], [bridge_ok(1)]))
        res = M.move_near(hero=0, x=5, y=5, radius=0)
        check('5D3-10 a composite tool may call a nested mutating action (move_near)',
              res.get('ok') is True and res.get('status') == 'completed'
              and res.get('error') not in ('in_flight',), str(res)[:200])


def test_query_gate() -> None:
    query_state = state(query={'type': 'yesno', 'text': 'Hire?'})
    with Env() as env:
        fake = env.agent(FakeAgent([query_state, state(day=2)]))
        res = M.game_act('end_turn')
        check('5D3-11 a mutating action is refused while a query is open',
              res.get('ok') is False and res.get('error') == 'query_pending'
              and res.get('sent') is False, str(res)[:200])
        check('5D3-12 the query_pending rejection never reached the bridge',
              fake.sent == [], str(fake.sent))
        check('5D3-13 the rejection carries the open query and a decision point',
              (res.get('query') or {}).get('type') == 'yesno'
              and 'query_opened' in (res.get('decisions') or []), str(res.get('query')))
        entries = read_jsonl(env.root / 'state' / 'actions.jsonl')
        check('5D3-14 the query_pending rejection is recorded in the ledger',
              any(entry.get('error') == 'query_pending' for entry in entries),
              str([entry.get('error') for entry in entries]))
    with Env() as env:
        fake = env.agent(FakeAgent([query_state, state(day=1)], [bridge_ok(1)]))
        res = M.game_act('answer', {'value': '0'})
        check('5D3-15 answer stays allowed while the query is open',
              res.get('error') != 'query_pending' and fake.sent == ['answer 0'],
              f"error={res.get('error')} sent={fake.sent}")
        check('5D3-16 answering the query is completed when the query clears',
              res.get('status') == 'completed', str(res.get('status')))
    with Env() as env:
        fake = env.agent(FakeAgent([query_state, state(day=1)]))
        res = M.game_act('close_window')
        check('5D3-17 close_window stays allowed while the query is open',
              res.get('error') != 'query_pending' and fake.sent == ['close_window_safe'],
              f"error={res.get('error')} sent={fake.sent}")


# =====================================================================================
# 5D-4: recruit_hero known no-op hire rejected before send
# =====================================================================================
def test_recruit_hero_precondition() -> None:
    visiting_town = make_town(0, built=[0])
    visiting_town['hero_visiting'] = True
    visiting_town['available_heroes'] = [{'index': 0, 'name': 'Candidates', 'hero_type': 7}]
    hero_in_town = {'index': 0, 'name': 'H', 'pos': [5, 5, 0], 'visitable': [4, 5, 0],
                    'movement': 1000, 'army': []}
    visiting_town['pos'] = [4, 5, 0]
    st = state(heroes=[hero_in_town], towns=[visiting_town])
    with Env() as env:
        fake = env.agent(FakeAgent([st, st]))
        res = M.game_act('recruit_hero', {'town': 0, 'index': 0})
        check('5D4-1 hiring in a town with a visiting hero is rejected before send',
              res.get('status') == 'rejected' and res.get('sent') is False
              and 'visiting hero' in str(res.get('evidence')), str(res.get('evidence'))[:200])
        check('5D4-2 the no-op hire never reached the bridge', fake.sent == [], str(fake.sent))
    empty_town = make_town(0, built=[0])
    empty_town['hero_visiting'] = False
    empty_town['available_heroes'] = []
    with Env() as env:
        fake = env.agent(FakeAgent([state(towns=[empty_town]), state(towns=[empty_town])]))
        res = M.game_act('recruit_hero', {'town': 0, 'index': 0})
        check('5D4-3 hiring with no available hero is rejected before send',
              res.get('status') == 'rejected' and res.get('sent') is False
              and 'no hero to hire' in str(res.get('evidence')), str(res.get('evidence'))[:200])
        check('5D4-4 the empty-pool hire never reached the bridge', fake.sent == [],
              str(fake.sent))


def main() -> int:
    test_rotate_archives_and_resets_chain()
    test_rotate_never_deletes_unarchived_chain()
    test_session_start_new_game_ordering()
    test_cross_run_leftovers_are_ignored()
    test_save_zero_then_real()
    test_save_stays_zero()
    test_save_never_settles()
    test_save_missing_and_unchanged()
    test_screenshot_default_path()
    test_in_flight_rejection()
    test_slot_released_on_exception()
    test_nested_composite_call()
    test_query_gate()
    test_recruit_hero_precondition()
    failed = [item for item in RESULTS if not item[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
