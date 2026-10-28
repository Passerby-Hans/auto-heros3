# -*- coding: utf-8 -*-
"""Dump WOW64 (x86) thread contexts of Heroes3: EIP + module attribution +
crude stack scan for return addresses inside the game exe.
Correct x86 CONTEXT layout (716 bytes) - the truncated version segfaults.
"""
import ctypes, struct, sys
import psutil

k32 = ctypes.WinDLL('kernel32', use_last_error=True)


class WOW64_FLOATING_SAVE_AREA(ctypes.Structure):
    _fields_ = [("ControlWord", ctypes.c_uint32), ("StatusWord", ctypes.c_uint32),
                ("TagWord", ctypes.c_uint32), ("ErrorOffset", ctypes.c_uint32),
                ("ErrorSelector", ctypes.c_uint32), ("DataOffset", ctypes.c_uint32),
                ("DataSelector", ctypes.c_uint32), ("RegisterArea", ctypes.c_uint8 * 80),
                ("Cr0NpxState", ctypes.c_uint32)]


class WOW64_CONTEXT(ctypes.Structure):
    _fields_ = [("ContextFlags", ctypes.c_uint32),
                ("Dr0", ctypes.c_uint32), ("Dr1", ctypes.c_uint32),
                ("Dr2", ctypes.c_uint32), ("Dr3", ctypes.c_uint32),
                ("Dr6", ctypes.c_uint32), ("Dr7", ctypes.c_uint32),
                ("FloatSave", WOW64_FLOATING_SAVE_AREA),
                ("SegGs", ctypes.c_uint32), ("SegFs", ctypes.c_uint32),
                ("SegEs", ctypes.c_uint32), ("SegDs", ctypes.c_uint32),
                ("Edi", ctypes.c_uint32), ("Esi", ctypes.c_uint32),
                ("Ebx", ctypes.c_uint32), ("Edx", ctypes.c_uint32),
                ("Ecx", ctypes.c_uint32), ("Eax", ctypes.c_uint32),
                ("Ebp", ctypes.c_uint32), ("Eip", ctypes.c_uint32),
                ("SegCs", ctypes.c_uint32), ("EFlags", ctypes.c_uint32),
                ("Esp", ctypes.c_uint32), ("SegSs", ctypes.c_uint32),
                ("ExtendedRegisters", ctypes.c_uint8 * 512)]


assert ctypes.sizeof(WOW64_CONTEXT) == 716, ctypes.sizeof(WOW64_CONTEXT)
CONTEXT_i386 = 0x00010000
CONTEXT_CONTROL = CONTEXT_i386 | 1
THREAD_ALL_NEEDED = 0x0002 | 0x0008 | 0x0800  # SUSPEND_RESUME | GET_CONTEXT | QUERY


def main(pid: int, proc_mem, exe_range, modules):
    lo, hi = exe_range

    def where(va):
        for a, b, n in modules:
            if a <= va < b:
                return f"{n}+{va - a:#x}"
        return "?"

    seen_exe = False
    for t in psutil.Process(pid).threads():
        h = k32.OpenThread(THREAD_ALL_NEEDED, False, t.id)
        if not h:
            print(f"tid={t.id}: open failed err={ctypes.get_last_error()}")
            continue
        try:
            k32.Wow64SuspendThread(h)
            ctx = WOW64_CONTEXT()
            ctx.ContextFlags = CONTEXT_CONTROL
            if not k32.Wow64GetThreadContext(h, ctypes.byref(ctx)):
                print(f"tid={t.id}: GetThreadContext failed err={ctypes.get_last_error()}")
                continue
            rets = []
            try:
                stack = proc_mem.read(ctx.Esp, 0x600)
                for i in range(0, len(stack) - 4, 4):
                    v = struct.unpack_from('<I', stack, i)[0]
                    if lo <= v < hi:
                        rets.append(v)
            except OSError:
                pass
            tag = ' <== MAIN? ' if rets else ''
            print(f"tid={t.id} EIP={ctx.Eip:#010x} [{where(ctx.Eip):40s}] "
                  f"ebp={ctx.Ebp:#010x} exe-frames={[hex(x) for x in rets[:8]]}{tag}")
            if lo <= ctx.Eip < hi or rets:
                seen_exe = True
        finally:
            k32.ResumeThread(h)
            k32.CloseHandle(h)
    return seen_exe


if __name__ == '__main__':
    from agent.h3.mem import Process
    p = Process.attach()
    ex = p.exe
    mods = [(m.base, m.base + m.size, name) for name, m in p.modules.items()]
    main(p.pid, p, (ex.base, ex.base + ex.size), mods)
