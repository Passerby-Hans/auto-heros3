# -*- coding: utf-8 -*-
"""Session bootstrap check: game alive, decoupling active, current screen."""
import sys
sys.stdout.reconfigure(encoding='utf-8')
import ctypes, ctypes.wintypes as wt, struct
from agent.h3.mem import Process, find_pids
from agent.h3.capture import h3_hwnds
from agent.h3.vision import ocr_screen
from agent.h3.binput import foreground_hwnd


def main():
    pids = find_pids()
    if not pids:
        print('game NOT running'); return
    print('game pid:', pids[0])
    hwnd = h3_hwnds()[0][0]
    print('background (good):', foreground_hwnd() != hwnd)
    user32 = ctypes.WinDLL('user32')
    r = wt.RECT(); user32.GetClipCursor(ctypes.byref(r))
    print('clip:', (r.left, r.top, r.right, r.bottom))
    p = Process.attach()
    dd = p.modules['ddraw.dll']
    slot = 0x1003e35c + (dd.base - 0x10000000)
    val = struct.unpack('<I', p.read(slot, 4))[0]
    ok = 0x401000 <= val < 0x7c0000
    print(f'ddraw ClipCursor IAT -> {val:#x}', 'PATCHED' if ok else 'NOT PATCHED - run launch/decouple!')
    items = ocr_screen(hwnd)
    print('screen:', ' | '.join(t for t, _, _, _ in items))


if __name__ == '__main__':
    main()
