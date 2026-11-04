# -*- coding: utf-8 -*-
"""AV.1c audio mux contract (offline, ffmpeg only; no game/audio session)."""
from __future__ import annotations

import json
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from auto_heros3.recording.audio_capture import (  # noqa: E402
    mux_audio_file, mux_raw_audio_file, analyze_raw_pcm, mux_raw_audio_segment)
from auto_heros3.recording.overlay_recorder import parse_segment_list  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    RESULTS.append((name, bool(ok), str(detail)))


def ffprobe(path: Path) -> dict:
    p = subprocess.run([shutil.which('ffprobe') or 'ffprobe', '-v', 'error', '-show_streams',
                        '-show_format', '-of', 'json', str(path)], capture_output=True, text=True)
    return json.loads(p.stdout) if p.returncode == 0 else {}


def test_mux() -> None:
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        check('ffmpeg_available', False, 'ffmpeg missing')
        return
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        video = root / 'v.mp4'
        wav = root / 'a.wav'
        out = root / 'out.mp4'
        subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=320x240:d=1',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-y', str(video)], check=True)
        with wave.open(str(wav), 'wb') as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000)
            data = bytearray()
            for i in range(48000):
                v = int(0.2 * 32767 * math.sin(2 * math.pi * 440 * i / 48000))
                data += struct.pack('<hh', v, v)
            w.writeframes(data)
        ok, err, cmd = mux_audio_file(video, wav, out, delay_ms=0)
        check('mux_success', ok and err is None and out.exists(), str(err))
        info = ffprobe(out)
        streams = info.get('streams', [])
        kinds = sorted(s.get('codec_type') for s in streams)
        check('mux_has_video_audio', kinds == ['audio', 'video'], str(kinds))
        v = next((s for s in streams if s.get('codec_type') == 'video'), {})
        a = next((s for s in streams if s.get('codec_type') == 'audio'), {})
        check('mux_video_copy_resolution', v.get('width') == 320 and v.get('height') == 240,
              f"{v.get('width')}x{v.get('height')}")
        check('mux_audio_aac', a.get('codec_name') == 'aac', str(a.get('codec_name')))
        check('mux_duration_not_negative',
              float(info.get('format', {}).get('duration', -1)) >= 0.8,
              str(info.get('format', {}).get('duration')))
        check('mux_command_reported', bool(cmd) and 'ffmpeg' in cmd)


def test_raw_mux() -> None:
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        return
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        video = root / 'v.mp4'
        raw = root / 'a.raw'
        out = root / 'out.mp4'
        subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=320x240:d=1',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-y', str(video)], check=True)
        data = bytearray()
        for i in range(44100):
            v = int(0.2 * 32767 * math.sin(2 * math.pi * 440 * i / 44100))
            data += struct.pack('<hh', v, v)
        raw.write_bytes(bytes(data))
        peak, rms, nonzero, duration = analyze_raw_pcm(raw)
        check('raw_analysis_signal', peak > 0.1 and rms > 0 and nonzero >= 0.9,
              f'peak={peak} rms={rms} nz={nonzero}')
        check('raw_analysis_duration', 0.9 <= duration <= 1.1, str(duration))
        ok, err, cmd = mux_raw_audio_file(video, raw, out, delay_ms=0)
        check('raw_mux_success', ok and err is None and out.exists(), str(err))
        info = ffprobe(out)
        kinds = sorted(s.get('codec_type') for s in info.get('streams', []))
        check('raw_mux_has_video_audio', kinds == ['audio', 'video'], str(kinds))


def test_segment_contract() -> None:
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        return
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        video = root / 'seg.mp4'
        raw = root / 'raw.raw'
        out = root / 'seg.av.mp4'
        subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                        'color=c=black:s=320x240:d=2', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', '-y', str(video)], check=True)
        data = bytearray()
        for i in range(44100 * 4):
            v = int(0.2 * 32767 * math.sin(2 * math.pi * 440 * i / 44100))
            data += struct.pack('<hh', v, v)
        raw.write_bytes(bytes(data))
        ok, err, _cmd = mux_raw_audio_segment(video, raw, out, 0, start_s=1.0,
                                              duration_s=1.0)
        check('segment_mux_success', ok and err is None and out.exists(), str(err))
        info = ffprobe(out)
        kinds = sorted(s.get('codec_type') for s in info.get('streams', []))
        check('segment_mux_has_video_audio', kinds == ['audio', 'video'], str(kinds))
        csv = root / 'segments.csv'
        csv.write_text('part-00000.mp4,0.000000,2.000000\n', encoding='utf-8')
        parsed = parse_segment_list(csv, root)
        check('segment_csv_filename_first',
              len(parsed) == 1 and parsed[0][0] == 0.0 and parsed[0][1] == 2.0,
              str(parsed))


def main() -> int:
    test_mux()
    test_raw_mux()
    test_segment_contract()
    for name, ok, detail in RESULTS:
        print(('PASS ' if ok else 'FAIL ') + name + (f'  ({detail})' if detail else ''))
    failed = [r for r in RESULTS if not r[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
