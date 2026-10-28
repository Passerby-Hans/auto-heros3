# -*- coding: utf-8 -*-
"""Find the adventure-map camera/scroll fields.

Pan the camera twice with the SAME small minimap step: a scroll variable must
react with the same delta both times (linear response), while caches/animations
behave randomly.  That invariant is the whole filter.
"""
import struct
import sys
import time

import numpy as np

sys.path.insert(0, '.')
from agent.h3 import lab


def snapshot(pr):
    return [(base, arr.copy()) for base, arr in pr.scan_regions(chunk=0x800000)]


def diff(s1, s2, maxd=4000):
    out = {}
    other = dict(s2)
    for base, arr in s1:
        o = other.get(base)
        if o is None:
            continue
        n = (len(arr) // 4) * 4
        x = arr[:n].view('<i4'); y = o[:n].view('<i4')
        m = (x != y) & (np.abs(x.astype(np.int64)) < 100_000_000) & (np.abs(y.astype(np.int64)) < 100_000_000) \
            & (np.abs(x.astype(np.int64) - y.astype(np.int64)) <= maxd)
        idx = np.nonzero(m)[0]
        for i in idx[:4000]:
            out[base + int(i) * 4] = int(y[i]) - int(x[i])
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
    mx, my = (int(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) > 2 else (700, 80)
    step = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    s0 = snapshot(pr)
    lab.sclick(mx, my, wait=1.6)
    s1 = snapshot(pr)
    lab.sclick(mx + step, my, wait=1.6)
    s2 = snapshot(pr)
    d1 = diff(s0, s1)
    d2 = diff(s1, s2)
    print(f'd1={len(d1)} d2={len(d2)}')
    both = sorted(set(d1) & set(d2))
    same = [a for a in both if d1[a] == d2[a]]
    print(f'{len(both)} changed twice, {len(same)} with an identical delta')
    for a in same[:60]:
        d = pr.try_read(a, 4)
        print(f'  {a:#010x}{annotate(pr, a)}: delta={d1[a]:+d} now={struct.unpack("<i", d)[0] if d else None}')
