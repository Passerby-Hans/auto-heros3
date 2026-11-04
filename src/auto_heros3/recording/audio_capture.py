# -*- coding: utf-8 -*-
"""Per-process audio capture + local-silence routing for the overlay recorder.

Why process loopback instead of the default endpoint loopback:

* the game is deliberately muted at the WASAPI session level by VcmiAgent, and a session-muted
  process produces silence in process loopback (measured);
* muting the default **endpoint** does not suppress process loopback (measured: a 440 Hz tone
  kept peak 0.25 through endpoint mute/volume 0), so we can capture the real game mix while the
  local speakers are silent;
* the recorder therefore snapshots/restores the game's session mute and the endpoint mute.

The capture DLL is ``process-audio-capture==1.0.0`` (MIT); it produces a WAVE_FORMAT_EXTENSIBLE
48 kHz stereo float32 file (ffmpeg reads it; Python's stdlib wave does not).
"""
from __future__ import annotations

import atexit
import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


class AudioUnavailable(RuntimeError):
    """No capturable process audio/endpoint, or the capture DLL refused to start."""


def binding_available() -> bool:
    try:
        import process_audio_capture  # noqa: F401
        return True
    except Exception:
        return False


def _sessions_for_pid(pid: int):
    from pycaw.pycaw import AudioUtilities
    return [s for s in AudioUtilities.GetAllSessions()
            if s.Process is not None and int(s.Process.pid) == int(pid)]


def _endpoint():
    from pycaw.pycaw import AudioUtilities
    return AudioUtilities.GetSpeakers().EndpointVolume


def game_volume_settings() -> tuple[int | None, int | None]:
    """(music, sound) from VCMI settings.json, or (None, None) if unavailable."""
    try:
        from auto_heros3.runtime.vcmi_agent import VCMI_SETTINGS
        data = json.loads(Path(VCMI_SETTINGS).read_text(encoding='utf-8-sig'))
        general = data.get('general') or {}
        return general.get('music'), general.get('sound')
    except Exception:
        return None, None


def mux_raw_audio_file(video: Path, raw: Path, out: Path, delay_ms: int = 0) -> tuple[bool, str | None, str]:
    """Mux headerless VCMI SDL disk PCM (s16le/44100/2) under an encoded video."""
    ffmpeg = shutil.which('ffmpeg') or r'C:\Users\Hans\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe'
    if not Path(ffmpeg).exists():
        return False, 'ffmpeg not found', ''
    af = f'adelay={max(0, delay_ms)}|{max(0, delay_ms)},apad'
    cmd = [ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
           '-i', str(video), '-f', 's16le', '-ar', '44100', '-ac', '2', '-i', str(raw),
           '-filter_complex', f'[1:a]{af}[a]',
           '-map', '0:v:0', '-map', '[a]', '-c:v', 'copy',
           '-c:a', 'aac', '-b:a', '192k', '-shortest', '-movflags', '+faststart', str(out)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as exc:  # noqa: BLE001
        return False, f'{type(exc).__name__}: {exc}', ' '.join(cmd)
    if p.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        try:
            out.unlink()
        except OSError:
            pass
        return False, (p.stderr[-800:] or f'ffmpeg rc={p.returncode}'), ' '.join(cmd)
    return True, None, ' '.join(cmd)


def analyze_raw_pcm(raw: Path) -> tuple[float, float, float, float]:
    """(peak, rms, nonzero_s, duration_s) for headerless s16le/44100/2."""
    import numpy as np
    p = subprocess.run([shutil.which('ffmpeg') or 'ffmpeg', '-v', 'error',
                        '-f', 's16le', '-ar', '44100', '-ac', '2', '-i', str(raw),
                        '-f', 'f32le', '-ac', '1', '-ar', '16000', '-'],
                       capture_output=True, timeout=120)
    if p.returncode != 0 or not p.stdout:
        return 0.0, 0.0, 0.0, 0.0
    arr = np.frombuffer(p.stdout, dtype='<f4')
    peak = float(np.abs(arr).max()) if arr.size else 0.0
    rms = float((arr.astype('float64') ** 2).mean() ** 0.5) if arr.size else 0.0
    step = max(1, int(0.1 * 16000))
    windows = [arr[i:i + step] for i in range(0, max(0, arr.size - step + 1), step)]
    nonzero = round(sum(1 for w in windows if float(np.abs(w).max()) > 1e-4) * 0.1, 2)
    return peak, rms, nonzero, round(arr.size / 16000, 3)


class DiskAudioSink:
    """Read VCMI's SDL disk-driver raw file; never touches the physical endpoint.

    VCMI is launched with ``SDL_AUDIODRIVER=disk`` and ``SDL_DISKAUDIOFILE=<bridge>/game-audio.raw``.
    The mixer writes headerless s16le/44100/2 in real time.  A recording starts at a byte offset
    (audio from before recording_start is discarded), and the tail is copied into the clip dir.
    """

    def __init__(self, state_dir: Path, out_dir: Path, log=print):
        self.state_dir = Path(state_dir)
        self.out_dir = Path(out_dir)
        self.sink_path = self.state_dir / 'game-audio.raw'
        self.raw_path = self.out_dir / 'audio.raw'
        self.meta = AudioMeta(source='sdl-disk', raw_format='s16le/44100/2',
                              wav=str(self.raw_path))
        self.log = log

    def start(self) -> AudioMeta:
        deadline = time.time() + 8.0
        while time.time() < deadline:
            if self.sink_path.exists():
                break
            time.sleep(0.02)
        if not self.sink_path.exists():
            raise AudioUnavailable(f'SDL disk sink not found: {self.sink_path}')
        self.meta.available = True
        self.meta.captured = True
        self.meta.start_offset_bytes = int(self.sink_path.stat().st_size)
        self.meta.start_wall = time.time()
        self.log(f'[audio] disk sink {self.sink_path} from byte '
                 f'{self.meta.start_offset_bytes}; endpoint untouched')
        return self.meta

    def stop(self, video_started_wall: float | None = None) -> AudioMeta:
        if self.meta.stop_wall is not None:
            return self.meta
        self.meta.stop_wall = time.time()
        start = int(self.meta.start_offset_bytes or 0)
        target = None
        if video_started_wall is not None and self.meta.start_wall is not None:
            video_duration = max(0.0, self.meta.stop_wall - video_started_wall)
            audio_delay = max(0.0, self.meta.start_wall - video_started_wall)
            target = int(max(0.0, video_duration - audio_delay) * 176400)
            self.meta.video_duration_s = round(video_duration, 3)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        wait_start = time.time()
        deadline = wait_start + 10.0
        size = 0
        try:
            if target is not None:
                # The relay may still be draining pipe-buffered samples.  Wait briefly for the
                # sink to cover the video interval, then copy only that interval (never post-stop
                # audio); do not let apad hide a real shortfall.
                while time.time() < deadline:
                    size = self.sink_path.stat().st_size
                    if size >= start + target:
                        break
                    time.sleep(0.05)
            self.meta.drain_wait_s = round(time.time() - wait_start, 3)
            size = self.sink_path.stat().st_size
            available = max(0, size - start)
            n = available if target is None else min(target, available)
            n -= n % 4
            with self.sink_path.open('rb') as src:
                src.seek(start)
                data = src.read(n)
            self.raw_path.write_bytes(data)
            self.meta.raw_bytes = len(data)
            self.meta.bytes = len(data)
            if target is not None:
                self.meta.shortfall_s = round((target - len(data)) / 176400, 3)
                self.meta.clock_ok = self.meta.shortfall_s <= 0.25
            if data:
                peak, rms, nonzero, duration = analyze_raw_pcm(self.raw_path)
                self.meta.peak, self.meta.rms = peak, rms
                self.meta.nonzero_s, self.meta.duration_s = nonzero, duration
        except Exception as exc:  # noqa: BLE001
            self.meta.error = f'sink read: {type(exc).__name__}: {exc}'
        return self.meta

    def mux_into(self, video: Path, out: Path, video_started_wall: float | None) -> AudioMeta:
        if not self.meta.raw_bytes:
            self.meta.mix_error = 'no disk audio bytes'
            return self.meta
        if self.meta.clock_ok is False:
            self.meta.mix_error = (f'raw audio short by {self.meta.shortfall_s}s vs video; '
                                   'refusing to hide it with apad')
            return self.meta
        if self.meta.peak is not None and self.meta.peak <= 1e-6:
            self.meta.mix_error = 'captured audio is silent'
            return self.meta
        delay_ms = 0
        if self.meta.start_wall and video_started_wall:
            delay_ms = max(0, int(round((self.meta.start_wall - video_started_wall) * 1000)))
        ok, err, cmd = mux_raw_audio_file(video, self.raw_path, out, delay_ms)
        self.meta.mix_command = cmd
        self.meta.mix_error = err
        return self.meta


def mux_audio_file(video: Path, wav: Path, out: Path, delay_ms: int = 0) -> tuple[bool, str | None, str]:
    """Mux a WAV/process-loopback audio file under an existing video (video copy + AAC)."""
    ffmpeg = shutil.which('ffmpeg') or r'C:\Users\Hans\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe'
    if not Path(ffmpeg).exists():
        return False, 'ffmpeg not found', ''
    af = f'adelay={max(0, delay_ms)}|{max(0, delay_ms)},apad'
    cmd = [ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
           '-i', str(video), '-i', str(wav),
           '-filter_complex', f'[1:a]{af}[a]',
           '-map', '0:v:0', '-map', '[a]', '-c:v', 'copy',
           '-c:a', 'aac', '-b:a', '192k', '-shortest', '-movflags', '+faststart', str(out)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as exc:  # noqa: BLE001
        return False, f'{type(exc).__name__}: {exc}', ' '.join(cmd)
    if p.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        try:
            out.unlink()
        except OSError:
            pass
        return False, (p.stderr[-800:] or f'ffmpeg rc={p.returncode}'), ' '.join(cmd)
    return True, None, ' '.join(cmd)


@dataclass
class AudioMeta:
    available: bool = False
    captured: bool = False
    wav: str | None = None
    pid: int | None = None
    endpoint_name: str | None = None
    endpoint_muted_before: bool | None = None
    endpoint_muted_during: bool | None = None
    session_mutes_before: list[bool] = field(default_factory=list)
    session_mutes_restored: bool = False
    start_wall: float | None = None
    stop_wall: float | None = None
    bytes: int | None = None
    duration_s: float | None = None
    peak: float | None = None
    rms: float | None = None
    nonzero_s: float | None = None
    error: str | None = None
    mix_command: str | None = None
    mix_error: str | None = None
    source: str | None = None
    raw_format: str | None = None
    start_offset_bytes: int | None = None
    raw_bytes: int | None = None
    video_duration_s: float | None = None
    shortfall_s: float | None = None
    drain_wait_s: float | None = None
    clock_ok: bool | None = None


class AudioCaptureSession:
    """Start audio capture for a window's process and mute the local endpoint temporarily."""

    def __init__(self, hwnd: int, wav_path: Path, log=print):
        self.hwnd = int(hwnd)
        self.wav_path = Path(wav_path)
        self.meta = AudioMeta(wav=str(self.wav_path))
        self._capture = None
        self._endpoint = None
        self._endpoint_state: tuple[bool, float] | None = None
        self._sessions: list[tuple[object, bool]] = []
        self._restored = False
        self.log = log

    # -- lifecycle ---------------------------------------------------------------------------
    def start(self) -> AudioMeta:
        if not binding_available():
            raise AudioUnavailable('process-audio-capture is not installed')
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'tools'))
            import vcmi_shot
            pid = int(vcmi_shot.window_process_id(self.hwnd))
        except Exception as exc:  # noqa: BLE001
            raise AudioUnavailable(f'cannot resolve window pid: {exc}') from exc
        self.meta.pid = pid
        sessions = _sessions_for_pid(pid)
        if not sessions:
            raise AudioUnavailable(f'no WASAPI session for pid {pid}')
        self._sessions = [(s, bool(s.SimpleAudioVolume.GetMute())) for s in sessions]
        self.meta.session_mutes_before = [m for _, m in self._sessions]
        try:
            from pycaw.pycaw import AudioUtilities
            speakers = AudioUtilities.GetSpeakers()
            endpoint = speakers.EndpointVolume
            self._endpoint = endpoint
            self.meta.endpoint_name = getattr(speakers, 'FriendlyName', None)
            self._endpoint_state = (bool(endpoint.GetMute()),
                                    float(endpoint.GetMasterVolumeLevelScalar()))
            self.meta.endpoint_muted_before = self._endpoint_state[0]
        except Exception as exc:  # noqa: BLE001
            raise AudioUnavailable(f'cannot resolve default audio endpoint: {exc}') from exc

        # The game session must be unmuted for process loopback to see samples.  Then mute the
        # ENDPOINT (not the session) so the local speakers stay quiet.
        try:
            for session, _ in self._sessions:
                session.SimpleAudioVolume.SetMute(0, None)
            self._endpoint.SetMute(1, None)
            self.meta.endpoint_muted_during = bool(self._endpoint.GetMute())
        except Exception as exc:  # noqa: BLE001
            self.stop()
            raise AudioUnavailable(f'cannot mute endpoint/unmute game session: {exc}') from exc

        try:
            from process_audio_capture import ProcessAudioCapture
            self.wav_path.parent.mkdir(parents=True, exist_ok=True)
            self._capture = ProcessAudioCapture(pid=pid, output_path=str(self.wav_path))
            self._capture.start()
        except Exception as exc:  # noqa: BLE001
            self.stop()
            raise AudioUnavailable(f'process loopback capture failed: {exc}') from exc
        self.meta.available = True
        self.meta.captured = True
        self.meta.start_wall = time.time()
        atexit.register(self.stop)
        self.log(f'[audio] capture pid={pid} -> {self.wav_path}; endpoint muted locally')
        return self.meta

    def stop(self) -> AudioMeta:
        if self._restored:
            return self.meta
        self._restored = True
        if self._capture is not None:
            try:
                self._capture.stop()
            except Exception as exc:  # noqa: BLE001
                self.meta.error = f'stop capture: {type(exc).__name__}: {exc}'
            self._capture = None
        self.meta.stop_wall = time.time()
        if self.wav_path.exists():
            self.meta.bytes = self.wav_path.stat().st_size
            try:
                p = subprocess.run([
                    shutil.which('ffmpeg') or 'ffmpeg', '-v', 'error', '-i', str(self.wav_path),
                    '-f', 'f32le', '-ac', '1', '-ar', '16000', '-'],
                    capture_output=True, timeout=60)
                if p.returncode == 0 and p.stdout:
                    import numpy as np
                    arr = np.frombuffer(p.stdout, dtype='<f4')
                    self.meta.peak = float(np.abs(arr).max()) if arr.size else 0.0
                    self.meta.rms = float((arr.astype('float64') ** 2).mean() ** 0.5) if arr.size else 0.0
                    # 100 ms windows, consistent with the AV.1c report's nonzero count
                    step = max(1, int(0.1 * 16000))
                    windows = [arr[i:i + step] for i in range(0, arr.size - step + 1, step)]
                    self.meta.nonzero_s = round(sum(1 for w in windows if float(np.abs(w).max()) > 1e-4) * 0.1, 2)
                    self.meta.duration_s = round(arr.size / 16000, 3)
            except Exception as exc:  # noqa: BLE001
                self.meta.error = f'audio analysis: {type(exc).__name__}: {exc}'
            try:
                p = subprocess.run([
                    shutil.which('ffprobe') or 'ffprobe', '-v', 'error',
                    '-show_entries', 'format=duration', '-of', 'json', str(self.wav_path)],
                    capture_output=True, text=True, timeout=15)
                if p.returncode == 0 and self.meta.duration_s is None:
                    self.meta.duration_s = float(json.loads(p.stdout)['format']['duration'])
            except Exception:
                pass
        if self._endpoint is not None and self._endpoint_state is not None:
            try:
                self._endpoint.SetMute(1 if self._endpoint_state[0] else 0, None)
            except Exception:
                pass
        for session, was_muted in self._sessions:
            try:
                session.SimpleAudioVolume.SetMute(1 if was_muted else 0, None)
            except Exception:
                pass
        self.meta.session_mutes_restored = True
        self._sessions = []
        return self.meta

    # -- mixing ------------------------------------------------------------------------------
    def mux_into(self, video: Path, out: Path, video_started_wall: float | None) -> AudioMeta:
        """Mux captured audio under the already encoded video (CFR duration preserved)."""
        if not self.meta.captured or not self.wav_path.exists() or self.wav_path.stat().st_size <= 0:
            self.meta.mix_error = 'no captured audio'
            return self.meta
        if self.meta.peak is not None and self.meta.peak <= 1e-6:
            self.meta.mix_error = 'captured audio is silent'
            return self.meta
        ffmpeg = shutil.which('ffmpeg') or r'C:\Users\Hans\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe'
        if not Path(ffmpeg).exists():
            self.meta.mix_error = 'ffmpeg not found'
            return self.meta
        delay_ms = 0
        if self.meta.start_wall and video_started_wall:
            delay_ms = max(0, int(round((self.meta.start_wall - video_started_wall) * 1000)))
        ok, err, cmd = mux_audio_file(video, self.wav_path, out, delay_ms)
        self.meta.mix_command = cmd
        self.meta.mix_error = err
        return self.meta
