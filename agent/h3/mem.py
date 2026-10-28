# -*- coding: utf-8 -*-
"""Windows process memory reading for Heroes3.exe (SoD 3.1 CN).

Phase-1 foundation:
- attach by process name, module base discovery (ASLR-aware)
- typed reads, string reads
- AOB signature scan + static-string anchor scan
  (our CN exe has unique layout, so we resolve code addresses at runtime by
   finding instructions that reference known .rdata strings; string RVAs come
   from tools/disasm_scan.py -> doc/findings/exe_xref_db.json)
Read-only: OpenProcess with PROCESS_VM_READ | PROCESS_QUERY_INFORMATION.
"""
from __future__ import annotations
import ctypes, ctypes.wintypes as wt, json, struct, sys
from dataclasses import dataclass
from pathlib import Path

k32 = ctypes.WinDLL('kernel32', use_last_error=True)

PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPPROCESS = 0x2
TH32CS_SNAPMODULE = 0x8
TH32CS_SNAPMODULE32 = 0x10

EXE_NAME = "Heroes3.exe"
STATIC_BASE = 0x400000  # preferred image base of our exe
XREF_DB = Path(__file__).resolve().parents[2] / 'doc' / 'findings' / 'exe_xref_db.json'


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_char * 260)]


class MODULEENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("GlblcntUsage", wt.DWORD), ("ProccntUsage", wt.DWORD), ("modBaseAddr", ctypes.c_void_p),
                ("modBaseSize", wt.DWORD), ("hModule", wt.HMODULE), ("szModule", ctypes.c_char * 256),
                ("szExePath", ctypes.c_char * 260)]


def find_pids(name: str = EXE_NAME) -> list[int]:
    """psutil primary (toolhelp32 Process32First fails with err 24 on this
    machine - some security-software hook); ctypes snapshot as fallback."""
    try:
        import psutil
        return [p.info['pid'] for p in psutil.process_iter(['pid', 'name'])
                if (p.info['name'] or '').lower() == name.lower()]
    except ImportError:
        pass
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    pe = PROCESSENTRY32(); pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
    pids = []
    if k32.Process32First(snap, ctypes.byref(pe)):
        while True:
            if pe.szExeFile.decode('mbcs').lower() == name.lower():
                pids.append(pe.th32ProcessID)
            if not k32.Process32Next(snap, ctypes.byref(pe)):
                break
    k32.CloseHandle(snap)
    return pids


@dataclass
class Module:
    base: int
    size: int
    name: str


class Process:
    def __init__(self, pid: int):
        self.pid = pid
        self.handle = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
        if not self.handle:
            raise OSError(f"OpenProcess({pid}) failed: err={ctypes.get_last_error()}")
        self.modules: dict[str, Module] = {}
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
        me = MODULEENTRY32(); me.dwSize = ctypes.sizeof(MODULEENTRY32)
        if k32.Module32First(snap, ctypes.byref(me)):
            while True:
                m = Module(me.modBaseAddr or 0, me.modBaseSize, me.szModule.decode('mbcs'))
                self.modules[m.name.lower()] = m
                if not k32.Module32Next(snap, ctypes.byref(me)):
                    break
        k32.CloseHandle(snap)

    @classmethod
    def attach(cls, name: str = EXE_NAME) -> "Process":
        pids = find_pids(name)
        if not pids:
            raise RuntimeError(f"{name} not running")
        return cls(pids[0])

    @property
    def exe(self) -> Module:
        return self.modules[EXE_NAME.lower()]

    def read(self, addr: int, size: int) -> bytes:
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t()
        if not k32.ReadProcessMemory(self.handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)):
            raise OSError(f"RPM({addr:#x},{size}) err={ctypes.get_last_error()}")
        return buf.raw[:got.value]

    def try_read(self, addr: int, size: int) -> bytes | None:
        try:
            return self.read(addr, size)
        except OSError:
            return None

    def _ensure_write_handle(self) -> None:
        if getattr(self, '_can_write', False):
            return
        h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_VM_WRITE |
                            PROCESS_VM_OPERATION | PROCESS_QUERY_INFORMATION,
                            False, self.pid)
        if not h:
            raise OSError(f"OpenProcess(for write) failed err={ctypes.get_last_error()}")
        self.handle = h
        self._can_write = True

    def write(self, addr: int, data: bytes) -> None:
        """WriteProcessMemory; upgrades handle to VM_WRITE|VM_OPERATION if needed."""
        self._ensure_write_handle()
        buf = ctypes.create_string_buffer(data, len(data))
        got = ctypes.c_size_t()
        if not k32.WriteProcessMemory(self.handle, ctypes.c_void_p(addr), buf,
                                      len(data), ctypes.byref(got)):
            raise OSError(f"WPM({addr:#x},{len(data)}) err={ctypes.get_last_error()}")
        if got.value != len(data):
            raise OSError(f"WPM partial write {got.value}/{len(data)}")

    def write_x(self, addr: int, data: bytes) -> None:
        """write() with temporary PAGE_READWRITE (for IAT / rdata pages)."""
        PAGE_READWRITE = 0x04
        self._ensure_write_handle()
        old = ctypes.c_ulong()
        if not k32.VirtualProtectEx(self.handle, ctypes.c_void_p(addr), len(data),
                                    PAGE_READWRITE, ctypes.byref(old)):
            raise OSError(f"VirtualProtectEx({addr:#x}) err={ctypes.get_last_error()}")
        try:
            self.write(addr, data)
        finally:
            k32.VirtualProtectEx(self.handle, ctypes.c_void_p(addr), len(data),
                                 old.value, ctypes.byref(old))

    def u32(self, addr: int) -> int:
        return struct.unpack('<I', self.read(addr, 4))[0]

    def i32(self, addr: int) -> int:
        return struct.unpack('<i', self.read(addr, 4))[0]

    def u8(self, addr: int) -> int:
        return self.read(addr, 1)[0]

    def cstr(self, addr: int, maxn=256) -> str:
        out = b''
        while len(out) < maxn:
            chunk = self.read(addr + len(out), 32)
            if b'\x00' in chunk:
                out += chunk[:chunk.index(b'\x00')]
                break
            out += chunk
        return out.decode('gbk', 'replace')

    def va(self, static_va: int) -> int:
        """Map a static (preferred-base) VA from our offline analysis to runtime."""
        return self.exe.base + (static_va - STATIC_BASE)

    def scan(self, pattern: bytes, mask: str | None = None,
             module: Module | None = None, limit=10) -> list[int]:
        """AOB scan. pattern may contain '??' wildcards as text, or be raw bytes
        with a mask like 'xx??xx'."""
        if mask is None and b'?' not in pattern:
            pb, pm = pattern, None
        else:
            toks = pattern.decode('latin1').split() if isinstance(pattern, bytes) and b' ' in pattern else None
            if toks is None:
                raise ValueError("mask needed for non-wildcarded pattern")
            pb = bytes(int(t, 16) if t != '??' else 0 for t in toks)
            pm = ''.join('x' if t != '??' else '?' for t in toks)
        mod = module or self.exe
        data = bytearray()
        step = 0x100000
        addr = mod.base
        hits: list[int] = []
        while addr < mod.base + mod.size and len(hits) < limit:
            chunk = self.try_read(addr, step) or b''
            if chunk:
                off = 0
                while True:
                    i = _find(chunk, pb, pm, off)
                    if i < 0:
                        break
                    hits.append(addr + i)
                    if len(hits) >= limit:
                        break
                    off = i + 1
            addr += step
        return hits

    def find_str(self, text: str, module: Module | None = None) -> list[int]:
        """Locate a NUL-terminated ASCII string in the module image."""
        mod = module or self.exe
        needle = text.encode('ascii', 'ignore') + b'\x00'
        data = self.read(mod.base, min(mod.size, 0x1000000))
        out, off = [], 0
        while True:
            i = data.find(needle, off)
            if i < 0:
                break
            out.append(mod.base + i)
            off = i + 1
        return out

    def xref_imm32(self, value: int, module: Module | None = None, limit=20) -> list[int]:
        """Find code sites embedding imm32 == value (push/mov offset ...)."""
        mod = module or self.exe
        needle = struct.pack('<I', value)
        sites = self.scan(needle, module=mod, limit=limit * 4)
        return sites

    def load_xref_db(self) -> dict:
        return json.loads(XREF_DB.read_text(encoding='utf-8'))

    # ---- whole-process scans (Phase 2 state extraction) ----

    def regions(self, only_readable: bool = True):
        return regions(self.handle, only_readable)

    def scan_regions(self, chunk=0x400000):
        """Yield (region_base, numpy_uint8_array) for all committed readable memory."""
        import numpy as np
        for base, size, prot, mtype in self.regions():
            done = 0
            while done < size:
                n = min(chunk, size - done)
                data = self.try_read(base + done, n)
                if data and len(data) == n:
                    yield base + done, np.frombuffer(data, dtype=np.uint8)
                done += n

    def find_i32(self, value: int, limit=200000) -> list[int]:
        """All addresses where a little-endian int32 equals value."""
        import numpy as np
        hits = []
        pat = np.array([value & 0xFF, (value >> 8) & 0xFF,
                        (value >> 16) & 0xFF, (value >> 24) & 0xFF], dtype=np.uint8)
        for base, arr in self.scan_regions():
            if len(arr) < 4:
                continue
            m = (arr[:-3] == pat[0]) & (arr[1:-2] == pat[1]) & \
                (arr[2:-1] == pat[2]) & (arr[3:] == pat[3])
            idx = np.nonzero(m)[0]
            for i in idx:
                hits.append(base + int(i))
                if len(hits) >= limit:
                    return hits
        return hits

    def find_i32_seq(self, values: list[int], window=64, limit=100) -> list[int]:
        """Addresses where `values` appear as consecutive int32s (in given order,
        allowing small gaps? no - exact consecutive). Returns addr of first."""
        import numpy as np, struct as _s
        pat = _s.pack('<' + 'i' * len(values), *values)
        hits = []
        for base, arr in self.scan_regions():
            view = arr[:len(arr) - len(pat) + 1]
            m = np.ones(len(view), dtype=bool)
            for k in range(len(pat)):
                m &= (arr[k:k + len(view)] == pat[k])
            for i in np.nonzero(m)[0]:
                hits.append(base + int(i))
                if len(hits) >= limit:
                    return hits
        return hits

    def find_pointer(self, target: int, limit=1000) -> list[int]:
        """Addresses that contain the 4-byte value `target` (pointer scan)."""
        return self.find_i32(target, limit=limit)


def _find(buf: bytes | bytearray, pat: bytes, mask: str | None, start: int) -> int:
    if mask is None:
        return bytes(buf).find(pat, start)
    n = len(pat)
    for i in range(start, len(buf) - n + 1):
        ok = True
        for j in range(n):
            if mask[j] == 'x' and buf[i + j] != pat[j]:
                ok = False
                break
        if ok:
            return i
    return -1


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wt.DWORD), ("RegionSize", ctypes.c_size_t),
                ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD)]


READABLE = {0x02, 0x04, 0x05, 0x20, 0x40, 0x80}  # R / RW / WC / ER / ERW / EWC
MEM_COMMIT = 0x1000


def regions(handle: int, only_readable: bool = True):
    """Walk the address space with VirtualQueryEx -> [(base, size, protect, type)]."""
    mbi = MEMORY_BASIC_INFORMATION()
    addr = 0
    out = []
    while addr < 0x7FFF0000:
        if not k32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                  ctypes.sizeof(mbi)):
            break
        size = mbi.RegionSize or 0x1000
        if mbi.State == MEM_COMMIT and (not only_readable or mbi.Protect in READABLE):
            out.append((mbi.BaseAddress or 0, size, mbi.Protect, mbi.Type))
        addr = (mbi.BaseAddress or 0) + size
    return out


if __name__ == '__main__':
    try:
        p = Process.attach()
    except RuntimeError as e:
        print(f"[!] {e} — start the game first (D:\\Heros3\\Heroes3.exe)")
        sys.exit(1)
    exe = p.exe
    print(f"attached pid={p.pid} base={exe.base:#x} size={exe.size:#x} "
          f"(relocated by {exe.base - STATIC_BASE:+#x})")
    # sanity: verify a known static string at its relocated VA
    db = p.load_xref_db()
    for item in db['string_xrefs']:
        if item['text'] == 'artifact.def':
            rt = p.va(int(item['str_va'], 16))
            got = p.read(rt, 13)
            print(f"anchor check artifact.def @ {rt:#x}: {got!r}")
            break
    hits = p.xref_imm32(p.va(0x7aef98))
    print(f"runtime xrefs to artifact.def string: {[hex(h) for h in hits[:8]]}")
