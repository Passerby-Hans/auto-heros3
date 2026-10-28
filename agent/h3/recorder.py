# -*- coding: utf-8 -*-
"""Frame recorder: continuously capture the (off-screen) game window via
PrintWindow into var/rec/*.jpg for later ffmpeg assembly into a demo video.

Usage:
  uv run python -m agent.h3.recorder start   # begin recording (background)
  uv run python -m agent.h3.recorder stop    # stop (writes stop-flag)
  uv run python -m agent.h3.recorder build   # assemble var/rec -> var/demo.mp4
"""
import sys, time
from pathlib import Path

REC = Path('var/rec')
FLAG = REC / 'STOP'


def record(fps=8):
    REC.mkdir(parents=True, exist_ok=True)
    FLAG.unlink(missing_ok=True)
    from agent.h3.capture import h3_hwnds, grab
    hwnd = h3_hwnds()[0][0]
    n = 0
    interval = 1.0 / fps
    print('recording...', flush=True)
    while not FLAG.exists():
        t0 = time.time()
        try:
            grab(hwnd).save(REC / f'f{n:05d}.jpg', quality=80)
            n += 1
        except Exception:
            pass
        dt = time.time() - t0
        if dt < interval:
            time.sleep(interval - dt)
    print(f'stopped, {n} frames', flush=True)


def stop():
    REC.mkdir(parents=True, exist_ok=True)
    FLAG.write_text('stop')


def build(fps=8, out='var/demo.mp4'):
    import subprocess
    n = len(list(REC.glob('f*.jpg')))
    if not n:
        print('no frames'); return
    subprocess.run(['ffmpeg', '-y', '-framerate', str(fps), '-i', str(REC / 'f%05d.jpg'),
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '23', out],
                   check=True, capture_output=True)
    print(f'{n} frames -> {out}')


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'record'
    if cmd == 'record':
        record()
    elif cmd == 'start':
        record()
    elif cmd == 'stop':
        stop()
    elif cmd == 'build':
        build()
