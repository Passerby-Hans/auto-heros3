# -*- coding: utf-8 -*-
"""Offline regression for N0: dangerous live test isolation + import-time path injection.

Pure subprocess/tempfile: it never imports mcp_server_v2 in-process, never starts VCMI, never
touches the formal dirs (doc/player, var/agent_mcp_v2, var/recordings — snapshotted before/after),
and never runs the real body of tools/test_session_memory.py (only its refusal and DRYRUN paths).

Run:
    .venv\\Scripts\\python.exe -X utf8 tools\\test_n0_isolation.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE_TEST = ROOT / 'tools' / 'test_session_memory.py'

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition, detail: str = '') -> None:
    RESULTS.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


# ------------------------------------------------------------------ helpers
def clean_env(extra: dict | None = None) -> dict:
    """A copy of the current environment without any AUTO_HEROS3_* isolation leakage."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('AUTO_HEROS3_')}
    env['PYTHONIOENCODING'] = 'utf-8'
    if extra:
        env.update(extra)
    return env


def run_py(args: list[str], env: dict, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, '-X', 'utf8', *args], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, encoding='utf-8', errors='replace',
                          timeout=timeout)


def make_tree() -> dict:
    root = Path(tempfile.mkdtemp(prefix='n0-isolation-'))
    return {'root': root, 'state': root / 'state', 'saves_root': root / 'saves-root',
            'memory': root / 'memory', 'recordings': root / 'recordings'}


def iso_env(tree: dict, extra: dict | None = None) -> dict:
    """Isolation env whose TEMP points at the tree root, so the guard's %TEMP% rule is exact."""
    env = clean_env({
        'TEMP': str(tree['root']), 'TMP': str(tree['root']),
        'AUTO_HEROS3_STATE_DIR': str(tree['state']),
        'AUTO_HEROS3_SAVES_DIR': str(tree['saves_root']),
        'AUTO_HEROS3_SAVE_SUBDIR': 'agent_n0_isolated',
        'AUTO_HEROS3_MEMORY_ROOT': str(tree['memory']),
        'AUTO_HEROS3_RECORDINGS_DIR': str(tree['recordings']),
    })
    if extra:
        env.update(extra)
    return env


def combined(proc: subprocess.CompletedProcess) -> str:
    return (proc.stdout or '') + (proc.stderr or '')


def parse_dryrun(output: str) -> dict:
    values: dict[str, str] = {}
    for line in output.splitlines():
        if line.startswith('DRYRUN ') and '=' in line:
            _, rest = line.split(' ', 1)
            key, _, value = rest.partition('=')
            values[key] = value
    return values


def parse_import(output: str) -> dict:
    values: dict[str, str] = {}
    for line in output.splitlines():
        if '=' in line and line.split('=', 1)[0] in ('STATE_DIR', 'SAVES_DIR', 'SAVE_SUBDIR',
                                                     'MEMORY_ROOT', 'RECORDINGS_DIR'):
            key, _, value = line.partition('=')
            values[key] = value
    return values


IMPORT_CODE = (
    "import sys; sys.path.insert(0, 'tools'); import mcp_server_v2 as M; "
    "print('STATE_DIR=' + str(M.STATE_DIR)); "
    "print('SAVES_DIR=' + str(M.SAVES_DIR)); "
    "print('SAVE_SUBDIR=' + str(M.SAVE_SUBDIR)); "
    "print('MEMORY_ROOT=' + str(M.MEMORY_ROOT)); "
    "print('RECORDINGS_DIR=' + str(M.RECORDINGS_DIR))"
)

FORMAL_DIRS = {
    'doc/player': ROOT / 'doc' / 'player',
    'var/agent_mcp_v2': ROOT / 'var' / 'agent_mcp_v2',
    'var/recordings': ROOT / 'var' / 'recordings',
}


def snapshot_formal() -> dict:
    out: dict[str, list[str]] = {}
    for label, path in FORMAL_DIRS.items():
        entries: list[str] = []
        if path.exists():
            for file_path in sorted(path.rglob('*')):
                if file_path.is_file():
                    stat = file_path.stat()
                    entries.append(f'{file_path.relative_to(ROOT)}|{stat.st_size}|'
                                   f'{stat.st_mtime_ns}')
        out[label] = entries
    return out


def snapshot_diff(before: dict, after: dict) -> str:
    changed = []
    for label in before:
        if before[label] != after[label]:
            changed.append(f'{label} ({len(before[label])}->{len(after[label])} files)')
    return 'unchanged' if not changed else 'CHANGED: ' + ', '.join(changed)


def vcmi_pids() -> list[int]:
    try:
        import psutil
    except Exception:                              # pragma: no cover - psutil is a hard dep here
        return []
    pids: list[int] = []
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            name = (proc.info.get('name') or '').lower()
        except Exception:
            continue
        if name.startswith('vcmi'):
            pids.append(proc.info['pid'])
    return sorted(pids)


def same_path(value: str, expected: Path) -> bool:
    try:
        return os.path.normcase(str(Path(value).resolve())) == \
            os.path.normcase(str(expected.resolve()))
    except (OSError, ValueError):
        return False


# ------------------------------------------------------------------ test cases
def test_no_env_refused(before: dict, pids_before: list[int]) -> None:
    proc = run_py([str(LIVE_TEST)], clean_env())
    output = combined(proc)
    check('N0-1 no env -> exit code 2', proc.returncode == 2, f'exit={proc.returncode}')
    check('N0-1b refusal names the live opt-in',
          'LIVE TEST REFUSED' in output and 'AUTO_HEROS3_LIVE_TEST' in output,
          (output.splitlines() or [''])[0][:120])
    check('N0-1c refusal lists the missing isolation variables',
          'AUTO_HEROS3_STATE_DIR is not set' in output
          and 'AUTO_HEROS3_MEMORY_ROOT is not set' in output
          and 'AUTO_HEROS3_SAVE_SUBDIR is not set' in output, '')
    check('N0-1d formal dirs untouched after refusal', snapshot_formal() == before,
          snapshot_diff(before, snapshot_formal()))
    check('N0-1e no VCMI process spawned by refusal', vcmi_pids() == pids_before,
          f'{pids_before}->{vcmi_pids()}')


def test_formal_paths_refused(tree: dict) -> None:
    cases = [
        ('formal STATE_DIR', {'AUTO_HEROS3_STATE_DIR': str(ROOT / 'var' / 'agent_mcp_v2')},
         'AUTO_HEROS3_STATE_DIR points at the formal STATE_DIR'),
        ('formal MEMORY_ROOT', {'AUTO_HEROS3_MEMORY_ROOT': str(ROOT / 'doc' / 'player')},
         'AUTO_HEROS3_MEMORY_ROOT points at the formal MEMORY_ROOT'),
        ('formal RECORDINGS_DIR',
         {'AUTO_HEROS3_RECORDINGS_DIR': str(ROOT / 'var' / 'recordings')},
         'AUTO_HEROS3_RECORDINGS_DIR points at the formal RECORDINGS_DIR'),
        ('formal SAVE_SUBDIR', {'AUTO_HEROS3_SAVE_SUBDIR': 'agent'},
         "AUTO_HEROS3_SAVE_SUBDIR='agent' is the formal save subdir"),
        ('non-test SAVE_SUBDIR', {'AUTO_HEROS3_SAVE_SUBDIR': 'agent2'},
         "must start with 'agent_n0'"),
    ]
    for label, overrides, expected in cases:
        proc = run_py([str(LIVE_TEST)], iso_env(tree, {'AUTO_HEROS3_LIVE_TEST': '1', **overrides}))
        output = combined(proc)
        check(f'N0-2 {label} -> exit 2', proc.returncode == 2, f'exit={proc.returncode}')
        check(f'N0-2b {label} names the violation', expected in output,
              expected if expected not in output else '')


def test_dryrun(tree: dict) -> None:
    proc = run_py([str(LIVE_TEST)],
                  iso_env(tree, {'AUTO_HEROS3_LIVE_TEST': '1',
                                 'AUTO_HEROS3_LIVE_TEST_DRYRUN': '1'}))
    output = combined(proc)
    values = parse_dryrun(output)
    check('N0-3 DRYRUN exit 0', proc.returncode == 0, f'exit={proc.returncode}')
    check('N0-3b DRYRUN prints all five resolved paths', set(values) ==
          {'STATE_DIR', 'SAVES_DIR', 'SAVE_SUBDIR', 'MEMORY_ROOT', 'RECORDINGS_DIR'},
          str(sorted(values)))
    check('N0-3c DRYRUN STATE_DIR == injected', same_path(values.get('STATE_DIR', ''),
                                                          tree['state']),
          f"{values.get('STATE_DIR')} vs {tree['state']}")
    check('N0-3d DRYRUN SAVES_DIR == injected', same_path(values.get('SAVES_DIR', ''),
                                                          tree['saves_root']),
          f"{values.get('SAVES_DIR')} vs {tree['saves_root']}")
    check('N0-3e DRYRUN SAVE_SUBDIR == injected', values.get('SAVE_SUBDIR') == 'agent_n0_isolated',
          str(values.get('SAVE_SUBDIR')))
    check('N0-3f DRYRUN MEMORY_ROOT == injected', same_path(values.get('MEMORY_ROOT', ''),
                                                            tree['memory']),
          f"{values.get('MEMORY_ROOT')} vs {tree['memory']}")
    check('N0-3g DRYRUN RECORDINGS_DIR == injected', same_path(values.get('RECORDINGS_DIR', ''),
                                                               tree['recordings']),
          f"{values.get('RECORDINGS_DIR')} vs {tree['recordings']}")
    # DRYRUN must not import the server: no memory defaults / state dir may have been created.
    check('N0-3h DRYRUN touches no path', not tree['state'].exists()
          and not tree['memory'].exists() and not tree['recordings'].exists(), str(tree['root']))
    # DRYRUN is safe without the live opt-in as well (it never imports / never starts a game).
    proc = run_py([str(LIVE_TEST)],
                  iso_env(tree, {'AUTO_HEROS3_LIVE_TEST_DRYRUN': '1'}))
    check('N0-3i DRYRUN works without AUTO_HEROS3_LIVE_TEST', proc.returncode == 0,
          f'exit={proc.returncode}')
    # The default saves root is tolerated in DRYRUN (no write happens).
    default_saves = Path.home() / 'Documents' / 'My Games' / 'vcmi' / 'Saves'
    proc = run_py([str(LIVE_TEST)],
                  iso_env(tree, {'AUTO_HEROS3_LIVE_TEST_DRYRUN': '1',
                                 'AUTO_HEROS3_SAVES_DIR': str(default_saves),
                                 'AUTO_HEROS3_SAVE_SUBDIR': 'agent_n0_defaultroot'}))
    values = parse_dryrun(combined(proc))
    check('N0-3j default saves root tolerated in DRYRUN', proc.returncode == 0
          and same_path(values.get('SAVES_DIR', ''), default_saves),
          f'exit={proc.returncode}')


def test_import_injection(tree: dict, before: dict) -> None:
    proc = run_py(['-c', IMPORT_CODE], iso_env(tree))
    output = combined(proc)
    values = parse_import(output)
    check('N0-4 import with isolation env exits 0', proc.returncode == 0, f'exit={proc.returncode}')
    check('N0-4b imported STATE_DIR is the isolated path',
          same_path(values.get('STATE_DIR', ''), tree['state']), str(values.get('STATE_DIR')))
    check('N0-4c imported SAVES_DIR is the isolated path',
          same_path(values.get('SAVES_DIR', ''), tree['saves_root']), str(values.get('SAVES_DIR')))
    check('N0-4d imported SAVE_SUBDIR is the isolated subdir',
          values.get('SAVE_SUBDIR') == 'agent_n0_isolated', str(values.get('SAVE_SUBDIR')))
    check('N0-4e imported MEMORY_ROOT is the isolated path',
          same_path(values.get('MEMORY_ROOT', ''), tree['memory']), str(values.get('MEMORY_ROOT')))
    check('N0-4f imported RECORDINGS_DIR is the isolated path',
          same_path(values.get('RECORDINGS_DIR', ''), tree['recordings']),
          str(values.get('RECORDINGS_DIR')))
    check('N0-4g import wrote memory defaults only under the isolated tree',
          (tree['memory'] / 'PLAYER.md').exists(),
          f"isolated PLAYER.md exists={(tree['memory'] / 'PLAYER.md').exists()}")
    check('N0-4h formal dirs untouched by isolated import', snapshot_formal() == before,
          snapshot_diff(before, snapshot_formal()))


def test_import_defaults(before: dict) -> None:
    proc = run_py(['-c', IMPORT_CODE], clean_env())
    output = combined(proc)
    values = parse_import(output)
    check('N0-5 clean-env import exits 0', proc.returncode == 0, f'exit={proc.returncode}')
    check('N0-5b default STATE_DIR unchanged',
          same_path(values.get('STATE_DIR', ''), ROOT / 'var' / 'agent_mcp_v2'),
          str(values.get('STATE_DIR')))
    check('N0-5c default SAVES_DIR unchanged',
          same_path(values.get('SAVES_DIR', ''),
                    Path.home() / 'Documents' / 'My Games' / 'vcmi' / 'Saves'),
          str(values.get('SAVES_DIR')))
    check('N0-5d default SAVE_SUBDIR unchanged', values.get('SAVE_SUBDIR') == 'agent',
          str(values.get('SAVE_SUBDIR')))
    check('N0-5e default MEMORY_ROOT unchanged',
          same_path(values.get('MEMORY_ROOT', ''), ROOT / 'doc' / 'player'),
          str(values.get('MEMORY_ROOT')))
    check('N0-5f default RECORDINGS_DIR unchanged',
          same_path(values.get('RECORDINGS_DIR', ''), ROOT / 'var' / 'recordings'),
          str(values.get('RECORDINGS_DIR')))
    check('N0-5g formal dirs untouched by clean-env import', snapshot_formal() == before,
          snapshot_diff(before, snapshot_formal()))


def test_invalid_subdir_rejected() -> None:
    for bad in ('../evil', '', 'abs/path', 'a\\b', '..'):
        proc = run_py(['-c', "import sys; sys.path.insert(0, 'tools'); import mcp_server_v2"],
                      clean_env({'AUTO_HEROS3_SAVE_SUBDIR': bad}))
        output = combined(proc)
        check(f'N0-6 invalid SAVE_SUBDIR {bad!r} rejects the import',
              proc.returncode != 0 and 'AUTO_HEROS3_SAVE_SUBDIR' in output,
              f'exit={proc.returncode}')


def main() -> int:
    before = snapshot_formal()
    pids_before = vcmi_pids()
    tree = make_tree()
    try:
        test_no_env_refused(before, pids_before)
        test_formal_paths_refused(tree)
        test_dryrun(tree)
        test_import_injection(tree, before)
        test_import_defaults(before)
        test_invalid_subdir_rejected()
    finally:
        shutil.rmtree(tree['root'], ignore_errors=True)

    check('N0-7 formal dirs untouched at end of run', snapshot_formal() == before,
          snapshot_diff(before, snapshot_formal()))
    check('N0-8 no VCMI process spawned at all', vcmi_pids() == pids_before,
          f'{pids_before}->{vcmi_pids()}')

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
