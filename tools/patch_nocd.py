# -*- coding: utf-8 -*-
"""No-CD patch for Heroes3.exe (SoD 3.1 CN, md5 86281cc5...).

Verified mechanics (2026-10-19):
1. Startup CD-drive scan at 0x5bb270 (GetLogicalDrives/GetDriveTypeA/
   GetDiskFreeSpaceA) returns 0=success; caller stores it in [0x826dd4].
   Without any optical drive it returns 2 -> "insert disc" abort.
   Patch1: entry `xor eax,eax; ret` -> always success.
2. The init flow then looks for 'h3abab1.smk' inside the CD-drive search
   paths (array [0x82ec1c], normally populated BY the scan we stubbed).
   Empty array -> `mov dword [0x826dd4], 5` at 0x58b59f -> restricted
   "RoE-disc" mode: startup dialog "insert the Shadow of Death disc" and
   single-player menu disabled.
   Patch2: NOP that write so the mode stays full.

Result: game boots straight into the full main menu, no dialog, single
player enabled, zero optical drives needed.

Usage:
  uv run python tools/patch_nocd.py apply    # backup + patch (idempotent)
  uv run python tools/patch_nocd.py restore  # restore original backup
  uv run python tools/patch_nocd.py status
"""
import hashlib, os, shutil, sys
from pathlib import Path
import pefile

EXE = Path(r"D:\Heros3\Heroes3.exe")
BAK = EXE.with_suffix(".exe.331cn.bak")

CHECK_VA = 0x5bb270
ORIG1 = bytes([0x55, 0x8B, 0xEC])            # push ebp; mov ebp,esp
PATCH1 = bytes([0x31, 0xC0, 0xC3])           # xor eax,eax; ret

DOWNGRADE_VA = 0x58b59f
ORIG2 = bytes.fromhex('c705d46d820005000000')  # mov dword[0x826dd4],5
PATCH2 = b'\x90' * len(ORIG2)

PATCHES = [('check', CHECK_VA, ORIG1, PATCH1), ('downgrade', DOWNGRADE_VA, ORIG2, PATCH2)]


def file_offset(va: int) -> int:
    pe = pefile.PE(str(EXE), fast_load=True)
    base = pe.OPTIONAL_HEADER.ImageBase
    for s in pe.sections:
        sva = base + s.VirtualAddress
        if sva <= va < sva + s.Misc_VirtualSize:
            return s.PointerToRawData + (va - sva)
    raise ValueError(f"va {va:#x} not in any section")


def read_at(fd, off, n):
    os.lseek(fd, off, 0)
    return os.read(fd, n)


def state() -> dict:
    out = {}
    fd = os.open(EXE, os.O_RDONLY)
    try:
        for name, va, orig, patch in PATCHES:
            cur = read_at(fd, file_offset(va), len(orig))
            if cur == patch[:len(cur)] and len(cur) == len(patch):
                out[name] = 'patched'
            elif cur == orig:
                out[name] = 'original'
            else:
                out[name] = f'unknown({cur.hex()})'
    finally:
        os.close(fd)
    return out


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def apply():
    if not BAK.exists():
        shutil.copy2(EXE, BAK)
        print(f"backup -> {BAK} (md5 {md5(BAK)})")
    fd = os.open(EXE, os.O_RDWR)
    try:
        for name, va, orig, patch in PATCHES:
            off = file_offset(va)
            cur = read_at(fd, off, len(patch))
            if cur == patch:
                print(f"[{name}] already patched")
                continue
            if cur != orig:
                print(f"[{name}] unexpected bytes at {va:#x}: {cur.hex()}; abort")
                sys.exit(1)
            os.lseek(fd, off, 0)
            os.write(fd, patch)
            print(f"[{name}] patched {va:#x}: {orig.hex()} -> {patch.hex()}")
    finally:
        os.close(fd)
    print(f"md5 now: {md5(EXE)}")


def restore():
    if not BAK.exists():
        print("no backup found"); sys.exit(1)
    shutil.copy2(BAK, EXE)
    print(f"restored (md5 {md5(EXE)}), state={state()}")


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    print('state:', state())
    if cmd == 'apply':
        apply()
    elif cmd == 'restore':
        restore()
    elif cmd != 'status':
        print(__doc__); sys.exit(2)
