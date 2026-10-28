# -*- coding: utf-8 -*-
"""Offline regression for W0 start-config identity collection (plan §5.1 '新局启动').

No game, no client: `VcmiAgent` is replaced by a stand-in that writes the bridge's start-option
/ scenario lines into the temp results.log; map files and the client log live in temp paths.

Covered:
  * requested only contains explicitly passed options (defaults are not recorded as requested)
  * resolved options are recorded exactly as written for the bridge
  * effective is parsed from 'OK start_options_effective ...'
  * local .h3m identity (path/bytes/mtime/sha256) and null+reason when missing
  * requested-vs-effective mismatch sets config_match=false with notes
  * <state>/start_config.json contains exactly the returned config

Run: python -X utf8 tools/test_w0_config.py
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
from test_action_contract import Env, state  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition, detail: str = '') -> None:
    RESULTS.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


class ConfigAgent:
    """Stand-in for VcmiAgent: writes the bridge lines a real start would log."""

    def __init__(self, result_lines: list[str], post_state: dict | None = None):
        self.lines = list(result_lines)
        self.post_state = dict(post_state or state(day=1))
        self.started = 0
        self.contract = {'match': True, 'protocol_ok': True, 'actual_sha256': 'b' * 64,
                         'built_from_commit': 'c0ffee', 'repo_head': 'c0ffee',
                         'source_matches_head': True}
        self.session = types.SimpleNamespace(session_id='w0-session', client_pid=420000,
                                             created_at=time.time())

    def start(self, timeout: float = 120.0) -> bool:
        self.started += 1
        try:
            (M._dir() / 'results.log').write_text('\n'.join(self.lines) + '\n',
                                                  encoding='utf-8')
        except OSError:
            pass
        return True

    def wait_in_game(self, timeout: float = 240.0) -> dict:
        return dict(self.post_state)

    def state(self, timeout: float = 10.0) -> dict:
        return dict(self.post_state)

    def window_is_offscreen(self) -> bool:
        return True

    def stop(self) -> dict:
        return {'killed': True}


def start_session(env: Env, agent: ConfigAgent, **kwargs):
    original = M.VcmiAgent
    M.VcmiAgent = lambda *args, **kw: agent
    try:
        return M.session_start(**kwargs)
    finally:
        M.VcmiAgent = original


EFFECTIVE_OK = [
    'INFO start_option_loaded difficulty=0',
    'OK start_option_applied difficulty=0',
    'OK start_options_effective difficulty=0 player=0 town=0 turn_time=0',
    'INFO scenario Key to Victory loaded for player red',
]


def test_requested_and_effective(tmp_path: Path) -> None:
    maps = tmp_path / 'maps'
    maps.mkdir(parents=True, exist_ok=True)
    map_file = maps / 'Key to Victory.h3m'
    map_file.write_bytes(b'H3M-map-bytes-for-test')
    client_log = tmp_path / 'VCMI_Client_log.txt'
    client_log.write_text('VCMI version 1.7.5\nLoading mod: hota\nINFO client started\n',
                          encoding='utf-8')
    saved_dirs, saved_log = M.MAP_FILE_DIRS, M.CLIENT_LOG_PATH
    M.MAP_FILE_DIRS = [maps]
    M.CLIENT_LOG_PATH = client_log
    try:
        with Env() as env:
            agent = ConfigAgent(EFFECTIVE_OK)
            res = start_session(env, agent, map_name='Maps/Key to Victory',
                                difficulty='easy', player_color='red',
                                turn_time_seconds=0)
            config = res.get('config') or {}
            requested = config.get('requested') or {}
            resolved = config.get('resolved') or {}
            effective = config.get('effective') or {}
            check('W0-1 session_start returns a config object', res.get('ok') is True
                  and isinstance(config, dict) and config.get('effective') == effective,
                  f'ok={res.get("ok")}')
            check('W0-1b requested records exactly the explicit arguments',
                  requested.get('map_name') == 'Maps/Key to Victory'
                  and requested.get('difficulty') == 'easy'
                  and requested.get('player_color') == 'red'
                  and requested.get('turn_time_seconds') == 0
                  and 'starting_hero' not in requested,
                  str(requested))
            check('W0-1c resolved holds the numeric bridge values',
                  resolved.get('difficulty') == 0 and resolved.get('player') == 0
                  and resolved.get('turn_time') == 0, str(resolved))
            check('W0-1d effective parsed from start_options_effective',
                  effective.get('difficulty') == 0 and effective.get('player') == 0
                  and effective.get('turn_time') == 0, str(effective))
            check('W0-1e requested vs effective match -> config_match true',
                  config.get('config_match') is True, str(config.get('match_notes')))
            identity = config.get('map') or {}
            check('W0-1f map file identity is a full-file sha256',
                  identity.get('path') == str(map_file)
                  and identity.get('sha256') == M._file_sha256(map_file)
                  and identity.get('bytes') == map_file.stat().st_size,
                  str(identity)[:200])
            client = config.get('client') or {}
            check('W0-1g client log head records VCMI version/mod lines',
                  client.get('found') is True
                  and any('version' in line.lower() for line in client.get('lines') or []),
                  str(client.get('lines'))[:160])
            check('W0-1h visible scenario INFO lines are recorded',
                  any('scenario' in line.lower() for line in
                      config.get('story_lines') or []),
                  str(config.get('story_lines'))[:160])
            on_disk = json.loads((env.root / 'state' / 'start_config.json').read_text(
                encoding='utf-8'))
            check('W0-1i <state>/start_config.json equals the returned config',
                  on_disk == config, '')
    finally:
        M.MAP_FILE_DIRS, M.CLIENT_LOG_PATH = saved_dirs, saved_log


def test_defaults_not_requested(tmp_path: Path) -> None:
    saved_dirs, saved_log = M.MAP_FILE_DIRS, M.CLIENT_LOG_PATH
    M.MAP_FILE_DIRS = [tmp_path / 'empty-maps']
    M.CLIENT_LOG_PATH = tmp_path / 'missing-log.txt'
    try:
        with Env() as env:
            agent = ConfigAgent(EFFECTIVE_OK)
            res = start_session(env, agent, map_name='Key to Victory')
            requested = (res.get('config') or {}).get('requested') or {}
            check('W0-2 defaults are not reported as requested options',
                  set(requested) == {'map_name'} and requested['map_name'] == 'Maps/Key to Victory',
                  str(requested))
            check('W0-2b missing client log is recorded as not found, not fabricated',
                  (res.get('config') or {}).get('client', {}).get('found') is False,
                  str((res.get('config') or {}).get('client'))[:160])
    finally:
        M.MAP_FILE_DIRS, M.CLIENT_LOG_PATH = saved_dirs, saved_log


def test_missing_map_file(tmp_path: Path) -> None:
    saved_dirs, saved_log = M.MAP_FILE_DIRS, M.CLIENT_LOG_PATH
    M.MAP_FILE_DIRS = [tmp_path / 'no-such-maps']
    M.CLIENT_LOG_PATH = tmp_path / 'missing-log.txt'
    try:
        with Env() as env:
            agent = ConfigAgent(EFFECTIVE_OK)
            res = start_session(env, agent, map_name='Maps/Unknown Map')
            identity = (res.get('config') or {}).get('map') or {}
            check('W0-3 missing map file -> null identity + reason (no settings.lastMap guess)',
                  identity.get('path') is None and identity.get('sha256') is None
                  and 'no local .h3m' in str(identity.get('reason')),
                  str(identity)[:200])
    finally:
        M.MAP_FILE_DIRS, M.CLIENT_LOG_PATH = saved_dirs, saved_log


def test_effective_mismatch(tmp_path: Path) -> None:
    saved_dirs, saved_log = M.MAP_FILE_DIRS, M.CLIENT_LOG_PATH
    M.MAP_FILE_DIRS = [tmp_path / 'no-maps']
    M.CLIENT_LOG_PATH = tmp_path / 'missing-log.txt'
    lines = ['OK start_options_effective difficulty=2 player=0 town=0 turn_time=0']
    try:
        with Env() as env:
            agent = ConfigAgent(lines)
            res = start_session(env, agent, map_name='Maps/Key to Victory',
                                difficulty='easy', player_color='red')
            config = res.get('config') or {}
            notes = config.get('match_notes') or []
            check('W0-4 requested/effective mismatch -> config_match false with notes',
                  config.get('config_match') is False
                  and any('difficulty' in note for note in notes), str(notes))
    finally:
        M.MAP_FILE_DIRS, M.CLIENT_LOG_PATH = saved_dirs, saved_log


def test_missing_effective(tmp_path: Path) -> None:
    saved_dirs, saved_log = M.MAP_FILE_DIRS, M.CLIENT_LOG_PATH
    M.MAP_FILE_DIRS = [tmp_path / 'no-maps']
    M.CLIENT_LOG_PATH = tmp_path / 'missing-log.txt'
    try:
        with Env() as env:
            agent = ConfigAgent(['INFO start_option_loaded difficulty=0'])
            res = start_session(env, agent, map_name='Maps/Key to Victory',
                                difficulty='easy')
            config = res.get('config') or {}
            check('W0-5 bridge without an effective line -> effective null, match undecidable',
                  config.get('effective') is None and config.get('config_match') is None,
                  f"effective={config.get('effective')} match={config.get('config_match')}")
    finally:
        M.MAP_FILE_DIRS, M.CLIENT_LOG_PATH = saved_dirs, saved_log


def test_random_equivalence(tmp_path: Path) -> None:
    """W0 semantic equivalence: requested random(-1) vs engine effective -2 is NOT a mismatch."""
    saved_dirs, saved_log = M.MAP_FILE_DIRS, M.CLIENT_LOG_PATH
    maps = tmp_path / 'maps'
    maps.mkdir(parents=True, exist_ok=True)
    (maps / 'Key to Victory.h3m').write_bytes(b'random-map-bytes')
    M.MAP_FILE_DIRS = [maps]
    M.CLIENT_LOG_PATH = tmp_path / 'missing-log.txt'
    try:
        lines = ['OK start_options_effective difficulty=0 player=0 town=0 hero=-2 bonus=-2']
        with Env() as env:
            agent = ConfigAgent(lines)
            res = start_session(env, agent, map_name='Maps/Key to Victory',
                                difficulty='easy', player_color='red',
                                starting_town='castle', starting_hero='random',
                                starting_bonus='random')
            config = res.get('config') or {}
            notes = config.get('match_notes') or []
            resolved = config.get('resolved') or {}
            check('W0-6 hero/bonus random(-1) vs engine(-2) is a semantic equivalence',
                  resolved.get('hero') == -1 and resolved.get('bonus') == -1
                  and config.get('config_match') is True
                  and any('semantic_equivalence' in note and 'hero' in note for note in notes)
                  and any('semantic_equivalence' in note and 'bonus' in note for note in notes),
                  str(notes))
        # a genuine mismatch (explicit hero id vs another effective id) stays false
        lines2 = ['OK start_options_effective difficulty=0 player=0 town=0 hero=7 bonus=-2']
        with Env() as env:
            agent = ConfigAgent(lines2)
            res = start_session(env, agent, map_name='Maps/Key to Victory',
                                difficulty='easy', player_color='red', starting_hero=3)
            config = res.get('config') or {}
            check('W0-6b a real hero mismatch still sets config_match false',
                  config.get('config_match') is False
                  and any('hero' in note and 'requested' in note
                          for note in config.get('match_notes') or []),
                  str(config.get('match_notes')))
    finally:
        M.MAP_FILE_DIRS, M.CLIENT_LOG_PATH = saved_dirs, saved_log


def main() -> int:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        test_requested_and_effective(base / 'case1')
        test_defaults_not_requested(base / 'case2')
        test_missing_map_file(base / 'case3')
        test_effective_mismatch(base / 'case4')
        test_missing_effective(base / 'case5')
        test_random_equivalence(base / 'case6')
    failed = [item for item in RESULTS if not item[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
