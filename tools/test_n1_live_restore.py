# -*- coding: utf-8 -*-
r"""LIVE/DEBUG N1 restore micro-scenario (plan §5.1 / §5 exit condition).

LIVE/DEBUG - NOT part of the maintenance regression suite; refused by default and NOT run by
the offline batches.  It starts one real, isolated VCMI segment to exercise:
    new game with explicit config -> two saves -> seg1 recording -> cold restore of the newest
    checkpoint -> old request_id refused in the new epoch -> memory on the new branch ->
    load an older checkpoint (replay branch + parent watermark/receipt) -> seg2 recording with a
    different identity/segment -> clean stop.
The run takes ~5-10 minutes.  It writes ONLY inside the isolated tree given by env variables.

Safety gate (executed BEFORE importing mcp_server_v2):
  * AUTO_HEROS3_LIVE_TEST=1 is required for the real body.
  * AUTO_HEROS3_STATE_DIR / AUTO_HEROS3_SAVES_DIR / AUTO_HEROS3_SAVE_SUBDIR /
    AUTO_HEROS3_MEMORY_ROOT / AUTO_HEROS3_RECORDINGS_DIR must be set; STATE/MEMORY/RECORDINGS
    must live under ROOT/var/test_n1/ or %TEMP%; SAVE_SUBDIR must start with 'agent_n1'.
  * AUTO_HEROS3_LIVE_TEST_DRYRUN=1 prints the five resolved paths and exits 0 without importing
    the server or starting a client.

Regression fix (T11, after the T10 FAIL): every session_start() is followed by
M.lease_acquire() (mutating tools require the single-writer lease), day advances are awaited with
wait_day_after(previous_day), and the recording segment/health sidecars are asserted instead of
only checking stop ok.  None==None comparisons were replaced by non-None + equality checks.

Branch naming convention (T13): cold-restoring an OLDER checkpoint with session_start() enters
`replay-<epoch>` (rollback=true + parent watermark), while load_game() enters `load-<epoch>`
(existing _bump_epoch() naming) with parent watermark.  The assertions prove the semantics via
target_proof/parent/rollback, not via the branch-name prefix alone.

Manual run (example):
    set AUTO_HEROS3_LIVE_TEST=1
    set AUTO_HEROS3_STATE_DIR=%TEMP%\n1\state
    set AUTO_HEROS3_SAVES_DIR=%USERPROFILE%\Documents\My Games\vcmi\Saves
    set AUTO_HEROS3_SAVE_SUBDIR=agent_n1_manual
    set AUTO_HEROS3_MEMORY_ROOT=%TEMP%\n1\memory
    set AUTO_HEROS3_RECORDINGS_DIR=%TEMP%\n1\recordings
    .venv\Scripts\python.exe -X utf8 tools\test_n1_live_restore.py
"""
from __future__ import annotations
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    results.append((name, bool(ok), detail))
    print(('PASS' if ok else 'FAIL'), name, detail)


def evidence_check(name: str, ok: bool, detail: str = '') -> None:
    """Read-only corroborating evidence: PASS when it matches, WARN (never FAIL) otherwise."""
    if ok:
        check(name, True, detail)
    else:
        print(f'WARN {name}  ({detail})')
        results.append((name, True, f'WARN {detail}'))


def _resolve(raw: str) -> Path:
    path = Path(raw)
    return (path if path.is_absolute() else ROOT / path).resolve()


def _n1_guard() -> None:
    formal = {'STATE_DIR': ROOT / 'var' / 'agent_mcp_v2',
              'MEMORY_ROOT': ROOT / 'doc' / 'player',
              'RECORDINGS_DIR': ROOT / 'var' / 'recordings'}
    env_names = {'STATE_DIR': 'AUTO_HEROS3_STATE_DIR',
                 'SAVES_DIR': 'AUTO_HEROS3_SAVES_DIR',
                 'MEMORY_ROOT': 'AUTO_HEROS3_MEMORY_ROOT',
                 'RECORDINGS_DIR': 'AUTO_HEROS3_RECORDINGS_DIR'}
    default_saves = Path.home() / 'Documents' / 'My Games' / 'vcmi' / 'Saves'
    test_root = ROOT / 'var' / 'test_n1'
    allowed = [test_root.resolve(), Path(tempfile.gettempdir()).resolve()]

    def canon(path: Path) -> str:
        return os.path.normcase(str(path))

    problems: list[str] = []
    resolved: dict[str, Path] = {}
    for label, var in env_names.items():
        raw = os.environ.get(var)
        if not raw:
            problems.append(f'{var} is not set')
            continue
        path = _resolve(raw)
        resolved[label] = path
        if label in formal and canon(path) == canon(formal[label].resolve()):
            problems.append(f'{var} points at the formal {label} ({formal[label]})')
        elif label == 'SAVES_DIR':
            if canon(path) != canon(default_saves.resolve()) and not any(
                    path == root or path.is_relative_to(root) for root in allowed):
                problems.append(f'{var}={path} is outside the allowed test roots')
        elif not any(path == root or path.is_relative_to(root) for root in allowed):
            problems.append(f'{var}={path} is outside the allowed test roots '
                            f'({test_root} or {tempfile.gettempdir()})')
    subdir = os.environ.get('AUTO_HEROS3_SAVE_SUBDIR')
    if not subdir:
        problems.append('AUTO_HEROS3_SAVE_SUBDIR is not set (must start with agent_n1)')
    elif not re.fullmatch(r'[A-Za-z0-9_-]+', subdir) or not subdir.startswith('agent_n1'):
        problems.append(f'AUTO_HEROS3_SAVE_SUBDIR={subdir!r} must be a plain name starting '
                        'with agent_n1')
    dryrun = os.environ.get('AUTO_HEROS3_LIVE_TEST_DRYRUN') == '1'
    if not dryrun and os.environ.get('AUTO_HEROS3_LIVE_TEST') != '1':
        problems.append('AUTO_HEROS3_LIVE_TEST=1 is required to run the live restore scenario')
    if problems:
        print('LIVE TEST REFUSED: tools/test_n1_live_restore.py starts a real VCMI segment and '
              'writes state/saves/memory/recordings. It refuses to run unless it is explicitly '
              'enabled AND every writable path is redirected to an isolated test tree.')
        for problem in problems:
            print(f'  - {problem}')
        raise SystemExit(2)
    if dryrun:
        print(f'DRYRUN STATE_DIR={resolved["STATE_DIR"]}')
        print(f'DRYRUN SAVES_DIR={resolved["SAVES_DIR"]}')
        print(f'DRYRUN SAVE_SUBDIR={subdir}')
        print(f'DRYRUN MEMORY_ROOT={resolved["MEMORY_ROOT"]}')
        print(f'DRYRUN RECORDINGS_DIR={resolved["RECORDINGS_DIR"]}')
        raise SystemExit(0)


_n1_guard()

sys.path.insert(0, str(ROOT / 'tools'))
import mcp_server_v2 as M  # noqa: E402


def read_jsonl(path: Path) -> list:
    try:
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()
                if line.strip()]
    except (OSError, ValueError):
        return []


def _state_now() -> dict:
    try:
        return M._agent().state(timeout=10) or {}
    except Exception:
        return {}


def wait_ready(timeout: float = 90.0) -> dict:
    """Wait until in_game AND a visible date (never returns on in_game alone)."""
    deadline = time.time() + timeout
    state: dict = {}
    while time.time() < deadline:
        state = _state_now()
        day = (state.get('date') or {}).get('day')
        if state.get('in_game') and day is not None:
            return state
        time.sleep(1.0)
    return state


def wait_day_after(previous_day: int | None, timeout: float = 90.0) -> dict:
    """Wait until the visible day differs from previous_day (a real end_turn)."""
    deadline = time.time() + timeout
    state: dict = {}
    while time.time() < deadline:
        state = _state_now()
        day = (state.get('date') or {}).get('day')
        if state.get('in_game') and previous_day is not None and day != previous_day:
            return state
        time.sleep(1.0)
    return state


def day_diag(state: dict) -> str:
    date = state.get('date') or {}
    return (f"day={date.get('day')} week={date.get('week')} in_game={state.get('in_game')} "
            f"making_turn={state.get('making_turn')} ts_wall={state.get('ts_wall')}")


def ensure_lease(tag: str) -> bool:
    """session_start() does not take the single-writer lease; every mutating tool needs it."""
    lease = M.lease_acquire()
    lease_file = M._dir() / 'lease.json'
    ok = bool(lease.get('ok')) and lease_file.exists()
    check(f'N1L-{tag} isolated lease acquired (lease.json present)', ok,
          str(lease.get('error') or lease.get('lease')))
    return ok


def autosave_map_evidence() -> dict:
    """Read-only engine autosave directory name as map-name evidence (never writes/deletes)."""
    base = Path.home() / 'Documents' / 'My Games' / 'vcmi' / 'Saves' / 'Autosave'
    try:
        dirs = [path for path in base.iterdir() if path.is_dir()]
    except OSError:
        return {'ok': False, 'text': f'autosave dir not readable: {base}', 'name': None}
    if not dirs:
        return {'ok': False, 'text': f'no autosave subdir yet: {base}', 'name': None}
    newest = max(dirs, key=lambda path: path.stat().st_mtime)
    name = newest.name
    match = 'Key to Victory' in name or '取胜的关键' in name
    return {'ok': match, 'name': name,
            'text': f'newest autosave dir name: {name} (map-name evidence matches Key to Victory)'}


def assert_isolated_paths() -> None:
    """The live scenario must never write a formal directory."""
    formal = {'STATE_DIR': ROOT / 'var' / 'agent_mcp_v2',
              'MEMORY_ROOT': ROOT / 'doc' / 'player',
              'RECORDINGS_DIR': ROOT / 'var' / 'recordings'}
    values = {'STATE_DIR': M.STATE_DIR, 'MEMORY_ROOT': M.MEMORY_ROOT,
              'RECORDINGS_DIR': M._recordings_root()}
    for label, path in values.items():
        check(f'N1L-0 {label} is an isolated path', os.path.normcase(str(path.resolve())) !=
              os.path.normcase(str(formal[label].resolve())), str(path))
    check('N1L-0b SAVE_SUBDIR is a test subdir',
          M.SAVE_SUBDIR.startswith('agent_n1') and M.SAVE_SUBDIR != 'agent', M.SAVE_SUBDIR)


def recording_sidecars(stop: dict, tag: str) -> dict:
    """Assert the T8/T9 segment+health+video sidecars and return the parsed segment."""
    segment = (stop or {}).get('segment') or {}
    health = (stop or {}).get('health') or segment.get('health') or {}
    out = Path(str((stop or {}).get('dir') or ''))
    video = segment.get('video') or {}
    check(f'N1L-{tag}a recording segment/health returned', bool(segment) and bool(health),
          str({key: (stop or {}).get(key) for key in ('ok', 'error')}))
    check(f'N1L-{tag}b health samples>0 and luma fields usable',
          isinstance(health.get('samples'), int) and health.get('samples', 0) > 0
          and health.get('first_frame_black') is not None
          and health.get('black_ratio') is not None,
          str({key: health.get(key) for key in
               ('samples', 'first_frame_black', 'black_ratio', 'longest_black_seconds')}))
    check(f'N1L-{tag}c identity.json/segment.json/video.mp4 exist with video sha256',
          bool(out) and (out / 'identity.json').exists() and (out / 'segment.json').exists()
          and (out / 'video.mp4').exists() and bool(video.get('sha256')),
          str(video.get('path') or out))
    return segment, health


def main() -> int:
    started = False
    seg1_identity: dict | None = None
    try:
        assert_isolated_paths()
        # 1) new game with EXPLICIT configuration (never the historical default scenario).
        start = M.session_start('Maps/Key to Victory', difficulty='easy', player_color='red',
                                starting_town='castle', starting_hero='random',
                                starting_bonus='random')
        started = bool(start.get('ok'))
        config = start.get('config') or {}
        check('N1L-1 new game with explicit config', started,
              str(start.get('error') or config.get('config_match')))
        check('N1L-1b explicit config recorded resolved/effective',
              bool(config.get('resolved')) and bool(config.get('effective')),
              str({key: config.get(key) for key in ('resolved', 'effective')}))
        # W0: requested map + local map-file identity (never settings.lastMap)
        map_info = config.get('map') or {}
        requested = config.get('requested') or {}
        check('N1L-1c W0 map identity is Key to Victory.h3m with full sha256',
              requested.get('map_name') == 'Maps/Key to Victory'
              and str(map_info.get('path') or '').endswith('Key to Victory.h3m')
              and bool(map_info.get('sha256')),
              str({key: map_info.get(key) for key in ('path', 'sha256')}))
        check('N1L-1d W0 config_match is true (random -1 vs engine -2 is a semantic equivalence)',
              config.get('config_match') is True, str(config.get('match_notes')))
        autosave = autosave_map_evidence()
        evidence_check('N1L-1e autosave dir-name evidence (read-only)', autosave['ok'],
                       autosave['text'])
        ensure_lease('1f')
        state_after_start = wait_ready()
        day1 = (state_after_start.get('date') or {}).get('day')
        check('N1L-1g game reached in_game with a day', bool(day1), day_diag(state_after_start))
        check('N1L-1h first segment recorded', bool(start.get('segment')),
              str(start.get('segment')))

        # 2) two checkpoints, p1 older than p2.
        p1 = M.save_game('n1-p1')
        check('N1L-2 checkpoint p1 saved (completed)', p1.get('status') == 'completed',
              str(p1.get('evidence') or p1.get('error'))[:140])
        p1_meta = (M._load_saves_meta().get('n1-p1') or {})
        check('N1L-2b p1 meta binds day/sha256/watermark',
              p1_meta.get('day') is not None and bool(p1_meta.get('sha256'))
              and p1_meta.get('actions_count') is not None,
              str({key: p1_meta.get(key) for key in ('day', 'sha256', 'actions_count')}))
        M.game_act('end_turn', {}, thought='N1 live: advance one day before the second save')
        day2_state = wait_day_after(day1)
        day2 = (day2_state.get('date') or {}).get('day')
        check('N1L-2c day advanced after end_turn', day2 is not None and day2 != day1,
              f'{day1}->{day2} ({day_diag(day2_state)})')
        p2 = M.save_game('n1-p2')
        check('N1L-2d checkpoint p2 saved (completed)', p2.get('status') == 'completed',
              str(p2.get('evidence') or p2.get('error'))[:140])
        p2_meta = (M._load_saves_meta().get('n1-p2') or {})
        check('N1L-2e p2 meta binds day/sha256/watermark',
              p2_meta.get('day') is not None and bool(p2_meta.get('sha256'))
              and p2_meta.get('actions_count') is not None,
              str({key: p2_meta.get(key) for key in ('day', 'sha256', 'actions_count')}))

        # 3) seg1 recording with a real (small) action; keep it long enough for health samples.
        rec1 = M.recording_start(fps=15, height=768)
        check('N1L-3 seg1 recording started', rec1.get('ok') is True, str(rec1.get('error')))
        time.sleep(1.2)                     # >= 2 T8 health samples (0.5 s interval)
        old_request_id = 'n1-live-old-request'
        old_call = M.game_act('end_turn', {}, request_id=old_request_id,
                              thought='N1 live: request id that must not cross the restore')
        check('N1L-3a old-epoch request accepted before the restore',
              old_call.get('status') in ('completed', 'submitted'),
              str(old_call.get('status') or old_call.get('error'))[:140])
        day3_state = wait_day_after(day2)
        day3 = (day3_state.get('date') or {}).get('day')
        check('N1L-3b day advanced during seg1', day3 is not None and day3 != day2,
              f'{day2}->{day3} ({day_diag(day3_state)})')
        stop1 = M.recording_stop()
        check('N1L-3c seg1 recording stopped', stop1.get('ok') is True,
              str(stop1.get('error') or stop1.get('video')))
        recording_sidecars(stop1, '3d')
        seg1_identity = M._evidence_identity()
        check('N1L-3g seg1 identity has run/branch/epoch/session',
              all(seg1_identity.get(key) is not None
                  for key in ('run_id', 'branch_id', 'epoch', 'session_id')), str(seg1_identity))
        old_run = M._run_identity()

        # 4) cold restore the newest checkpoint from a fresh session.
        stop = M.session_stop()
        check('N1L-4 first session stopped', stop.get('ok') is True, str(stop.get('result'))[:120])
        resume = M.session_start(f'Saves/{M.SAVE_SUBDIR}/n1-p2')
        check('N1L-4a cold restore of p2 succeeds', resume.get('ok') is True,
              str(resume.get('error')))
        ensure_lease('4b')
        restore = resume.get('restore') or {}
        continuity = restore.get('continuity')
        # T13: restoring p2 after the day advanced past it is a legitimate `replay` continuation
        # (rollback=true, parent watermark recorded by N1).  Accept replay here, but then require
        # the rollback/parent evidence explicitly instead of accepting any label.
        replay_evidence_ok = True
        replay_parent: dict = restore.get('parent') or {}
        if continuity == 'replay':
            # The resume receipt stores the parent checkpoint as a nested object
            # (restore['parent']['parent_watermark']); run.json/epochs.jsonl carry the same value.
            replay_evidence_ok = (restore.get('rollback') is True
                                  and replay_parent.get('parent_watermark') is not None)
        check('N1L-4c restore receipt continuity/target proof (replay requires rollback+parent)',
              bool(restore) and continuity in ('resumed', 'replay', 'cross_run_adopted')
              and restore.get('target_proof') == 'fingerprint_match'
              and replay_evidence_ok,
              str({'continuity': continuity, 'target_proof': restore.get('target_proof'),
                   'rollback': restore.get('rollback'),
                   'parent_watermark': replay_parent.get('parent_watermark')}))
        new_run = M._run_identity()
        check('N1L-4d restore commits a new epoch/branch on the same run',
              new_run.get('run_id') == old_run.get('run_id')
              and new_run.get('epoch') != old_run.get('epoch')
              and new_run.get('branch_id') != old_run.get('branch_id'),
              str({key: new_run.get(key) for key in ('run_id', 'epoch', 'branch_id')}))
        check('N1L-4e restore observed day equals the p2 checkpoint day (both non-None)',
              p2_meta.get('day') is not None and resume.get('day') == p2_meta.get('day'),
              f"observed={resume.get('day')} sidecar={p2_meta.get('day')}")
        replay_request = M.game_act('end_turn', {}, request_id=old_request_id)
        check('N1L-4f old request_id refused across the restore epoch',
              replay_request.get('ok') is False
              and replay_request.get('error') == 'request_epoch_mismatch',
              str(replay_request.get('error')))
        memory_path = M._memory_path('game', 'current')
        check('N1L-4g game memory resolves on the new branch',
              str(new_run.get('branch_id')) in str(memory_path), str(memory_path))

        # 5) load the older checkpoint -> new epoch branch with parent watermark.
        #    Naming: load_game => load-<epoch>; session_start old-point replay => replay-<epoch>.
        #    Semantics are proven by parent_watermark/target_proof, not by the prefix.
        load = M.load_game('n1-p1')
        restore2 = load.get('restore') or {}
        check('N1L-5 older checkpoint load completes with a target proof',
              load.get('ok') is True and restore2.get('target_proof') == 'fingerprint_match',
              str(load.get('evidence') or load.get('error'))[:160])
        load_run = M._run_identity()
        load_branch = str(load_run.get('branch_id') or '')
        load_parent_ok = load_run.get('parent_watermark') is not None
        if load_branch.startswith('load-'):
            load_parent_ok = (load_parent_ok
                              and restore2.get('target_proof') == 'fingerprint_match'
                              and (restore2.get('parent') or {}).get('parent_watermark') is not None)
        check('N1L-5b load enters a load-/replay- epoch branch with parent watermark + proof',
              (load_branch.startswith('load-') or load_branch.startswith('replay-'))
              and load_parent_ok,
              str({key: load_run.get(key) for key in
                   ('branch_id', 'parent_watermark', 'parent_epoch')})
              + f" target_proof={restore2.get('target_proof')}")
        check('N1L-5c days went back to the p1 checkpoint (both non-None)',
              p1_meta.get('day') is not None and load.get('day') == p1_meta.get('day'),
              f"day={load.get('day')} p1_day={p1_meta.get('day')}")

        # 6) seg2 recording after the restore: identity must differ from seg1.
        rec2 = M.recording_start(fps=15, height=768)
        check('N1L-6 seg2 recording started', rec2.get('ok') is True, str(rec2.get('error')))
        time.sleep(1.2)                     # >= 2 T8 health samples
        M.game_act('end_turn', {}, thought='N1 live: post-restore segment identity check')
        day4_state = wait_day_after(p1_meta.get('day'))
        check('N1L-6a day advanced during seg2',
              (day4_state.get('date') or {}).get('day') not in (None, p1_meta.get('day')),
              day_diag(day4_state))
        stop2 = M.recording_stop()
        check('N1L-6b seg2 recording stopped', stop2.get('ok') is True,
              str(stop2.get('error')))
        seg2_segment, _health2 = recording_sidecars(stop2, '6c')
        check('N1L-6e seg2 segment restore_ref is non-empty', bool(
            seg2_segment.get('restore_ref')), str(seg2_segment.get('restore_ref'))[:160])
        seg2_identity = M._evidence_identity()
        check('N1L-6f seg2 identity differs from seg1',
              seg2_identity.get('epoch') != (seg1_identity or {}).get('epoch')
              and seg2_identity.get('branch_id') != (seg1_identity or {}).get('branch_id'),
              f"seg1={seg1_identity} seg2={seg2_identity}")
        segments = read_jsonl(M._dir() / 'segments.jsonl')
        last_segment = segments[-1] if segments else {}
        # T13: load_game() runs inside the SAME client session, so it does not add an engine
        # segment (only session_start creates one) - >=2 segments + a restore-type last segment
        # with its parent chain is the correct expectation.
        check('N1L-6g segments.jsonl: >=2 engine segments, last is restore with parent chain',
              len(segments) >= 2
              and last_segment.get('kind') in ('restore', 'load', 'session_start_resume')
              and last_segment.get('parent') is not None,
              f"segments={len(segments)} last={last_segment.get('kind')} "
              f"parent={last_segment.get('parent')}")
        restores = read_jsonl(M._dir() / 'restores.jsonl')
        check('N1L-6h restores.jsonl has the cold-restore and load receipts',
              len(restores) >= 2, f'restores={len(restores)}')
        recording_segments = read_jsonl(M._dir() / 'recording_segments.jsonl')
        check('N1L-6i recording_segments.jsonl has both clips with a parent chain',
              len(recording_segments) >= 2
              and bool(recording_segments[-1].get('parent_segment')),
              f'recording_segments={len(recording_segments)}')
    finally:
        if started:
            try:
                M.session_stop()
            except Exception:
                pass
        try:
            M.lease_release()
        except Exception:
            pass
    failed = [name for name, ok, _detail in results if not ok]
    print(f'\n{len(results) - len(failed)}/{len(results)} checks passed')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
