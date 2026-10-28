# -*- coding: utf-8 -*-
"""Interactive probing helpers for live H3 sessions (session 13).

Purpose: the low-level modules (capture/binput/vcursor) are primitives; probing a
screen usually needs them combined — hover-then-read-status-bar, click-until-OCR,
save a shot, split the posted lParam from the virtual cursor, etc.

Design notes (verified 2026-10-21, see doc/findings/input-model.md):
- The game NEVER reads the mouse position from the message lParam; it polls
  GetCursorPos every frame. So every synthetic click must go through
  vcursor.set_pos() (vclick does this).
- H3's status bar (bottom strip, y≈546..572) is the cheapest "what is under the
  cursor / what would this click do" channel.  Use status_bar_text().
- Nested dialogs (e.g. the build confirmation window) ignore mouse messages; the
  keyboard works there (see tap_until / HANDOVER §七.8).
"""
from __future__ import annotations
import ctypes
import time

from agent.h3.mem import Process
from agent.h3.capture import h3_hwnds, grab, liveness
from agent.h3.vcursor import set_pos, vclick
from agent.h3.binput import (post, tap, WM_MOUSEMOVE, WM_LBUTTONDOWN,
                             WM_LBUTTONUP, WM_RBUTTONDOWN, WM_RBUTTONUP,
                             lparam, MK_LBUTTON)

SHOTS = 'var/shots'
_user32 = ctypes.WinDLL('user32', use_last_error=True)

# SMTO_ABORTIFHUNG|SMTO_BLOCK — never wedge the agent on a busy game thread.
_SM_FLAGS = 0x0002 | 0x0001


def _send(hw: int, msg: int, wparam: int, lparam_: int, timeout: int = 1500) -> bool:
    """Deliver a message straight into the game's WndProc (bypasses its queue).

    Verified 2026-10-21: on the adventure map / in combat H3 sometimes stops
    draining *posted* mouse messages (the event ring at [0x826d38] stays frozen)
    while posted keyboard still works.  SendMessage is processed immediately and
    makes mouse input reliable again.  Trade-off: it is synchronous, so always
    use SendMessageTimeout with a short timeout.
    """
    res = ctypes.c_size_t()
    ok = _user32.SendMessageTimeoutW(hw, msg, wparam, lparam_, _SM_FLAGS,
                                     timeout, ctypes.byref(res))
    return bool(ok)


def hwnd() -> int:
    return h3_hwnds()[0][0]


def proc() -> Process:
    return Process.attach()


def snap(name: str, crop=None, scale: float = 1, hw=None):
    """Save a PrintWindow shot to var/shots/<name>.png; return the path."""
    img = grab(hw or hwnd())
    if crop:
        img = img.crop(crop)
    if scale != 1:
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    import os
    os.makedirs(SHOTS, exist_ok=True)
    path = f'{SHOTS}/{name}.png'
    img.save(path)
    return path


def hover(x: int, y: int, wait: float = 0.5, pr=None, hw=None):
    """Park the virtual cursor and post a move, then let the game settle."""
    post(hw or hwnd(), WM_MOUSEMOVE, 0, lparam(x, y))
    set_pos(pr or proc(), x, y)
    time.sleep(wait)


def shover(x: int, y: int, wait: float = 0.5, pr=None, hw=None):
    """Hover via SendMessage — reliable variant (see _send)."""
    hw = hw or hwnd()
    set_pos(pr or proc(), x, y)
    _send(hw, WM_MOUSEMOVE, 0, lparam(x, y))
    time.sleep(wait)


def sclick(x: int, y: int, hold: float = 0.10, wait: float = 0.6, pr=None, hw=None,
           button: str = 'left'):
    """Click via SendMessage — reliable variant (see _send)."""
    hw = hw or hwnd()
    pr = pr or proc()
    set_pos(pr, x, y)
    _send(hw, WM_MOUSEMOVE, 0, lparam(x, y))
    time.sleep(0.10)
    if button == 'left':
        _send(hw, WM_LBUTTONDOWN, MK_LBUTTON, lparam(x, y))
        time.sleep(hold)
        _send(hw, WM_LBUTTONUP, 0, lparam(x, y))
    else:
        _send(hw, WM_RBUTTONDOWN, 1, lparam(x, y))
        time.sleep(hold)
        _send(hw, WM_RBUTTONUP, 0, lparam(x, y))
    time.sleep(wait)


def click(x: int, y: int, hold: float = 0.08, wait: float = 0.5, pr=None, hw=None):
    """Standard background click: virtual cursor + posted down/up."""
    vclick(pr or proc(), hw or hwnd(), x, y, hold=hold)
    time.sleep(wait)


def split_click(lparam_xy, virt_xy, hold=0.08, wait=0.8, moves=2, hw=None):
    """Post messages with one position while the virtual cursor sits elsewhere.

    Only useful for A/B experiments on which coordinate source a screen uses
    (answer, verified: GetCursorPos/virtual only).
    """
    lx, ly = lparam_xy
    vx, vy = virt_xy
    hw = hw or hwnd()
    set_pos(proc(), vx, vy)
    for _ in range(moves):
        post(hw, WM_MOUSEMOVE, 0, lparam(lx, ly))
        time.sleep(0.12)
    post(hw, WM_LBUTTONDOWN, MK_LBUTTON, lparam(lx, ly))
    time.sleep(hold)
    post(hw, WM_LBUTTONUP, 0, lparam(lx, ly))
    time.sleep(wait)


def right_click(x: int, y: int, wait: float = 0.6):
    hover(x, y, 0.3)
    hw = hwnd()
    post(hw, WM_RBUTTONDOWN, 1, lparam(x, y))
    time.sleep(0.08)
    post(hw, WM_RBUTTONUP, 0, lparam(x, y))
    time.sleep(wait)


def status_bar_text(hw=None, box=(150, 546, 800, 572)) -> str:
    """OCR the status/rollover bar — H3's cheapest state probe."""
    from agent.h3.vision import ocr_image
    import numpy as np
    img = grab(hw or hwnd()).crop(box)
    items = ocr_image(np.asarray(img.convert('RGB')))
    return ''.join(t for t, _, _, _ in items)


def ocr_all(hw=None):
    from agent.h3.vision import ocr_screen
    return [(t, x, y) for t, x, y, _ in ocr_screen(hw or hwnd())]


def screen_text(hw=None) -> str:
    return ' '.join(t for t, _, _ in ocr_all(hw))


def click_until(x: int, y: int, keyword: str, tries: int = 5, wait: float = 1.2,
                hold: float = 0.08) -> int:
    """Hover+click until OCR shows `keyword`. H3 swallows the first click after a
    screen change ("首击吞噬"), so callers should retry. Returns attempts (-1 fail)."""
    for i in range(tries):
        if keyword in screen_text():
            return i
        hover(x, y, 0.35)
        click(x, y, hold=hold, wait=wait)
    return tries if keyword in screen_text() else -1


def tap_until(vk: int, keyword: str, tries: int = 4, wait: float = 0.9) -> bool:
    """Keyboard workaround for nested modal dialogs (build confirmation etc.)."""
    for _ in range(tries):
        if keyword in screen_text():
            return True
        tap(hwnd(), vk, hold=0.10)
        time.sleep(wait)
    return keyword in screen_text()


def alive() -> int:
    return liveness(hwnd())
