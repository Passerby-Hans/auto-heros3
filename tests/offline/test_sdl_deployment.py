"""SDL ownership/upgrade/recovery contracts using only temporary byte fixtures.

No DLL is loaded, no process is launched, and no user runtime/settings are touched.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tools'))

from auto_heros3.runtime import sdl_deployment as D
from auto_heros3.runtime import vcmi_agent as A
from auto_heros3.recording import native_artifacts as N

RESULTS = []


def check(name, condition):
    RESULTS.append((name, bool(condition)))


def sha(value):
    return hashlib.sha256(value).hexdigest()


@contextmanager
def fixture():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        stage = root / 'stage'
        runtime = root / 'runtime'
        stage.mkdir()
        runtime.mkdir()
        original, private = stage / 'SDL2-original.dll', stage / 'SDL2.dll'
        original.write_bytes(b'canonical-original')
        private.write_bytes(b'private-v1')
        target = runtime / 'SDL2.dll'
        target.write_bytes(original.read_bytes())
        artifacts = {'original_sdl': original, 'private_sdl': private,
                     'original_sdl_sha256': sha(original.read_bytes()),
                     'private_sdl_sha256': sha(private.read_bytes()),
                     'known_private_sdl_sha256': [sha(private.read_bytes())],
                     'source_commit': 'fixture-clean-build',
                     'checkpoint_sha256': sha(b'checkpoint'),
                     'checkpoint_path': stage / 'checkpoint.json'}
        yield root, target, artifacts


def original_only(artifacts):
    return {key: value for key, value in artifacts.items()
            if key in ('original_sdl', 'original_sdl_sha256', 'known_private_sdl_sha256')}


def error_code(callback):
    try:
        callback()
    except D.SdlDeploymentError as exc:
        return exc.code
    return None


def test_upgrade_and_restore():
    with fixture() as (_, target, artifacts):
        backup = target.with_name('SDL2.dll.original')
        backup.write_bytes(b'user-owned-backup')
        installed = D.deploy_sdl(target, artifacts, private=True)
        check('install_private', installed['changed'] and target.read_bytes() == b'private-v1')
        record_path = Path(installed['record'])
        record = json.loads(record_path.read_text())
        check('record_ownership_identity', record['owner'] == D.OWNER and
              record['original_sha256'] == artifacts['original_sdl_sha256'] and
              record['checkpoint_sha256'] == artifacts['checkpoint_sha256'] and
              record['source_commit'] == 'fixture-clean-build')
        old_hash = artifacts['private_sdl_sha256']
        artifacts['private_sdl'].write_bytes(b'private-v2')
        artifacts['private_sdl_sha256'] = sha(b'private-v2')
        artifacts['known_private_sdl_sha256'] = [sha(b'private-v2')]
        upgraded = D.deploy_sdl(target, artifacts, private=True)
        record = json.loads(record_path.read_text())
        check('old_managed_private_upgrades', upgraded['previous_sha256'] == old_hash and
              target.read_bytes() == b'private-v2')
        check('both_generations_retained', set(record['managed_private_sha256']) ==
              {sha(b'private-v1'), sha(b'private-v2')})
        # Rollback is independent of the private artifact and native recorder.
        artifacts['private_sdl'].unlink()
        original = original_only(artifacts)
        original['known_private_sdl_sha256'] = []
        restored = D.deploy_sdl(target, original, private=False)
        check('disk_restores_without_current_private', restored['state'] == 'original' and
              target.read_bytes() == b'canonical-original')
        check('restore_idempotent', not D.deploy_sdl(target, original, private=False)['changed'])
        check('user_backup_untouched', backup.read_bytes() == b'user-owned-backup')


def test_explicit_historical_pin():
    with fixture() as (_, target, artifacts):
        target.write_bytes(b'pinned-historical-private')
        artifacts['known_private_sdl_sha256'].append(sha(target.read_bytes()))
        installed = D.deploy_sdl(target, artifacts, private=True)
        check('historical_private_adoption', installed['previous_sha256'] ==
              sha(b'pinned-historical-private') and target.read_bytes() == b'private-v1')


def test_unknown_and_invalid_inputs():
    with fixture() as (_, target, artifacts):
        target.write_bytes(b'third-party-user-dll')
        check('unknown_private_install_rejected', error_code(
            lambda: D.deploy_sdl(target, artifacts, private=True)) == 'sdl_runtime_unmanaged')
        check('unknown_disk_restore_rejected', error_code(
            lambda: D.deploy_sdl(target, original_only(artifacts), private=False)) == 'sdl_runtime_unmanaged')
        check('unknown_dll_untouched', target.read_bytes() == b'third-party-user-dll')
        check('unknown_not_adopted', not target.with_name('.a3-sdl-deployment.json').exists())
    with fixture() as (_, target, artifacts):
        artifacts['original_sdl'].unlink()
        check('missing_original_rejected', error_code(
            lambda: D.deploy_sdl(target, artifacts, private=True)) == 'sdl_original_invalid')
        check('missing_original_runtime_preserved', target.read_bytes() == b'canonical-original')
    with fixture() as (_, target, artifacts):
        artifacts['private_sdl'].write_bytes(b'changed-build')
        check('changed_stage_hash_rejected', error_code(
            lambda: D.deploy_sdl(target, artifacts, private=True)) == 'sdl_source_invalid')
        check('stage_mismatch_runtime_preserved', target.read_bytes() == b'canonical-original')
    with fixture() as (_, target, artifacts):
        target.with_name('.a3-sdl-deployment.json').write_text('{broken')
        check('corrupt_ownership_rejected', error_code(
            lambda: D.deploy_sdl(target, artifacts, private=True)) == 'sdl_deployment_record_invalid')
        check('corrupt_record_runtime_preserved', target.read_bytes() == b'canonical-original')


def test_pending_transactions_and_lock():
    with fixture() as (_, target, artifacts):
        replace = D.os.replace

        def deny_runtime(source, destination):
            if Path(destination) == target:
                raise PermissionError('injected runtime file lock')
            return replace(source, destination)

        with patch.object(D.os, 'replace', side_effect=deny_runtime):
            check('replace_failure_explicit', error_code(
                lambda: D.deploy_sdl(target, artifacts, private=True)) == 'sdl_deployment_io_failed')
        record = json.loads(target.with_name('.a3-sdl-deployment.json').read_text())
        check('failed_replace_retains_original_and_pending', target.read_bytes() == b'canonical-original'
              and record['state'] == 'pending')
        check('pending_can_restore', D.deploy_sdl(target, original_only(artifacts), private=False)['state'] == 'original')
        check('temporary_files_cleaned', not list(target.parent.glob('*.tmp')))
    with fixture() as (_, target, artifacts):
        write_record = D._write_record

        def fail_publication(path, record):
            if record['state'] == 'private':
                raise OSError('injected final record failure')
            return write_record(path, record)

        with patch.object(D, '_write_record', side_effect=fail_publication):
            check('publication_failure_explicit', error_code(
                lambda: D.deploy_sdl(target, artifacts, private=True)) == 'sdl_deployment_io_failed')
        check('publication_failure_recoverable', target.read_bytes() == b'private-v1' and
              json.loads(target.with_name('.a3-sdl-deployment.json').read_text())['state'] == 'pending')
        D.deploy_sdl(target, original_only(artifacts), private=False)
        check('post_replace_crash_restores_original', target.read_bytes() == b'canonical-original')
        with D._deployment_lock(target.with_name('.a3-sdl-deployment.lock')):
            check('concurrent_deployment_rejected', error_code(
                lambda: D.deploy_sdl(target, artifacts, private=True)) == 'sdl_deployment_busy')


def test_failed_launch_and_session_binding():
    with fixture() as (root, target, artifacts):
        with patch.dict(A.os.environ, {'AUTO_HEROS3_AUDIO_DRIVER': 'a3capture'}):
            agent = A.VcmiAgent(root / 'bridge', log=False)
        agent._sdl_paths = lambda: (artifacts['private_sdl'], artifacts['original_sdl'], target)
        agent._stop_audio_relay = lambda: None
        agent._restore_video_settings = lambda: None
        agent._load_session = lambda: None

        def failed_start(timeout):
            agent._ensure_private_sdl()
            raise RuntimeError('injected before launcher')

        agent._start_impl = failed_start
        with patch.object(N, 'load_native_artifacts', return_value=artifacts), \
                patch.object(N, 'load_original_sdl', return_value=original_only(artifacts)), \
                patch.object(A.psutil, 'process_iter', return_value=[]):
            try:
                agent.start()
            except RuntimeError as exc:
                check('failed_launch_preserves_cause', str(exc) == 'injected before launcher')
            else:
                check('failed_launch_preserves_cause', False)
        check('failed_launch_product_canonical_restore', target.read_bytes() == b'canonical-original'
              and agent.sdl_deployment['state'] == 'original' and not agent._sdl_stage_active)
        check('explicit_environment_snapshotted', agent.audio_driver_explicit)
        session = A.Session(audio_driver='a3capture', audio_driver_explicit=True,
                            audio_capture_file=str(root / 'actual-session.a3pcm'),
                            audio_sdl_sha256=artifacts['private_sdl_sha256'])
        restored = A.Session.from_json(session.to_json())
        check('session_audio_binding_roundtrip', restored.audio_driver == 'a3capture' and
              restored.audio_driver_explicit and restored.audio_capture_file == session.audio_capture_file
              and restored.audio_sdl_sha256 == session.audio_sdl_sha256)
        check('old_session_binding_is_unknown', A.Session.from_json('{"dir":"old"}').audio_driver == '')
        agent._sdl_stage_active = True
        target.write_bytes(b'private-v1')
        with patch.object(A.psutil, 'process_iter', return_value=[
                SimpleNamespace(info={'pid': 123, 'name': 'VCMI_client.exe'})]):
            try:
                agent._rollback_failed_sdl_stage()
            except A.BridgeError as exc:
                check('live_client_defers_rollback', 'sdl_rollback_deferred' in str(exc))
            else:
                check('live_client_defers_rollback', False)
        check('live_client_runtime_not_replaced', target.read_bytes() == b'private-v1')


def main():
    test_upgrade_and_restore()
    test_explicit_historical_pin()
    test_unknown_and_invalid_inputs()
    test_pending_transactions_and_lock()
    test_failed_launch_and_session_binding()
    for name, ok in RESULTS:
        print(('PASS ' if ok else 'FAIL ') + name)
    failures = sum(not ok for _, ok in RESULTS)
    print(f'\n{len(RESULTS) - failures}/{len(RESULTS)} checks passed')
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
