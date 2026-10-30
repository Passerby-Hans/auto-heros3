# -*- coding: utf-8 -*-
"""Capture the VCMI client window while it sits *off-screen*.

Verified 2026-10-21 on VCMI 1.7.5 (SDL2/OpenGL, window at x=-2989):
`PrintWindow(hwnd, PW_RENDERFULLCONTENT)` DOES return the real frame — no focus
needed, no window visibility needed.  Two gotchas handled here:

  * DPI awareness: without `SetProcessDPIAware()` Windows virtualises the window
    to 2/3 size (1024x768 -> 682x512); we force awareness and return physical px.
  * PrintWindow paints the *whole window* (title bar included) into the bitmap,
    so the client area must be cropped out by the client-vs-window offset.

`mss` capture is kept as a fallback for the on-screen dev mode.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys

user32 = ctypes.WinDLL('user32', use_last_error=True)
gdi32 = ctypes.WinDLL('gdi32')
user32.SetProcessDPIAware()

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)


def find_window(title_prefix='VCMI'):
    """First visible top-level window whose title starts with `title_prefix`."""
    out = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value.startswith(title_prefix):
            out.append((hwnd, buf.value))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def client_geometry(hwnd):
    """(client_w, client_h, client_screen_x, client_screen_y)."""
    cr = wt.RECT(); user32.GetClientRect(hwnd, ctypes.byref(cr))
    pt = wt.POINT(0, 0); user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return cr.right - cr.left, cr.bottom - cr.top, pt.x, pt.y


def grab(hwnd, pw_flag=2):
    """PrintWindow capture cropped to the client area (works off-screen)."""
    import numpy as np
    from PIL import Image

    wr = wt.RECT(); user32.GetWindowRect(hwnd, ctypes.byref(wr))
    ww, wh = wr.right - wr.left, wr.bottom - wr.top
    cw, ch, cx, cy = client_geometry(hwnd)
    off_x, off_y = cx - wr.left, cy - wr.top

    hdc = user32.GetDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, ww, wh)
    gdi32.SelectObject(mem, bmp)
    try:
        if not user32.PrintWindow(hwnd, mem, pw_flag):
            if not user32.PrintWindow(hwnd, mem, 0):
                raise OSError('PrintWindow failed')
        buf = ctypes.create_string_buffer(ww * wh * 4)
        gdi32.GetBitmapBits(bmp, ww * wh * 4, buf)
        arr = np.frombuffer(buf.raw, dtype=np.uint8).reshape(wh, ww, 4)
        rgb = arr[:, :, [2, 1, 0]]
        img = Image.fromarray(rgb, 'RGB').crop((off_x, off_y, off_x + cw, off_y + ch))
        return img
    finally:
        gdi32.DeleteObject(bmp); gdi32.DeleteDC(mem); user32.ReleaseDC(hwnd, hdc)


def grab_screen(hwnd):
    """mss capture of the window region — only meaningful when it is on-screen."""
    import mss
    from PIL import Image
    cw, ch, cx, cy = client_geometry(hwnd)
    with mss.mss() as sct:
        img = sct.grab({'left': cx, 'top': cy, 'width': cw, 'height': ch})
    return Image.frombytes('RGB', (img.width, img.height), img.rgb)


def liveness(hwnd, interval=1.0):
    """Pixel-diff between two captures; 0 means the render loop is frozen."""
    import numpy as np
    a = np.asarray(grab(hwnd).convert('L')).astype(int)
    import time; time.sleep(interval)
    b = np.asarray(grab(hwnd).convert('L')).astype(int)
    return int((np.abs(a - b) > 8).sum())


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    wins = find_window()
    if not wins:
        print('no VCMI window'); raise SystemExit(1)
    hwnd, title = wins[0]
    print(f'hwnd={hwnd:#x} title={title!r} client={client_geometry(hwnd)[:2]}')
    print('liveness:', liveness(hwnd))
    out = sys.argv[1] if len(sys.argv) > 1 else 'var/shots/vcmi.png'
    grab(hwnd).save(out)
    print('saved', out)
