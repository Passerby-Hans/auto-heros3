# -*- coding: utf-8 -*-
"""P5 placement contract: VCMI settings backup/patch/restore and display mode flags.

Pure filesystem test; no VCMI process, no display mutation.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools'))

from auto_heros3.runtime import vcmi_agent as A  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    RESULTS.append((name, bool(ok), str(detail)))


def test_patch_restore() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / 'settings.json'
        original = {
            'video': {'displayIndex': 0, 'fullscreen': True,
                      'resolution': {'width': 1920, 'height': 1080}},
            'other': {'keep': True},
        }
        raw = json.dumps(original, ensure_ascii=False, indent=2).encode('utf-8')
        path.write_bytes(raw)
        old = A.VCMI_SETTINGS
        A.VCMI_SETTINGS = path
        try:
            agent = A.VcmiAgent(Path(td) / 'bridge', offscreen=True, log=False,
                                display_index=2)
            check('display_disables_offscreen', agent.offscreen is False)
            agent._patch_video_settings()
            patched = json.loads(path.read_text(encoding='utf-8'))
            check('patched_display_index', patched['video']['displayIndex'] == 2,
                  str(patched.get('video')))
            check('patched_fullscreen_false', patched['video']['fullscreen'] is False)
            check('patched_resolution_4x3',
                  patched['video']['resolution'] == {'width': 1024, 'height': 768},
                  str(patched['video'].get('resolution')))
            check('patched_music_sound_nonzero',
                  patched['general']['music'] == 30 and patched['general']['sound'] == 40,
                  str(patched.get('general')))
            check('other_settings_preserved', patched['other'] == {'keep': True})
            # session_stop constructs a fresh agent: the persisted backup must restore even
            # when the instance that patched settings is gone.
            fresh = A.VcmiAgent(Path(td) / 'bridge', log=False)
            fresh._restore_video_settings()
            check('restore_exact_bytes_fresh_agent', path.read_bytes() == raw,
                  'bytes differ after fresh-agent restore')
            agent._restore_video_settings()  # idempotent
            check('restore_idempotent', path.read_bytes() == raw)
        finally:
            A.VCMI_SETTINGS = old


def test_missing_settings() -> None:
    with tempfile.TemporaryDirectory() as td:
        old = A.VCMI_SETTINGS
        A.VCMI_SETTINGS = Path(td) / 'missing.json'
        try:
            agent = A.VcmiAgent(Path(td) / 'bridge', log=False, display_index=2)
            try:
                agent._patch_video_settings()
                check('missing_settings_rejected', False, 'no error')
            except A.BridgeError:
                check('missing_settings_rejected', True)
            check('missing_settings_no_backup', agent._settings_backup is None)
        finally:
            A.VCMI_SETTINGS = old


def main() -> int:
    test_patch_restore()
    test_missing_settings()
    for name, ok, detail in RESULTS:
        print(('PASS ' if ok else 'FAIL ') + name + (f'  ({detail})' if detail else ''))
    failed = [r for r in RESULTS if not r[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
