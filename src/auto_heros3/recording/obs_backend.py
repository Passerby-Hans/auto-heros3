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


def _process_in_job(pid: int) -> bool:
    """True when pid is inside a Windows job (a kill-on-close job would kill a child)."""
    try:
        import ctypes
        import ctypes.wintypes as wt
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            inside = ctypes.c_int()
            ok = k.IsProcessInJob(h, None, ctypes.byref(inside))
            return bool(ok and inside.value)
        finally:
            k.CloseHandle(h)
    except Exception:
        return False


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
        self.native_pid: int | None = None
        self.wmi_launched = False
        self._wmi_wrapper_pid: int | None = None
        self._handle = None
        self._reader: threading.Thread | None = None
        self._stop_reader = False
        self._responses: queue.Queue[dict] = queue.Queue()
        self._events: list[dict] = []
        self._lock = threading.Lock()

    @property
    def pipe_path(self) -> str:
        return '\\\\.\\pipe\\' + self.pipe_name

    @property
    def pid(self) -> int | None:
        return self.native_pid or (self.proc.pid if self.proc is not None else None)

    def alive(self) -> bool:
        if self.proc is not None:
            return self.proc.poll() is None
        if self.native_pid:
            try:
                import psutil
                return psutil.pid_exists(int(self.native_pid))
            except Exception:
                return False
        return False

    def _spawn_wmi(self, cmd: list[str]) -> None:
        """Launch outside the controller's kill-on-close job via WMI."""
        import win32com.client
        log_path = self.log_path or Path(os.environ.get('TEMP', '.')) / f'{self.pipe_name}.log'
        wmi_cmd = list(cmd) + ['--log', str(log_path)]
        command_line = subprocess.list2cmdline(wmi_cmd)
        loc = win32com.client.Dispatch('WbemScripting.SWbemLocator')
        svc = loc.ConnectServer('.', r'root\cimv2')
        obj = svc.Get('Win32_Process')
        params = obj.Methods_('Create').InParameters.SpawnInstance_()
        params.CommandLine = command_line
        out = svc.ExecMethod('Win32_Process', 'Create', params)
        if int(out.ReturnValue) != 0:
            raise ObsBackendError(f'WMI Win32_Process.Create failed rc={out.ReturnValue}')
        self.proc = None
        self.wmi_launched = True
        self._wmi_wrapper_pid = int(out.ProcessId)
        self.native_pid = int(out.ProcessId)

    def connect(self, timeout: float = 20.0) -> dict:
        """Launch the native server and connect to its named pipe."""
        if self.proc is not None or self.native_pid is not None:
            raise ObsBackendError('recorder already started')
        log = self.log_path.open('w', encoding='utf-8') if self.log_path else subprocess.DEVNULL
        cmd = [str(self.exe), '--serve', '--pipe', self.pipe_name,
               '--obs-dir', str(self.obs_dir)]
        base_flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        breakaway = getattr(subprocess, 'CREATE_BREAKAWAY_FROM_JOB', 0)
        # The MCP host may run inside a kill-on-close job (shell/service supervision).
        # A recorder that inherits that job is killed before it can finalize its MP4s
        # when the controller dies.  Ask to break away; if the job forbids it, launch
        # through WMI (WmiPrvSE is outside this process's job).
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=log, stderr=subprocess.STDOUT,
                creationflags=base_flags | breakaway)
        except OSError:
            self.proc = subprocess.Popen(
                cmd, stdout=log, stderr=subprocess.STDOUT, creationflags=base_flags)
        if self.proc is not None and (_process_in_job(self.proc.pid) or
                                      os.environ.get('AUTO_HEROS3_OBS_FORCE_WMI') == '1'):
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                pass
            self.proc = None
            self._spawn_wmi(cmd)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                import win32file
                self._handle = win32file.CreateFile(
                    self.pipe_path, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0, None, win32file.OPEN_EXISTING, 0, None)
                break
            except Exception:
                if self.proc is not None and self.proc.poll() is not None:
                    raise ObsBackendError('native recorder exited before IPC connect')
                if self.proc is None and self.native_pid and not self.alive():
                    raise ObsBackendError('native recorder exited before IPC connect')
                time.sleep(0.05)
        if self._handle is None:
            self.close(force=True)
            raise ObsBackendError('timed out connecting to native recorder IPC')
        hello = self.request('hello', timeout=10.0)
        try:
            self.native_pid = int(hello.get('pid') or self.native_pid or 0) or self.native_pid
        except Exception:
            pass
        return hello

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
                max_time_sec: int = 900, max_size_mb: int = 0,
                duration_ms: int = 0) -> dict:
        args: dict[str, Any] = {'out_dir': str(out_dir), 'max_time_sec': int(max_time_sec),
                                'max_size_mb': int(max_size_mb),
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
        elif self.native_pid:
            deadline = time.time() + 15
            while time.time() < deadline and self.alive():
                time.sleep(0.1)
            if force and self.alive():
                try:
                    import psutil
                    psutil.Process(int(self.native_pid)).terminate()
                except Exception:
                    pass
        self.native_pid = None

    @property
    def events(self) -> list[dict]:
        with self._lock:
            return list(self._events)
