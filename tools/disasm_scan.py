# -*- coding: utf-8 -*-
"""Static recon of Heroes3.exe (SoD 3.1 CN) with capstone:
- function prologue census in .text
- string xref map: imm32 operands pointing into .rdata/.data ASCII strings
- IAT call sites for interesting imports (DirectDrawCreate, ReadFile, ...)
Outputs a markdown report + JSON xref db for later AOB work.
"""
import json, re, sys, struct
from collections import defaultdict
import pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_32

EXE = r"D:\Heros3\Heroes3.exe"

KEYWORDS = [
    'hero', 'town', 'castle', 'resource', 'gold', 'turn', 'day', 'week', 'month',
    'map', 'save', 'load', 'army', 'creature', 'monster', 'spell', 'artifact',
    'morale', 'luck', 'move', 'attack', 'defen', 'player', 'enemy', 'ai',
    'victory', 'defeat', 'win', 'lose', 'campaign', 'scenario', 'new game',
    'adventure', 'battle', 'combat', 'recruit', 'build', 'market', 'guild',
    'skills', 'experience', 'level', 'mana', 'speed', 'init'
]


def get_strings(pe, blob):
    """ASCII strings (>=5 chars) with VA, in .rdata/.data sections."""
    out = []
    for s in pe.sections:
        name = s.Name.rstrip(b'\x00').decode()
        if name not in ('.rdata', '.data'):
            continue
        va = pe.OPTIONAL_HEADER.ImageBase + s.VirtualAddress
        raw = blob[s.PointerToRawData:s.PointerToRawData + s.SizeOfRawData]
        for m in re.finditer(rb'[\x20-\x7e]{5,}', raw):
            out.append((va + m.start(), m.group().decode('ascii', 'replace')))
    return out


def main():
    blob = open(EXE, 'rb').read()
    pe = pefile.PE(EXE)
    base = pe.OPTIONAL_HEADER.ImageBase
    text = None
    for s in pe.sections:
        if s.Name.rstrip(b'\x00') == b'.text':
            text = s
    tva = base + text.VirtualAddress
    traw = blob[text.PointerToRawData:text.PointerToRawData + text.SizeOfRawData]

    md = Cs(CS_ARCH_X86, CS_MODE_32)
    md.detail = False

    # 1. function census: 55 8B EC (push ebp; mov ebp, esp)
    prologues = [m.start() for m in re.finditer(b'\x55\x8b\xec', traw)]
    print(f".text: va={tva:#x} size={len(traw):#x}")
    print(f"function prologues (55 8B EC): {len(prologues)}")

    # 2. import thunk table
    iat = {}  # thunk_va -> name
    for entry in pe.DIRECTORY_ENTRY_IMPORT:
        dll = entry.dll.decode()
        for imp in entry.imports:
            if imp.name:
                iat[imp.address] = f"{dll}!{imp.name.decode()}"

    # 3. call [iat] sites for interesting imports
    interesting = ('DirectDrawCreate', 'CreateFileA', 'ReadFile', 'WriteFile',
                   'GetKeyState', 'ShowCursor', 'PostMessageA', 'timeGetTime')
    callsites = defaultdict(list)
    for m in re.finditer(rb'\xff\x15(....)', traw, re.S):
        va = struct.unpack('<I', m.group(1))[0]
        name = iat.get(va)
        if name and any(k in name for k in interesting):
            for short in interesting:
                if short in name:
                    callsites[f"{short}"].append(tva + m.start())
    for k, v in callsites.items():
        print(f"call sites {k}: {len(v)}  first@ {', '.join(hex(x) for x in v[:6])}")

    # 4. string xrefs: push imm32 / mov reg, imm32 pointing to strings
    strs = get_strings(pe, blob)
    strset = {}
    for va, s in strs:
        strset[va] = s
    xrefs = defaultdict(list)  # str_va -> [code_va]
    pat = re.compile(rb'(?:\x68|\xb8|\xb9|\xba|\xbb|\xbe|\xbf|\xbd)(....)', re.S)
    for m in pat.finditer(traw):
        va = struct.unpack('<I', m.group(1))[0]
        if va in strset:
            xrefs[va].append(tva + m.start())
    print(f"strings: {len(strs)}, xref'd: {len(xrefs)}")

    # keyword-interesting strings sorted by xref count
    hits = []
    for va, refs in xrefs.items():
        s = strset[va]
        low = s.lower()
        if any(k in low for k in KEYWORDS) and len(s) < 60:
            hits.append((len(refs), va, s, refs[:8]))
    hits.sort(reverse=True)
    print("\n-- keyword strings by xref count --")
    for n, va, s, refs in hits[:60]:
        print(f"{n:3d}x {va:#010x} {s[:48]!r} @ {[hex(r) for r in refs[:3]]}")

    # dump json for later
    db = {
        'exe': EXE,
        'md5': __import__('hashlib').md5(blob).hexdigest(),
        'text_va': hex(tva), 'text_size': hex(len(traw)),
        'prologues': len(prologues),
        'callsites': {k: [hex(x) for x in v] for k, v in callsites.items()},
        'string_xrefs': [
            {'str_va': hex(va), 'text': strset[va][:80], 'refs': [hex(r) for r in refs[:20]],
             'nrefs': len(refs)}
            for va, refs in xrefs.items()],
    }
    with open('doc/findings/exe_xref_db.json', 'w', encoding='utf-8') as f:
        json.dump(db, f, ensure_ascii=False, indent=1)
    print(f"\nwrote doc/findings/exe_xref_db.json")


if __name__ == '__main__':
    main()
