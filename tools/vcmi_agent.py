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
import json
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / 'vendor' / 'vcmi-src' / 'build' / 'bin' / 'VCMI_client.exe'
WORKDIR = ROOT / 'vendor' / 'vcmi-src' / 'build' / 'bin'
CONANRUN = ROOT / 'vendor' / 'vcmi-src' / 'conan-msvc' / 'conanrun.ps1'
DEFAULT_BRIDGE = ROOT / 'var' / 'agent'
LOGDIR = ROOT / 'var' / 'logs' / 'vcmi'
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

    @property
    def ok(self) -> bool:
        return self.status == 'COMPLETED'

    def __str__(self) -> str:
        return f'#{self.seq} {self.status} {self.line or "-"} {self.detail}'.strip()


class VcmiAgent:
    def __init__(self, bridge_dir: Path | str = DEFAULT_BRIDGE, map_name: str | None = None,
                 offscreen: bool = True, log: bool = True, max_state_age: float = 3.0):
        self.dir = Path(bridge_dir).resolve()
        self.map_name = map_name
        self.offscreen = offscreen
        self.log = log
        self.max_state_age = max_state_age
        self.proc = None
        self.hwnd: int | None = None
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
        cmdline = (f"& {q(CONANRUN)}; $env:AUTO_HEROS3_CHEAT_FILE={q(cheat_path)}; "
                   f"& {args[0]} " + ' '.join(args[1:]))
        self.proc = subprocess.Popen(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                      '-Command', cmdline],
                                     cwd=str(WORKDIR),
                                     creationflags=CREATE_NO_WINDOW,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        self._place_and_mute()
        # hard guarantee: window rect must be outside the virtual desktop, and stay there
        if not self._place_offscreen():
            self._print('[agent] ERROR: cannot place the window off-screen - aborting start')
            self._abort_start(client_pid=client_pid, create_time=create_time)
            raise BridgeError('refusing to run a game that is not off-screen')
        self._stop_watch.clear()
        self._watch_thread = threading.Thread(target=self._offscreen_watchdog, daemon=True)
        self._watch_thread.start()
        sess = Session(dir=str(self.dir), map_name=self.map_name, client_pid=client_pid,
                       client_create_time=create_time, launcher_pid=self.proc.pid,
                       created_at=time.time())
        rect = self._window_rect()
        sess.notes.append(f'window_rect={rect} offscreen={self.window_is_offscreen()}')
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
        self.proc = None

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
        else:
            user32.SetWindowPos(self.hwnd, 0, 0, 0, 0, 0, 0x1 | 0x4 | 0x10)
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
            with open(self.dir / 'commands.txt', 'a', encoding='utf-8', newline='\n') as f:
                f.write(line + '\n')
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
        except OSError as exc:
            return CommandResult(0, 'NOT_SENT', '', str(exc))
        try:
            kind, ln = self.wait_seq(seq, timeout=result_timeout)
        except TimeoutError:
            return CommandResult(seq, 'UNKNOWN', '', 'no reply before timeout')
        if kind == 'ERR':
            return CommandResult(seq, 'REJECTED', ln)
        if verify is None:
            return CommandResult(seq, 'COMPLETED', ln, 'submitted (no post-condition given)')
        t0 = time.time()
        while time.time() - t0 < verify_timeout:
            try:
                done, detail = verify()
            except StaleStateError as exc:
                return CommandResult(seq, 'UNKNOWN', ln, f'state lost: {exc}')
            if done:
                return CommandResult(seq, 'COMPLETED', ln, detail)
            time.sleep(0.25)
        return CommandResult(seq, 'UNKNOWN', ln, 'reply received but effect not observed')

    # ------------------------------------------------------------------ screenshots
    def shot(self, path: Path | str | None = None):
        import sys
        sys.path.insert(0, str(ROOT / 'tools'))
        import vcmi_shot
        path = Path(path or (ROOT / 'var' / 'shots' / 'vcmi_agent.png'))
        path.parent.mkdir(parents=True, exist_ok=True)
        img = vcmi_shot.grab(self.hwnd)
        img.save(path)
        self._print(f'[agent] shot -> {path} {img.size}')
        return path

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
