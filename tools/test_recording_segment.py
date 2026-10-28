# -*- coding: utf-8 -*-
"""Offline regression for the recording identity/health/segment sidecars (§5.2, MCP side).

No game/client/recorder: the pure `finalize_recording_segment()` helper is driven against
tempfile clips; `recording_start`/`recording_stop` are exercised with a fake Popen and a temp
ROOT (no real recorder, no encoder, no VCMI).

Run: python -X utf8 tools/test_recording_segment.py
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))
import mcp_server_v2 as M  # noqa: E402
from test_action_contract import Env  # noqa: E402

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


HEALTH = {'ok': True, 'samples': 6, 'game_area_rect': [0, 0, 128, 96],
          'first_frame_black': True, 'last_frame_black': False, 'black_ratio': 0.6667,
          'longest_black_seconds': 1.5, 'black_intervals': [],
          'panel_active_during_black': True, 'window_lost_samples': 0,
          'note': '黑屏判据 game_luma<12（测试样例）'}


class FakeProc:
    def __init__(self):
        self.pid = 424242

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def kill(self):
        return None


def test_finalize_pure(tmp_path: Path) -> None:
    with Env() as env:
        # the chain fixture belongs to the ACTIVE run: a parent/restore_ref is only ever chained
        # inside one run (ISSUE-01 / R8 resets them on a new game)
        run = M._run_identity()
        run_id = str(run.get('run_id'))
        out = tmp_path / 'clip1'
        out.mkdir(parents=True, exist_ok=True)
        video_bytes = b'fake-video-bytes-for-sha'
        (out / 'video.mp4').write_bytes(video_bytes)
        meta = {'ok': True, 'frames': 30, 'fps_target': 15, 'fps_actual': 14.7,
                'height': 768, 'canvas': '1366x768', 'crf': 10, 'elapsed': 2.1,
                'video': str(out / 'video.mp4'), 'bytes': len(video_bytes)}
        write_json(out / 'meta.json', meta)
        write_json(out / M._HEALTH_SUMMARY_FILE, HEALTH)
        write_json(env.root / 'state' / 'restore.json',
                   {'kind': 'session_start_resume', 'slot': 'Saves/agent/x',
                    'file_sha256': 'ab' * 32, 'continuity': 'resumed',
                    'target_proof': 'fingerprint_match',
                    'committed': {'run_id': run_id, 'branch_id': run.get('branch_id'),
                                  'epoch': run.get('epoch')},
                    'at': '2026-10-25 21:00:00'})
        # a previous segment of the SAME run must stay untouched (parent chain)
        (env.root / 'state' / M._RECORDING_SEGMENTS_FILE).write_text(
            json.dumps({'segment_id': f'{run_id}/rec-1', 'run_id': run_id,
                        'branch_id': run.get('branch_id'), 'epoch': run.get('epoch')}) + '\n',
            encoding='utf-8')
        now = time.time()
        stamp = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(now))
        old_stamp = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(now - 3600))
        (env.root / 'state' / 'actions.jsonl').write_text(
            json.dumps({'ts': old_stamp, 'action': 'ancient'}) + '\n'
            + json.dumps({'ts': stamp, 'action': 'end_turn'}) + '\n', encoding='utf-8')
        identity = {'segment_id': f'{run_id}/rec-2', 'run_id': run_id,
                    'branch_id': run.get('branch_id'), 'epoch': run.get('epoch'),
                    'mcp_pid': 123, 'client_pid': 456, 'recorder_pid': 789}
        segment = M.finalize_recording_segment(out, identity=identity, started_at=now - 10,
                                              meta=meta)
        on_disk = json.loads((out / 'segment.json').read_text(encoding='utf-8'))
        check('R1 finalize writes <out>/segment.json equal to the returned segment',
              on_disk == segment and segment.get('kind') == 'recording_segment', '')
        check('R1b segment identity/run/branch/epoch are bound',
              segment.get('identity', {}).get('segment_id') == f'{run_id}/rec-2'
              and segment.get('run_id') == run_id and segment.get('branch_id')
              and isinstance(segment.get('epoch'), int),
              str({key: segment.get(key) for key in ('run_id', 'branch_id', 'epoch')}))
        check('R1c video full-file sha256/bytes are correct',
              segment['video']['sha256'] == hashlib.sha256(video_bytes).hexdigest()
              and segment['video']['bytes'] == len(video_bytes)
              and segment['video']['meta'].get('frames') == 30, str(segment['video'])[:200])
        check('R1d health_summary is passed through',
              segment['health'].get('available') is True
              and segment['health'].get('black_ratio') == 0.6667
              and segment['health'].get('first_frame_black') is True, '')
        check('R1e restore_ref is the newest restore.json receipt of the same run',
              (segment.get('restore_ref') or {}).get('target_proof') == 'fingerprint_match'
              and segment['restore_ref'].get('slot') == 'Saves/agent/x', '')
        check('R1f parent_segment points at the previous appended segment',
              segment.get('parent_segment') == f'{run_id}/rec-1',
              str(segment.get('parent_segment')))
        window = segment.get('actions_window') or {}
        check('R1g actions_window counts only the clip window actions',
              window.get('count') == 1 and window.get('actions') == ['end_turn'],
              str(window))
        appended = read_jsonl(env.root / 'state' / M._RECORDING_SEGMENTS_FILE)
        check('R1h recording_segments.jsonl appends without overwriting older lines',
              len(appended) == 2 and appended[0].get('segment_id') == f'{run_id}/rec-1'
              and appended[1].get('segment_id') == f'{run_id}/rec-2', str(len(appended)))

        # second clip becomes the child of the first appended segment
        out2 = tmp_path / 'clip2'
        out2.mkdir(parents=True, exist_ok=True)
        second = M.finalize_recording_segment(out2, identity={'segment_id': f'{run_id}/rec-3'},
                                              started_at=now, meta={})
        check('R2 a second clip chains parent_segment to the first clip',
              second.get('parent_segment') == f'{run_id}/rec-2',
              str(second.get('parent_segment')))
        check('R2b missing health -> null + reason, no crash',
              second['health'].get('available') is False
              and 'reason' in second['health'], str(second['health'])[:160])
        check('R2c missing video -> sha256 null, no crash',
              second['video'].get('sha256') is None and second['video'].get('exists') is False,
              str(second['video'])[:160])
        check('R2d segment.json is written even without health/video',
              (out2 / 'segment.json').exists(), '')

        # ISSUE-01 guard: leftover evidence from ANOTHER run is never chained into this run.
        (env.root / 'state' / M._RECORDING_SEGMENTS_FILE).write_text(
            json.dumps({'segment_id': 'oldrun/rec-9', 'run_id': 'oldrun'}) + '\n',
            encoding='utf-8')
        write_json(env.root / 'state' / 'restore.json',
                   {'kind': 'session_start_resume', 'slot': 'Saves/agent/old',
                    'committed': {'run_id': 'oldrun'}, 'target_proof': 'fingerprint_match'})
        out3 = tmp_path / 'clip3'
        out3.mkdir(parents=True, exist_ok=True)
        third = M.finalize_recording_segment(out3, identity={'segment_id': f'{run_id}/rec-4'},
                                             started_at=now, meta={})
        check('R1i a previous-run clip is never used as parent',
              third.get('parent') is None and third.get('parent_segment') is None,
              str(third.get('parent_segment')))
        check('R1j a previous-run restore receipt is not attached as restore_ref',
              third.get('restore_ref') is None, str(third.get('restore_ref'))[:160])


def test_recording_start_stop_identity(tmp_path: Path) -> None:
    with Env() as env:
        original_root = M.ROOT
        original_popen = subprocess.Popen
        M.ROOT = tmp_path / 'root'
        subprocess.Popen = lambda *args, **kwargs: FakeProc()
        M._recorder.update({'proc': None, 'dir': None})
        try:
            started = M.recording_start(fps=15, height=768)
            out = Path(started['dir'])
            identity = json.loads((out / 'identity.json').read_text(encoding='utf-8'))
            required = {'run_id', 'branch_id', 'epoch', 'segment_id', 'recording_started_at',
                        'client_pid', 'mcp_pid', 'exe_sha256', 'protocol',
                        'contract_built_from_commit', 'window_rect', 'offscreen', 'canvas',
                        'recorder_pid'}
            check('R3 recording_start writes <out>/identity.json with full identity',
                  started.get('ok') is True and required <= set(identity),
                  str(sorted(required - set(identity))))
            check('R3b identity is bound to the active run and recorder pid',
                  identity.get('run_id') == M._run_identity().get('run_id')
                  and identity.get('recorder_pid') == 424242
                  and identity.get('canvas') == started.get('canvas'),
                  str({key: identity.get(key) for key in ('run_id', 'segment_id', 'canvas')}))
            state = json.loads((env.root / 'state' / 'recording.json').read_text(
                encoding='utf-8'))
            check('R3c recording.json keeps the identity + segment id',
                  state.get('identity') == identity and state.get('segment_id') == identity.get(
                      'segment_id'), str(state.get('segment_id')))

            video_bytes = b'clip-video-bytes'
            (out / 'video.mp4').write_bytes(video_bytes)
            write_json(out / M._HEALTH_SUMMARY_FILE, HEALTH)
            write_json(out / 'meta.json', {'ok': True, 'frames': 12, 'fps_target': 15,
                                           'video': str(out / 'video.mp4')})
            stopped = M.recording_stop()
            segment = stopped.get('segment') or {}
            check('R4 recording_stop returns segment+health and writes segment.json',
                  stopped.get('ok') is True and segment and (out / 'segment.json').exists()
                  and stopped.get('health', {}).get('available') is True,
                  str(stopped.get('error')))
            check('R4b stop segment binds identity, video sha and run/branch/epoch',
                  segment.get('identity', {}).get('segment_id') == identity.get('segment_id')
                  and segment['video']['sha256'] == hashlib.sha256(video_bytes).hexdigest()
                  and segment.get('run_id') == identity.get('run_id'),
                  str(segment.get('segment_id')))
            status = M.recording_status()
            check('R5 recording_status reports the last segment and health availability',
                  status.get('ok') is True
                  and (status.get('last_segment') or {}).get('segment_id') ==
                  identity.get('segment_id'),
                  str(status.get('last_segment'))[:160])
        finally:
            subprocess.Popen = original_popen
            M.ROOT = original_root
            M._recorder.update({'proc': None, 'dir': None})


def main() -> int:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        test_finalize_pure(base / 'pure')
        test_recording_start_stop_identity(base / 'integration')
    failed = [item for item in RESULTS if not item[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
