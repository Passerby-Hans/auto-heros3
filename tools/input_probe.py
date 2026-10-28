# -*- coding: utf-8 -*-
"""Probe whether synthetic input actually reaches / is consumed by H3.

Why: "the click did nothing" has three very different causes and this tool tells
them apart in one shot (see doc/findings/input-model.md §2):

  1. WndProc never saw it        -> posted message lost (wrong hwnd?)
  2. enqueued but not consumed   -> the screen's input loop ignores mouse events
  3. enqueued + consumed         -> the game saw the click and *rejected* it
                                    (hit-test / game rule / wrong target)

Instruments
  * event queue: obj=[0x826d38], write idx +0x83c, read idx +0x838,
    slot = obj+0x38+(i&0x3f)*0x20  (type@+0, x@+4, y@+8)
    type: 4=move 8=left-down 0x10=left-up 0x20=right-down 0x40=right-up
  * WndProc side effect: H3 calls SetCapture on left-down; readable cross-process
    via GetGUIThreadInfo(tid).hwndCapture
  * activation flag 0x807f28 (WM_ACTIVATE), game hwnd global [0x826f00]

Usage:
    python -X utf8 tools/input_probe.py [x y]      # default 400 300
"""
from __future__ import annotations
import ctypes
import ctypes.wintypes as wt
import struct
import sys
import time

sys.path.insert(0, '.')
from agent.h3.mem import Process
from agent.h3 import lab
from agent.h3.vcursor import set_pos
from agent.h3.binput import post, WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, lparam

user32 = ctypes.WinDLL('user32', use_last_error=True)
OBJ_PTR = 0x826D38
W_IDX = 0x83C
R_IDX = 0x838
HWND_GLOBAL = 0x826F00
ACTIVE_FLAG = 0x807F28


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [('cbSize', wt.DWORD), ('flags', wt.DWORD), ('hwndActive', wt.HWND),
                ('hwndFocus', wt.HWND), ('hwndCapture', wt.HWND), ('hwndMenuOwner', wt.HWND),
                ('hwndMoveSize', wt.HWND), ('hwndCaret', wt.HWND), ('rcCaret', wt.RECT)]


def capture_of(hwnd):
    tid = user32.GetWindowThreadProcessId(hwnd, None)
    gti = GUITHREADINFO(); gti.cbSize = ctypes.sizeof(GUITHREADINFO)
    user32.GetGUIThreadInfo(tid, ctypes.byref(gti))
    return gti.hwndCapture or 0


def indices(pr):
    o = struct.unpack('<I', pr.read(OBJ_PTR, 4))[0]
    if not (0x01000000 <= o < 0x10000000):
        return None, 0, 0
    w = struct.unpack('<I', pr.read(o + W_IDX, 4))[0]
    r = struct.unpack('<I', pr.read(o + R_IDX, 4))[0]
    return o, w, r


def events(pr, o, count=6):
    _, w, _ = indices(pr)
    out = []
    for i in range(max(0, w - count), w):
        slot = o + 0x38 + (i & 0x3f) * 0x20
        d = pr.try_read(slot, 0x18)
        if not d:
            continue
        typ, x, y = struct.unpack('<3i', d[:12])
        out.append((i, typ, x, y))
    return out


def main(x=400, y=300):
    pr, hw = Process.attach(), lab.hwnd()
    print(f'game hwnd={hw:#x}  global hwnd={struct.unpack("<I", pr.read(HWND_GLOBAL, 4))[0]:#x}')
    print(f'isActive byte = {pr.read(ACTIVE_FLAG, 1)[0]}')
    o, w, r = indices(pr)
    if o is None:
        print('event queue object not found — is the game at the main menu?')
    else:
        print(f'event queue obj={o:#x} write={w} read={r} pending={w - r}')

    print(f'\n[1] cursor -> ({x},{y}) + WM_MOUSEMOVE')
    set_pos(pr, x, y)
    post(hw, WM_MOUSEMOVE, 0, lparam(x, y))
    time.sleep(0.4)
    if o:
        print('    queue:', indices(pr))

    print('[2] WM_LBUTTONDOWN (H3 replies with SetCapture if its WndProc handled it)')
    post(hw, WM_LBUTTONDOWN, 1, lparam(x, y))
    time.sleep(0.4)
    print(f'    capture={capture_of(hw):#x}  queue={indices(pr)}')

    print('[3] WM_LBUTTONUP')
    post(hw, WM_LBUTTONUP, 0, lparam(x, y))
    time.sleep(0.6)
    print(f'    capture={capture_of(hw):#x}  queue={indices(pr)}')

    if o:
        print('\nlast events (idx, type, x, y):')
        for e in events(pr, o, 8):
            print('   ', e)
    print('\nverdict guide:')
    print('  write idx did not advance      -> message never reached the WndProc')
    print('  write advanced, read lagged    -> enqueued but not consumed by this screen')
    print('  write and read advanced        -> consumed; the game rejected the click itself')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    args = [int(a) for a in sys.argv[1:3]]
    main(*(args if len(args) == 2 else (400, 300)))
