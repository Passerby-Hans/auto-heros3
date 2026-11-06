# -*- coding: utf-8 -*-
"""Drive the patched VCMI client through the agent bridge (protocol v2).

Trust rules implemented here (D-007, first minimal set):
  * session identity: every bridge instance publishes a session_id in state.json and the
    Python side persists it in <dir>/session.json together with the owned client PID, the
    process creation time and the exe path;
  * freshness: state() rejects snapshots of another session, snapshots older than
    `max_state_age` seconds, and snapshots whose owned process is gone;
  * command replies: the bridge prefixes every result produced while executing a command
    with `seq=<n>`; submit() returns that sequence number and wait_seq() only accepts the
    line carrying the same number (no more "next OK in the file" races);
  * completion: execute() treats the immediate OK as *submitted* and requires the caller's
    post-condition (state or event) before reporting COMPLETED; otherwise UNKNOWN.
  * ownership: stop() only kills processes recorded as owned by this session, verifies they
    exited, and refuses to mass-kill by image name.

Usage:
    python -X utf8 tools/vcmi_agent.py --map Maps/Arrogance
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psutil

# B0.4: this module lives in src/auto_heros3/runtime/ now; resolve the repository root by
# marker instead of a fragile parents[N] hop, and keep the legacy bare-module imports
# (vcmi_shot / agent.h3.audio) working through sys.path exactly as before.
_HERE = Path(__file__).resolve()
for _candidate in (_HERE, *_HERE.parents):
    if ((_candidate / 'tools' / 'vcmi_agent.py').is_file()
            and (_candidate / 'platform' / 'vcmi-patch' / 'apply.py').is_file()):
        _REPO = _candidate
        break
else:  # pragma: no cover - a broken checkout must fail loudly, never guess a directory
    raise RuntimeError(f'cannot locate the auto-heros3 repository root above {_HERE}')
for _path in (str(_REPO), str(_REPO / 'tools'), str(_REPO / 'src')):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from auto_heros3.paths import repo_root  # noqa: E402

ROOT = repo_root(_HERE)
if ROOT != _REPO:  # pragma: no cover - a layout/resolver disagreement must fail loudly
    raise RuntimeError(f'repository root disagreement: bootstrap={_REPO} paths={ROOT}')
EXE = ROOT / 'vendor' / 'vcmi-src' / 'build' / 'bin' / 'VCMI_client.exe'
WORKDIR = ROOT / 'vendor' / 'vcmi-src' / 'build' / 'bin'
CONANRUN = ROOT / 'vendor' / 'vcmi-src' / 'conan-msvc' / 'conanrun.ps1'
DEFAULT_BRIDGE = ROOT / 'var' / 'agent'
LOGDIR = ROOT / 'var' / 'logs' / 'vcmi'
# VCMI keeps its launcher settings in the user's Documents tree (not the repo).  A targeted
# display run patches video.displayIndex/fullscreen/resolution before launch and restores the
# original bytes on stop; see _patch_video_settings/_restore_video_settings.
VCMI_SETTINGS = Path.home() / 'Documents' / 'My Games' / 'vcmi' / 'config' / 'settings.json'
SESSION_FILE = 'session.json'
EPOCH_FILE = 'state_epoch.json'
# Map names are embedded in a PowerShell -Command string; allow only inert characters.
MAP_NAME_RE = re.compile(r'^[A-Za-z0-9 _\-./\\:]+$')
# The client is pinned: this file records the binary hash + frozen bridge behaviour, so an MCP
# change is validated against a known engine instead of silently assuming a new one.
CONTRACT_FILE = ROOT / 'tools' / 'bridge_contract.json'


def load_bridge_contract() -> dict:
    try:
        data = json.loads(CONTRACT_FILE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def sha256_file(path: Path) -> str | None:
    import hashlib
    try:
        digest = hashlib.sha256()
        with open(path, 'rb') as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b''):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _git_out(*args: str) -> str | None:
    """Best-effort git query; None when git/the repo is unavailable (offline tests)."""
    try:
        proc = subprocess.run(['git', *args], cwd=str(ROOT), capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()

CREATE_NO_WINDOW = 0x08000000
PROTOCOL_VERSION = 2


class BridgeError(RuntimeError):
    """Generic bridge/session problem."""


class SubmitUnknown(OSError):
    """The command file was opened but writing/closing failed: submission is indeterminate.

    A failure BEFORE the file is opened (lock/read/count errors, open failure) is a known
    not-sent error and is reported as ordinary OSError -> CommandResult(NOT_SENT, sent=False).
    A failure after open is represented by this subclass -> CommandResult(UNKNOWN, sent=None):
    bytes may already have reached commands.txt, so consumers must NOT treat it as unsent.
    """


class StaleStateError(BridgeError):
    """The state.json snapshot is not from this session, is too old, or the client is dead."""


@dataclass
class Session:
    session_id: str = ''
    dir: str = ''
    map_name: str | None = None
    client_pid: int | None = None
    client_create_time: float | None = None
    launcher_pid: int | None = None
    exe: str = str(EXE)
    created_at: float = 0.0
    protocol: int = PROTOCOL_VERSION
    notes: list = field(default_factory=list)

    def alive(self) -> bool:
        if not self.client_pid:
            return False
        try:
            p = psutil.Process(self.client_pid)
        except psutil.Error:
            return False
        if not p.is_running():
            return False
        # identity check: same process, not a recycled PID
        if self.client_create_time is not None:
            try:
                if abs(p.create_time() - self.client_create_time) > 1.0:
                    return False
            except psutil.Error:
                return False
        try:
            if Path(p.exe()) != Path(self.exe):
                return False
        except psutil.Error:
            pass
        return True

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=1)

    @classmethod
    def from_json(cls, txt: str) -> 'Session':
        return cls(**json.loads(txt))


@dataclass
class CommandResult:
    seq: int
    status: str            # COMPLETED | REJECTED | UNKNOWN | NOT_SENT
    line: str = ''
    detail: str = ''
    # N1: explicit submission classification.  None = submission may have happened but the
    # outcome is unknown; False = known not sent; True = the command was submitted.
    sent: bool | None = None

    @property
    def ok(self) -> bool:
        return self.status == 'COMPLETED'

    def __str__(self) -> str:
        return f'#{self.seq} {self.status} {self.line or "-"} {self.detail}'.strip()


class VcmiAgent:
    def __init__(self, bridge_dir: Path | str = DEFAULT_BRIDGE, map_name: str | None = None,
                 offscreen: bool = True, log: bool = True, max_state_age: float = 3.0,
                 display_index: int | None = None):
        self.dir = Path(bridge_dir).resolve()
        self.map_name = map_name
        self.display_index = None if display_index is None else int(display_index)
        self.offscreen = bool(offscreen) and self.display_index is None
        self.log = log
        self.max_state_age = max_state_age
        self.proc = None
        self.hwnd: int | None = None
        self._settings_backup: tuple[Path, bytes] | None = None
        self._settings_backup_file = self.dir / 'vcmi-settings-backup.bin'
        self.audio_sink_path = self.dir / 'game-audio.raw'
        self.audio_ready_path = self.dir / 'audio-relay.ready'
        self.audio_relay_proc: subprocess.Popen | None = None
        self.audio_pipe_name: str | None = None
        # A2 spike hook.  Default remains the existing SDL disk sink.  The
        # private backend is selected explicitly for staged-DLL experiments.
        self.audio_driver = os.environ.get('AUTO_HEROS3_AUDIO_DRIVER', 'disk').strip()
        self.a3capture_file = Path(os.environ.get(
            'AUTO_HEROS3_A3CAPTURE_FILE', str(self.dir / 'a3capture.raw'))).resolve()
        self.a3capture_ready_path = Path(str(self.a3capture_file) + '.ready')
        self.session: Session | None = None
        self._pending_seq = 0
        self._seq_offset: dict[int, int] = {}
        self._stop_watch = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self.contract: dict = {}
        self.session_path = self.dir / SESSION_FILE

    # ------------------------------------------------------------------ helpers
    def _print(self, msg: str) -> None:
        if self.log:
            print(msg, flush=True)

    def _load_session(self) -> Session | None:
        try:
            return Session.from_json(self.session_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError, TypeError):
            return None

    def _save_session(self, sess: Session) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.session_path.write_text(sess.to_json(), encoding='utf-8')
        self.session = sess

    def _find_client_pid(self, launcher_pid: int | None) -> tuple[int | None, float | None]:
        """Resolve the VCMI_client.exe process owned by this launch (not by name matching)."""
        me = psutil.Process()
        best = None
        for p in psutil.process_iter(['pid', 'name', 'exe', 'ppid', 'create_time']):
            try:
                if (p.info['name'] or '').lower() != 'vcmi_client.exe':
                    continue
                if Path(p.info['exe'] or '') != Path(EXE):
                    continue
                # prefer a client whose ancestry includes our launcher or this python process
                if launcher_pid and p.info['ppid'] == launcher_pid:
                    best = p
                    break
                if p.info['ppid'] == me.pid:
                    best = p
            except psutil.Error:
                continue
        if best is None:
            return None, None
        return best.info['pid'], best.info['create_time']


    # ------------------------------------------------------------------ off-screen guard
    @staticmethod
    def _virtual_screen() -> tuple[int, int, int, int]:
        import ctypes
        u = ctypes.WinDLL('user32')
        u.SetProcessDPIAware()
        return (u.GetSystemMetrics(76), u.GetSystemMetrics(77),   # SM_X/YVIRTUALSCREEN
                u.GetSystemMetrics(78), u.GetSystemMetrics(79))   # SM_CX/CYVIRTUALSCREEN

    def _window_rect(self) -> tuple[int, int, int, int] | None:
        if not self.hwnd:
            return None
        import ctypes
        import ctypes.wintypes as wt
        u = ctypes.WinDLL('user32')
        r = wt.RECT()
        if not u.GetWindowRect(self.hwnd, ctypes.byref(r)):
            return None
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)

    def window_is_offscreen(self) -> bool:
        rect = self._window_rect()
        if rect is None:
            return False
        left, top, vw, vh = self._virtual_screen()
        x, y, w, h = rect
        return (x + w <= left) or (x >= left + vw) or (y + h <= top) or (y >= top + vh)

    def _place_offscreen(self) -> bool:
        """Move the game window completely outside the virtual desktop (never a focus steal)."""
        if not self.hwnd:
            return False
        import ctypes
        u = ctypes.WinDLL('user32')
        u.SetProcessDPIAware()
        left, top, vw, vh = self._virtual_screen()
        rect = self._window_rect()
        w, h = (rect[2], rect[3]) if rect else (1024, 768)
        # left of the left-most monitor is the safest spot; fall back to above / right
        candidates = [
            (left - w - 64, top),
            (left, top - h - 64),
            (left + vw + 64, top),
            (left - w - 64, top - h - 64),
        ]
        SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x4, 0x10
        for x, y in candidates:
            u.SetWindowPos(self.hwnd, 0, int(x), int(y), 0, 0,
                           SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            time.sleep(0.15)
            if self.window_is_offscreen():
                return True
        return False

    def _offscreen_watchdog(self) -> None:
        """Keep the window off-screen for the whole session (game may re-centre itself)."""
        while not self._stop_watch.is_set():
            try:
                if self.hwnd and not self.window_is_offscreen():
                    self._place_offscreen()
            except Exception:
                pass
            self._stop_watch.wait(2.0)

    # ------------------------------------------------------------------ lifecycle
    def contract_status(self) -> dict:
        """Compare the pinned bridge contract (tools/bridge_contract.json) with the binary.

        Besides the EXE hash this checks whether the *client source* still matches the commit
        the binary was built from: a stale `built_from_commit` means the frozen behaviour text
        and the actual code may have drifted even when the EXE hash matches.
        """
        client = load_bridge_contract().get('client') or {}
        if not isinstance(client, dict):
            client = {}
        expected = client.get('sha256')
        actual = sha256_file(EXE)
        pinned_protocol = client.get('protocol')
        built_from = client.get('built_from_commit')
        repo_head = _git_out('rev-parse', '--short', 'HEAD')
        source_matches_head: bool | None = None
        if built_from and repo_head:
            if built_from == repo_head:
                source_matches_head = True
            else:
                # Git diff is empty when the client build inputs did not change since that commit.
                diff = _git_out('diff', '--name-only', built_from, repo_head, '--',
                                'platform/vcmi-patch/client/AgentBridge.cpp',
                                'platform/vcmi-patch/client/AgentBridge.h',
                                'platform/vcmi-patch/apply.py')
                if diff is not None:
                    source_matches_head = (diff == '')
        return {
            'expected_sha256': expected,
            'actual_sha256': actual,
            'match': bool(expected and actual and expected == actual),
            'pinned_protocol': pinned_protocol,
            'protocol_ok': pinned_protocol in (None, PROTOCOL_VERSION),
            'built_at': client.get('built_at'),
            'built_from_commit': built_from,
            'repo_head': repo_head,
            'source_matches_head': source_matches_head,
        }

    # ------------------------------------------------------------------ audio relay
    def _start_audio_relay(self) -> None:
        """Create the SDL disk-audio pipe relay before the client starts.

        SDL opens its disk file with a deny-read share mode.  The relay receives the same PCM
        through a named pipe and appends it to a normal raw file; the recorder can read that file
        while the game is running.  No physical audio endpoint is touched.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.audio_driver != 'disk':
            # The private a3capture backend writes its own stream; no disk pipe
            # or relay exists in this experimental path.
            self.audio_pipe_name = None
            return
        try:
            self.audio_sink_path.unlink()
        except FileNotFoundError:
            pass
        try:
            self.audio_ready_path.unlink()
        except FileNotFoundError:
            pass
        self.audio_pipe_name = f'auto_heros3_audio_{os.getpid()}_{time.time_ns()}'
        cmd = [sys.executable, '-X', 'utf8',
               str(ROOT / 'src' / 'auto_heros3' / 'recording' / 'audio_relay.py'),
               '--pipe', self.audio_pipe_name, '--out', str(self.audio_sink_path),
               '--ready', str(self.audio_ready_path)]
        self.audio_relay_proc = subprocess.Popen(
            cmd, cwd=str(ROOT), creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 8.0
        while time.time() < deadline:
            if self.audio_ready_path.exists():
                self._print(f'[agent] audio relay ready pipe={self.audio_pipe_name}')
                return
            if self.audio_relay_proc.poll() is not None:
                break
            time.sleep(0.02)
        self._stop_audio_relay()
        raise BridgeError('audio relay did not become ready')

    def _stop_audio_relay(self) -> None:
        proc = self.audio_relay_proc
        self.audio_relay_proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            self.audio_ready_path.unlink()
        except OSError:
            pass

    def start(self, timeout: float = 120.0) -> bool:
        self.dir.mkdir(parents=True, exist_ok=True)
        # Host command safety: validate the map name *before* any process/file side effects.
        if self.map_name and not MAP_NAME_RE.match(self.map_name):
            raise BridgeError(f'illegal map name {self.map_name!r}')
        # Pinned-client check: hash mismatch is a loud warning (debug builds change), while a
        # protocol mismatch is a hard error because the whole command/state contract differs.
        self.contract = self.contract_status()
        if not self.contract.get('protocol_ok'):
            raise BridgeError(
                f"pinned bridge protocol {self.contract.get('pinned_protocol')!r} != "
                f"{PROTOCOL_VERSION}; update tools/bridge_contract.json deliberately")
        if not self.contract.get('match'):
            self._print(f"[agent] WARNING: VCMI_client.exe differs from the pinned client "
                        f"({str(self.contract.get('actual_sha256'))[:12]} != "
                        f"{str(self.contract.get('expected_sha256'))[:12]}); "
                        f"re-validate the bridge contract before trusting results")
        if self.contract.get('source_matches_head') is False:
            self._print(f"[agent] WARNING: client source changed after "
                        f"built_from_commit={self.contract.get('built_from_commit')} "
                        f"(head={self.contract.get('repo_head')}); rebuild the client and "
                        f"update tools/bridge_contract.json before trusting the contract text")
        # Only touch a client that an existing session file proves we own.
        old = self._load_session()
        if old and old.alive():
            self._print(f'[agent] stopping previously owned client pid={old.client_pid}')
            self._kill_owned(old)
        # Report (do not kill) any unowned client: it would block/build-conflict.
        unowned = []
        for p in psutil.process_iter(['pid', 'name']):
            try:
                if (p.info['name'] or '').lower() == 'vcmi_client.exe':
                    unowned.append(p.info['pid'])
            except psutil.Error:
                continue
        if unowned:
            raise BridgeError(f'unowned VCMI_client.exe is running (pids={unowned}); '
                              f'refusing to start (ownership rule)')
        (self.dir / 'commands.txt').write_text('', encoding='utf-8')
        # results.log is append-only in the bridge and its seq numbers restart at 1 for every
        # new engine instance: truncate it here so stale replies can never be matched again.
        try:
            (self.dir / 'results.log').write_text('', encoding='utf-8')
        except OSError:
            pass
        # Debug-only resource cheat channel: the engine server polls this file only when the
        # launcher exports AUTO_HEROS3_CHEAT_FILE (see server/CGameHandler::processDebugCheatFile).
        cheat_path = self.dir / 'cheat.txt'
        cheat_path.write_text('', encoding='utf-8')
        LOGDIR.mkdir(parents=True, exist_ok=True)
        self._pending_seq = 0
        self._seq_offset = {}
        # State epoch: anything written before this moment belongs to a previous engine
        # instance and must never be adopted by wait_in_game()/state().
        epoch = {'started_at': time.time(), 'map_name': self.map_name or ''}
        try:
            (self.dir / EPOCH_FILE).write_text(json.dumps(epoch), encoding='utf-8')
        except OSError:
            pass
        # Host command safety: the map name was validated at the top of start(); every value
        # embedded in the PowerShell -Command is single-quoted instead of concatenated raw.
        def q(value) -> str:
            return "'" + str(value).replace("'", "''") + "'"

        args = [q(EXE), '--nointro', '--logLocation', q(LOGDIR),
                '--agent-view', q(self.dir)]
        if self.map_name:
            args += ['--agent-start', q(self.map_name)]
        # Recover a backup left by a crashed/previous MCP process before taking a new snapshot.
        if self.audio_driver not in ('disk', 'a3capture'):
            self._restore_video_settings()
            raise BridgeError(f'unsupported audio driver {self.audio_driver!r}')
        if self.audio_driver == 'a3capture':
            # A killed previous client may leave a stale ready marker; readiness
            # must be produced by this launch, not inherited from the last one.
            try:
                self.a3capture_ready_path.unlink()
            except OSError:
                pass
        self._restore_video_settings()
        self._patch_video_settings()
        try:
            self._start_audio_relay()
        except Exception:
            self._restore_video_settings()
            raise
        if self.audio_driver == 'disk':
            audio_pipe_path = '\\\\.\\pipe\\' + str(self.audio_pipe_name)
            audio_env = (f"$env:SDL_AUDIODRIVER='disk'; "
                         f"$env:SDL_DISKAUDIOFILE={q(audio_pipe_path)}; ")
        else:
            self.a3capture_file.parent.mkdir(parents=True, exist_ok=True)
            audio_env = (f"$env:SDL_AUDIODRIVER='{self.audio_driver}'; "
                         f"$env:A3CAPTURE_FILE={q(str(self.a3capture_file))}; ")
        cmdline = (f"& {q(CONANRUN)}; "
                   + audio_env +
                   f"$env:AUTO_HEROS3_CHEAT_FILE={q(cheat_path)}; "
                   f"& {args[0]} " + ' '.join(args[1:]))
        self.proc = None
        try:
            self.proc = subprocess.Popen(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                          '-Command', cmdline],
                                         cwd=str(WORKDIR),
                                         creationflags=CREATE_NO_WINDOW,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            self._stop_audio_relay()
            self._restore_video_settings()
            raise
        self._print(f'[agent] launcher pid={self.proc.pid}')
        t0 = time.time()
        client_pid, create_time = None, None
        while time.time() - t0 < timeout:
            client_pid, create_time = self._find_client_pid(self.proc.pid)
            if client_pid:
                break
            time.sleep(0.5)
        if not client_pid:
            self._print('[agent] client process never appeared')
            self._abort_start()
            return False
        sys_path = str(ROOT / 'tools')
        import sys
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        import vcmi_shot
        t0 = time.time()
        win = None
        while time.time() - t0 < timeout:
            ws = vcmi_shot.find_window()
            if ws:
                win = ws[0]
                break
            time.sleep(0.5)
        if not win:
            self._print('[agent] window never appeared')
            self._abort_start(client_pid=client_pid, create_time=create_time)
            return False
        self.hwnd = win[0]
        time.sleep(1.0)
        try:
            self._place_and_mute()
        except Exception:
            self._restore_video_settings()
            self._abort_start(client_pid=client_pid, create_time=create_time)
            raise
        if self.offscreen:
            # legacy mode: hard guarantee the window is outside the virtual desktop
            if not self._place_offscreen():
                self._print('[agent] ERROR: cannot place the window off-screen - aborting start')
                self._abort_start(client_pid=client_pid, create_time=create_time)
                raise BridgeError('refusing to run a game that is not off-screen')
            self._stop_watch.clear()
            self._watch_thread = threading.Thread(target=self._offscreen_watchdog, daemon=True)
            self._watch_thread.start()
        audio_ready = True
        audio_ready_detail = 'disk relay path (legacy)'
        if self.audio_driver == 'a3capture':
            deadline = time.time() + 15.0
            while time.time() < deadline and not self.a3capture_ready_path.exists():
                time.sleep(0.05)
            audio_ready = self.a3capture_ready_path.exists()
            audio_ready_detail = (f'a3capture ready={audio_ready} '
                                  f'path={self.a3capture_ready_path}')
            if not audio_ready:
                self._abort_start(client_pid=client_pid, create_time=create_time)
                raise BridgeError('a3capture audio_ready=false')
        sess = Session(dir=str(self.dir), map_name=self.map_name, client_pid=client_pid,
                       client_create_time=create_time, launcher_pid=self.proc.pid,
                       created_at=time.time())
        rect = self._window_rect()
        sess.notes.append(f'window_rect={rect} offscreen={self.window_is_offscreen()}')
        if self.audio_driver == 'a3capture':
            sess.notes.append(f'audio_sink={self.audio_driver} raw={self.a3capture_file} '
                              f'format=s16le/44100/2 physical_endpoint_untouched=true '
                              f'{audio_ready_detail}')
        else:
            sess.notes.append(f'audio_sink=disk raw={self.audio_sink_path} '
                              f'format=s16le/44100/2 physical_endpoint_untouched=true')
        if self.display_index is not None:
            sess.notes.append(f'target_display_index={self.display_index} '
                              f'first_placement_kept=true')
        if self.contract:
            sess.notes.append(
                f"client_sha256={self.contract.get('actual_sha256')} "
                f"contract_match={self.contract.get('match')} "
                f"pinned_at={self.contract.get('built_from_commit')} "
                f"repo_head={self.contract.get('repo_head')} "
                f"client_source_matches_head={self.contract.get('source_matches_head')}")
        self._save_session(sess)
        self._print(f'[agent] client pid={client_pid} (owned by session {sess.session_id or "?"})')
        return True

    def _abort_start(self, client_pid: int | None = None,
                     create_time: float | None = None) -> None:
        """Best-effort cleanup when start() fails after spawning the launcher."""
        sess = None
        if client_pid:
            sess = Session(dir=str(self.dir), map_name=self.map_name,
                           client_pid=client_pid, client_create_time=create_time or 0.0)
        else:
            sess = self._load_session()
        try:
            if self.proc is not None:
                subprocess.run(['taskkill', '/PID', str(self.proc.pid), '/T', '/F'],
                               capture_output=True)
        except Exception:                       # pragma: no cover - best effort
            pass
        if sess is not None:
            try:
                self._kill_owned(sess)
            except Exception:                   # pragma: no cover - best effort
                pass
        self._stop_audio_relay()
        self._restore_video_settings()
        self.proc = None

    def _patch_video_settings(self) -> None:
        """Temporarily set display/audio launch settings, preserving exact bytes.

        Display placement must happen at first window creation.  Independently of placement, the
        audio section raises the in-game music/sound only when the user has them at 0, so the SDL
        disk sink has real game audio to write; the exact original bytes are restored after the
        owned client stops (or on abort).  No physical endpoint is muted here.
        """
        if self._settings_backup is not None:
            return
        path = VCMI_SETTINGS
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise BridgeError(f'cannot read VCMI settings for session launch: {exc}') from exc
        try:
            data = json.loads(raw.decode('utf-8-sig'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeError(f'cannot parse VCMI settings for session launch: {exc}') from exc
        if self.display_index is not None:
            video = data.setdefault('video', {})
            if not isinstance(video, dict):
                raise BridgeError('VCMI video settings is not an object')
            video['displayIndex'] = int(self.display_index)
            video['fullscreen'] = False
            video['resolution'] = {'width': 1024, 'height': 768}
        # The game writes its audio to the SDL disk sink, never to the physical endpoint.  Raise
        # only zero volumes (a user who chose e.g. 10 keeps 10); restore exact bytes later.
        general = data.setdefault('general', {})
        if isinstance(general, dict):
            general.setdefault('music', 30)
            general.setdefault('sound', 40)
            if general['music'] == 0:
                general['music'] = 30
            if general['sound'] == 0:
                general['sound'] = 40
        payload = json.dumps(data, ensure_ascii=False, indent=4).encode('utf-8')
        try:
            path.write_bytes(payload)
        except OSError as exc:
            raise BridgeError(f'cannot write VCMI settings for session launch: {exc}') from exc
        self._settings_backup = (path, raw)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._settings_backup_file.write_bytes(raw)
        except OSError as exc:
            # Keep the in-memory backup: stop()/atexit can still restore it.
            self._print(f'[agent] WARNING: cannot persist settings backup: {exc}')
        atexit.register(self._restore_video_settings)
        self._print(f'[agent] patched settings {path}; display_index={self.display_index}')

    def _restore_video_settings(self) -> None:
        path, raw = VCMI_SETTINGS, None
        if self._settings_backup is not None:
            path, raw = self._settings_backup
            self._settings_backup = None
        elif self._settings_backup_file.exists():
            # A different VcmiAgent instance (session_stop constructs a fresh one) or an
            # MCP restart must still restore the user's settings.
            try:
                raw = self._settings_backup_file.read_bytes()
            except OSError:
                raw = None
        if raw is None:
            return
        try:
            path.write_bytes(raw)
            try:
                self._settings_backup_file.unlink()
            except OSError:
                pass
            self._print(f'[agent] restored VCMI settings {path}')
        except OSError as exc:                      # pragma: no cover - best effort
            self._print(f'[agent] WARNING: cannot restore VCMI settings: {exc}')

    def _place_and_mute(self) -> None:
        import sys
        sys.path.insert(0, str(ROOT / 'tools'))
        import vcmi_shot
        wins = vcmi_shot.find_window()
        if not wins:
            return
        self.hwnd = wins[0][0]
        import ctypes
        user32 = ctypes.WinDLL('user32')
        user32.SetProcessDPIAware()
        if self.offscreen:
            self._place_offscreen()
        elif self.display_index is None:
            user32.SetWindowPos(self.hwnd, 0, 0, 0, 0, 0, 0x1 | 0x4 | 0x10)
        # display_index is not None: keep the first-created SDL window exactly where the
        # launcher placed it; no post-creation move means the first-placement contract holds.
        try:
            from agent.h3.audio import set_mute
            for p in psutil.process_iter(['pid', 'name']):
                if (p.info['name'] or '').lower().startswith('vcmi'):
                    set_mute(p.info['pid'], True)
        except Exception as exc:                      # pragma: no cover
            self._print(f'[agent] mute skipped: {exc}')

    def _kill_owned(self, sess: Session, timeout: float = 12.0) -> bool:
        self._stop_watch.set()
        if not sess.alive():
            return True
        pid = sess.client_pid
        # taskkill is the normal path (it also walks the child tree); on some hosts the WMI
        # provider fails ("Provider load failure", seen 2026-10-25), so fall back to a direct
        # psutil kill of this owned process tree instead of leaving a client behind.
        try:
            subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True)
        except OSError:
            pass
        t0 = time.time()
        fallback_used = False
        while time.time() - t0 < timeout:
            try:
                p = psutil.Process(pid)
                if not p.is_running():
                    return True
            except psutil.Error:
                return True
            if not fallback_used and time.time() - t0 > 3.0:
                fallback_used = True
                try:
                    for child in p.children(recursive=True):
                        try:
                            child.kill()
                        except psutil.Error:
                            pass
                    p.kill()
                except psutil.Error:
                    return True
            time.sleep(0.2)
        return False

    def stop(self) -> dict:
        """Stop only the client/server owned by this session; report what actually happened."""
        sess = self.session or self._load_session()
        result = {'owned_pid': None, 'killed': False, 'leftover': [], 'session_removed': False}
        if sess is None:
            result['detail'] = 'no session file'
            self._stop_audio_relay()
            self._restore_video_settings()
            return result
        result['owned_pid'] = sess.client_pid
        if sess.alive():
            result['killed'] = self._kill_owned(sess)
        else:
            result['killed'] = True    # nothing to kill
        for p in psutil.process_iter(['pid', 'name']):
            try:
                if (p.info['name'] or '').lower().startswith('vcmi'):
                    result['leftover'].append(f"{p.info['name']}:{p.info['pid']}")
            except psutil.Error:
                continue
        if result['killed']:
            try:
                self.session_path.unlink()
                result['session_removed'] = True
            except OSError:
                pass
            self.session = None
        self._stop_watch.set()
        self._stop_audio_relay()
        self._restore_video_settings()
        self.proc = None
        return result

    # ------------------------------------------------------------------ state
    def state(self, timeout: float = 10.0) -> dict:
        path = self.dir / 'state.json'
        if not self.session:
            self.session = self._load_session()
        t0 = time.time()
        last_err = 'no state'
        while time.time() - t0 < timeout:
            try:
                st = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                last_err = 'state.json unreadable'
                time.sleep(0.08)
                continue
            err = self._validate_state(st)
            if err is None:
                return st
            last_err = err
            time.sleep(0.08)
        raise StaleStateError(last_err)

    def _epoch_started_at(self) -> float | None:
        """Wall-clock time of the current engine start (None when no epoch file exists)."""
        try:
            data = json.loads((self.dir / EPOCH_FILE).read_text(encoding='utf-8'))
            value = data.get('started_at')
            return float(value) if isinstance(value, (int, float)) else None
        except (OSError, ValueError, TypeError):
            return None

    def _validate_state(self, st: dict) -> str | None:
        sess = self.session
        if sess is None:
            return 'no session (call start() or use a dir with session.json)'
        if not sess.alive():
            return f'owned client pid={sess.client_pid} is not running'
        if not st.get('in_game'):
            return 'in_game=false'
        if st.get('protocol') != 2:
            return f"unsupported protocol {st.get('protocol')!r} (expected 2)"
        # Freshness comes FIRST: neither identity binding nor command submission may use a
        # stale snapshot (including a leftover state.json from a previous engine instance).
        ts = st.get('ts_wall')
        if not isinstance(ts, (int, float)):
            return 'state has no ts_wall'
        ts_seconds = float(ts) / 1000.0
        age = time.time() - ts_seconds
        if age > self.max_state_age:
            return f'state is stale ({age:.1f}s old > {self.max_state_age}s)'
        if age < -10:
            return f'state timestamp is in the future ({-age:.1f}s)'
        epoch = self._epoch_started_at()
        if epoch is not None and ts_seconds < epoch - 5:
            return 'state predates this engine start (stale file from a previous run)'
        sid = st.get('session_id')
        if not sess.session_id:
            if not sid:
                return 'state has no session_id'
            sess.session_id = sid            # fresh snapshot: safe to bind identity
            self._save_session(sess)
            return None
        if sid != sess.session_id:
            return f'state session_id={sid!r} does not match {sess.session_id!r}'
        return None

    def wait_in_game(self, timeout: float = 180.0) -> dict:
        """Wait for a *fresh* in-game snapshot of this engine instance, then bind identity."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                st = json.loads((self.dir / 'state.json').read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                time.sleep(0.5)
                continue
            if not st.get('in_game'):
                time.sleep(0.5)
                continue
            ts = st.get('ts_wall')
            epoch = self._epoch_started_at()
            fresh = isinstance(ts, (int, float)) and (
                time.time() - float(ts) / 1000.0 <= self.max_state_age)
            new_enough = (epoch is None) or (
                isinstance(ts, (int, float)) and float(ts) / 1000.0 >= epoch - 5)
            if fresh and new_enough:
                return self.state(timeout=10)
            time.sleep(0.5)
        raise TimeoutError('game never reached a fresh in_game=true state')

    # ------------------------------------------------------------------ commands
    def _command_lines(self) -> int:
        try:
            return len((self.dir / 'commands.txt').read_text(encoding='utf-8').splitlines())
        except OSError:
            return 0

    def peek_next_seq(self) -> int:
        """Sequence number the next submit() will use (single-writer lease assumption)."""
        return self._command_lines() + 1

    def bridge_identity(self) -> dict:
        """Identity of the current bridge generation, used to validate recovered replies."""
        return {'session_id': self.session.session_id if self.session else None,
                'epoch_started_at': self._epoch_started_at()}

    def _acquire_submit_lock(self, timeout: float = 10.0):
        """Cross-process writer lock so read-lines+append+seq is one transaction."""
        import os
        lock_path = self.dir / 'commands.lock'
        t0 = time.time()
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode('ascii'))
                return fd, lock_path
            except FileExistsError:
                try:
                    if time.time() - lock_path.stat().st_mtime > 10:
                        lock_path.unlink()          # stale lock from a crashed writer
                        continue
                except OSError:
                    pass
                if time.time() - t0 > timeout:
                    raise BridgeError('commands.lock busy: another writer is submitting')
                time.sleep(0.05)

    def submit(self, line: str) -> int:
        """Append one command and return its 1-based sequence number (bridge uses line index)."""
        import os
        if not isinstance(line, str) or not line.strip():
            raise BridgeError('empty command')
        if '\n' in line or '\r' in line:
            # A newline would become a *second* bridge command outside the whitelist/ledger.
            raise BridgeError('command must be a single line (embedded newline rejected)')
        fd, lock_path = self._acquire_submit_lock()
        try:
            lines = self._command_lines()
            # Reply lines for THIS command can only appear after this offset; recording it lets
            # a later reconciliation distinguish this reply from a stale one of another session.
            offset = len(self.results())
            try:
                command_file = open(self.dir / 'commands.txt', 'a', encoding='utf-8',
                                    newline='\n')
            except OSError:
                # Known not sent: the file was never opened, so no command bytes were queued.
                raise
            try:
                with command_file:
                    command_file.write(line + '\n')
            except OSError as exc:
                # The file was opened; a partial write is possible.  Never report this as a
                # confirmed NOT_SENT.
                raise SubmitUnknown(f'command file write failed after open: {exc}') from exc
            seq = lines + 1
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                lock_path.unlink()
            except OSError:
                pass
        self._seq_offset[seq] = offset
        if len(self._seq_offset) > 256:            # keep the map bounded in long sessions
            for stale in sorted(self._seq_offset)[:-128]:
                self._seq_offset.pop(stale, None)
        self._pending_seq = seq
        return seq

    def results(self) -> list[str]:
        path = self.dir / 'results.log'
        if not path.exists():
            return []
        return path.read_text(encoding='utf-8', errors='replace').splitlines()

    def wait_seq(self, seq: int, timeout: float = 20.0,
                 from_index: int | None = None) -> tuple[str, str]:
        """Return (kind, line) for the reply carrying *this* seq, or raise TimeoutError.

        from_index skips lines that existed before this command was submitted, so a stale
        `seq=N` line from a previous bridge generation can never satisfy a new command.
        """
        prefix = f'seq={seq} '
        start = self._seq_offset.get(seq, 0) if from_index is None else from_index
        t0 = time.time()
        while time.time() - t0 < timeout:
            for ln in self.results()[start:]:
                if not ln.startswith(prefix):
                    continue
                body = ln[len(prefix):]
                for kind in ('OK', 'ERR', 'QUERY', 'INFO', 'AUTO'):
                    if body.startswith(kind):
                        return kind, ln
            time.sleep(0.1)
        raise TimeoutError(f'no reply with seq={seq}')

    def execute(self, line: str, verify=None, verify_timeout: float = 8.0,
                result_timeout: float = 20.0) -> CommandResult:
        """Submit a command; OK means *submitted*.  verify() must confirm the effect.

        verify is either None (no post-condition: the caller accepts submission) or a
        callable() -> (bool, detail) polled until verify_timeout.  Without confirmation the
        result is UNKNOWN - never a blind retry.
        """
        try:
            seq = self.submit(line)
        except SubmitUnknown as exc:
            return CommandResult(0, 'UNKNOWN', '', f'submission unknown: {exc}', sent=None)
        except OSError as exc:
            return CommandResult(0, 'NOT_SENT', '', str(exc), sent=False)
        try:
            kind, ln = self.wait_seq(seq, timeout=result_timeout)
        except TimeoutError:
            return CommandResult(seq, 'UNKNOWN', '', 'no reply before timeout', sent=True)
        if kind == 'ERR':
            return CommandResult(seq, 'REJECTED', ln, sent=True)
        if verify is None:
            return CommandResult(seq, 'COMPLETED', ln, 'submitted (no post-condition given)',
                                 sent=True)
        t0 = time.time()
        while time.time() - t0 < verify_timeout:
            try:
                done, detail = verify()
            except StaleStateError as exc:
                return CommandResult(seq, 'UNKNOWN', ln, f'state lost: {exc}', sent=True)
            if done:
                return CommandResult(seq, 'COMPLETED', ln, detail, sent=True)
            time.sleep(0.25)
        return CommandResult(seq, 'UNKNOWN', ln, 'reply received but effect not observed',
                             sent=True)

    # ------------------------------------------------------------------ screenshots
    def capture_screenshot(self, path: Path | str | None = None) -> dict:
        """Capture the CURRENT session window and write a freshness sidecar.

        Why: `PrintWindow` can return an older surface if the cached hwnd is dead/reused or the
        window is no longer the one the session owns.  AV.1a therefore re-resolves the window by
        the session's client PID (not "first VCMI-titled window"), verifies the process create
        time, refuses hidden/minimized surfaces, and records capture-time/window/source metadata
        in `<path>.json`.  A screenshot is never silently reported as fresh when this binding
        cannot be established - the caller gets a StaleStateError-style failure.
        """
        import sys
        sys.path.insert(0, str(ROOT / 'tools'))
        import vcmi_shot

        target = Path(path or (ROOT / 'var' / 'shots' / 'vcmi_agent.png'))
        sess = self.session or self._load_session()
        if sess is None or not sess.client_pid:
            raise StaleStateError('no session/client pid: cannot bind a live screenshot source')
        pid = int(sess.client_pid)
        try:
            proc = psutil.Process(pid)
            create_time = float(proc.create_time())
        except psutil.Error as exc:
            raise StaleStateError(f'client pid {pid} is not alive: {exc}') from exc
        if sess.client_create_time and abs(create_time - float(sess.client_create_time)) > 2.0:
            raise StaleStateError(
                f'client pid {pid} was reused by another process (create-time mismatch); '
                'refusing a stale screenshot')
        candidates = vcmi_shot.find_window_for_pid(pid)
        if not candidates:
            raise StaleStateError(f'no live window owned by client pid {pid}')
        hwnd, title, visible, minimized = candidates[0]
        if not visible or minimized:
            raise StaleStateError(
                f'client window {hwnd:#x} is not capturable (visible={visible}, '
                f'minimized={minimized})')
        target.parent.mkdir(parents=True, exist_ok=True)
        img = vcmi_shot.grab(hwnd)
        img.save(target)
        cw, ch, cx, cy = vcmi_shot.client_geometry(hwnd)
        meta = {
            'captured_at_wall': time.time(),
            'captured_at_monotonic': time.monotonic(),
            'client_pid': pid,
            'hwnd': int(hwnd),
            'window_title': title,
            'window_visible': visible,
            'window_minimized': minimized,
            'client_rect': [int(cw), int(ch)],
            'client_screen_origin': [int(cx), int(cy)],
            'image_size': [int(img.size[0]), int(img.size[1])],
            'candidates': [{'hwnd': int(item[0]), 'title': item[1],
                            'visible': bool(item[2]), 'minimized': bool(item[3])}
                           for item in candidates],
        }
        sidecar = target.with_suffix(target.suffix + '.json')
        sidecar.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding='utf-8')
        self.hwnd = hwnd
        self.last_shot_meta = meta
        self._print(f'[agent] shot -> {target} {img.size} hwnd={hwnd:#x} pid={pid}')
        return {'path': str(target), 'meta': meta, 'sidecar': str(sidecar)}

    def shot(self, path: Path | str | None = None):
        """Backward-compatible screenshot entry (returns the image path)."""
        return Path(self.capture_screenshot(path)['path'])

    # ------------------------------------------------------------------ verification helpers
    def wait_until(self, predicate, timeout: float = 10.0, interval: float = 0.3):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                ok, detail = predicate()
            except StaleStateError as exc:
                return False, f'state lost: {exc}'
            if ok:
                return True, detail
            time.sleep(interval)
        return False, 'timeout'

    @staticmethod
    def hero_position(st: dict, hero_idx: int = 0):
        heroes = st.get('heroes') or []
        if hero_idx >= len(heroes):
            return None
        return tuple(heroes[hero_idx]['visitable'][:2])

    def verify_move(self, hero_idx: int, target_xy) -> callable:
        target = tuple(target_xy[:2])

        def _check():
            st = self.state(timeout=5)
            pos = self.hero_position(st, hero_idx)
            if pos is None:
                return False, f'hero {hero_idx} missing'
            return pos == target, f'hero {hero_idx} at {pos}'
        return _check

    def verify_build(self, town_idx: int, building_id: int) -> callable:
        def _check():
            st = self.state(timeout=5)
            towns = st.get('towns') or []
            if town_idx >= len(towns):
                return False, 'town missing'
            built = towns[town_idx].get('built') or []
            return building_id in built, f'town {town_idx} built={built}'
        return _check

    def verify_recruit(self, town_idx: int, creature_id: int | None, min_delta: int = 1) -> callable:
        baseline = {'count': None}

        def _check():
            st = self.state(timeout=5)
            towns = st.get('towns') or []
            heroes = st.get('heroes') or []
            if town_idx >= len(towns):
                return False, 'town missing'
            total = 0
            for g in towns[town_idx].get('garrison', []):
                if creature_id is None or g['creature'] == creature_id:
                    total += g['count']
            for h in heroes:
                for a in h.get('army', []):
                    if creature_id is None or a['creature'] == creature_id:
                        total += a['count']
            if baseline['count'] is None:
                baseline['count'] = total
                return False, f'baseline {total}'
            return total >= baseline['count'] + min_delta, f'count {baseline["count"]} -> {total}'
        return _check

    def verify_hero_army_gain(self, hero_idx: int = 0, min_gain: int = 1) -> callable:
        """Post-condition for garrison -> hero transfers: the hero's army total must grow."""
        baseline = {'total': None}

        def _check():
            st = self.state(timeout=5)
            heroes = st.get('heroes') or []
            if hero_idx >= len(heroes):
                return False, 'hero missing'
            total = sum(a['count'] for a in heroes[hero_idx].get('army') or [])
            if baseline['total'] is None:
                baseline['total'] = total
                return False, f'baseline {total}'
            return total >= baseline['total'] + min_gain, f'hero army {baseline["total"]} -> {total}'
        return _check

    @staticmethod
    def quick_state(agent: 'VcmiAgent') -> dict:
        return agent.state(timeout=5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--map', default=None)
    ap.add_argument('--dir', default=str(DEFAULT_BRIDGE))
    ap.add_argument('--onscreen', action='store_true')
    ap.add_argument('--keep', action='store_true')
    args = ap.parse_args()
    agent = VcmiAgent(args.dir, args.map, offscreen=not args.onscreen)
    try:
        if not agent.start():
            return 1
        st = agent.wait_in_game()
        print('[agent] session', agent.session.session_id, 'client', agent.session.client_pid)
        print('[agent] state keys:', sorted(st.keys()))
        print('[agent] day', st['date']['day'], 'gold', st['resources']['gold'],
              'heroes', [(h['name'], h['visitable']) for h in st['heroes']])
        print('[agent] town', st['towns'][0]['name'],
              'buildable', st['towns'][0].get('buildable'),
              'build_options', len(st['towns'][0].get('build_options', [])))
        if args.keep:
            return 0
    finally:
        if not args.keep:
            print('[agent] stop:', agent.stop())
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
