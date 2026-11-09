"""Native promotion preflight/routing regressions; no process or media access."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcp_server_v2 as M  # noqa: E402
from auto_heros3.recording.native_artifacts import (  # noqa: E402
    NativeArtifactsError, load_native_artifacts, load_original_sdl,
)
from test_action_contract import Env  # noqa: E402

CHECKS = 0


def check(name, condition):
    global CHECKS
    assert condition, name
    CHECKS += 1
    print(f'PASS {name}')


def rejected(name, callback, code):
    try:
        callback()
    except NativeArtifactsError as exc:
        check(name, exc.code == code)
    else:
        raise AssertionError(f'{name}: accepted invalid data')


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def record(path, content=None):
    if content is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    value = path.read_bytes()
    return {'path': str(path), 'bytes': len(value), 'sha256': hashlib.sha256(value).hexdigest()}


class Fixture:
    def __init__(self, root):
        self.root = root
        self.obs = root / 'installed-obs'
        inputs = [record(self.obs / path, path.encode()) for path in (
            'bin/64bit/obs.dll', 'bin/64bit/libobs-d3d11.dll',
            'obs-plugins/64bit/obs-ffmpeg.dll', 'obs-plugins/64bit/obs-x264.dll',
            'obs-plugins/64bit/win-capture.dll')]
        self.original = root / 'platform/sdl-capture-patch/stage/SDL2-original.dll'
        original = record(self.original, b'canonical-original')
        write_json(root / 'platform/sdl-capture-patch/source-lock.json', {
            'schema_version': 1, 'runtime_sha256': original['sha256'],
            'historical_checkpoint': {'private_dll_sha256': 'e' * 64}})
        self.commit = 'a' * 40
        source = {'commit': self.commit, 'dirty': False, 'tree': 'b' * 40}
        self.provenances = {}
        self.checkpoint = {'schema': 'a3-native-checkpoint/1', 'state': 'built_unvalidated',
                           'source_commit': self.commit, 'source_dirty': False, 'components': []}
        self.path = root / 'platform/obs-recorder/bin/native-checkpoint.json'
        for name, directory, artifact in (
            ('native-recorder', 'platform/obs-recorder/build-media', 'bin/auto-heros3-recorder.exe'),
            ('a3-obs-outputs', 'platform/obs-recorder/bin', 'a3-obs-outputs.dll'),
            ('sdl-a3capture', 'platform/sdl-capture-patch', 'stage/SDL2.dll'),
        ):
            base = root / directory
            artifact_record = record(base / artifact, name.encode())
            start = {'schema': 'a3-build-provenance/1', 'component': name, 'state': 'started',
                     'source': source, 'require_clean': True, 'inputs': inputs,
                     'configuration': {'obs_version': '32.1.0'}}
            start_path = base / 'provenance/build.start.json'
            write_json(start_path, start)
            provenance = start | {'state': 'built', 'source_end': source,
                                 'source_unchanged': True, 'vendors': [],
                                 'start_record': str(start_path),
                                 'start_record_sha256': record(start_path)['sha256'],
                                 'artifacts': [artifact_record]}
            if name == 'native-recorder':
                # A full checkpoint includes diagnostics; the deployment does not.
                missing_test = base / 'bin/audio-core-tests.exe'
                provenance['artifacts'].append(record(missing_test, b'test-executable'))
                missing_test.unlink()
                self.exe = base / artifact
            path = base / 'provenance/build.json'
            self.provenances[name] = (path, provenance)
            self.checkpoint['components'].append({
                'component': name, 'provenance': str(path),
                'artifacts': list(provenance['artifacts'])})
        self.persist()

    def persist(self):
        for component in self.checkpoint['components']:
            path, provenance = self.provenances[component['component']]
            write_json(path, provenance)
            component['provenance_sha256'] = record(path)['sha256']
        write_json(self.path, self.checkpoint)

    def load(self):
        return load_native_artifacts(self.root, obs_dir=self.obs)


def test_artifacts():
    with tempfile.TemporaryDirectory() as temp:
        fixture = Fixture(Path(temp))
        result = fixture.load()
        check('complete checkpoint accepted without current HEAD or diagnostic executable',
              result['source_commit'] == fixture.commit and len(result['artifacts']) == 8)
        check('migration recognizes pinned historical and current private SDL',
              set(result['known_private_sdl_sha256']) == {'e' * 64, result['private_sdl_sha256']})
        fixture.exe.unlink()
        rejected('missing executable fails closed', fixture.load, 'native_artifact_unreadable')
        check('canonical SDL fallback independent of missing native executable',
              load_original_sdl(fixture.root)['original_sdl'] == fixture.original)

    mutations = [
        ('unknown checkpoint schema', lambda f: f.checkpoint.update(schema='a3-native-checkpoint/99'),
         'native_checkpoint_schema'),
        ('dirty checkpoint', lambda f: f.checkpoint.update(source_dirty=True), 'native_checkpoint_dirty'),
        ('duplicate component', lambda f: f.checkpoint['components'].__setitem__(
            1, dict(f.checkpoint['components'][0])), 'native_component_invalid'),
        ('wrong component provenance source', lambda f: f.provenances['native-recorder'][1].update(
            source={'commit': 'f' * 40, 'dirty': False}), 'native_provenance_source_mismatch'),
        ('build source changed', lambda f: f.provenances['native-recorder'][1].update(
            source_unchanged=False), 'native_provenance_not_clean'),
        ('build start input differs', lambda f: f.provenances['native-recorder'][1].update(
            inputs=[]), 'native_build_start_mismatch'),
        ('checkpoint artifact differs from provenance', lambda f: f.checkpoint['components'][0].update(
            artifacts=[f.checkpoint['components'][0]['artifacts'][0]]),
         'native_artifact_declaration_mismatch'),
        ('unsupported ABI', lambda f: replace_configuration(f, '99.0'), 'native_obs_abi_unsupported'),
        ('unlisted installed dependency', lambda f: remove_dependency(f), 'native_dependency_unlisted'),
    ]
    for name, mutate, code in mutations:
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            mutate(fixture)
            fixture.persist()
            rejected(name, fixture.load, code)
    for name, target, code in (
        ('runtime binary changed', lambda f: f.exe, 'native_hash_mismatch'),
        ('OBS plugin changed', lambda f: f.obs / 'obs-plugins/64bit/obs-x264.dll', 'native_hash_mismatch'),
        ('canonical SDL changed', lambda f: f.original, 'native_hash_mismatch'),
        ('original provenance changed', lambda f: f.provenances['native-recorder'][0], 'native_hash_mismatch'),
    ):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(Path(temp))
            target(fixture).write_bytes(b'changed')
            rejected(name, fixture.load, code)


def replace_configuration(fixture, version):
    _, provenance = fixture.provenances['native-recorder']
    provenance['configuration'] = {'obs_version': version}
    start_path = Path(provenance['start_record'])
    start = json.loads(start_path.read_text())
    start['configuration'] = provenance['configuration']
    write_json(start_path, start)
    provenance['start_record_sha256'] = record(start_path)['sha256']


def remove_dependency(fixture):
    _, provenance = fixture.provenances['native-recorder']
    provenance['inputs'] = provenance['inputs'][:-1]
    start_path = Path(provenance['start_record'])
    start = json.loads(start_path.read_text())
    start['inputs'] = provenance['inputs']
    write_json(start_path, start)
    provenance['start_record_sha256'] = record(start_path)['sha256']


def test_audio_route():
    with Env() as env:
        directory = M._dir()
        stale = directory / 'game-audio.raw'
        stale.write_bytes(b'old audio')
        framed = directory / 'current-session.a3a'
        ready = Path(str(framed) + '.ready')
        identity = {'client_pid': 123, 'session_id': 'current'}
        session = SimpleNamespace(**identity, audio_driver='a3capture', audio_driver_explicit=False,
                                  audio_capture_file=str(framed), audio_sdl_sha256='c' * 64,
                                  alive=lambda: True)
        agent = SimpleNamespace(session=session, audio_driver='disk', a3capture_file=stale,
                                _load_session=lambda: session)
        env.agent(agent)
        route = lambda: M._select_native_audio_source(identity=identity)
        rejected('stale raw never masks missing session framed stream', route, 'native_session_audio_missing')
        framed.write_bytes(b'framed fixture')
        rejected('framed without ready fails, no raw fallback', route, 'native_audio_not_ready')
        ready.write_text('ready')
        result = route()
        check('session binding wins over current constructor and stale raw',
              result['audio_framed'] == framed and result['audio_raw'] is None
              and result['audio_framed_live'] is True)
        agent.session = None
        check('MCP restart reloads persisted audio session', route()['audio_framed'] == framed)
        agent._load_session = lambda: None
        rejected('unknown restored session fails closed', route, 'native_audio_session_missing')
        agent.session = session
        with patch.object(session, 'alive', return_value=False):
            rejected('dead or recycled client cannot supply native session audio', route,
                     'native_audio_session_dead')
        with patch.object(session, 'alive', side_effect=RuntimeError('identity check unavailable')):
            rejected('client identity check exception cannot select stale raw', route,
                     'native_audio_session_unavailable')
        with patch.object(M, '_agent', side_effect=RuntimeError('broken lease')):
            rejected('agent exception cannot select stale raw', route, 'native_audio_session_unavailable')
        session.audio_driver = 'wasapi'
        rejected('unexpected audio driver rejected', route, 'native_audio_driver_mismatch')
        session.audio_driver = 'disk'
        with patch.dict('os.environ', {'AUTO_HEROS3_AUDIO_DRIVER': 'disk'}):
            rejected('changing environment cannot turn old session into explicit disk', route,
                     'native_audio_driver_mismatch')
        session.audio_driver_explicit = True
        session.audio_capture_file = str(stale)
        check('explicit session disk compatibility uses its recorded raw file', route()['audio_raw'] == stale)
        session.session_id = 'old'
        rejected('session identity mismatch rejects stale stream', route, 'native_audio_session_mismatch')
        with patch.object(M, '_agent', side_effect=AssertionError('explicit diagnostic should not use agent')):
            selected = M._select_native_audio_source(str(framed), False)
            check('explicit replay remains deliberate and does not become live',
                  selected['audio_route'] == 'explicit_framed' and not selected['audio_framed_live'])
        session.session_id, session.audio_driver = 'current', 'a3capture'
        session.audio_capture_file = str(framed)
        ready.unlink()
        executable = directory / 'recorder.exe'
        executable.write_bytes(b'never execute')
        with patch.object(M, '_obs_recorder_exe', return_value=executable), \
                patch.object(M, '_evidence_identity', return_value=identity), \
                patch.object(M, '_acquire_recording_lock', side_effect=AssertionError('must fail before lock')), \
                patch.object(M, '_libobs_start_client', side_effect=AssertionError('must not start recorder')):
            failed = M._recording_start_libobs(60, 1080, 10, 60)
            check('public native start reports readiness error before any lock or process',
                  failed['ok'] is False and failed['error'] == 'native_audio_not_ready')
        write_json(directory / 'session.json', {'audio_driver': 'a3capture'})
        with patch.object(M, '_active_recording_info', return_value=None), \
                patch.object(M, '_acquire_recording_lock', side_effect=AssertionError('must not lock')):
            failed = M.recording_start(backend='legacy')
            check('legacy cannot silently record stale raw from an a3capture session',
                  failed['ok'] is False and failed['error'] == 'legacy_audio_driver_mismatch')


def test_launch_preflight():
    from auto_heros3.recording import native_artifacts, obs_backend
    with tempfile.TemporaryDirectory() as temp:
        fixture = Fixture(Path(temp))
        verified = fixture.load()
        out = fixture.root / 'recording'
        with patch.object(native_artifacts, 'load_native_artifacts', return_value=verified), \
                patch.object(M, '_obs_recorder_exe', return_value=fixture.exe), \
                patch.object(obs_backend, 'ObsRecorder', side_effect=AssertionError('must not create host')):
            rejected('different running SDL checkpoint fails before process creation',
                     lambda: M._libobs_start_client(out, client_pid=123, audio_raw=None,
                                                   audio_sdl_sha256='d' * 64, max_time_sec=60),
                     'native_session_sdl_mismatch')
            with patch.object(M, '_obs_recorder_exe', return_value=fixture.root / 'other.exe'):
                rejected('unverified executable override fails before process creation',
                         lambda: M._libobs_start_client(out, client_pid=123, audio_raw=None,
                                                       max_time_sec=60),
                         'native_recorder_override_mismatch')
        check('failed preflight creates no run artifacts', not out.exists())


if __name__ == '__main__':
    with patch('subprocess.Popen', side_effect=AssertionError('offline tests cannot start processes')):
        test_artifacts()
        test_audio_route()
        test_launch_preflight()
    print(f'{CHECKS}/{CHECKS} checks passed')
