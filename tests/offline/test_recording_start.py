"""Concurrent recording admission: fake clients and temporary files only."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
from contextlib import ExitStack
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcp_server_v2 as M
from test_native_stop import NativeEnv, join

CHECKS = 0


def check(name, condition):
    global CHECKS
    assert condition, name
    CHECKS += 1
    print(f'PASS {name}')


def spawn(results, function):
    def run():
        try:
            results.append(function())
        except BaseException as exc:
            results.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    return thread


def test_concurrent_admission():
    with NativeEnv() as env:
        entered, proceed = threading.Event(), threading.Event()

        def blocked_start(out, **kwargs):
            entered.set()
            assert proceed.wait(3), 'test preparation barrier timed out'
            return env.start_client(out, **kwargs)

        results = []
        with patch.object(M, '_libobs_start_client', blocked_start):
            first = spawn(results, lambda: M.recording_start(audio_framed=str(env.framed)))
            assert entered.wait(3)
            lock_path = M._recording_lock_path()
            before = lock_path.read_bytes()
            owner = json.loads(before)
            check('preparing owner persisted before client publication',
                  owner['preparing_token'] == M._RECORDING_PREPARING['token'] and not env.clients)
            native = M.recording_start(audio_framed=str(env.framed))
            legacy = M.recording_start(backend='legacy')
            stop = M.recording_stop()
            check('concurrent native start is rejected before launch',
                  native['error'] == 'recording_start_in_progress' and native['retryable'])
            check('cross backend start is rejected during prepare',
                  legacy['error'] == 'recording_start_in_progress' and not env.clients)
            check('stop during prepare is explicit and cannot cancel unknown run',
                  stop['error'] == 'recording_start_in_progress' and stop['retryable'])
            duplicate = M._acquire_recording_lock({'mcp_pid': M.os.getpid()})
            check('same PID cannot steal its pending persistent lock',
                  duplicate['error'] == 'recording_start_in_progress')
            check('rejected requests preserve preparing ownership',
                  lock_path.read_bytes() == before and M._RUNTIME.recorder is env.old)
            proceed.set()
            join(first)
        check('exactly one client is published', len(results) == 1 and results[0]['ok']
              and len(env.clients) == 1 and M._RUNTIME.recorder['client'] is env.clients[0])
        check('admission clears only after publication', M._RECORDING_PREPARING is None
              and not M._RECORDING_ADMISSION_LOCK.locked())
        check('published run remains normally stoppable', M.recording_stop()['ok'])


def test_stop_then_start():
    with NativeEnv() as env:
        rec, client = env.start()
        client.allow_stop = threading.Event()
        results = []
        thread = spawn(results, M.recording_stop)
        assert client.entered.wait(3)
        refused = M.recording_start(audio_framed=str(env.framed))
        check('native stop in progress keeps run active for admission',
              refused['error'] == 'recording_active' and M._RUNTIME.recorder is rec)
        client.allow_stop.set()
        join(thread)
        new, new_client = env.start()
        check('start after completed stop gets a fresh run', results[0]['ok']
              and new is not rec and new_client is not client and client.stop_calls == 1)
        check('new run can stop independently', M.recording_stop()['ok'])
    with NativeEnv() as env:
        entered, proceed = threading.Event(), threading.Event()
        results = []

        def legacy_cleanup(rec):
            entered.set()
            assert proceed.wait(3), 'test legacy cleanup barrier timed out'
            return {'ok': True}

        with patch.object(M, '_recording_stop_admitted', legacy_cleanup):
            thread = spawn(results, M.recording_stop)
            assert entered.wait(3)
            start = M.recording_start(audio_framed=str(env.framed))
            second_stop = M.recording_stop()
            check('legacy stop excludes new starts until cleanup completes',
                  start['error'] == 'recording_stop_in_progress' and start['retryable'])
            check('concurrent legacy stop does not repeat cleanup',
                  second_stop['error'] == 'recording_stop_in_progress')
            check('legacy stop contention preserves existing state',
                  M._RUNTIME.recorder is env.old and not env.clients)
            proceed.set()
            join(thread)
        check('legacy admission lock is released', results == [{'ok': True}]
              and not M._RECORDING_ADMISSION_LOCK.locked())


def test_failed_preparation_releases_admission():
    with NativeEnv() as env:
        with patch.object(M, '_libobs_start_client', side_effect=RuntimeError('injected prepare failure')):
            failed = M.recording_start(audio_framed=str(env.framed))
        check('failed preparation returns explicit failure', not failed['ok']
              and 'injected prepare failure' in failed['error'])
        check('failed preparation releases only its pending lock', not M._recording_lock_path().exists()
              and M._RECORDING_PREPARING is None and M._RUNTIME.recorder is env.old)
        env.start()
        check('failed preparation can be retried', len(env.clients) == 1)
        M.recording_stop()
    with NativeEnv() as env:
        with patch.object(M, '_recordings_root', side_effect=OSError('injected before manifest')):
            try:
                M.recording_start(audio_framed=str(env.framed))
            except OSError as exc:
                check('unexpected preparation exception preserved', 'injected before manifest' in str(exc))
            else:
                check('unexpected preparation exception preserved', False)
        check('unexpected exception cannot strand preparing lock', not M._recording_lock_path().exists()
              and M._RECORDING_PREPARING is None and not M._RECORDING_ADMISSION_LOCK.locked())
        env.start()
        M.recording_stop()
    with NativeEnv():
        lock_path = M._recording_lock_path()
        lock_path.write_bytes(b'')
        rejected = M._acquire_recording_lock({'mcp_pid': M.os.getpid()})
        check('partially published cross process lock fails closed',
              rejected['error'] == 'recording_lock_busy' and lock_path.read_bytes() == b'')


def test_launched_client_publication_failure():
    from auto_heros3.recording.recording_manifest import RecordingManifest
    for point in ('manifest', 'identity.json', 'recording.json', 'snapshot', 'watchdog'):
        with NativeEnv() as env:
            original_write = M._atomic_write_json
            original_transition = RecordingManifest.transition_run

            def failed_write(path, data):
                if Path(path).name == point:
                    raise OSError(f'injected {point} publication failure')
                return original_write(path, data)

            def failed_transition(manifest, state, **kwargs):
                if state == 'recording':
                    raise OSError('injected manifest publication failure')
                return original_transition(manifest, state, **kwargs)

            with ExitStack() as faults:
                if point == 'manifest':
                    faults.enter_context(patch.object(RecordingManifest, 'transition_run', failed_transition))
                elif point == 'watchdog':
                    faults.enter_context(patch.object(M.threading.Thread, 'start', side_effect=RuntimeError('injected watchdog')))
                elif point == 'snapshot':
                    faults.enter_context(patch.object(M, '_libobs_run_snapshot', side_effect=OSError('injected snapshot')))
                else:
                    faults.enter_context(patch.object(M, '_atomic_write_json', failed_write))
                result = M.recording_start(audio_framed=str(env.framed))
            check(f'{point} post-launch failure is explicit', not result['ok']
                  and result['error'] == 'native_start_failed' and result['process_exit_confirmed'])
            check(f'{point} failed publication leaves no live host or ownership lock',
                  len(env.clients) == 1 and env.clients[0].proc.poll() is not None
                  and not M._recording_lock_path().exists() and M._active_recording_info() is None)
            check(f'{point} failed run remains a failed terminal stop', M.recording_stop() == result)
            _, new_client = env.start()
            check(f'{point} retry starts exactly one replacement', len(env.clients) == 2
                  and new_client.proc.poll() is None and env.clients[0].proc.poll() is not None)
            M.recording_stop()


def test_unconfirmed_failed_start_exit_retains_owner():
    from test_native_stop import FakeClient
    with NativeEnv() as env:
        original = M._atomic_write_json

        def failed_identity(path, data):
            if Path(path).name == 'identity.json':
                raise OSError('injected post-launch identity failure')
            return original(path, data)

        with patch.object(FakeClient, 'close', side_effect=OSError('injected terminate denied')):
            with patch.object(M, '_atomic_write_json', failed_identity):
                failed = M.recording_start(audio_framed=str(env.framed))
            rec = M._RUNTIME.recorder
            check('unconfirmed exit is an explicit retryable partial',
                  failed['error'] == 'native_start_cleanup_pending' and failed['retryable']
                  and not failed['process_exit_confirmed'])
            state = json.loads(M._recording_state_path().read_text())
            owner = json.loads(M._recording_lock_path().read_text())
            check('failed-close run retains process and persisted ownership',
                  rec['client'] is env.clients[0] and rec['active'] and state['start_cleanup_pending']
                  and owner['native_pid'] == env.clients[0].pid)
            refused = M.recording_start(audio_framed=str(env.framed))
            check('failed-close owner blocks a second host', refused['error'] == 'recording_active'
                  and len(env.clients) == 1)
            retried = M.recording_stop()
            check('failed cleanup can be retried without losing lock or faking success',
                  retried['retryable'] and M._recording_lock_path().exists()
                  and rec['client'] is env.clients[0] and rec['last_stop_result'] is None)
        stopped = M.recording_stop()
        check('later cleanup confirms exit and releases ownership',
              not stopped['ok'] and not stopped['retryable'] and stopped['process_exit_confirmed']
              and not M._recording_lock_path().exists() and env.clients[0].proc.poll() is not None)
        env.start()
        check('new recording allowed only after failed owner is gone', len(env.clients) == 2)
        M.recording_stop()


def test_restart_publication_failure_closes_new_host():
    from auto_heros3.recording.recording_manifest import RecordingManifest
    with NativeEnv() as env:
        rec, old_client = env.start()
        old_client.proc.closed = True
        original = RecordingManifest.transition_run

        def injected(manifest, state, **kwargs):
            if state == 'recording':
                raise OSError('injected restart manifest failure')
            return original(manifest, state, **kwargs)

        with patch.object(RecordingManifest, 'transition_run', injected):
            M._libobs_restart_after_death(rec, 7)
        check('restart publication failure closes its new host',
              len(env.clients) == 2 and all(client.proc.poll() is not None for client in env.clients))
        check('restart failure leaves no active phantom run',
              M._active_recording_info() is None and not M._recording_lock_path().exists()
              and M.recording_stop()['error'] == 'native_start_failed')


def test_internal_spawn_failure_keeps_owner():
    from auto_heros3.recording import native_artifacts, obs_backend
    from test_native_stop import FakeProc
    real_start = M._libobs_start_client
    for point in ('connect', 'prepare', 'start', 'close_denied', 'wmi_prepare', 'wmi_unknown'):
        with NativeEnv() as env:
            stubborn = [point in ('close_denied', 'wmi_unknown')]
            owner = FakeProc()
            owner.pid = 424242
            owner.create_time = lambda: 123.0
            owner.exe = lambda: str(env.env.root / 'recorder.exe')
            owner.is_running = lambda: not owner.closed
            # A WMI owner intentionally has no Popen poll method.
            class WmiOwner:
                pid = owner.pid
                create_time, exe, is_running = (staticmethod(owner.create_time),
                    staticmethod(owner.exe), staticmethod(owner.is_running))

            class SpawnFailure:
                def __init__(self, exe, **kwargs):
                    self.exe, self.proc, self.native_pid = exe, None, None
                    self.wmi_launched = point.startswith('wmi_')
                    self.native_identity = None
                    env.clients.append(self)

                @property
                def pid(self):
                    return self.native_pid or (self.proc.pid if self.proc is not None else None)

                def connect(self):
                    self.proc = None if self.wmi_launched else owner
                    self.native_pid = owner.pid
                    if point == 'wmi_prepare':
                        self.native_identity = {'pid': owner.pid, 'create_time': 123.0,
                                                'exe': str(self.exe)}
                    if point == 'connect':
                        raise RuntimeError('injected connect after spawn')

                def prepare(self, *args, **kwargs):
                    if point in ('prepare', 'close_denied', 'wmi_prepare', 'wmi_unknown'):
                        raise RuntimeError('injected prepare after spawn')
                    return {'ok': True}

                def start_recording(self):
                    raise RuntimeError('injected start after spawn')

                def close(self, force=False):
                    if stubborn[0]:
                        raise RuntimeError('injected close denied')
                    owner.closed = True
                    self.proc = self.native_pid = None

            verified = {'recorder_exe': env.env.root / 'recorder.exe',
                        'outputs_module': env.env.root / 'outputs.dll', 'obs_dir': env.env.root,
                        'private_sdl_sha256': 'f' * 64, 'source_commit': 'a' * 40,
                        'checkpoint_path': env.env.root / 'checkpoint.json',
                        'checkpoint_sha256': 'b' * 64, 'artifacts': {}}
            with patch.object(M, '_libobs_start_client', real_start), \
                    patch.object(native_artifacts, 'load_native_artifacts', return_value=verified), \
                    patch.object(obs_backend, 'ObsRecorder', SpawnFailure), \
                    patch('psutil.Process', return_value=WmiOwner()), \
                    patch('psutil.pid_exists', side_effect=lambda pid: not owner.closed):
                result = M.recording_start(audio_framed=str(env.framed))
            if stubborn[0]:
                check('internal prepare/close failure retains spawned owner',
                      result['retryable'] and result['error'] == 'native_start_cleanup_pending'
                      and M._RUNTIME.recorder['client'] is env.clients[0] and not owner.closed)
                refused = M.recording_start(audio_framed=str(env.framed))
                check('internal spawn failure cannot launch a second host',
                      refused['error'] == 'recording_active' and len(env.clients) == 1)
                stubborn[0] = False
                stopped = M.recording_stop()
                check('internal spawn owner can be cleaned on retry',
                      stopped['process_exit_confirmed'] and owner.closed)
            else:
                check(f'{point} internal failure closes its spawned owner',
                      result['error'] == 'native_start_failed' and result['process_exit_confirmed'] and owner.closed)
            check(f'{point} cleanup preserves failed result and releases ownership only after exit',
                  not M._recording_lock_path().exists() and M._active_recording_info() is None)


def test_guarded_server_transactions():
    from auto_heros3.recording.lock_guard import recording_lock_guard
    with NativeEnv():
        path = M._recording_lock_path()
        original = json.dumps({'mcp_pid': M.os.getpid(), 'nonce': 'protected'}).encode()
        path.write_bytes(original)
        with recording_lock_guard(path):
            refused = M._acquire_recording_lock({'mcp_pid': M.os.getpid()})
            released = M._release_recording_lock()
        check('server claim and release both honor OS guard',
              refused['error'] == 'recording_lock_busy' and released is False and path.read_bytes() == original)
        path.write_text(json.dumps({'mcp_pid': 999999, 'native_pid': 888888}))
        with patch.object(M, '_pid_alive', side_effect=lambda pid: pid == 888888):
            refused = M._acquire_recording_lock({'mcp_pid': M.os.getpid()})
        check('dead MCP lock cannot be reclaimed while its native owner is alive',
              refused['error'] == 'recording_active' and refused['reason'] == 'pending_native_owner_alive')


def test_stop_and_restart_cannot_abandon_live_owner():
    from test_native_stop import FakeClient
    for action in ('stop', 'restart'):
        with NativeEnv() as env:
            rec, client = env.start()
            with patch.object(FakeClient, 'close', side_effect=OSError('injected termination denied')):
                if action == 'stop':
                    failed = M.recording_stop()
                    check('stop close failure remains retryable', failed['error'] == 'native_stop_cleanup_pending'
                          and failed['retryable'])
                else:
                    M._libobs_restart_after_death(rec, 'health failure', force=True)
                    check('restart does not spawn replacement while old owner survives',
                          len(env.clients) == 1 and rec['start_cleanup_pending'])
                refused = M.recording_start(audio_framed=str(env.framed))
                check(f'{action} unconfirmed close preserves live owner and lock',
                      refused['error'] == 'recording_active' and rec['client'] is client
                      and M._recording_lock_path().exists() and client.proc.poll() is None)
            terminal = M.recording_stop()
            check(f'{action} cleanup retry releases only confirmed dead owner',
                  terminal['process_exit_confirmed'] and not terminal['retryable']
                  and not M._recording_lock_path().exists() and client.proc.poll() is not None)


def test_spawn_callback_persists_before_connect_returns():
    from auto_heros3.recording import native_artifacts, obs_backend
    from test_native_stop import FakeProc
    real_start = M._libobs_start_client
    for mode in ('popen', 'wmi_transition', 'publish_failure'):
        with NativeEnv() as env:
            entered, proceed = threading.Event(), threading.Event()
            process = FakeProc()
            wmi_alive = [False]
            original_write, original_exists = M._atomic_write_json, __import__('psutil').pid_exists

            class ConnectingClient:
                def __init__(self, exe, *, on_spawn=None, **kwargs):
                    self.exe, self.on_spawn = exe, on_spawn
                    self.proc = self.native_pid = self.native_identity = None
                    self.wmi_launched = False
                    env.clients.append(self)

                @property
                def pid(self):
                    return self.native_pid or (self.proc.pid if self.proc else None)

                def connect(self):
                    self.proc = process
                    self.on_spawn(self)
                    if mode == 'wmi_transition':
                        process.closed = True
                        self.proc = None
                        self.native_pid = 424243
                        self.wmi_launched = True
                        wmi_alive[0] = True
                        self.on_spawn(self)
                    entered.set()
                    assert proceed.wait(3), 'test IPC connect barrier timed out'
                    raise RuntimeError('injected connect wait interrupted')

                def close(self, force=False):
                    process.closed = True
                    wmi_alive[0] = False
                    self.proc = self.native_pid = None

            def failed_publish(path, data):
                if mode == 'publish_failure' and Path(path).name == 'recording.lock' and data.get('native_pid'):
                    raise OSError('injected spawn ownership persistence failure')
                return original_write(path, data)

            verified = {'recorder_exe': env.env.root / 'recorder.exe',
                        'outputs_module': env.env.root / 'outputs.dll', 'obs_dir': env.env.root,
                        'private_sdl_sha256': 'f' * 64, 'source_commit': 'a' * 40,
                        'checkpoint_path': env.env.root / 'checkpoint.json',
                        'checkpoint_sha256': 'b' * 64, 'artifacts': {}}
            results = []
            with patch.object(M, '_libobs_start_client', real_start), \
                    patch.object(M, '_atomic_write_json', failed_publish), \
                    patch.object(native_artifacts, 'load_native_artifacts', return_value=verified), \
                    patch.object(obs_backend, 'ObsRecorder', ConnectingClient), \
                    patch('psutil.pid_exists', side_effect=lambda pid: wmi_alive[0] if pid == 424243 else original_exists(pid)):
                worker = spawn(results, lambda: M.recording_start(audio_framed=str(env.framed)))
                if mode != 'publish_failure':
                    assert entered.wait(3), 'connect did not reach its barrier'
                    owner = json.loads(M._recording_lock_path().read_text())
                    expected_pid = 424243 if mode == 'wmi_transition' else process.pid
                    check(f'{mode} native PID is durable before connect returns',
                          owner['native_pid'] == expected_pid and M._RUNTIME.recorder['native_pid'] == expected_pid)
                    if mode == 'wmi_transition':
                        check('WMI replacement discards the exited Popen owner immediately',
                              M._RUNTIME.recorder['proc'] is None and not M._RUNTIME.recorder.get('native_process'))
                    refused = M.recording_start(audio_framed=str(env.framed))
                    check(f'{mode} blocked connect excludes a second start', refused['error'] == 'recording_start_in_progress')
                    proceed.set()
                join(worker)
            check(f'{mode} spawn/callback failure cleans the registered owner',
                  len(results) == 1 and results[0]['error'] == 'native_start_failed'
                  and results[0]['process_exit_confirmed'] and process.closed and not wmi_alive[0]
                  and not M._recording_lock_path().exists())


def test_watchdog_fault_snapshot_precedes_cleanup():
    real_watchdog = M._libobs_watchdog

    class OnePoll:
        def __init__(self):
            self.calls = 0

        def wait(self, timeout):
            self.calls += 1
            return self.calls > 1

        def set(self):
            self.calls = 2

    with NativeEnv() as env:
        rec, client = env.start()
        old_out, old_run = Path(rec['dir']), rec['recording_run_id']
        artifacts = {'source_commit': 'a' * 40, 'checkpoint_sha256': 'b' * 64,
                     'artifacts': {'private-sdl': 'c' * 64}}
        (old_out / 'native-artifacts.json').write_text(json.dumps(artifacts))
        native = {'ok': True, 'active': True, 'state': 'recording',
                  'framed_audio': {'enabled': True, 'opened': True,
                                   'error_code': 'late_budget_exceeded',
                                   'error': 'packet age 750000000ns',
                                   'submitted_end_sample': 123456, 'max_packet_age_ns': 750000000,
                                   'packet_integrity_ok': True, 'stream_integrity_ok': True,
                                   'timeline_ok': True, 'submitted_gaps': 0,
                                   'nested_future_metric': {'unmodified': [4, 5, 6]}},
                  'output': {'total_frames': 72000, 'total_bytes': 1234567}}
        closed = client.close
        observed = []

        def checked_close(force=False):
            refs = rec['watchdog_faults']
            document = json.loads(Path(refs[-1]['path']).read_text(encoding='utf-8'))
            observed.append(document)
            check('watchdog full fault is on disk before closing live host',
                  force and not client.proc.closed and refs[-1]['saved']
                  and document['native_status'] == native)
            closed(force=force)

        rec['watchdog_stop'] = OnePoll()
        with patch.object(client, 'status', return_value=native), \
                patch.object(client, 'close', checked_close):
            real_watchdog(rec)
        doc = observed[0]
        check('fault preserves old run identity and exact checkpoint declaration',
              doc['recording_run_id'] == old_run and doc['dir'] == str(old_out)
              and doc['artifact_manifest']['data'] == artifacts
              and doc['observation_point'] == 'before_native_process_cleanup'
              and doc['observed_at'] > 0 and doc['monotonic_ns'] > 0)
        check('fault preserves status observation without truncating nested framed metrics',
              doc['last_status_observation']['response'] == native
              and doc['status_exception'] is None and doc['reason'] == 'framed_audio_failed')
        persisted = json.loads(M._recording_state_path().read_text(encoding='utf-8'))
        check('restart persists fault reference and old run while clearing stale current health',
              rec['restart_count'] == 1 and 'last_native_status' not in rec
              and persisted['watchdog_faults'] == rec['watchdog_faults']
              and persisted['last_restart']['fault_saved'] is True
              and persisted['runs'][-2]['watchdog_fault']['fault_id'] == doc['fault_id'])
        current = M.recording_status()
        check('recording status exposes restart and fault even after replacement',
              current['restart_count'] == 1 and current['watchdog_faults'] == rec['watchdog_faults']
              and current['last_restart']['fault_id'] == doc['fault_id'])
        first = rec['watchdog_faults'][0]
        first_bytes = Path(first['path']).read_bytes()
        replacement = env.clients[-1]
        replacement.proc.closed = True
        M._libobs_restart_after_death(rec, 7)
        check('second restart uses a new immutable fault and retains prior reference',
              rec['restart_count'] == 2 and len(rec['watchdog_faults']) == 2
              and rec['watchdog_faults'][1]['fault_id'] != first['fault_id']
              and Path(first['path']).read_bytes() == first_bytes)
        second = json.loads(Path(rec['watchdog_faults'][1]['path']).read_text())
        check('dead host snapshot does not invent a fresh or previous-host status',
              second['native_status'] is None and second['last_status_observation'] is None
              and second['reason'] == 7)
        result = M.recording_stop()
        check('final stop retains all fault references without changing superseded semantics',
              result['ok'] and result['restart_count'] == 2
              and result['watchdog_faults'] == rec['watchdog_faults']
              and result['last_restart']['fault_id'] == rec['watchdog_faults'][-1]['fault_id'])
        check('previous interrupted files remain explicitly unvalidated',
              all(item['state'] == 'unvalidated' for item in result['files']
                  if item['run_dir'] != result['dir']))


def test_watchdog_status_exception_keeps_last_observation():
    real_watchdog = M._libobs_watchdog

    class TwoPolls:
        def __init__(self):
            self.calls = 0

        def wait(self, timeout):
            self.calls += 1
            return self.calls > 2

        def set(self):
            self.calls = 3

    with NativeEnv() as env:
        rec, client = env.start()
        rec['watchdog_stop'] = TwoPolls()
        healthy = {'ok': True, 'active': True, 'state': 'recording',
                   'framed_audio': {'enabled': False}, 'last_exact_sample': 654321}
        with patch.object(client, 'status', side_effect=[healthy, TimeoutError('injected IPC deadline')]), \
                patch.object(M.time, 'monotonic', side_effect=[10.0, 13.0]):
            real_watchdog(rec)
        fault = rec['watchdog_faults'][0]
        doc = json.loads(Path(fault['path']).read_text())
        check('status failure distinguishes current exception from last successful response',
              doc['native_status'] is None and doc['last_status_observation']['response'] == healthy
              and doc['status_exception'] == {'type': 'TimeoutError', 'message': 'injected IPC deadline'}
              and doc['reason'] == 'status_failed')
        check('missing provenance file is disclosed rather than filled from a new checkpoint',
              doc['artifact_manifest']['data'] is None
              and 'FileNotFoundError' in doc['artifact_manifest']['error'])
        check('status failure evidence survives successful recovery stop',
              M.recording_stop()['watchdog_faults'] == [fault])


def test_watchdog_fault_write_failure_is_explicit():
    with NativeEnv() as env:
        rec, client = env.start()
        with patch.object(M, '_libobs_write_fault_snapshot', side_effect=OSError('injected disk full')):
            M._libobs_restart_after_death(rec, 'framed_audio_failed', force=True,
                                        native_status={'framed_audio': {'error_code': 'late_budget_exceeded'}})
        ref = rec['watchdog_faults'][0]
        check('fault write failure never claims saved and does not abandon old owner',
              ref['saved'] is False and 'injected disk full' in ref['error']
              and not Path(ref['path']).exists() and client.proc.closed and rec['restart_count'] == 1)
        persisted = json.loads(M._recording_state_path().read_text())
        check('unsaved fault and full observation are exposed in replacement state',
              persisted['watchdog_faults'] == [ref]
              and persisted['last_restart']['fault_saved'] is False
              and ref['unsaved_snapshot']['native_status']['framed_audio']['error_code'] == 'late_budget_exceeded')
        result = M.recording_stop()
        check('stop discloses diagnostic loss even when recovered media checks pass',
              result['ok'] and result['watchdog_faults'] == [ref]
              and result['last_restart']['fault_error'] == ref['error'])
    for point in ('fsync', 'replace'):
        with NativeEnv() as env:
            path = env.env.root / f'watchdog-fault-{point}.json'
            with patch.object(M.os, point, side_effect=OSError(f'injected {point} failure')):
                try:
                    M._libobs_write_fault_snapshot(path, {'unaltered': 1})
                except OSError as exc:
                    check(f'fault writer propagates {point} failure', point in str(exc))
                else:
                    check(f'fault writer propagates {point} failure', False)
            check(f'fault writer {point} failure leaves no successful file or temp',
                  not path.exists() and not path.with_name(path.name + '.tmp').exists())


if __name__ == '__main__':
    test_concurrent_admission()
    test_stop_then_start()
    test_failed_preparation_releases_admission()
    test_launched_client_publication_failure()
    test_unconfirmed_failed_start_exit_retains_owner()
    test_restart_publication_failure_closes_new_host()
    test_internal_spawn_failure_keeps_owner()
    test_guarded_server_transactions()
    test_stop_and_restart_cannot_abandon_live_owner()
    test_spawn_callback_persists_before_connect_returns()
    test_watchdog_fault_snapshot_precedes_cleanup()
    test_watchdog_status_exception_keeps_last_observation()
    test_watchdog_fault_write_failure_is_explicit()
    print(f'{CHECKS}/{CHECKS} checks passed')
