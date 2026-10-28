# -*- coding: utf-8 -*-
"""Find the combat stack struct: snapshot -> move ONE stack -> snapshot -> diff.

Only one unit changes position for our move, so the hex-index field changes by
exactly one neighbour step while everything else is noise (animations, timers).
"""
import struct
import sys
import time

import numpy as np

sys.path.insert(0, '.')
from agent.h3 import lab


def snapshot(pr):
    return [(base, arr.copy()) for base, arr in pr.scan_regions(chunk=0x800000)]


def small(a, b, lo=-5, hi=400):
    if a.shape != b.shape:
        return None, None, None
    n = (len(a) // 4) * 4
    x = a[:n].view('<i4'); y = b[:n].view('<i4')
    m = (x != y) & (x >= lo) & (x <= hi) & (y >= lo) & (y <= hi)
    idx = np.nonzero(m)[0]
    return idx, x, y


def diff(s1, s2):
    out = {}
    other = dict(s2)
    for base, arr in s1:
        o = other.get(base)
        if o is None:
            continue
        idx, x, y = small(arr, o)
        if idx is None:
            continue
        for i in idx:
            out[base + int(i) * 4] = (int(x[i]), int(y[i]))
    return out


def annotate(pr, addr):
    d = addr - pr.exe.base
    if 0 <= d < pr.exe.size:
        return f' exe+{d:#x}'
    for name, m in pr.modules.items():
        if m.base <= addr < m.base + m.size:
            return f' {name}+{addr - m.base:#x}'
    return ''


if __name__ == '__main__':
    pr = lab.proc()
    x, y = int(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 0
    x, y = (x, y) if y else (160, 260)
    lab.shover(x, y, 0.5)
    print('hover text:', lab.status_bar_text())
    a = snapshot(pr)
    print('snap A done; clicking')
    lab.sclick(x, y, wait=0.9)
    b = snapshot(pr)
    print('snap B done; diff')
    ch = diff(a, b)
    print(f'{len(ch)} small-range dword changes')
    # position-like fields: change is tiny
    pos = {k: v for k, v in ch.items() if abs(v[1] - v[0]) <= 3}
    print(f'{len(pos)} with |delta| <= 3')
    for addr, (o, n) in sorted(pos.items())[:80]:
        print(f'  {addr:#010x}{annotate(pr, addr)}: {o} -> {n}')
