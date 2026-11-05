# -*- coding: utf-8 -*-
"""S4a recording manifest/journal contract (offline, no game/media/audio)."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from auto_heros3.recording.recording_manifest import (  # noqa: E402
    ManifestError, RecordingManifest, read_journal)
import auto_heros3.recording.recording_manifest as rm  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    RESULTS.append((name, bool(ok), str(detail)))


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        run_dir = Path(td) / 'run'
        m = RecordingManifest.create(run_dir, recording_run_id='run-1', owner_token='owner-a',
                                     backend='test', game_identity={'session_id': 's1'})
        check('create_manifest', m.path.exists() and m.data['state'] == 'preparing')
        check('create_journal_event', any(e.get('event') == 'run_created' for e in m.events))

        loaded = RecordingManifest.load(run_dir)
        check('reload_manifest', loaded.data['recording_run_id'] == 'run-1')
        loaded.assert_owner('owner-a')
        try:
            loaded.assert_owner('other')
            check('owner_mismatch', False)
        except ManifestError:
            check('owner_mismatch', True)

        m.transition_run('recording')
        m.add_segment('seg-0', index=0)
        m.add_segment('seg-0', index=0)
        check('segment_add_idempotent', len(m.data['segments']) == 1)
        m.seal_segment('seg-0', path='seg-0.mp4', bytes=1234)
        m.transition_segment('seg-0', 'validating')
        m.transition_segment('seg-0', 'validated')
        m.transition_segment('seg-0', 'complete')
        check('segment_transition_chain', m.data['segments'][0]['state'] == 'complete')

        try:
            m.transition_segment('seg-0', 'sealed')
            check('illegal_segment_transition', False)
        except ManifestError:
            check('illegal_segment_transition', True)

        first = m.append_event('probe', event_key='probe:1', value=1)
        second = m.append_event('probe', event_key='probe:1', value=2)
        check('duplicate_event_key', first and not second)
        check('journal_seq_monotonic',
              [e['seq'] for e in read_journal(m.journal_path)] ==
              sorted(e['seq'] for e in read_journal(m.journal_path)))

        m.transition_run('stopping')
        m.transition_run('finalizing')
        m.transition_run('complete')
        check('run_transition_chain', m.data['state'] == 'complete')
        try:
            m.transition_run('recording')
            check('illegal_run_transition', False)
        except ManifestError:
            check('illegal_run_transition', True)

        # A crash during the final journal append leaves one truncated line; valid facts survive.
        text = m.journal_path.read_text(encoding='utf-8')
        m.journal_path.write_text(text + '{"seq":999,"event":"trunc', encoding='utf-8')
        events = read_journal(m.journal_path)
        check('truncated_journal_tolerated', len(events) == len(m.events))

        # Corrupt interior JSON is not silently ignored.
        bad = run_dir / 'bad.jsonl'
        bad.write_text('{"seq":1}\nnot-json\n{"seq":2}\n', encoding='utf-8')
        try:
            read_journal(bad)
            check('corrupt_interior_rejected', False)
        except ManifestError:
            check('corrupt_interior_rejected', True)

        # Reload after the final state and verify the segment snapshot survives.
        reloaded = RecordingManifest.load(run_dir)
        check('final_reload', reloaded.data['state'] == 'complete'
              and reloaded.data['segments'][0]['bytes'] == 1234)

        # Windows transient PermissionError on os.replace must be retried.
        atomic = run_dir / 'atomic.json'
        original_replace = rm.os.replace
        attempts = {'n': 0}
        def flaky_replace(src, dst):
            attempts['n'] += 1
            if attempts['n'] < 3:
                raise PermissionError('simulated transient lock')
            return original_replace(src, dst)
        rm.os.replace = flaky_replace
        try:
            rm._atomic_json(atomic, {'ok': True})
            check('atomic_replace_retry', attempts['n'] == 3 and atomic.exists(),
                  f'attempts={attempts["n"]}')
        finally:
            rm.os.replace = original_replace

    for name, ok, detail in RESULTS:
        print(('PASS ' if ok else 'FAIL ') + name + (f'  ({detail})' if detail else ''))
    failed = [r for r in RESULTS if not r[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
