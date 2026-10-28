# -*- coding: utf-8 -*-
"""Game window discovery + screenshot capture for Heroes3.

Window facts (verified live, 2026-10-19):
  class = '魔法门之英雄无敌III', title contains '3.1 Jul 18 2000'
  800x600 client area (cnc-ddraw windowed)
We locate the window by PID (robust vs. the localized class name).
"""
from __future__ import annotations
import ctypes, ctypes.wintypes as wt, sys, time
from pathlib import Path

user32 = ctypes.WinDLL('user32', use_last_error=True)
user32.SetProcessDPIAware()
gdi32 = ctypes.WinDLL('gdi32')

from agent.h3.mem import find_pids, EXE_NAME

CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)


def h3_hwnds() -> list[tuple[int, int]]:
    """[(hwnd, pid)] for visible top-level windows of the game process."""
    pids = set(find_pids(EXE_NAME))
    out = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            out.append((hwnd, pid.value))
        return True

    user32.EnumWindows(CB(cb), 0)
    return out


def client_box(hwnd: int) -> tuple[int, int, int, int]:
    """Client area in screen coords (left, top, w, h)."""
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    return pt.x, pt.y, r.right - r.left, r.bottom - r.top


def window_box(hwnd: int) -> tuple[int, int, int, int]:
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def grab(hwnd: int):
    """Capture the window content DIRECTLY via PrintWindow (PW_RENDERFULLCONTENT)
    - occlusion-proof, position-independent. Crops to the client area."""
    from PIL import Image
    wr = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wr))
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    off_x, off_y = pt.x - wr.left, pt.y - wr.top
    cr = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(cr))
    w, h = cr.right, cr.bottom
    hdc_win = user32.GetDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_win, wr.right - wr.left, wr.bottom - wr.top)
    gdi32.SelectObject(hdc_mem, hbmp)
    try:
        ok = user32.PrintWindow(hwnd, hdc_mem, 2) or user32.PrintWindow(hwnd, hdc_mem, 0)
        if not ok:
            raise OSError('PrintWindow failed')
        ww, wh = wr.right - wr.left, wr.bottom - wr.top
        buf = ctypes.create_string_buffer(ww * wh * 4)
        gdi32.GetBitmapBits(hbmp, ww * wh * 4, buf)
        # GetBitmapBits returns BGRA (DIB byte order) - swap to RGB
        import numpy as np
        arr = np.frombuffer(buf.raw, dtype=np.uint8).reshape(wh, ww, 4)
        rgb = arr[:, :, [2, 1, 0, 3]]
        from PIL import Image
        img = Image.frombytes('RGBX', (ww, wh), rgb.tobytes()).convert('RGB')
        return img.crop((off_x, off_y, off_x + w, off_y + h))
    finally:
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)


def grab_screen(hwnd: int):
    """Screen-region capture via mss (sees whatever occludes the window)."""
    import mss
    x, y, w, h = client_box(hwnd)
    with mss.mss() as sct:
        img = sct.grab({'left': x, 'top': y, 'width': w, 'height': h})
    from PIL import Image
    return Image.frombytes('RGB', (w, h), img.rgb)


if __name__ == '__main__':
    wins = h3_hwnds()
    if not wins:
        print('game window not found'); sys.exit(1)
    hwnd = wins[0][0]
    print(f'hwnd={hwnd:#x} title={title(hwnd)!r}')
    print(f'client={client_box(hwnd)} window={window_box(hwnd)}')
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('doc/findings/shot.png')
    out.parent.mkdir(parents=True, exist_ok=True)
    img = grab(hwnd)
    img.save(out)
    print(f'saved {out} ({img.size[0]}x{img.size[1]})')


def liveness(hwnd: int, interval: float = 1.2) -> int:
    """Frame-liveness probe: pixel diff between two grabs. 0 = frozen render."""
    import numpy as np
    a = np.asarray(grab(hwnd).convert('RGB')).astype(int)
    time.sleep(interval)
    b = np.asarray(grab(hwnd).convert('RGB')).astype(int)
    return int((np.abs(a - b).sum(axis=2) > 30).sum())


def ensure_alive(hwnd: int, max_retry: int = 3) -> bool:
    """Re-focus (Alt trick) until frames animate again. False if still frozen."""
    from agent.h3 import input as hinput
    for _ in range(max_retry):
        if liveness(hwnd) > 0:
            return True
        hinput.focus(hwnd)
        time.sleep(1.0)
    return liveness(hwnd) > 0
