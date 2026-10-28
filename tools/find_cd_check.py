# -*- coding: utf-8 -*-
"""Locate and analyze the CD-check code in Heroes3.exe (SoD 3.1 CN).

Finds call sites of GetLogicalDrives/GetDriveTypeA/GetDiskFreeSpaceA,
prints the surrounding disassembly with function-boundary detection.
"""
import pefile, re, struct, sys
from capstone import Cs, CS_ARCH_X86, CS_MODE_32

EXE = r"D:\Heros3\Heroes3.exe"


def main():
    pe = pefile.PE(EXE)
    blob = open(EXE, 'rb').read()
    base = pe.OPTIONAL_HEADER.ImageBase
    iat = {}
    for entry in pe.DIRECTORY_ENTRY_IMPORT:
        for imp in entry.imports:
            if imp.name:
                iat[imp.address] = imp.name.decode()
    targets = {k: v for k, v in iat.items()
               if v in ('GetLogicalDrives', 'GetDriveTypeA', 'GetDiskFreeSpaceA')}
    text = next(s for s in pe.sections if s.Name.rstrip(b'\x00') == b'.text')
    tva = base + text.VirtualAddress
    traw = blob[text.PointerToRawData:text.PointerToRawData + text.SizeOfRawData]
    print("IAT thunks:", {hex(k): v for k, v in targets.items()})

    md = Cs(CS_ARCH_X86, CS_MODE_32)
    sites = []
    for m in re.finditer(rb'\xff\x15(....)', traw, re.S):
        t = struct.unpack('<I', m.group(1))[0]
        if t in targets:
            sites.append((tva + m.start(), targets[t]))

    for va, name in sites:
        # find enclosing function: nearest prologue 55 8B EC before
        off = va - tva
        fend = off
        while fend > 0 and traw[fend-3:fend] != b'\x55\x8b\xec':
            fend -= 1
        fend = max(0, fend - 3)
        print(f"\n=== {name} call @{va:#x} (function likely starts {tva+fend:#x}) ===")
        code = traw[fend:fend + 400]
        for ins in md.disasm(code, tva + fend):
            mark = '  <== CALL' if ins.address <= va < ins.address + ins.size else ''
            print(f"  {ins.address:#010x}  {ins.bytes.hex():20s} {ins.mnemonic:7s} {ins.op_str}{mark}")
            if ins.mnemonic == 'ret' and ins.address > va:
                break


if __name__ == '__main__':
    main()
