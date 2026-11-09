"""Native stop ownership/retry regression; fake IPC/media, temporary files only."""
from __future__ import annotations
import json

import sys
import json
import threading
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcp_server_v2 as M  # noqa: E402
from test_action_contract import Env  # noqa: E402

CHECKS = 0


def check(name, condition):
    global CHECKS
    assert condition, name
    CHECKS += 1
    print(f'PASS {name}')


class FakeProc:
    pid = 424242

    def __init__(self):
        self.closed = False

    def poll(self):
        return 0 if self.closed else None


class FakeClient:
    pid = 424242

    def __init__(self, out):
        self.proc = FakeProc()
        self.out = out
        self.stop_calls = 0
        self.entered = threading.Event()
        self.allow_stop = None
        (out / 'clip-000.mp4').write_bytes(b'offline fake media')

    def status(self):
        return {'ok': True, 'active': not self.proc.closed}

    def stop(self):
        self.stop_calls += 1
        self.entered.set()
        if self.allow_stop is not None:
            assert self.allow_stop.wait(3), 'test stop gate timed out'
        return {'checks_ok': True, 'files': [{'name': 'clip-000.mp4'}]}

    def shutdown(self):
        pass

    def close(self, force=False):
        self.proc.closed = True


class ControlledLock:
    def __init__(self, failures=0, delayed_thread=None):
        self.lock = threading.Lock()
        self.failures = failures
        self.delayed_thread = delayed_thread
        self.delayed = threading.Event()
        self.resume = threading.Event()
        self.second_attempt = threading.Event()
        self.attempts = 0

    def acquire(self, blocking=True, timeout=-1):
        self.attempts += 1
        if self.attempts >= 2:
            self.second_attempt.set()
        if threading.current_thread().name == self.delayed_thread:
            self.delayed.set()
            assert self.resume.wait(3), 'test acquisition gate timed out'
        if self.failures:
            self.failures -= 1
            return False
        return self.lock.acquire(blocking, timeout)

    def release(self):
        self.lock.release()

    def locked(self):
        return self.lock.locked()


class NativeEnv:
    def __enter__(self):
        self.stack = ExitStack()
        self.env = self.stack.enter_context(Env())
        self.clients = []
        self.count = 0
        self.old = {'proc': None, 'dir': None, 'sentinel': 'previous run'}
        self.stack.enter_context(patch.object(M._RUNTIME, 'recorder', self.old))
        stub = self.env.root / 'recorder.exe'
        stub.write_bytes(b'not executable')
        self.framed = self.env.root / 'fixture.a3a'
        self.framed.write_bytes(b'not decoded')
        self.stack.enter_context(patch.object(M, '_obs_recorder_exe', lambda: stub))
        self.stack.enter_context(patch.object(M, '_evidence_identity', lambda: {
            'client_pid': 12345, 'run_id': 'fake-run', 'session_id': 'fake-session'}))
        self.stack.enter_context(patch.object(M, '_recordings_root', self.recordings_root))
        self.stack.enter_context(patch.object(M, '_libobs_start_client', self.start_client))
        self.stack.enter_context(patch.object(M, '_libobs_watchdog', lambda rec: None))
        self.stack.enter_context(patch.object(M, '_overlay', lambda *a, **kw: None))
        self.stack.enter_context(patch.object(M, '_probe_native_media', lambda path: {
            'ok': True, 'has_audio': True, 'has_video': True, 'audio_codec': 'aac',
            'video_codec': 'h264', 'audio_duration_s': 1.0, 'video_duration_s': 1.0,
            'duration_s': 1.0, 'bytes': path.stat().st_size}))
        self.stack.enter_context(patch('subprocess.Popen', side_effect=AssertionError(
            'offline native stop tests must not start any external process')))
        return self

    def recordings_root(self):
        self.count += 1
        return self.env.root / f'recordings-{self.count}'

    def start_client(self, out, **kwargs):
        client = FakeClient(out)
        self.clients.append(client)
        return client, {'session_id': 'fake-native'}

    def start(self):
        result = M.recording_start(backend='libobs', audio_framed=str(self.framed))
        assert result['ok'], result
        persisted = json.loads(M._recording_state_path().read_text(encoding='utf-8'))
        assert persisted['config'] == M._RUNTIME.recorder['config']
        assert persisted['config']['audio_route'] == 'explicit_framed'
        return M._RUNTIME.recorder, self.clients[-1]

    def __exit__(self, *args):
        self.stack.close()


def spawn_stop(results, name):
    def run():
        try:
            results.append(M.recording_stop())
        except BaseException as exc:
            results.append(exc)
    thread = threading.Thread(target=run, name=name)
    thread.start()
    return thread


def join(thread):
    thread.join(4)
    assert not thread.is_alive(), f'{thread.name} did not stop'


def main():
    with NativeEnv() as env:
        rec, client = env.start()
        rec['config']['max_segment_gb'] = 8 / 1_000_000_000
        out = client.out
        result = M.recording_stop()
        manifest = json.loads((out / 's4b/manifest.json').read_text(encoding='utf-8'))
        errors = result.get('validation_errors', [])
        check('oversized segment cannot become validated complete',
              not result['ok'] and result['native_checks_ok']
              and result['files'][0]['state'] == 'partial'
              and manifest['segments'][0]['state'] == 'partial'
              and manifest['state'] == 'partial')
        check('oversized segment discloses actual bytes and configured cap',
              any(error.get('error') == 'size_cap_exceeded' and error.get('cap_bytes') == 8
                  and error.get('bytes') == (out / 'clip-000.mp4').stat().st_size for error in errors))
    # Two genuine contenders call teardown once and receive the same terminal result.
    with NativeEnv() as env:
        rec, client = env.start()
        check('start replaces run object without clearing old waiters',
              rec is not env.old and env.old['sentinel'] == 'previous run')
        rec['stop_lock'] = lock = ControlledLock()
        client.allow_stop = threading.Event()
        results = []
        first = spawn_stop(results, 'first')
        assert client.entered.wait(3)
        second = spawn_stop(results, 'second')
        assert lock.second_attempt.wait(3)
        client.allow_stop.set()
        join(first); join(second)
        check('two concurrent stops share one terminal result',
              len(results) == 2 and results[0] == results[1] and results[0]['ok'])
        check('concurrent stop tears down fake client once', client.stop_calls == 1)
        check('terminal stop can be replayed after backend cleared',
              M.recording_stop() == results[0] and client.stop_calls == 1)

    # A waiter can arrive at the lock after the first owner has already released it.
    with NativeEnv() as env:
        rec, client = env.start()
        rec['stop_lock'] = lock = ControlledLock(delayed_thread='delayed')
        results = []
        delayed = spawn_stop(results, 'delayed')
        assert lock.delayed.wait(3)
        first = M.recording_stop()
        lock.resume.set(); join(delayed)
        check('delayed lock entry replays terminal result', results == [first] and first['ok'])
        check('delayed stop does not touch closed IPC', client.stop_calls == 1)

    # A fresh request can arrive after cleanup cleared backend/client, while the
    # owner still holds stop_lock and has not published its answer yet.
    with NativeEnv() as env:
        rec, client = env.start()
        rec['stop_lock'] = lock = ControlledLock()
        cleaned = threading.Event()
        publish = threading.Event()
        real_stop = M._recording_stop_libobs
        def pause_after_cleanup(owner):
            result = real_stop(owner)
            cleaned.set()
            assert publish.wait(3), 'test cache publication gate timed out'
            return result
        results = []
        with patch.object(M, '_recording_stop_libobs', pause_after_cleanup):
            first = spawn_stop(results, 'before-publication')
            assert cleaned.wait(3)
            second = spawn_stop(results, 'after-cleanup')
            assert lock.second_attempt.wait(3)
            publish.set(); join(first); join(second)
        check('request after cleanup still joins native stop lock',
              len(results) == 2 and results[0] == results[1] and results[0]['ok'])
        check('cleanup-publication window never repeats IPC stop', client.stop_calls == 1)

    # Timeouts leave a live run. They must not poison the idempotency cache.
    with NativeEnv() as env:
        rec, client = env.start()
        rec['restart_lock'] = ControlledLock(failures=1)
        first = M.recording_stop()
        check('restart timeout is explicitly retryable',
              first['error'] == 'restart_lock_timeout' and first['retryable'])
        check('restart timeout leaves live client and no cached terminal result',
              rec['client'] is client and rec['active'] and rec['last_stop_result'] is None)
        second = M.recording_stop()
        check('stop after restart lock becomes free completes', second['ok'] and client.stop_calls == 1)
        check('successful retry replaces timeout with terminal answer', M.recording_stop() == second)
    with NativeEnv() as env:
        rec, client = env.start()
        rec['stop_lock'] = ControlledLock(failures=1)
        first = M.recording_stop()
        check('stop serialization timeout is retryable',
              first['error'] == 'stop_in_progress' and first['retryable'])
        check('stop serialization timeout is not cached', rec['last_stop_result'] is None)
        check('stop retries after serialization timeout', M.recording_stop()['ok'] and client.stop_calls == 1)

    # A captured old run survives a new run installation; its waiter cannot stop
    # the replacement or write an old result into the replacement's cache.
    with NativeEnv() as env:
        old, old_client = env.start()
        old['stop_lock'] = lock = ControlledLock(delayed_thread='old-waiter')
        results = []
        waiter = spawn_stop(results, 'old-waiter')
        assert lock.delayed.wait(3)
        old_result = M.recording_stop()
        new, new_client = env.start()
        lock.resume.set(); join(waiter)
        check('new run gets independent locks and token',
              new is not old and new['stop_lock'] is not old['stop_lock'] and new['run_token'] != old['run_token'])
        check('old waiter receives its old terminal result', results == [old_result])
        check('old waiter cannot stop or contaminate new run',
              new_client.stop_calls == 0 and new['active'] and new['last_stop_result'] is None)
        check('new run can independently stop', M.recording_stop()['ok'] and new_client.stop_calls == 1)

    # Defensive token check also protects callers that replace fields in place.
    with NativeEnv() as env:
        rec, client = env.start()
        def replaced_during_stop(owner):
            owner.clear()
            owner.update({'backend': 'libobs', 'run_token': 'replacement',
                          'last_stop_result': None, 'last_stop_token': None})
            return {'ok': False, 'error': 'old-result'}
        with patch.object(M, '_recording_stop_libobs', replaced_during_stop):
            result = M.recording_stop()
        check('cache publication rechecks run token',
              result['error'] == 'old-result' and rec['run_token'] == 'replacement'
              and rec['last_stop_result'] is None and rec['last_stop_token'] is None)
    print(f'{CHECKS}/{CHECKS} checks passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
