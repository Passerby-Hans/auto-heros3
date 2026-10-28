# -*- coding: utf-8 -*-
"""Background input for Heroes3: PostMessage-based mouse/keyboard that does
NOT move the Windows cursor and does NOT require window focus.

Also provides grab_print(): PrintWindow capture that works when the window
is occluded or in background (cnc-ddraw renders via GDI; PW_RENDERFULLCONTENT).

Coordinate note: game client is 800x600 unstretched, so client coords == game
coords; PostMessage lParam takes client coords directly.
"""
from __future__ import annotations
import ctypes, ctypes.wintypes as wt, time

user32 = ctypes.WinDLL('user32', use_last_error=True)
gdi32 = ctypes.WinDLL('gdi32')
user32.SetProcessDPIAware()

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
MK_LBUTTON = 0x0001


def lparam(x: int, y: int) -> int:
    return (y << 16) | (x & 0xFFFF)


def key_lparam(vk: int) -> int:
    scan = user32.MapVirtualKeyW(vk, 0)
    return 1 | (scan << 16)


def post(hwnd: int, msg: int, wparam: int, lparam: int) -> None:
    if not user32.PostMessageW(hwnd, msg, wparam, lparam):
        raise OSError(f"PostMessage {msg:#x} failed err={ctypes.get_last_error()}")


def move(hwnd: int, x: int, y: int) -> None:
    post(hwnd, WM_MOUSEMOVE, 0, lparam(x, y))


def click(hwnd: int, x: int, y: int, button="left", delay=0.05) -> None:
    move(hwnd, x, y)
    time.sleep(delay)
    dn, up = (WM_LBUTTONDOWN, WM_LBUTTONUP) if button == "left" else (WM_RBUTTONDOWN, WM_RBUTTONUP)
    post(hwnd, dn, MK_LBUTTON, lparam(x, y))
    time.sleep(0.04)
    post(hwnd, up, 0, lparam(x, y))


def tap(hwnd: int, vk: int, hold=0.05) -> None:
    post(hwnd, WM_KEYDOWN, vk, key_lparam(vk))
    time.sleep(hold)
    post(hwnd, WM_KEYUP, vk, key_lparam(vk) | (1 << 30) | (1 << 31))


def foreground_hwnd() -> int:
    return user32.GetForegroundWindow()


def grab_print(hwnd: int):
    """Background capture via PrintWindow -> PIL Image (RGB)."""
    from PIL import Image
    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    w, h = r.right, r.bottom
    hdc_win = user32.GetDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    gdi32.SelectObject(hdc_mem, hbmp)
    try:
        # PW_RENDERFULLCONTENT = 2 (captures D3D/layered content)
        ok = user32.PrintWindow(hwnd, hdc_mem, 2)
        if not ok:
            ok = user32.PrintWindow(hwnd, hdc_mem, 0)
        if not ok:
            raise OSError("PrintWindow failed")
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetBitmapBits(hbmp, w * h * 4, buf)
        img = Image.frombytes('RGBX', (w, h), buf.raw)
        return img.convert('RGB')
    finally:
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)


if __name__ == '__main__':
    from agent.h3.capture import h3_hwnds, title
    wins = h3_hwnds()
    if not wins:
        print('game not running'); raise SystemExit(1)
    hwnd = wins[0][0]
    fg = foreground_hwnd()
    print(f'game hwnd={hwnd:#x}, foreground={fg:#x} ({title(fg)!r}), '
          f'game_is_foreground={fg == hwnd}')
    img = grab_print(hwnd)
    img.save('var/shots/bg_print.png')
    print(f'PrintWindow capture ok: {img.size}')
