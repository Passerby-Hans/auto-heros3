# -*- coding: utf-8 -*-
"""Relocate the map-object layer from the hero movement-points pair.

Session 6 (HANDOVER): [0x3143cfc]=current movement, [0x3143d00]=base movement,
and the map header sits exactly +0x24 after them (0x3143d20: w,h + tile arrays).
All those addresses drift (heap), so search for the *values*:

    X      : current movement   (0 .. base)
    X+4    : base movement      (1400..1800 for a hero)
    X+0x24 : map w,h            (square, one of 36/72/108/144)
"""
import struct
import sys

import numpy as np

sys.path.insert(0, '.')
from agent.h3 import lab

DIMS = (36, 48, 72, 96, 108, 144)


def main():
    pr = lab.proc()
    cand = []
    for base, arr in pr.scan_regions():
        n = (len(arr) // 4) * 4
        x = arr[:n].view('<i4').astype(np.int64)
        if len(x) < 16:
            continue
        cur, bas = x[:-1], x[1:]
        m = (bas >= 1400) & (bas <= 1800) & (cur >= 0) & (cur <= bas)
        idx = np.nonzero(m)[0]
        for i in idx:
            addr = base + int(i) * 4
            hdr = pr.try_read(addr + 0x24, 8)
            if not hdr:
                continue
            w, h = struct.unpack('<ii', hdr)
            if w == h and w in DIMS:
                cand.append((addr, int(x[i]), int(x[i + 1]), w, h))
    print(f'{len(cand)} movement-point candidates with a map header behind them')
    for addr, cur, bas, w, h in cand[:10]:
        print(f'  move@{addr:#010x} cur={cur} base={bas}  map=hdr@{addr+0x24:#x} {w}x{h}'
              f'  [+0x24..+0x50]: {pr.try_read(addr + 0x24, 0x2c).hex(" ")}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
