"""Read-only, fail-closed verification of the native recording checkpoint.

This verifies build identity and installed bytes, not media acceptance. The
original clean build can precede HEAD; subsequent Python/test changes must not
rewrite its provenance. No DLL is loaded and no process is started here.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


class NativeArtifactsError(RuntimeError):
    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail
        super().__init__(f'{code}: {detail}')


def _require(condition: bool, code: str, detail: str) -> None:
    if not condition:
        raise NativeArtifactsError(code, detail)


def _sha(value: object) -> str:
    _require(isinstance(value, str) and bool(re.fullmatch(r'[0-9a-f]{64}', value)),
             'native_hash_invalid', str(value))
    return value


def _hash(path: Path) -> str:
    try:
        before = path.stat()
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        after = path.stat()
        _require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
                 'native_file_changed', str(path))
        return digest.hexdigest()
    except OSError as exc:
        raise NativeArtifactsError('native_artifact_unreadable', f'{path}: {exc}') from exc


def _json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise NativeArtifactsError('native_manifest_unreadable', f'{path}: {exc}') from exc
    _require(isinstance(data, dict), 'native_manifest_invalid', str(path))
    return data


def _inside(value: object, root: Path) -> Path:
    _require(isinstance(value, str) and bool(value), 'native_path_invalid', repr(value))
    path = Path(value).resolve()
    _require(path.is_relative_to(root), 'native_path_outside_root', str(path))
    return path


def _records(values: object, root: Path) -> dict[Path, dict]:
    _require(isinstance(values, list) and bool(values), 'native_records_invalid', str(root))
    records = {}
    for record in values:
        _require(isinstance(record, dict), 'native_record_invalid', repr(record))
        path = _inside(record.get('path'), root)
        _require(path not in records, 'native_record_duplicate', str(path))
        size = record.get('bytes')
        _require(type(size) is int and size > 0, 'native_size_invalid', str(path))
        records[path] = {'bytes': size, 'sha256': _sha(record.get('sha256'))}
    return records


def _verify(path: Path, record: dict) -> None:
    actual = _hash(path)
    _require(actual == _sha(record.get('sha256')), 'native_hash_mismatch', str(path))
    if 'bytes' in record:
        _require(path.stat().st_size == record['bytes'], 'native_size_mismatch', str(path))


def load_original_sdl(root: Path) -> dict:
    """Verify canonical disk-driver SDL independently of native availability."""
    root = Path(root).resolve()
    lock = _json(root / 'platform/sdl-capture-patch/source-lock.json')
    _require(lock.get('schema_version') == 1, 'sdl_source_lock_schema', str(root))
    original_sha = _sha(lock.get('runtime_sha256'))
    original = root / 'platform/sdl-capture-patch/stage/SDL2-original.dll'
    _verify(original, {'sha256': original_sha})
    historical = (lock.get('historical_checkpoint') or {}).get('private_dll_sha256')
    known = []
    if historical is not None:
        historical = _sha(historical)
        _require(historical != original_sha, 'sdl_source_lock_identity', historical)
        known.append(historical)
    return {'original_sdl': original, 'original_sdl_sha256': original_sha,
            'known_private_sdl_sha256': known}


def load_native_artifacts(root: Path, *, obs_dir: Path | None = None) -> dict:
    """Verify the three clean components and their pinned OBS runtime DLLs.

    Checkpoint ``built_unvalidated`` is immutable build status, not an assertion
    that current media validation passed. Test executables are not deployment
    prerequisites. Runtime DLLs and the product executable are prerequisites.
    """
    root = Path(root).resolve()
    obs_dir = Path(obs_dir or r'C:\Program Files\obs-studio').resolve()
    checkpoint_path = root / 'platform/obs-recorder/bin/native-checkpoint.json'
    checkpoint_sha = _hash(checkpoint_path)
    checkpoint = _json(checkpoint_path)
    _require(checkpoint.get('schema') == 'a3-native-checkpoint/1',
             'native_checkpoint_schema', str(checkpoint_path))
    _require(checkpoint.get('state') == 'built_unvalidated',
             'native_checkpoint_state', str(checkpoint.get('state')))
    commit = checkpoint.get('source_commit')
    _require(isinstance(commit, str) and bool(re.fullmatch(r'[0-9a-f]{40}', commit)),
             'native_commit_invalid', str(commit))
    _require(checkpoint.get('source_dirty') is False,
             'native_checkpoint_dirty', str(checkpoint_path))
    specs = {
        'native-recorder': ('platform/obs-recorder/build-media',
                            'bin/auto-heros3-recorder.exe', 'recorder_exe'),
        'a3-obs-outputs': ('platform/obs-recorder/bin', 'a3-obs-outputs.dll', 'outputs_module'),
        'sdl-a3capture': ('platform/sdl-capture-patch', 'stage/SDL2.dll', 'private_sdl'),
    }
    components = checkpoint.get('components')
    _require(isinstance(components, list) and len(components) == len(specs),
             'native_components_invalid', str(checkpoint_path))
    result = load_original_sdl(root)
    seen, verified, provenance_records = set(), {}, {}
    for component in components:
        _require(isinstance(component, dict), 'native_component_invalid', repr(component))
        name = component.get('component')
        _require(name in specs and name not in seen, 'native_component_invalid', str(name))
        seen.add(name)
        relative, required, key = specs[name]
        component_root = root / relative
        provenance_path = _inside(component.get('provenance'), component_root)
        _verify(provenance_path, {'sha256': component.get('provenance_sha256')})
        provenance = _json(provenance_path)
        _require(provenance.get('schema') == 'a3-build-provenance/1'
                 and provenance.get('component') == name and provenance.get('state') == 'built',
                 'native_provenance_schema', str(provenance_path))
        _require(provenance.get('require_clean') is True
                 and provenance.get('source_unchanged') is True,
                 'native_provenance_not_clean', str(provenance_path))
        source = provenance.get('source') or {}
        source_end = provenance.get('source_end') or {}
        _require(source.get('commit') == commit and source.get('dirty') is False
                 and source_end == source,
                 'native_provenance_source_mismatch', str(provenance_path))
        for vendor in provenance.get('vendors', []):
            _require(vendor.get('dirty') is False,
                     'native_vendor_dirty', str(provenance_path))
        start_path = _inside(provenance.get('start_record'), component_root)
        _verify(start_path, {'sha256': provenance.get('start_record_sha256')})
        start = _json(start_path)
        _require(start.get('schema') == 'a3-build-provenance/1'
                 and start.get('state') == 'started' and start.get('component') == name
                 and start.get('source') == source and start.get('require_clean') is True
                 and start.get('inputs') == provenance.get('inputs')
                 and start.get('configuration') == provenance.get('configuration'),
                 'native_build_start_mismatch', str(start_path))
        artifacts = _records(component.get('artifacts'), component_root)
        _require(artifacts == _records(provenance.get('artifacts'), component_root),
                 'native_artifact_declaration_mismatch', str(provenance_path))
        required_path = component_root / required
        _require(required_path in artifacts, 'native_required_artifact_unlisted', str(required_path))
        for path, record in artifacts.items():
            if path == required_path or path.suffix.lower() == '.dll':
                _verify(path, record)
                verified[str(path)] = record['sha256']
        result[key] = required_path
        provenance_records[name] = provenance

    recorder = provenance_records['native-recorder']
    _require((recorder.get('configuration') or {}).get('obs_version') == '32.1.0',
             'native_obs_abi_unsupported', str(recorder.get('configuration')))
    dependencies = {}
    for record in recorder.get('inputs', []):
        path = Path(record.get('path', '')).resolve()
        if path.is_relative_to(obs_dir) and path.suffix.lower() == '.dll':
            _require(path not in dependencies, 'native_dependency_duplicate', str(path))
            dependencies[path] = record
    for relative in ('bin/64bit/obs.dll', 'bin/64bit/libobs-d3d11.dll',
                     'obs-plugins/64bit/obs-ffmpeg.dll', 'obs-plugins/64bit/obs-x264.dll',
                     'obs-plugins/64bit/win-capture.dll'):
        _require(obs_dir / relative in dependencies,
                 'native_dependency_unlisted', str(obs_dir / relative))
    for path, record in dependencies.items():
        _verify(path, record)
        verified[str(path)] = record['sha256']
    _require(_hash(checkpoint_path) == checkpoint_sha,
             'native_checkpoint_changed', str(checkpoint_path))
    private_sha = verified[str(result['private_sdl'])]
    _require(private_sha != result['original_sdl_sha256'], 'native_sdl_identity', private_sha)
    result['known_private_sdl_sha256'] = sorted(set(result['known_private_sdl_sha256'] + [private_sha]))
    result.update(private_sdl_sha256=private_sha, source_commit=commit,
                  checkpoint_path=checkpoint_path, checkpoint_sha256=checkpoint_sha,
                  obs_dir=obs_dir, artifacts=verified)
    return result
