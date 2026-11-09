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
from typing import Any, Callable


def _controller_process_identity() -> tuple[int, int]:
    """Exact Win32 creation FILETIME; floats cannot carry this identity losslessly."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    if not kernel32.GetProcessTimes(kernel32.GetCurrentProcess(), ctypes.byref(created),
                                   ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
        raise ctypes.WinError(ctypes.get_last_error())
    value = (created.dwHighDateTime << 32) | created.dwLowDateTime
    if value <= 0:
        raise RuntimeError('controller creation FILETIME unavailable')
    return os.getpid(), value


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
                 obs_dir: Path | None = None, log_path: Path | None = None,
                 *, outputs_module: Path | None = None,
                 on_spawn: Callable[['ObsRecorder'], None] | None = None):
        self.exe = Path(exe)
        self.outputs_module = Path(outputs_module) if outputs_module else None
        self.pipe_name = pipe_name or f'auto-heros3-obs-{os.getpid()}-{uuid.uuid4().hex[:8]}'
        self.obs_dir = Path(obs_dir) if obs_dir else Path(r'C:\Program Files\obs-studio')
        self.log_path = Path(log_path) if log_path else None
        self.proc: subprocess.Popen | None = None
        self.native_pid: int | None = None
        self.native_identity: dict[str, Any] | None = None
        self.wmi_launched = False
        self._wmi_wrapper_pid: int | None = None
        self._handle = None
        self._reader: threading.Thread | None = None
        self._stop_reader = False
        self._responses: queue.Queue[dict] = queue.Queue()
        self._events: list[dict] = []
        self._lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._shutdown_response: dict | None = None
        self.shutdown_ack_sent = False
        self.last_error: str | None = None
        self._on_spawn = on_spawn

    def _notify_spawn(self) -> None:
        if self._on_spawn is not None:
            self._on_spawn(self)

    @property
    def pipe_path(self) -> str:
        return '\\\\.\\pipe\\' + self.pipe_name

    @property
    def pid(self) -> int | None:
        return self.native_pid or (self.proc.pid if self.proc is not None else None)

    def alive(self) -> bool:
        if self.proc is not None:
            try:
                return self.proc.poll() is None
            except Exception:
                return True  # unconfirmed exit must retain the admission owner
        if self.native_pid:
            try:
                import psutil
            except ImportError:
                return True
            try:
                process = psutil.Process(int(self.native_pid))
                if self.native_identity is not None and not self._matches_native_identity(process):
                    return False  # PID reused: the recorded process has exited
                return process.is_running()
            except psutil.NoSuchProcess:
                return False
            except Exception:
                return True
        return False

    def _matches_native_identity(self, process) -> bool:
        import psutil
        identity = self.native_identity
        try:
            return bool(identity and int(process.pid) == identity['pid'] and
                        process.create_time() == identity['create_time'] and
                        Path(process.exe()).resolve() == Path(identity['exe']))
        except psutil.NoSuchProcess:
            return False

    def _remember_native_identity(self, pid: int) -> None:
        import psutil
        process = psutil.Process(pid)
        exe = Path(process.exe()).resolve()
        created = process.create_time()
        if exe != self.exe.resolve() or created <= 0:
            raise ObsBackendError('WMI recorder process identity mismatch', code='native_owner_mismatch')
        self.native_identity = {'pid': pid, 'create_time': created, 'exe': str(exe)}

    def _spawn_wmi(self, cmd: list[str]) -> None:
        """Launch outside the controller's job, initializing COM on this worker."""
        import pythoncom
        import win32com.client
        log_path = self.log_path or Path(os.environ.get('TEMP', '.')) / f'{self.pipe_name}.log'
        wmi_cmd = list(cmd) + ['--log', str(log_path)]
        command_line = subprocess.list2cmdline(wmi_cmd)
        # MCP and watchdog calls may run on different pool threads. Importing
        # win32com initializes only its importing thread, not each later caller.
        # CoInitializeEx also distinguishes an already-initialized MTA: WMI can
        # use that apartment, but we must not uninitialize the caller's COM ref.
        com_owned = False
        try:
            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
            com_owned = True
        except pythoncom.com_error as exc:
            if (exc.hresult & 0xffffffff) != 0x80010106:  # RPC_E_CHANGED_MODE
                raise
        loc = svc = obj = params = out = None
        try:
            loc = win32com.client.Dispatch('WbemScripting.SWbemLocator')
            svc = loc.ConnectServer('.', r'root\cimv2')
            obj = svc.Get('Win32_Process')
            params = obj.Methods_('Create').InParameters.SpawnInstance_()
            params.CommandLine = command_line
            out = svc.ExecMethod('Win32_Process', 'Create', params)
            if int(out.ReturnValue) != 0:
                raise ObsBackendError(f'WMI Win32_Process.Create failed rc={out.ReturnValue}')
            native_pid = int(out.ProcessId)
            if native_pid <= 0:
                raise ObsBackendError('WMI returned no recorder PID', code='native_owner_missing')
            # Publish ownership before further fallible identity/COM cleanup.
            # A failed connect must leave the parent able to retain this owner.
            self.proc = None
            self.wmi_launched = True
            self._wmi_wrapper_pid = native_pid
            self.native_pid = native_pid
            self._notify_spawn()
            self._remember_native_identity(native_pid)
            self._notify_spawn()
        finally:
            # Release apartment-bound dispatch objects before balancing COM.
            out = params = obj = svc = loc = None
            if com_owned:
                pythoncom.CoUninitialize()

    def connect(self, timeout: float = 20.0) -> dict:
        """Launch the native server and connect to its named pipe."""
        if self.proc is not None or self.native_pid is not None:
            raise ObsBackendError('recorder already started')
        owner_pid, owner_created = _controller_process_identity()
        log = self.log_path.open('w', encoding='utf-8') if self.log_path else subprocess.DEVNULL
        cmd = [str(self.exe), '--serve', '--pipe', self.pipe_name,
               '--obs-dir', str(self.obs_dir), '--owner-pid', str(owner_pid),
               '--owner-created-filetime', str(owner_created)]
        if self.outputs_module:
            cmd += ['--outputs-module', str(self.outputs_module)]
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
        self._notify_spawn()
        if self.proc is not None and (_process_in_job(self.proc.pid) or
                                      os.environ.get('AUTO_HEROS3_OBS_FORCE_WMI') == '1'):
            # Never lose a still-running first child or launch a second native
            # process when breaking out of the inherited job failed.
            self._close_process(force=True)
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
        reported_pid = int(hello.get('pid') or self.pid or 0)
        if self.pid and reported_pid != self.pid:
            raise ObsBackendError('native hello PID differs from launched owner', code='native_owner_mismatch')
        self.native_pid = reported_pid or self.native_pid
        return hello

    def _abort_ipc(self, reason: str) -> None:
        """Close the pipe so a blocked ReadFile cannot outlive its request deadline."""
        handle = self._handle
        self._handle = None
        self._buf = b''
        if handle is not None:
            try:
                import win32file
                win32file.CloseHandle(handle)
            except Exception:
                pass
        self.last_error = reason

    def _read_response(self, request_id: str, timeout: float = 30.0) -> dict:
        """Read one response with a real wall-clock deadline.

        Named-pipe ReadFile is synchronous and has no timeout.  Peek first, then read
        only when data is available; on deadline close the pipe (the caller gets a
        deterministic error instead of hanging stop/cleanup forever).
        """
        import win32file
        import win32pipe
        deadline = time.monotonic() + max(0.05, float(timeout))
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
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._abort_ipc(f'IPC timeout waiting for {request_id}')
                raise ObsBackendError(f'IPC timeout waiting for {request_id}')
            if self._handle is None:
                raise ObsBackendError('recorder IPC not connected')
            try:
                _data, avail, _left = win32pipe.PeekNamedPipe(self._handle, 0)
            except Exception as exc:
                self._abort_ipc(f'native recorder closed IPC: {exc}')
                raise ObsBackendError(f'native recorder closed IPC: {exc}') from exc
            if not avail:
                time.sleep(min(0.01, remaining))
                continue
            try:
                _rc, chunk = win32file.ReadFile(self._handle, 65536)
            except Exception as exc:
                self._abort_ipc(f'IPC read failed: {exc}')
                raise ObsBackendError(f'IPC read failed: {exc}') from exc
            if not chunk:
                self._abort_ipc('native recorder closed IPC')
                raise ObsBackendError('native recorder closed IPC')
            buf += chunk

    def request(self, command: str, timeout: float = 30.0, **args: Any) -> dict:
        with self._request_lock:
            if self._handle is None:
                raise ObsBackendError('recorder IPC not connected')
            req = {'cmd': command, 'request_id': f'{command}-{uuid.uuid4().hex[:8]}', **args}
            import win32file
            try:
                win32file.WriteFile(self._handle,
                                    (json.dumps(req, ensure_ascii=False) + '\n').encode('utf-8'))
            except Exception as exc:
                self._abort_ipc(f'IPC write failed: {exc}')
                raise ObsBackendError(f'IPC write failed: {exc}') from exc
            return self._read_response(req['request_id'], timeout=timeout)


    def prepare(self, out_dir: Path, *, window_pid: int | None = None,
                window_title: str | None = None, window_class: str | None = None,
                window_exe: str | None = None, audio_raw: Path | None = None,
                audio_framed: Path | None = None,
                audio_framed_live: bool = False,
                max_time_sec: int = 900, max_size_mb: int = 0,
                overlay_path: Path | None = None,
                duration_ms: int = 0) -> dict:
        args: dict[str, Any] = {'out_dir': str(out_dir), 'max_time_sec': int(max_time_sec),
                                'max_size_mb': int(max_size_mb),
                                'duration_ms': int(duration_ms)}
        if overlay_path:
            args['overlay_path'] = str(overlay_path)
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
        if audio_framed:
            args['audio_framed'] = str(audio_framed)
            args['audio_framed_live'] = bool(audio_framed_live)
        return self.request('prepare', timeout=30.0, **args)

    def start_recording(self, timeout: float = 30.0) -> dict:
        return self.request('start', timeout=timeout)

    def status(self, timeout: float = 10.0) -> dict:
        return self.request('status', timeout=timeout)

    def stop(self, timeout: float = 120.0) -> dict:
        return self.request('stop', timeout=timeout)

    def shutdown(self, timeout: float = 30.0) -> dict:
        with self._shutdown_lock:
            if self._shutdown_response is not None:
                return dict(self._shutdown_response)
            try:
                response = self.request('shutdown', timeout=timeout)
            except ObsBackendError:
                return {}
            if response.get('ok') is True:
                # Receipt is sent only after parsing the complete response.
                # Cache first: a broken ACK write must not repeat shutdown.
                self._shutdown_response = dict(response)
                seq = response.get('seq')
                if isinstance(seq, int) and not isinstance(seq, bool) and seq > 0:
                    ack = {'cmd': 'shutdown_ack', 'shutdown_seq': seq}
                    with self._request_lock:
                        try:
                            import win32file
                            if self._handle is None:
                                raise ObsBackendError('recorder IPC not connected for shutdown receipt')
                            win32file.WriteFile(self._handle,
                                (json.dumps(ack) + '\n').encode('utf-8'))
                            self.shutdown_ack_sent = True
                        except Exception as exc:
                            # The native host has its own <=1s receipt deadline.
                            # A received response remains known even if it exits
                            # before receiving this final, one-way receipt.
                            self.last_error = f'shutdown receipt write failed: {exc}'
            return response

    def _close_process(self, force: bool) -> None:
        """Clear an owner only after exit; retain it on uncertain cleanup."""
        if self.proc is not None:
            process = self.proc  # Popen's Windows handle is bound to this child.
            if process.poll() is None:
                if not force:
                    try:
                        process.wait(timeout=15)
                    except Exception:
                        pass
                if process.poll() is None:
                    try:
                        process.terminate()
                    except Exception:
                        pass
                    try:
                        process.wait(timeout=3)
                    except Exception:
                        pass
                if process.poll() is None:
                    try:
                        process.kill()
                    except Exception:
                        pass
                    try:
                        process.wait(timeout=3)
                    except Exception:
                        pass
            if process.poll() is None:
                raise ObsBackendError(f'native cleanup could not confirm exit pid={process.pid}',
                                      code='native_cleanup_unconfirmed')
        elif self.native_pid:
            import psutil
            try:
                process = psutil.Process(int(self.native_pid))
            except psutil.NoSuchProcess:
                process = None
            if process is not None:
                if self.native_identity is None:
                    raise ObsBackendError(f'native cleanup has no verified identity pid={self.native_pid}',
                                          code='native_cleanup_unconfirmed')
                if self._matches_native_identity(process):
                    if not force:
                        try:
                            process.wait(timeout=15)
                        except psutil.TimeoutExpired:
                            pass
                    for operation in ('terminate', 'kill'):
                        if not self.alive():
                            break
                        # Recheck PID/create-time/executable immediately before
                        # each mutation. Access failures preserve the owner.
                        if not self._matches_native_identity(process):
                            break
                        try:
                            getattr(process, operation)()
                            process.wait(timeout=3)
                        except psutil.NoSuchProcess:
                            break
                        except (psutil.AccessDenied, psutil.TimeoutExpired):
                            pass
                    if self.alive():
                        raise ObsBackendError(f'native cleanup could not confirm exit pid={self.native_pid}',
                                              code='native_cleanup_unconfirmed')
                # An identity mismatch proves that the *old* owner exited.
                # Do not signal the unrelated replacement process.
        self.proc = None
        self.native_pid = None
        self.native_identity = None
        self._wmi_wrapper_pid = None

    def close(self, force: bool = False) -> None:
        """Close IPC; cleanup failure retains process ownership for retry."""
        if self._handle is not None and not force:
            try:
                self.shutdown()
            except Exception:
                pass
        if self._handle is not None:
            try:
                import win32file
                win32file.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None
        self._stop_reader = True
        try:
            self._close_process(force)
        except ObsBackendError:
            raise
        except Exception as exc:
            raise ObsBackendError(f'native cleanup could not confirm exit pid={self.pid}: {exc}',
                                  code='native_cleanup_unconfirmed') from exc

    @property
    def events(self) -> list[dict]:
        with self._lock:
            return list(self._events)
