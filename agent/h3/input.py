# -*- coding: utf-8 -*-
"""SendInput-based mouse/keyboard injection for Heroes3 (cnc-ddraw window).

Verified environment: virtual desktop 7680x2160 (multi-monitor), game client
800x600 at screen (528,1280). SendInput ABSOLUTE coords are normalized to the
VIRTUAL DESKTOP (flag MOUSEEVENTF_VIRTUALDESKTOP) so multi-monitor works.
"""
from __future__ import annotations
import ctypes, ctypes.wintypes as wt, time

user32 = ctypes.WinDLL('user32', use_last_error=True)
user32.SetProcessDPIAware()

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESKTOP = 0x4000
KEYEVENTF_KEYUP = 0x0002
VK_MENU = 0x12  # Alt (for foreground-lock workaround)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("union", _U)]


def _send(*inputs: INPUT) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    n = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if n != len(inputs):
        raise OSError(f"SendInput sent {n}/{len(inputs)} err={ctypes.get_last_error()}")


def _mouse(flags: int, nx: int | None = None, ny: int | None = None) -> INPUT:
    mi = MOUSEINPUT(0, 0, 0, flags, 0, None)
    if nx is not None:
        mi.dx, mi.dy = nx, ny
    return INPUT(INPUT_MOUSE, _U(mi=mi))


def _key(vk: int, up=False) -> INPUT:
    return INPUT(INPUT_KEYBOARD, _U(ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, None)))


def virt_size() -> tuple[int, int]:
    return (user32.GetSystemMetrics(76 or 78), user32.GetSystemMetrics(77))  # SM_XVIRTUALSCREEN hack below


def virtual_screen() -> tuple[int, int, int, int]:
    """(x, y, w, h) of the virtual desktop."""
    return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
            user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))


def _abs(screen_x: int, screen_y: int) -> tuple[int, int]:
    vx, vy, vw, vh = virtual_screen()
    return (screen_x - vx) * 65535 // (vw - 1), (screen_y - vy) * 65535 // (vh - 1)


def focus(hwnd: int) -> None:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # foreground-lock workaround: a synthetic Alt keypress lets this process
    # take foreground even if the user last touched another window
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.SetForegroundWindow(hwnd)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.15)


def move_screen(x: int, y: int) -> None:
    nx, ny = _abs(x, y)
    _send(_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESKTOP, nx, ny))


def cursor_pos() -> tuple[int, int]:
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def click_screen(x: int, y: int, button="left", delay=0.04) -> None:
    move_screen(x, y)
    time.sleep(delay)
    dn = MOUSEEVENTF_LEFTDOWN if button == "left" else MOUSEEVENTF_RIGHTDOWN
    up = MOUSEEVENTF_LEFTUP if button == "left" else MOUSEEVENTF_RIGHTUP
    _send(_mouse(dn))
    time.sleep(0.03)
    _send(_mouse(up))


def click_client(hwnd: int, cx: int, cy: int, **kw) -> None:
    """Click at client-area coords (0..800, 0..600) of the game window."""
    import ctypes as ct
    pt = wt.POINT(cx, cy)
    if not user32.ClientToScreen(hwnd, ct.byref(pt)):
        raise OSError("ClientToScreen failed")
    click_screen(pt.x, pt.y, **kw)


def tap(vk: int, hold=0.03) -> None:
    _send(_key(vk))
    time.sleep(hold)
    _send(_key(vk, up=True))


if __name__ == '__main__':
    from agent.h3.capture import h3_hwnds, client_box, grab
    import sys
    wins = h3_hwnds()
    if not wins:
        print("game not running"); sys.exit(1)
    hwnd = wins[0][0]
    focus(hwnd)
    x, y, w, h = client_box(hwnd)
    print(f"client box on screen: ({x},{y}) {w}x{h}; virtual {virtual_screen()}")
    # hover demo: move to client center, verify cursor
    cx, cy = x + w // 2, y + h // 2
    move_screen(cx, cy)
    time.sleep(0.2)
    gx, gy = cursor_pos()
    print(f"moved to ({cx},{cy}), GetCursorPos=({gx},{gy}) -> {'OK' if abs(gx-cx)<=2 and abs(gy-cy)<=2 else 'MISMATCH'}")
    print("no click performed (safe demo); use click_client(hwnd, x, y) for real clicks")
