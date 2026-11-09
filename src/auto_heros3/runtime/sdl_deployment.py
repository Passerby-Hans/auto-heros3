"""Managed, hash-checked SDL deployment; no game or endpoint operations.

Normal native sessions may leave the managed private DLL installed. A disk
session explicitly restores the pinned original. The on-disk transaction
records both sides before replacement so an interrupted upgrade is recoverable
even after the stage directory contains a newer private DLL.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

SCHEMA = 'a3-sdl-deployment/1'
OWNER = 'auto-heros3/private-sdl'
SHA256 = re.compile(r'^[0-9a-f]{64}$')


class SdlDeploymentError(RuntimeError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f'{code}: {detail}')


def _hash(path: Path) -> str:
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def _check_hash(value) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise SdlDeploymentError('sdl_identity_invalid', 'invalid SHA256 identity')
    return value


def _atomic_bytes(path: Path, content: bytes) -> None:
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('xb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_record(path: Path, record: dict) -> None:
    _atomic_bytes(path, (json.dumps(record, sort_keys=True, indent=2) + '\n').encode('utf-8'))


@contextmanager
def _deployment_lock(path: Path):
    # OS locks are released on process death. A stale lock filename therefore
    # never causes us to guess whether it is safe to delete another owner.
    with path.open('a+b') as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b'\0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # deterministic file-only tests also run outside Windows
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SdlDeploymentError('sdl_deployment_busy', str(path)) from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def deploy_sdl(target: Path, artifacts: dict, *, private: bool) -> dict:
    """Install a verified private/original artifact without adopting unknown DLLs.

    ``artifacts`` must come from load_native_artifacts (or the independently
    verified original-only loader used for rollback). The target is fixed by
    the owning launcher, not supplied by a recording request.
    """
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    record_path = target.with_name('.a3-sdl-deployment.json')
    lock_path = target.with_name('.a3-sdl-deployment.lock')
    original = Path(artifacts['original_sdl']).resolve()
    original_hash = _check_hash(artifacts['original_sdl_sha256'])
    source = Path(artifacts['private_sdl'] if private else original).resolve()
    wanted = _check_hash(artifacts['private_sdl_sha256'] if private else original_hash)
    known_private = {_check_hash(value) for value in artifacts.get('known_private_sdl_sha256', [])}
    if private:
        known_private.add(wanted)
    if original_hash in known_private:
        raise SdlDeploymentError('sdl_identity_invalid', 'original and private identities overlap')
    if source == target or original == target:
        raise SdlDeploymentError('sdl_identity_invalid', 'stage and runtime paths must differ')
    try:
        if not original.is_file() or _hash(original) != original_hash:
            raise SdlDeploymentError('sdl_original_invalid', str(original))
        if not source.is_file() or _hash(source) != wanted:
            raise SdlDeploymentError('sdl_source_invalid', str(source))
        with _deployment_lock(lock_path):
            record = None
            if record_path.exists():
                try:
                    record = json.loads(record_path.read_text(encoding='utf-8'))
                    if (record.get('schema') != SCHEMA or record.get('owner') != OWNER or
                            Path(record['target']).resolve() != target or
                            record['original_sha256'] != original_hash or
                            record['state'] not in ('private', 'original', 'pending')):
                        raise ValueError('record identity or state mismatch')
                    recorded = {_check_hash(x) for x in record['managed_private_sha256']}
                    if original_hash in recorded:
                        raise ValueError('original listed as private')
                    known_private.update(recorded)
                    _check_hash(record['desired_sha256'])
                    if record['desired_sha256'] not in known_private | {original_hash}:
                        raise ValueError('unknown transaction target')
                except (KeyError, TypeError, ValueError) as exc:
                    raise SdlDeploymentError('sdl_deployment_record_invalid', str(record_path)) from exc
            observed = _hash(target) if target.is_file() else None
            if observed is not None and observed not in known_private | {original_hash}:
                raise SdlDeploymentError('sdl_runtime_unmanaged',
                                         f'{target} sha256={observed}; refusing replacement')
            # Recheck exact source bytes inside the lock; a stage rebuild must
            # not race the earlier hash inspection and deploy different bytes.
            content = source.read_bytes()
            if hashlib.sha256(content).hexdigest() != wanted:
                raise SdlDeploymentError('sdl_source_changed', str(source))
            transaction = {
                'schema': SCHEMA, 'owner': OWNER, 'operation_token': uuid.uuid4().hex,
                'writer_pid': os.getpid(), 'target': str(target),
                'original_path': str(original), 'original_sha256': original_hash,
                'managed_private_sha256': sorted(known_private),
                'previous_sha256': observed, 'desired_sha256': wanted,
                'desired_role': 'private' if private else 'original', 'state': 'pending',
                'checkpoint_path': str(artifacts.get('checkpoint_path') or ''),
                'checkpoint_sha256': str(artifacts.get('checkpoint_sha256') or ''),
                'source_commit': str(artifacts.get('source_commit') or ''),
            }
            _write_record(record_path, transaction)
            if observed != wanted:
                _atomic_bytes(target, content)
            if _hash(target) != wanted:
                raise SdlDeploymentError('sdl_runtime_verification_failed', str(target))
            transaction['state'] = transaction['desired_role']
            _write_record(record_path, transaction)
            return {'state': transaction['state'], 'path': str(target), 'sha256': wanted,
                    'previous_sha256': observed, 'changed': observed != wanted,
                    'record': str(record_path), 'operation_token': transaction['operation_token']}
    except SdlDeploymentError:
        raise
    except OSError as exc:
        raise SdlDeploymentError('sdl_deployment_io_failed', str(exc)) from exc
