# -*- coding: utf-8 -*-
"""Find H3's combat stack array by its creature-count signature.

Battle snapshot (round 2): our stack = 11, enemy stacks = 3,3,3,3,3,30.
Stacks live in a fixed-stride array, so look for `11` followed (or preceded) by
`3,3,3,...` at a constant stride.
"""
import struct
import sys

import numpy as np

sys.path.insert(0, '.')
from agent.h3 import lab

COUNT = 11
PATTERN = [3, 3, 3, 3]
STRIDES = list(range(0x30, 0x120, 4))


def main():
    pr = lab.proc()
    hits = pr.find_i32(COUNT, limit=200000)
    print(f'{len(hits)} dwords == {COUNT}')
    found = []
    for h in hits:
        for st in STRIDES:
            for direction in (1, -1):
                ok = True
                for k, want in enumerate(PATTERN, start=1):
                    a = h + direction * k * st
                    d = pr.try_read(a, 4)
                    if not d or struct.unpack('<i', d)[0] != want:
                        ok = False
                        break
                if ok:
                    found.append((h, st, direction))
    print(f'{len(found)} signature matches')
    for h, st, d in found[:20]:
        base = h if d == 1 else h - 5 * st
        dump = pr.try_read(base - 0x20, 0x20 + 7 * st)
        print(f'  hit={h:#x} stride={st:#x} dir={d:+d} -> array guess {base:#x}')
        if dump:
            for off in range(0, min(len(dump), 7 * st + 0x20), 16):
                vals = struct.unpack('<4i', dump[off:off + 16]) if off + 16 <= len(dump) else ()
                print(f'    +{base + off - 0x20:#x}: {dump[off:off+16].hex(" ")}  {vals}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
