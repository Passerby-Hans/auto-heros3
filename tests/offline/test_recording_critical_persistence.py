"""Critical publication faults at real replace/fsync boundaries; fake native only."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcp_server_v2 as M
from test_native_stop import NativeEnv, FakeProc, FakeClient

REAL_START = M._libobs_start_client
CHECKS = 0


def check(name, value):
    global CHECKS
    assert value, name
    CHECKS += 1
    print('PASS ' + name)


@contextmanager
def deny_replace(target_name, *, predicate=lambda data: True):
    """Inject below both JSON writers, while allowing unrelated evidence I/O."""
    original = M.os.replace
    calls = []

    def replace(src, dst, *args, **kwargs):
        if Path(dst).name == target_name and predicate(json.loads(Path(src).read_text(encoding='utf-8'))):
            calls.append((Path(src), Path(dst)))
            raise PermissionError('critical replace denied: ' + target_name)
        return original(src, dst, *args, **kwargs)

    with patch.object(M.os, 'replace', replace):
        yield calls


@contextmanager
def deny_fsync(target_name):
    """Identify real open file descriptors; do not replace the JSON helper."""
    original_open, original_fsync = Path.open, M.os.fsync
    opened, calls = {}, []
    def observed_open(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        opened[handle.fileno()] = (path, handle)
        return handle
    def fsync(fd):
        path, handle = opened.get(fd, (None, None))
        if (path is not None and not handle.closed and
                path.name.startswith(target_name + '.tmp-')):
            calls.append(path)
            raise OSError('critical fsync denied: ' + target_name)
        return original_fsync(fd)
    with patch.object(Path, 'open', observed_open), patch.object(M.os, 'fsync', fsync):
        yield calls


@contextmanager
def real_launch_with_fake_native(env, *, stubborn=None, wmi=False):
    """Exercise actual artifacts/on_spawn/prepare/start code without any process."""
    from auto_heros3.recording import native_artifacts, obs_backend
    stubborn = stubborn if stubborn is not None else [False]
    alive = [False]

    class Native(FakeClient):
        def __init__(self, exe, *, on_spawn=None, log_path=None, **kwargs):
            self.exe, self.on_spawn = exe, on_spawn
            self.out = Path(log_path).parent
            self.proc = None
            self.native_pid = None
            self.native_identity = None
            self.wmi_launched = wmi
            self.stop_calls = self.prepare_calls = self.start_calls = self.close_calls = 0
            self.entered = threading.Event()
            self.allow_stop = None
            self.owner = FakeProc()
            self.owner.pid = 424242 + len(env.clients)
            def denied_signal():
                raise PermissionError('fake owned process signal denied')
            self.owner.terminate = self.owner.kill = denied_signal
            env.clients.append(self)

        @property
        def pid(self):
            return self.native_pid

        def connect(self):
            self.proc = None if wmi else self.owner
            self.native_pid = self.owner.pid
            alive[0] = True
            self.on_spawn(self)

        def prepare(self, out, **kwargs):
            self.prepare_calls += 1
            (Path(out) / 'clip-000.mp4').write_bytes(b'offline fake media')
            return {'ok': True, 'session_id': 'fake-native',
                    'audio_framed_transport': kwargs['audio_framed_transport']}

        def start_recording(self):
            self.start_calls += 1
            return {'ok': True}

        def close(self, force=False):
            self.close_calls += 1
            if stubborn[0]:
                raise PermissionError('fake native termination denied')
            self.owner.closed = True
            alive[0] = False

    verified = {'recorder_exe': env.env.root / 'recorder.exe',
                'outputs_module': env.env.root / 'outputs.dll', 'obs_dir': env.env.root,
                'private_sdl_sha256': 'f' * 64, 'source_commit': 'a' * 40,
                'checkpoint_path': env.env.root / 'checkpoint.json',
                'checkpoint_sha256': 'b' * 64, 'artifacts': {}}
    with ExitStack() as stack:
        stack.enter_context(patch.object(M, '_libobs_start_client', REAL_START))
        stack.enter_context(patch.object(native_artifacts, 'load_native_artifacts', return_value=verified))
        stack.enter_context(patch.object(obs_backend, 'ObsRecorder', Native))
        if wmi:
            stack.enter_context(patch('psutil.pid_exists', side_effect=lambda pid: alive[0]))
            stack.enter_context(patch('psutil.Process', side_effect=AssertionError('unknown WMI identity must not be killed')))
        yield alive


def test_strict_writer_failures():
    with NativeEnv() as env:
        target = env.env.root / 'critical.json'
        old = b'{"old":1}'
        foreign = target.with_name(target.name + '.tmp-foreign')
        foreign.write_bytes(b'foreign')
        for point in ('fsync', 'replace'):
            target.write_bytes(old)
            error = OSError('original ' + point + ' error')
            with patch.object(M.os, point, side_effect=error) as injected:
                with patch.object(M.os, 'replace', wraps=M.os.replace) if point == 'fsync' else ExitStack() as replace_spy:
                    try:
                        M._recording_write_json(target, {'new': 2})
                    except OSError as caught:
                        check(point + ' original exception propagated', caught is error)
                    else:
                        check(point + ' original exception propagated', False)
                    check(point + ' syscall reached', injected.call_count == 1)
                    if point == 'fsync':
                        check('fsync failure prevents rename', replace_spy.call_count == 0)
            check(point + ' leaves old target unchanged', target.read_bytes() == old)
            check(point + ' removes only its own temp', list(env.env.root.glob('critical.json.tmp-*')) == [foreign]
                  and foreign.read_bytes() == b'foreign')
        fixed = SimpleNamespace(hex='collision')
        collision = target.with_name(target.name + '.tmp-collision')
        collision.write_bytes(b'previous attempt')
        with patch.object(M.uuid, 'uuid4', return_value=fixed):
            try:
                M._recording_write_json(target, {'new': 3})
            except FileExistsError:
                check('exclusive temp collision propagates actual FileExistsError', True)
            else:
                check('exclusive temp collision propagates actual FileExistsError', False)
        check('exclusive temp collision preserves foreign temp', collision.read_bytes() == b'previous attempt')
        check('collision cannot replace prior target', target.read_bytes() == old)
        real_unlink = Path.unlink
        def deny_temp_cleanup(path, *args, **kwargs):
            if path.name.startswith('critical.json.tmp-') and path not in (foreign, collision):
                raise PermissionError('secondary cleanup error')
            return real_unlink(path, *args, **kwargs)
        with patch.object(M.os, 'replace', side_effect=error), patch.object(Path, 'unlink', deny_temp_cleanup):
            try:
                M._recording_write_json(target, {'new': 4})
            except OSError as caught:
                check('cleanup failure cannot replace original exception', caught is error)
        for temp in env.env.root.glob('critical.json.tmp-*'):
            if temp not in (foreign, collision):
                temp.unlink()
        M._recording_write_json(target, {'new': 5})
        check('stale siblings do not collide with unique successful publication',
              json.loads(target.read_text()) == {'new': 5} and foreign.exists() and collision.exists())
        with patch.object(M.os, 'replace', side_effect=PermissionError('legacy remains best effort')):
            M._atomic_write_json(env.env.root / 'legacy.json', {'x': 1})
        check('legacy best effort writer remains nonthrowing', True)


def test_pre_spawn_artifacts_failure():
    for point, inject in (('replace', deny_replace), ('fsync', deny_fsync)):
        with NativeEnv() as env, real_launch_with_fake_native(env):
            with inject('native-artifacts.json') as calls:
                result = M.recording_start(backend='libobs', audio_framed=str(env.framed))
            out = Path(result['dir'])
            check('artifacts failure reaches low level ' + point, len(calls) == 1)
            check('artifacts failure cannot spawn or report success', not result['ok'] and not env.clients)
            check('artifacts failure leaves failed manifest and no published artifacts',
                  json.loads((out / 's4b/manifest.json').read_text())['state'] == 'failed'
                  and not (out / 'native-artifacts.json').exists() and not list(out.glob('*.tmp-*')))
            check('artifacts failure releases pending admission', not M._recording_lock_path().exists()
                  and M._RECORDING_PREPARING is None and not M._RECORDING_ADMISSION_LOCK.locked())
            rec, _ = env.start()
            check('artifacts retry constructs only one host', len(env.clients) == 1 and rec['active'])
            M.recording_stop()


def test_on_spawn_lock_failure():
    for wmi in (False, True):
        with NativeEnv() as env:
            stubborn = [wmi]
            with real_launch_with_fake_native(env, stubborn=stubborn, wmi=wmi):
                with deny_replace('recording.lock', predicate=lambda data: bool(data.get('native_pid'))) as calls:
                    result = M.recording_start(backend='libobs', audio_framed=str(env.framed))
                client = env.clients[0]
                check(f'lock failure wmi={wmi} never prepares or starts', calls and len(env.clients) == 1
                      and client.prepare_calls == 0 and client.start_calls == 0 and not result['ok'])
                check(f'lock failure wmi={wmi} cleans its temp', not list(M._recording_lock_path().parent.glob('recording.lock.tmp-*')))
                if not wmi:
                    check('confirmed on_spawn cleanup releases owner', result['process_exit_confirmed']
                          and client.owner.closed and not M._recording_lock_path().exists())
                else:
                    rec = M._RUNTIME.recorder
                    check('unknown WMI owner remains in memory and retryable', result['retryable']
                          and not result['process_exit_confirmed'] and rec['client'] is client and rec['native_pid'] == client.pid)
                    owner = json.loads(M._recording_lock_path().read_text())
                    check('failed bind does not pretend native PID was persisted', not owner.get('native_pid'))
                    check('unknown WMI owner blocks second launch', M.recording_start(backend='libobs',
                          audio_framed=str(env.framed))['error'] == 'recording_active' and len(env.clients) == 1)
                    stubborn[0] = False
                    retry = M.recording_stop()
                    check('unknown WMI later exit permits only failed cleanup', retry['process_exit_confirmed']
                          and not retry['ok'] and not M._recording_lock_path().exists())


def test_post_start_identity_state_failure():
    for target in ('identity.json', 'recording.json'):
        for denied_close in (False, True):
            with NativeEnv() as env, real_launch_with_fake_native(env, stubborn=(stubborn := [denied_close])):
                with deny_replace(target) as calls:
                    result = M.recording_start(backend='libobs', audio_framed=str(env.framed))
                client = env.clients[0]
                check(f'{target} close_denied={denied_close} fails after real start path', bool(calls)
                      and client.start_calls == 1 and not result['ok'])
                if not denied_close:
                    check(target + ' confirmed cleanup releases live owner', result['process_exit_confirmed']
                          and client.owner.closed and not M._recording_lock_path().exists())
                else:
                    rec = M._RUNTIME.recorder
                    owner = json.loads(M._recording_lock_path().read_text())
                    check(target + ' unconfirmed cleanup preserves persisted native PID', result['retryable']
                          and not result['process_exit_confirmed'] and owner['native_pid'] == client.pid
                          and rec['client'] is client and rec['active'])
                    check(target + ' unconfirmed failure is explicit', 'critical replace denied' in result['detail'])
                    check(target + ' cleanup exercises denied terminate and kill',
                          any('terminate: PermissionError' in x for x in result['cleanup_errors'])
                          and any('kill: PermissionError' in x for x in result['cleanup_errors']))
                    if target == 'recording.json':
                        check('pending state publication failure is disclosed', any('pending state' in x
                              and 'critical replace denied' in x for x in result['cleanup_errors']))
                    check(target + ' surviving owner blocks another host', M.recording_start(backend='libobs',
                          audio_framed=str(env.framed))['error'] == 'recording_active' and len(env.clients) == 1)
                    stubborn[0] = False
                    retry = M.recording_stop()
                    check(target + ' later stop is failed cleanup not successful media', not retry['ok']
                          and retry['process_exit_confirmed'] and not retry['retryable']
                          and client.owner.closed and not M._recording_lock_path().exists())


def test_restart_state_failure():
    for denied_close in (False, True):
        with NativeEnv() as env, real_launch_with_fake_native(env, stubborn=(stubborn := [False])):
            rec, first = env.start()
            first.owner.closed = True
            # Old exit is confirmed before replacement; deny only the new close.
            close = type(first).close
            def close_replacement(self, force=False):
                if self is not first and denied_close:
                    raise PermissionError('replacement close denied')
                return close(self, force=force)
            with deny_replace('recording.json', predicate=lambda data: int(data.get('restart_count', 0)) == 1) as calls, \
                    patch.object(type(first), 'close', close_replacement):
                M._libobs_restart_after_death(rec, 7)
            replacement = env.clients[-1]
            check(f'restart state denied close={denied_close} reached replace and no r2', bool(calls)
                  and len(env.clients) == 2 and first.owner.closed and rec['restart_count'] == 1)
            check('restart failed evidence retains fault and run history', rec['watchdog_faults']
                  and rec['last_restart']['fault_saved'] and rec.get('recording_failed'))
            if denied_close:
                check('unconfirmed replacement retains its persisted ownership', rec['active']
                      and rec['client'] is replacement and json.loads(M._recording_lock_path().read_text())['native_pid'] == replacement.pid)
                check('unconfirmed replacement cannot produce another host', M.recording_start(backend='libobs',
                      audio_framed=str(env.framed))['error'] == 'recording_active' and len(env.clients) == 2)
            terminal = M.recording_stop()
            check('restart publication failure never becomes recovery success', not terminal['ok']
                  and terminal['process_exit_confirmed'] and replacement.owner.closed
                  and not M._recording_lock_path().exists())


def main():
    test_strict_writer_failures()
    test_pre_spawn_artifacts_failure()
    test_on_spawn_lock_failure()
    test_post_start_identity_state_failure()
    test_restart_state_failure()
    print(f'{CHECKS}/{CHECKS} checks passed')


if __name__ == '__main__':
    main()
