# -*- coding: utf-8 -*-
"""Python client for the native libobs recorder IPC (S3a protocol).

The native process owns libobs and the media pipeline.  This adapter only speaks JSON-lines over
a local named pipe; it never creates OBS sources itself and never opens an audio playback device.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class ObsBackendError(RuntimeError):
    def __init__(self, message: str, code: str | None = None, response: dict | None = None):
        super().__init__(message)
        self.code = code
        self.response = response or {}


class ObsRecorder:
    def __init__(self, exe: Path, pipe_name: str | None = None,
                 obs_dir: Path | None = None, log_path: Path | None = None):
        self.exe = Path(exe)
        self.pipe_name = pipe_name or f'auto-heros3-obs-{os.getpid()}-{uuid.uuid4().hex[:8]}'
        self.obs_dir = Path(obs_dir) if obs_dir else Path(r'C:\Program Files\obs-studio')
        self.log_path = Path(log_path) if log_path else None
        self.proc: subprocess.Popen | None = None
        self._handle = None
        self._reader: threading.Thread | None = None
        self._stop_reader = False
        self._responses: queue.Queue[dict] = queue.Queue()
        self._events: list[dict] = []
        self._lock = threading.Lock()

    @property
    def pipe_path(self) -> str:
        return '\\\\.\\pipe\\' + self.pipe_name

    def connect(self, timeout: float = 20.0) -> dict:
        """Launch the native server and connect to its named pipe."""
        if self.proc is not None:
            raise ObsBackendError('recorder already started')
        log = self.log_path.open('w', encoding='utf-8') if self.log_path else subprocess.DEVNULL
        cmd = [str(self.exe), '--serve', '--pipe', self.pipe_name,
               '--obs-dir', str(self.obs_dir)]
        self.proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                import win32file
                self._handle = win32file.CreateFile(
                    self.pipe_path, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0, None, win32file.OPEN_EXISTING, 0, None)
                break
            except Exception:
                if self.proc.poll() is not None:
                    raise ObsBackendError('native recorder exited before IPC connect')
                time.sleep(0.05)
        if self._handle is None:
            self.close(force=True)
            raise ObsBackendError('timed out connecting to native recorder IPC')
        return self.request('hello', timeout=10.0)

    def _read_response(self, request_id: str) -> dict:
        import win32file
        buf = getattr(self, '_buf', b'')
        while True:
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode('utf-8'))
                except Exception:
                    continue
                if obj.get('event'):
                    with self._lock:
                        self._events.append(obj)
                    continue
                if obj.get('request_id') in (request_id, None):
                    self._buf = buf
                    return obj
            _rc, chunk = win32file.ReadFile(self._handle, 65536)
            if not chunk:
                raise ObsBackendError('native recorder closed IPC')
            buf += chunk

    def request(self, command: str, timeout: float = 30.0, **args: Any) -> dict:
        if self._handle is None:
            raise ObsBackendError('recorder IPC not connected')
        req = {'cmd': command, 'request_id': f'{command}-{uuid.uuid4().hex[:8]}', **args}
        import win32file
        try:
            win32file.WriteFile(self._handle, (json.dumps(req, ensure_ascii=False) + '\n').encode('utf-8'))
        except Exception as exc:
            raise ObsBackendError(f'IPC write failed: {exc}') from exc
        return self._read_response(req['request_id'])


    def prepare(self, out_dir: Path, *, window_pid: int | None = None,
                window_title: str | None = None, window_class: str | None = None,
                window_exe: str | None = None, audio_raw: Path | None = None,
                max_time_sec: int = 900, duration_ms: int = 0) -> dict:
        args: dict[str, Any] = {'out_dir': str(out_dir), 'max_time_sec': int(max_time_sec),
                                'duration_ms': int(duration_ms)}
        if window_pid:
            args['window_pid'] = int(window_pid)
        if window_title:
            args['window_title'] = window_title
        if window_class:
            args['window_class'] = window_class
        if window_exe:
            args['window_exe'] = window_exe
        if audio_raw:
            args['audio_raw'] = str(audio_raw)
        return self.request('prepare', timeout=30.0, **args)

    def start_recording(self, timeout: float = 30.0) -> dict:
        return self.request('start', timeout=timeout)

    def status(self, timeout: float = 10.0) -> dict:
        return self.request('status', timeout=timeout)

    def stop(self, timeout: float = 120.0) -> dict:
        return self.request('stop', timeout=timeout)

    def shutdown(self, timeout: float = 30.0) -> dict:
        try:
            return self.request('shutdown', timeout=timeout)
        except ObsBackendError:
            return {}

    def close(self, force: bool = False) -> None:
        if self._handle is not None:
            try:
                self.shutdown()
            except Exception:
                pass
            try:
                import win32file
                win32file.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None
        self._stop_reader = True
        if self.proc is not None:
            try:
                self.proc.wait(timeout=15)
            except Exception:
                if force or self.proc.poll() is None:
                    try:
                        self.proc.terminate()
                    except Exception:
                        pass
            self.proc = None

    @property
    def events(self) -> list[dict]:
        with self._lock:
            return list(self._events)
