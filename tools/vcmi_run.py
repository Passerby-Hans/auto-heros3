# -*- coding: utf-8 -*-
"""Dev harness for the VCMI track: start / place / mute / screenshot the client.

Why a dedicated launcher: the VCMI client is a console program (it pops a console
window), it plays music even at volume 0 unless muted at the process level, and its
SDL window is free-floating.  For agent development we need a *repeatable* window:
fixed position, fixed size, no console, no sound.

Usage:
    python -X utf8 tools/vcmi_run.py            # start, place at 0,0, mute, print info
    python -X utf8 tools/vcmi_run.py --shot     # ... and save var/shots/vcmi.png
    python -X utf8 tools/vcmi_run.py --stop     # kill any running client
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

user32 = ctypes.WinDLL('user32', use_last_error=True)
user32.SetProcessDPIAware()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, 'vendor', 'vcmi-1.7.5', 'VCMI_client.exe')
WORKDIR = os.path.join(ROOT, 'vendor', 'vcmi-1.7.5')
LOGDIR = os.path.join(ROOT, 'var', 'logs', 'vcmi')
SHOT = os.path.join(ROOT, 'var', 'shots', 'vcmi.png')

# locally built client (tools/vcmi_build.ps1) needs the Conan runtime env
BUILT_EXE = os.path.join(ROOT, 'vendor', 'vcmi-src', 'build', 'bin', 'VCMI_client.exe')
BUILT_WORKDIR = os.path.join(ROOT, 'vendor', 'vcmi-src', 'build', 'bin')
CONANRUN = os.path.join(ROOT, 'vendor', 'vcmi-src', 'conan-msvc', 'conanrun.ps1')

CREATE_NO_WINDOW = 0x08000000
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
VK_F3 = 0x72          # globalScreenshot (see config/keyBindingsConfig.json)


def cursor_pos():
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


def clip_rect():
    r = wt.RECT()
    user32.GetClipCursor(ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def post_key(hwnd, vk, scan=0):
    user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 1 | (scan << 16))
    time.sleep(0.05)
    user32.PostMessageW(hwnd, WM_KEYUP, vk, 1 | (scan << 16) | (1 << 30) | (1 << 31))


def find_client_windows():
    out = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        if title.startswith('VCMI'):
            pid = wt.DWORD(); user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            cr = wt.RECT(); user32.GetClientRect(hwnd, ctypes.byref(cr))
            pt = wt.POINT(0, 0); user32.ClientToScreen(hwnd, ctypes.byref(pt))
            out.append({'hwnd': hwnd, 'title': title, 'pid': pid.value,
                        'client': (cr.right - cr.left, cr.bottom - cr.top),
                        'origin': (pt.x, pt.y)})
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def wait_window(timeout=25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        w = find_client_windows()
        if w:
            return w[0]
        time.sleep(0.5)
    return None


def place(hwnd, x=0, y=0):
    SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x4, 0x10
    user32.SetWindowPos(hwnd, 0, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)


def mute(pid):
    try:
        sys.path.insert(0, ROOT)
        from agent.h3.audio import set_mute
        return set_mute(pid, True)
    except Exception as e:                     # pragma: no cover
        print('mute failed:', e)
        return 0


def stop():
    try:
        from agent.h3.mem import find_pids
        pids = [p.info['pid'] for p in __import__('psutil').process_iter(['pid', 'name'])
                if (p.info['name'] or '').lower().startswith('vcmi')]
    except Exception:
        pids = []
    for pid in pids:
        subprocess.run(['taskkill', '/PID', str(pid), '/F'], capture_output=True)
    print('stopped', pids)


def shot(path=SHOT, win=None):
    import mss
    from PIL import Image
    win = win or wait_window()
    if not win:
        raise RuntimeError('no VCMI window')
    w, h = win['client']
    x, y = win['origin']
    with mss.mss() as sct:
        img = sct.grab({'left': x, 'top': y, 'width': w, 'height': h})
    im = Image.frombytes('RGB', (img.width, img.height), img.rgb)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.save(path)
    print(f'shot {path} {im.size}')
    return im


def main():
    if '--stop' in sys.argv:
        stop(); return
    os.makedirs(LOGDIR, exist_ok=True)

    # --- mouse-safety baseline: the agent must never steal the user's pointer ---
    pos_before, clip_before = cursor_pos(), clip_rect()
    print(f'user cursor before: {pos_before} clip={clip_before}')

    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    use_built = '--built' in sys.argv and os.path.exists(BUILT_EXE)
    if use_built:
        # the locally built client needs Conan's runtime DLLs on PATH; the wrapper
        # script that CMake generates does exactly this, so mirror it
        cmdline = (f"& '{CONANRUN}'; & '{BUILT_EXE}' --nointro --logLocation '{LOGDIR}'")
        p = subprocess.Popen(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                              '-Command', cmdline],
                             cwd=BUILT_WORKDIR, env=env,
                             creationflags=CREATE_NO_WINDOW,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print('mode: locally built client')
    else:
        p = subprocess.Popen([EXE, '--nointro', '--logLocation', LOGDIR],
                             cwd=WORKDIR, env=env,
                             creationflags=CREATE_NO_WINDOW,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print('mode: prebuilt portable client')
    print('pid', p.pid)
    win = wait_window()
    if not win:
        print('window never appeared'); return
    time.sleep(1.5)
    win = find_client_windows()[0]
    if '--onscreen' in sys.argv:
        place(win['hwnd'], 0, 0)
    else:
        place(win['hwnd'], -3000, 100)          # off-screen by default
    time.sleep(0.6)
    win = find_client_windows()[0]
    print(f"hwnd={win['hwnd']:#x} title={win['title']!r} client={win['client']} origin={win['origin']}")
    print('muted sessions:', mute(win['pid']))

    # --- mouse-safety check *after* the window exists (and after moving it) ---
    time.sleep(0.8)
    pos_after, clip_after = cursor_pos(), clip_rect()
    print(f'user cursor after : {pos_after} clip={clip_after}')
    if pos_after != pos_before:
        print('WARNING: VCMI moved the user cursor!')
    if clip_after != clip_before and clip_after[2] - clip_after[0] < 10000:
        print('WARNING: VCMI confined/clipped the cursor!')

    # --- off-screen observability: engine screenshot via F3 ---
    if '--f3' in sys.argv:
        post_key(win['hwnd'], VK_F3)
        time.sleep(1.5)
        shots_dir = os.path.join(os.path.expanduser('~'), 'Documents', 'My Games', 'vcmi', 'Screenshots')
        files = sorted(os.listdir(shots_dir)) if os.path.isdir(shots_dir) else []
        print('screenshot dir:', shots_dir, '->', files[-3:] if files else 'EMPTY')
    if '--shot' in sys.argv:
        shot()
    print('READY')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
