"""OS recording lock tests; only temporary files and owned Python children.

No game/media process, runtime DLL, display, or audio endpoint is accessed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from auto_heros3.recording.lock_guard import RecordingLockBusy, recording_lock_guard

CHECKS = 0


def check(name, condition):
    global CHECKS
    assert condition, name
    CHECKS += 1
    print(f'PASS {name}')


def worker(mode, path, ready=None):
    try:
        if mode == 'claim':
            deadline = time.monotonic() + 8
            while not ready.exists():
                if time.monotonic() > deadline:
                    raise RuntimeError('start barrier timed out')
                time.sleep(0.01)
        with recording_lock_guard(path):
            if mode == 'hold':
                ready.write_text(str(os.getpid()), encoding='utf-8')
                sys.stdin.read(1)
            elif mode == 'claim':
                previous = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
                if previous.get('owner') not in (None, 'stale'):
                    print('busy', flush=True)
                    return
                # Widen the old stale-read/unlink/recreate race while guarded.
                time.sleep(0.1)
                path.unlink(missing_ok=True)
                path.write_text(json.dumps({'owner': os.getpid()}), encoding='utf-8')
                print('claimed', flush=True)
            else:
                print('acquired', flush=True)
    except RecordingLockBusy:
        print('busy', flush=True)


def child(mode, path, ready=None):
    # Windows venv python.exe is a launcher; killing that wrapper would not
    # prove abrupt death of the process actually holding the OS byte lock.
    interpreter = getattr(sys, '_base_executable', sys.executable)
    command = [interpreter, '-X', 'utf8', str(Path(__file__).resolve()),
               '--worker', mode, str(path)]
    if ready is not None:
        command.append(str(ready))
    return subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def finish(process, text=None):
    try:
        stdout, stderr = process.communicate(input=text, timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=5)
        raise AssertionError('owned file-lock child timed out')
    assert process.returncode == 0, stderr
    return stdout.strip()


def await_file(path, process):
    deadline = time.monotonic() + 5
    while not path.exists():
        if process.poll() is not None:
            raise AssertionError(f'file-lock child exited: {finish(process)}')
        assert time.monotonic() < deadline, 'child lock acquisition timed out'
        time.sleep(0.01)
    assert int(path.read_text(encoding='utf-8')) == process.pid, 'child must be actual lock owner'


def main():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        path = root / 'recording.lock'
        guard = root / 'recording.lock.guard'
        path.write_text('user ownership bytes', encoding='utf-8')
        with recording_lock_guard(path):
            check('stable guard is created separately from ownership file', guard.is_file()
                  and path.read_text(encoding='utf-8') == 'user ownership bytes')
            inode = guard.stat().st_ino
            try:
                with recording_lock_guard(path):
                    check('second same process handle is excluded', False)
            except RecordingLockBusy:
                check('second same process handle is excluded', True)
        check('guard remains after release', guard.exists() and guard.stat().st_ino == inode)
        try:
            with recording_lock_guard(path):
                raise ValueError('guarded failure')
        except ValueError as exc:
            check('protected exception is preserved', str(exc) == 'guarded failure')
        with recording_lock_guard(path):
            check('exception releases OS lock without replacing guard', guard.stat().st_ino == inode)

        ready = root / 'child-ready'
        owner = child('hold', path, ready)
        try:
            await_file(ready, owner)
            contender = child('try', path)
            check('second process is excluded', finish(contender) == 'busy')
            try:
                with recording_lock_guard(path):
                    check('parent cannot bypass child ownership', False)
            except RecordingLockBusy:
                check('parent cannot bypass child ownership', True)
            finish(owner, 'x')
            check('normal child exit permits later acquisition', finish(child('try', path)) == 'acquired')
        finally:
            if owner.poll() is None:
                owner.kill()
                owner.communicate(timeout=5)

        ready.unlink()
        owner = child('hold', path, ready)
        try:
            await_file(ready, owner)
            owner.kill()
            owner.communicate(timeout=5)
            check('crash releases OS lock', finish(child('try', path)) == 'acquired')
            check('crash never deletes or replaces guard inode', guard.exists()
                  and guard.stat().st_ino == inode)
            check('guard operations preserve unrelated ownership bytes',
                  path.read_text(encoding='utf-8') == 'user ownership bytes')
        finally:
            if owner.poll() is None:
                owner.kill()
                owner.communicate(timeout=5)

        # Both contenders observe a deliberately stale owner. The guard must
        # cover read/decide/unlink/write, so one cannot erase the other's claim.
        path.write_text(json.dumps({'owner': 'stale'}), encoding='utf-8')
        barrier = root / 'claim-start'
        contenders = [child('claim', path, barrier) for _ in range(2)]
        try:
            barrier.write_text('go', encoding='utf-8')
            results = [finish(process) for process in contenders]
            check('two stale reclaimers produce one winner', sorted(results) == ['busy', 'claimed'])
            winner = contenders[results.index('claimed')].pid
            check('loser cannot delete newly claimed ownership',
                  json.loads(path.read_text(encoding='utf-8'))['owner'] == winner)
            check('reclaim preserves stable guard inode', guard.stat().st_ino == inode)
        finally:
            for process in contenders:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

        missing = root / 'absent-directory' / 'recording.lock'
        try:
            with recording_lock_guard(missing):
                check('guard I/O failure is fail closed', False)
        except RecordingLockBusy as exc:
            check('guard I/O failure is fail closed', exc.path.name == 'recording.lock.guard'
                  and bool(exc.reason))
        check('failed guard does not create caller directories', not missing.parent.exists())
    print(f'{CHECKS}/{CHECKS} checks passed')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--worker':
        worker(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]) if len(sys.argv) > 4 else None)
    else:
        main()
