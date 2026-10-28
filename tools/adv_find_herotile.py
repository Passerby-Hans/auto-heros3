# -*- coding: utf-8 -*-
"""Find the hero's map tile (x,y) by a byte-level whole-memory diff over one move.

Signature of an (x,y) pair: two bytes within 32 bytes of each other, both in
0..143, both changed by +-1..+-4 in the same snapshot pair.  Nothing else in the
engine changes like that for a single hero step, so the filter is very selective.
"""
import struct
import sys
import time

import numpy as np

sys.path.insert(0, '.')
from agent.h3 import lab

MAXC = 144


def snapshot(pr):
    return [(base, arr.copy()) for base, arr in pr.scan_regions(chunk=0x800000)]


def byte_changes(a, b):
    if a.shape != b.shape:
        return None, None
    x = a.astype(np.int16)
    y = b.astype(np.int16)
    d = y - x
    m = (d != 0) & (np.abs(d) <= 4) & (y >= 0) & (y < MAXC) & (x >= 0) & (x < MAXC)
    return np.nonzero(m)[0], d


def main():
    pr = lab.proc()
    lab.sclick(692, 346, wait=1.5)
    # find an open destination: try a few offsets and use the first with empty hint
    anchor = (300, 268)
    target = None
    for off, px, py in [((0, 2), 236, 300), ((3, 0), 396, 316), ((0, 3), 204, 316),
                        ((-1, 1), 236, 268), ((1, 3), 268, 332)]:
        lab.shover(px, py, 0.4)
        h = lab.status_bar_text()
        print(off, (px, py), repr(h))
        if not h:
            target = (px, py)
            break
    if not target:
        raise SystemExit('no open destination found')
    a = snapshot(pr)
    for _ in range(2):
        lab.sclick(target[0], target[1], wait=1.3)
    time.sleep(1.0)
    b = snapshot(pr)

    others = dict(b)
    pairs = []
    for base, arr in a:
        o = others.get(base)
        if o is None or len(arr) != len(o):
            continue
        idx, d = byte_changes(arr, o)
        if len(idx) < 2:
            continue
        s = set(int(i) for i in idx)
        for i in idx:
            i = int(i)
            for j in range(i + 1, min(i + 33, len(arr))):
                if j in s:
                    pairs.append((base + i, base + j, int(arr[i]), int(o[i]),
                                  int(arr[j]), int(o[j])))
    print(f'{len(pairs)} candidate byte pairs')
    for v in pairs[:40]:
        print(f'  {v[0]:#010x} & {v[1]:#010x}: ({v[2]}->{v[3]}) ({v[4]}->{v[5]})')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
