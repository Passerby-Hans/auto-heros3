# -*- coding: utf-8 -*-
"""S4a recording run/segment manifest and journal primitives.

This module is deliberately independent of the current recorder.  It defines the durable state
model that the native/legacy recording backends will share: one recording run, many segments, an
append-only journal (facts) and an atomically replaced manifest (snapshot).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
RUN_STATES = {
    'preparing': {'recording', 'failed'},
    'recording': {'stopping', 'failed'},
    'stopping': {'finalizing', 'failed'},
    'finalizing': {'complete', 'partial', 'failed'},
    'complete': set(),
    'partial': set(),
    'failed': set(),
}
SEGMENT_STATES = {
    'open': {'sealed', 'failed'},
    'sealed': {'validating', 'failed'},
    'validating': {'validated', 'partial', 'failed'},
    'validated': {'complete', 'failed'},
    'complete': set(),
    'partial': set(),
    'failed': set(),
}


class ManifestError(ValueError):
    pass


def _now() -> float:
    return time.time()


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    payload = json.dumps(data, ensure_ascii=False, indent=1).encode('utf-8')
    with tmp.open('wb') as f:
        f.write(payload)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def _append_journal(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(event, ensure_ascii=False) + '\n')
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass


def read_journal(path: Path) -> list[dict]:
    """Read complete JSONL events; a truncated final line is ignored, corrupt interior fails."""
    out: list[dict] = []
    if not path.exists():
        return out
    for lineno, line in enumerate(path.read_text(encoding='utf-8', errors='replace').splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            if lineno == len(path.read_text(encoding='utf-8', errors='replace').splitlines()):
                break  # crash during the final append; prior events remain facts
            raise ManifestError(f'corrupt journal line {lineno}: {path}')
        if not isinstance(obj, dict):
            raise ManifestError(f'journal line {lineno} is not an object')
        out.append(obj)
    return out


class RecordingManifest:
    def __init__(self, run_dir: Path, data: dict):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / 'manifest.json'
        self.journal_path = self.run_dir / 'journal.jsonl'
        self.data = data
        self.events = read_journal(self.journal_path)
        self._keys = {e.get('event_key') for e in self.events if e.get('event_key')}

    # -- constructors -----------------------------------------------------------------
    @classmethod
    def create(cls, run_dir: Path, *, recording_run_id: str, owner_token: str,
               backend: str = 'legacy', game_identity: dict | None = None,
               clock_origin_ns: int | None = None, metadata: dict | None = None) -> 'RecordingManifest':
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        data = {
            'schema_version': SCHEMA_VERSION,
            'kind': 'recording_run',
            'recording_run_id': recording_run_id,
            'owner_token': owner_token,
            'backend': backend,
            'state': 'preparing',
            'created_at': _now(),
            'updated_at': _now(),
            'game_identity': game_identity or {},
            'clock_origin_ns': clock_origin_ns,
            'metadata': metadata or {},
            'segments': [],
        }
        obj = cls(run_dir, data)
        obj.save()
        obj.append_event('run_created', event_key=f'run_created:{recording_run_id}')
        return obj

    @classmethod
    def load(cls, run_dir: Path) -> 'RecordingManifest':
        path = Path(run_dir) / 'manifest.json'
        if not path.exists():
            raise ManifestError(f'manifest missing: {path}')
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(f'manifest unreadable: {exc}') from exc
        if data.get('schema_version') != SCHEMA_VERSION:
            raise ManifestError(f'unsupported manifest schema: {data.get("schema_version")}')
        return cls(Path(run_dir), data)

    # -- persistence -------------------------------------------------------------------
    def save(self) -> None:
        self.data['updated_at'] = _now()
        _atomic_json(self.path, self.data)

    def assert_owner(self, token: str) -> None:
        if token != self.data.get('owner_token'):
            raise ManifestError('owner token mismatch')

    def append_event(self, event: str, *, segment_id: str | None = None,
                     event_key: str | None = None, **fields: Any) -> bool:
        if event_key and event_key in self._keys:
            return False
        seq = int(self.data.get('next_event_seq') or len(self.events) + 1)
        entry = {
            'seq': seq,
            'ts': _now(),
            'event': event,
            'recording_run_id': self.data.get('recording_run_id'),
            'segment_id': segment_id,
            'event_key': event_key,
            **fields,
        }
        _append_journal(self.journal_path, entry)
        self.events.append(entry)
        self.data['next_event_seq'] = seq + 1
        if event_key:
            self._keys.add(event_key)
        self.save()
        return True

    # -- state transitions -------------------------------------------------------------
    def transition_run(self, new_state: str, *, reason: str | None = None) -> None:
        old = self.data.get('state')
        if new_state not in RUN_STATES.get(old, set()):
            raise ManifestError(f'illegal run transition {old} -> {new_state}')
        self.data['state'] = new_state
        self.data[f'{new_state}_at'] = _now()
        self.append_event('run_state', event_key=f'run_state:{new_state}',
                          from_state=old, to_state=new_state, reason=reason)
        self.save()

    def _find_segment(self, segment_id: str) -> dict:
        for seg in self.data.get('segments') or []:
            if seg.get('segment_id') == segment_id:
                return seg
        raise ManifestError(f'unknown segment: {segment_id}')

    def add_segment(self, segment_id: str, *, index: int, staging_path: str | None = None,
                    request_id: str | None = None) -> dict:
        if any(s.get('segment_id') == segment_id for s in self.data.get('segments') or []):
            return self._find_segment(segment_id)
        seg = {
            'segment_id': segment_id, 'index': int(index), 'state': 'open',
            'created_at': _now(), 'staging_path': staging_path,
            'request_id': request_id, 'bytes': None, 'sha256': None,
            'audio': {}, 'continuity': {}, 'validation': {},
        }
        self.data.setdefault('segments', []).append(seg)
        self.append_event('segment_created', segment_id=segment_id,
                          event_key=f'segment_created:{segment_id}', index=int(index))
        self.save()
        return seg

    def transition_segment(self, segment_id: str, new_state: str, *,
                           reason: str | None = None, **fields: Any) -> dict:
        seg = self._find_segment(segment_id)
        old = seg.get('state')
        if new_state not in SEGMENT_STATES.get(old, set()):
            raise ManifestError(f'illegal segment transition {old} -> {new_state}')
        seg['state'] = new_state
        seg[f'{new_state}_at'] = _now()
        seg.update(fields)
        self.append_event('segment_state', segment_id=segment_id,
                          event_key=f'segment_state:{segment_id}:{new_state}',
                          from_state=old, to_state=new_state, reason=reason)
        self.save()
        return seg

    def seal_segment(self, segment_id: str, *, path: str | None = None,
                     bytes: int | None = None, sha256: str | None = None) -> dict:
        seg = self.transition_segment(segment_id, 'sealed')
        if path is not None:
            seg['path'] = path
        if bytes is not None:
            seg['bytes'] = int(bytes)
        if sha256 is not None:
            seg['sha256'] = sha256
        self.save()
        return seg

    def summary(self) -> dict:
        return {
            'schema_version': self.data.get('schema_version'),
            'recording_run_id': self.data.get('recording_run_id'),
            'owner_token': self.data.get('owner_token'),
            'backend': self.data.get('backend'),
            'state': self.data.get('state'),
            'segments': [
                {k: s.get(k) for k in ('segment_id', 'index', 'state', 'path', 'bytes',
                                       'sha256', 'staging_path')}
                for s in self.data.get('segments') or []
            ],
        }
