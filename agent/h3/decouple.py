# -*- coding: utf-8 -*-
"""Cursor decoupling: neutralize cnc-ddraw's real-cursor manipulation.

The game (via cnc-ddraw + noactivateapp=true) keeps running while unfocused,
but cnc-ddraw clips/warps the REAL Windows cursor (user reports 'mouse
hijacked'). Fix: overwrite ddraw.dll's IAT entries for ClipCursor and
SetCursorPos with addresses of `ret N` gadgets inside Heroes3.exe code, so
those calls become no-ops in the game process. GetCursorPos is left intact
(game uses it for hover tracking - harmless).

Run after each game launch:  uv run python -m agent.h3.decouple
"""
from __future__ import annotations
import ctypes, ctypes.wintypes as wt, struct, sys, time

user32 = ctypes.WinDLL('user32')
user32.SetProcessDPIAware()

DDRAW_PREFERRED_BASE = 0x10000000
# IAT slots (VA in ddraw.dll image, from static analysis of D:\Heros3\ddraw.dll)
IAT = {'ClipCursor': 0x1003e35c, 'SetCursorPos': 0x1003e380}
# stdcall arg counts -> required ret-N gadget
ARGS = {'ClipCursor': 1, 'SetCursorPos': 2}


def release_clip() -> bool:
    return bool(user32.ClipCursor(None))


def clip_rect():
    r = wt.RECT()
    user32.GetClipCursor(ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def find_gadget(proc, code: bytes) -> int:
    """Find `code` (e.g. b'\xc2\x04\x00' = ret 4) inside game .text, return VA."""
    exe = proc.exe
    blob = proc.read(exe.base, min(exe.size, 0x3b0000))
    off = blob.find(code)
    while off != -1:
        va = exe.base + off
        # sanity: page-readable
        if proc.try_read(va, 3):
            return va
        off = blob.find(code, off + 1)
    raise RuntimeError(f'gadget {code.hex()} not found in game image')


def decouple():
    from agent.h3.mem import Process
    p = Process.attach()
    dd = p.modules.get('ddraw.dll')
    if dd is None:
        print('ddraw.dll not loaded in game process'); return False
    delta = DDRAW_PREFERRED_BASE and (dd.base - DDRAW_PREFERRED_BASE)
    patched = {}
    for name, slot_va in IAT.items():
        runtime_slot = slot_va + delta
        cur = struct.unpack('<I', p.read(runtime_slot, 4))[0]
        nargs = ARGS[name]
        gadget = find_gadget(p, bytes([0xC2, nargs, 0x00]))
        p.write_x(runtime_slot, struct.pack('<I', gadget))
        patched[name] = (hex(cur), hex(gadget))
        print(f'{name}: IAT@{runtime_slot:#x} {hex(cur)} -> ret {nargs} @{gadget:#x}')
    # free any existing clip
    release_clip()
    print('clip released:', clip_rect())
    return True


def monitor(seconds: float = 6.0):
    """Watch the clip: report if anything re-clips away from the virtual screen."""
    vs = (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
          user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))
    # virtual-screen rect is not always (vx,vy,vx+w,vy+h) for clip compare; just log
    t0 = time.time()
    last = clip_rect()
    while time.time() - t0 < seconds:
        time.sleep(0.5)
        r = clip_rect()
        if r != last:
            print(f'clip changed: {last} -> {r}')
            last = r
    print('final clip:', last, '| virtual screen:', vs)


if __name__ == '__main__':
    if decouple():
        monitor(6.0)
