# -*- coding: utf-8 -*-
r"""LIVE/DEBUG acceptance test for P1 (honest action status) + P2 (two-tier memory) + window safety.

LIVE/DEBUG - NOT part of the maintenance regression suite; refused by default.

This test runs a real (off-screen, debug) game: it starts a scenario, fast-forwards with cheated
gold, writes lease.json, saves, and writes the player/game memory tree.  It must never run against
the formal paths, and an accidental `python tools/test_session_memory.py` must do nothing.

Safety gate (executed BEFORE importing mcp_server_v2, so a refused run touches no path):
  * AUTO_HEROS3_LIVE_TEST=1 is required for the real body.
  * every writable path must be redirected before the import, because the server resolves its
    path constants at import time:
        AUTO_HEROS3_STATE_DIR       -> isolated state dir
        AUTO_HEROS3_SAVES_DIR       -> saves root (the default root is tolerated)
        AUTO_HEROS3_SAVE_SUBDIR     -> must be a plain name starting with 'agent_n0'
        AUTO_HEROS3_MEMORY_ROOT     -> isolated memory tree
        AUTO_HEROS3_RECORDINGS_DIR  -> isolated recordings dir
    STATE/MEMORY/RECORDINGS must live under ROOT/var/test_n0/ or %TEMP%; the formal dirs
    (var/agent_mcp_v2, doc/player, var/recordings) and the 'agent' save subdir are refused.
  * AUTO_HEROS3_LIVE_TEST_DRYRUN=1 prints the resolved paths and exits 0 WITHOUT importing the
    server module or touching the game.  tools/test_n0_isolation.py uses this to verify the
    import-time injection offline.

Manual run (only after creating an isolated tree):
    set AUTO_HEROS3_LIVE_TEST=1
    set AUTO_HEROS3_STATE_DIR=%TEMP%\n0\state
    set AUTO_HEROS3_SAVES_DIR=<saves root>
    set AUTO_HEROS3_SAVE_SUBDIR=agent_n0_manual
    set AUTO_HEROS3_MEMORY_ROOT=%TEMP%\n0\player
    set AUTO_HEROS3_RECORDINGS_DIR=%TEMP%\n0\recordings
    .venv\\Scripts\\python.exe -X utf8 tools\\test_session_memory.py
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


def _resolve_test_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def _n0_isolation_guard() -> None:
    """Refuse to continue unless every writable path is an explicit, isolated test tree."""
    formal = {
        'STATE_DIR': ROOT / 'var' / 'agent_mcp_v2',
        'MEMORY_ROOT': ROOT / 'doc' / 'player',
        'RECORDINGS_DIR': ROOT / 'var' / 'recordings',
    }
    env_names = {
        'STATE_DIR': 'AUTO_HEROS3_STATE_DIR',
        'SAVES_DIR': 'AUTO_HEROS3_SAVES_DIR',
        'MEMORY_ROOT': 'AUTO_HEROS3_MEMORY_ROOT',
        'RECORDINGS_DIR': 'AUTO_HEROS3_RECORDINGS_DIR',
    }
    default_saves = Path.home() / 'Documents' / 'My Games' / 'vcmi' / 'Saves'
    test_root = ROOT / 'var' / 'test_n0'
    allowed_roots = [test_root.resolve(), Path(tempfile.gettempdir()).resolve()]

    def canon(path: Path) -> str:
        return os.path.normcase(str(path))

    def under_allowed(path: Path) -> bool:
        return any(path == root or path.is_relative_to(root) for root in allowed_roots)

    problems: list[str] = []
    resolved: dict[str, Path] = {}
    for label, var in env_names.items():
        raw = os.environ.get(var)
        if not raw:
            problems.append(f'{var} is not set')
            continue
        path = _resolve_test_path(raw)
        resolved[label] = path
        if label in formal and canon(path) == canon(formal[label].resolve()):
            problems.append(f'{var} points at the formal {label} ({formal[label]})')
        elif label == 'SAVES_DIR':
            if canon(path) != canon(default_saves.resolve()) and not under_allowed(path):
                problems.append(f'{var}={path} is not the default saves root and is outside the '
                                f'allowed test roots ({test_root} or {tempfile.gettempdir()})')
        elif not under_allowed(path):
            problems.append(f'{var}={path} is outside the allowed test roots '
                            f'({test_root} or {tempfile.gettempdir()})')

    subdir = os.environ.get('AUTO_HEROS3_SAVE_SUBDIR')
    if not subdir:
        problems.append('AUTO_HEROS3_SAVE_SUBDIR is not set (must be a plain name starting '
                        "with 'agent_n0')")
    elif not re.fullmatch(r'[A-Za-z0-9_-]+', subdir):
        problems.append(f'AUTO_HEROS3_SAVE_SUBDIR={subdir!r} is not a plain directory name '
                        'matching [A-Za-z0-9_-]+')
    elif subdir == 'agent':
        problems.append("AUTO_HEROS3_SAVE_SUBDIR='agent' is the formal save subdir; use a name "
                        "starting with 'agent_n0'")
    elif not subdir.startswith('agent_n0'):
        problems.append(f"AUTO_HEROS3_SAVE_SUBDIR={subdir!r} must start with 'agent_n0'")

    dryrun = os.environ.get('AUTO_HEROS3_LIVE_TEST_DRYRUN') == '1'
    if not dryrun and os.environ.get('AUTO_HEROS3_LIVE_TEST') != '1':
        problems.append('AUTO_HEROS3_LIVE_TEST=1 is required to run this live test (its body '
                        'starts a real game and writes memory/saves)')

    if problems:
        print('LIVE TEST REFUSED: tools/test_session_memory.py is a LIVE/DEBUG test that starts '
              'a real game and writes the state dir, lease, saves and memory tree. It refuses to '
              'run unless it is explicitly enabled AND every writable path is redirected to an '
              'isolated test tree.')
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


_n0_isolation_guard()

sys.path.insert(0, str(ROOT / 'tools'))
import mcp_server_v2 as M  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    results.append((name, ok, detail))
    print(('PASS' if ok else 'FAIL'), name, detail)


def day_now() -> int | None:
    return (M._agent().state(timeout=10).get('date') or {}).get('day')


def wait_day_change(previous: int | None, timeout: float = 30.0) -> int | None:
    deadline = time.time() + timeout
    current = previous
    while time.time() < deadline:
        current = day_now()
        if current != previous:
            return current
        time.sleep(0.5)
    return current


def main() -> int:
    started = False
    try:
        start = M.session_start('Maps/Arrogance')
        started = start.get('ok', False)
        check('session_start', bool(start.get('ok')), str(start.get('day')))
        check('lease_acquire', bool(M.lease_acquire().get('ok')))

        # 1) close_window must never pop the adventure map / corrupt the window stack.
        line = M._agent().execute('close_window', verify=None, result_timeout=10).line
        check('close_window_safe_no_root_pop', 'OK' not in line and 'unsafe_window' in line, line)

        # 2) end_turn: honest status + the old crash path stays dead.
        before_day = day_now()
        res = M.game_act('end_turn', None, request_id='test-end-turn-1')
        check('end_turn_status_honest',
              res.get('ok') == (res.get('status') == 'completed')
              and res.get('status') in ('completed', 'submitted'),
              f"{res.get('status')} {res.get('evidence')}")
        after_day = wait_day_change(before_day)
        check('end_turn_no_crash', after_day is not None and after_day != before_day,
              f'{before_day}->{after_day}')

        # 3) argument validation happens BEFORE anything is sent (no newline -> 2nd command).
        injected = M.game_act('build', {'town': 0, 'id': '8\nend_turn'})
        check('newline_injection_rejected',
              injected.get('ok') is False and injected.get('sent') is False,
              str(injected.get('error'))[:70])

        # 4) lease token: a lease file with a foreign token must not authorise actions.
        lease_path = M._dir() / 'lease.json'
        good_lease = json.loads(lease_path.read_text(encoding='utf-8'))
        lease_path.write_text(json.dumps(dict(good_lease, token='foreign-token')),
                              encoding='utf-8')
        blocked = M.game_act('build', {'town': 0, 'id': 8}, request_id='test-lease-bad')
        check('lease_token_mismatch_rejected', blocked.get('ok') is False
              and 'token' in str(blocked.get('error', '')), str(blocked.get('error'))[:70])
        M.lease_acquire()

        # 5) build: completed only when the building really appeared.
        M.debug_resources({'gold': 50000})
        turn_deadline = time.time() + 90
        while time.time() < turn_deadline:
            if M._agent().state(timeout=10).get('making_turn'):
                break
            time.sleep(0.5)
        res: dict = {}
        for attempt in range(5):
            res = M.game_act('build', {'town': 0, 'id': 8},
                             request_id=f'test-build-{attempt}')
            if res.get('status') == 'completed':
                break
            if 'not_my_turn' in str(res.get('evidence', '')):
                time.sleep(2)                   # opponent turn still settling, retry
                continue
            time.sleep(1)
        check('build_completed', res.get('status') == 'completed' and res.get('ok') is True,
              f"{res.get('status')} {res.get('evidence')}")
        check('build_evidence_is_diff', 'appeared' in str(res.get('evidence', '')),
              str(res.get('evidence'))[:70])
        action_id = res.get('action_id')
        lookup = M.game_action_status(action_id)
        check('action_status_lookup',
              bool(lookup.get('ok')) and lookup['action'].get('status') == 'completed'
              and lookup['action'].get('action_id') == action_id, action_id or '')

        # 6) request_id idempotency: same id never sends a second command.
        used = res.get('request_id')
        retry = M.game_act('build', {'town': 0, 'id': 8}, request_id=used)
        check('request_dedup', retry.get('deduplicated') is True
              and retry.get('action_id') == action_id, f"seq={retry.get('seq')}")
        conflict = M.game_act('build', {'town': 0, 'id': 9}, request_id=used)
        check('request_id_conflict', conflict.get('ok') is False,
              str(conflict.get('error'))[:70])

        # 7) two-tier memory: write -> read back -> stale revision conflict.
        body = ('# 当前目标\n\n先证明动作与记忆闭环可信：建一个建筑并记录依据。\n\n'
                '## 历史观察\n\n- 2026-10-24 测试局：建造返回 completed（见 source_refs）。\n')
        existing = M.memory_read('game', 'current')
        base_rev = int(existing.get('revision') or 0)
        wrote = M.memory_write('game', 'current', body, expected_revision=base_rev,
                               sources=[action_id or 'test-build-1', 'actions.jsonl'],
                               observed_day=after_day)
        check('memory_write', bool(wrote.get('ok')) and wrote.get('revision') == base_rev + 1,
              str(wrote.get('path')))
        read = M.memory_read('game', 'current')
        check('memory_read_back', read.get('exists') and '动作与记忆闭环' in read.get('body', ''),
              f"rev={read.get('revision')}")
        stale = M.memory_write('game', 'current', 'x', expected_revision=0)
        check('memory_revision_conflict', stale.get('error') == 'revision_conflict',
              str(stale.get('error')))
        topic = M.memory_write('long_term', 'battle', '# 战斗经验\n\n- candidate：射手先打。')
        check('memory_long_term', bool(topic.get('ok')), str(topic.get('path')))
        bad = M.memory_read('long_term', '../etc/passwd')
        check('memory_sandbox', bad.get('ok') is False, str(bad.get('error'))[:70])

        # 8) persistence: events stream + brief identity/memory index.
        events = M.game_events()
        check('events_reported', isinstance(events, dict) and 'events' in events,
              str(list(events))[:70])
        brief = M.game_brief()
        check('brief_identity', brief.get('identity', {}).get('run_id') is not None, '')
        check('brief_memory_index', 'current' in brief.get('memory', {}),
              str(brief.get('memory', {}).get('current')))
    finally:
        if started:
            try:
                M.session_stop()
            except Exception:                   # pragma: no cover - cleanup only
                pass
    failed = [name for name, ok, _ in results if not ok]
    print(f'\n{len(results) - len(failed)}/{len(results)} checks passed')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
