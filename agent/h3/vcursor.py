# -*- coding: utf-8 -*-
"""Virtual cursor: make the game (and cnc-ddraw) read OUR cursor position
instead of the real Windows mouse.

Why: H3/cnc-ddraw derive the in-game cursor from GetCursorPos (real mouse).
With the window on-screen the user's mouse interferes; off-screen the game
never sees the cursor inside and drops input. Virtualizing GetCursorPos via
an IAT redirect to an injected stub solves both: the agent writes the
virtual position, posts messages, and the real mouse becomes irrelevant.

Injection site: the dead CD-check function body at 0x5bb270 (already
stubbed by patch_nocd) provides a ~0x490-byte code cave in .text.
"""
from __future__ import annotations
import struct

CAVE = 0x5bb280            # inside dead CD-check function (after entry stub)
VIRT = 0x5bb2f0            # 8 bytes: virtual x, y (i32 each)
HWND_SLOT = 0x5bb2f8       # 4 bytes: our hwnd (for fg-stub)
FG_STUB = 0x5bb2a0         # GetForegroundWindow stub: mov eax,[HWND_SLOT]; ret
EXE_IAT_GETCURSORPOS = 0x83fe3c
EXE_IAT_GETFGWND = 0x83fe30
DDRAW_IAT_GETCURSORPOS = 0x1003e384
DDRAW_IAT_GETFGWND = 0x1003e34c
DDRAW_PREFERRED = 0x10000000


def _stub_code() -> bytes:
    """x86 stdcall GetCursorPos(LPPOINT) -> BOOL reading VIRT."""
    code = b''
    code += b'\xa1' + struct.pack('<I', VIRT)            # mov eax,[VIRT]
    code += b'\x8b\x4c\x24\x04'                          # mov ecx,[esp+4]
    code += b'\x89\x01'                                  # mov [ecx],eax
    code += b'\xa1' + struct.pack('<I', VIRT + 4)        # mov eax,[VIRT+4]
    code += b'\x89\x41\x04'                              # mov [ecx+4],eax
    code += b'\xb8\x01\x00\x00\x00'                      # mov eax,1
    code += b'\xc2\x04\x00'                              # ret 4
    return code


def install(proc, hwnd: int | None = None) -> dict:
    """Inject stubs + redirect IATs. Idempotent. hwnd enables the
    always-foreground stub (kills render-throttle when unfocused)."""
    stub = _stub_code()
    proc.write_x(CAVE, stub)
    proc.write_x(VIRT, struct.pack('<ii', 400, 300))
    dd_base = proc.modules['ddraw.dll'].base
    out = {}
    for name, slot in [('exe:GetCursorPos', EXE_IAT_GETCURSORPOS),
                       ('ddraw:GetCursorPos', DDRAW_IAT_GETCURSORPOS + (dd_base - DDRAW_PREFERRED))]:
        proc.write_x(slot, struct.pack('<I', CAVE))
        out[name] = f'{slot:#x} -> cave'
    if hwnd is not None:
        proc.write_x(HWND_SLOT, struct.pack('<I', hwnd))
        fg_stub = bytes([0xA1]) + struct.pack('<I', HWND_SLOT) + bytes([0xC3])
        proc.write_x(FG_STUB, fg_stub)
        for name, slot in [('exe:GetForegroundWindow', EXE_IAT_GETFGWND),
                           ('ddraw:GetForegroundWindow', DDRAW_IAT_GETFGWND + (dd_base - DDRAW_PREFERRED))]:
            proc.write_x(slot, struct.pack('<I', FG_STUB))
            out[name] = f'{slot:#x} -> fg-stub'
    return out


def set_pos(proc, x: int, y: int):
    proc.write(VIRT, struct.pack('<ii', int(x), int(y)))


def vclick(proc, hwnd, x: int, y: int, button='left', delay=0.06, hold=0.08):
    """Deterministic click: virtual cursor + posted messages, verified 1-2px."""
    from agent.h3.binput import post, WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, \
        WM_RBUTTONDOWN, WM_RBUTTONUP, lparam, MK_LBUTTON
    import time
    set_pos(proc, x, y)
    post(hwnd, WM_MOUSEMOVE, 0, lparam(x, y))
    time.sleep(delay)
    if button == 'left':
        post(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam(x, y))
        time.sleep(hold)
        post(hwnd, WM_LBUTTONUP, 0, lparam(x, y))
    else:
        post(hwnd, WM_RBUTTONDOWN, 1, lparam(x, y))
        time.sleep(hold)
        post(hwnd, WM_RBUTTONUP, 0, lparam(x, y))
    time.sleep(delay)


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    from agent.h3.mem import Process
    from agent.h3.binput import post, WM_MOUSEMOVE, lparam
    from agent.h3.capture import h3_hwnds, grab
    import time
    p = Process.attach()
    print(install(p))
    hwnd = h3_hwnds()[0][0]
    set_pos(p, 400, 200)
    post(hwnd, WM_MOUSEMOVE, 0, lparam(400, 200))
    time.sleep(0.8)
    grab(hwnd).save('var/shots/vcursor_test.png')
    print('virtual (400,200) set; check var/shots/vcursor_test.png')
