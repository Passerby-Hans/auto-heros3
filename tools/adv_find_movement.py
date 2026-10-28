# -*- coding: utf-8 -*-
"""Find the hero's remaining-movement field (map layer) by a move diff.

A one-tile move costs roughly 100 movement points, so the field we want changes
by -50..-400 into a value in [0, 3000].  That band is narrow enough to survive a
whole-process diff even with the camera/caches churning.
"""
import struct
import sys
import time

import numpy as np

sys.path.insert(0, '.')
from agent.h3 import lab


def snapshot(pr):
    return [(base, arr.copy()) for base, arr in pr.scan_regions(chunk=0x800000)]


def main():
    pr = lab.proc()
    lab.sclick(692, 346, wait=1.5)              # center on the hero
    a = snapshot(pr)
    # move: click a couple of tiles below-right of the hero
    lab.shover(430, 350, 0.4)
    print('hover:', repr(lab.status_bar_text()))
    for _ in range(2):
        lab.sclick(430, 350, wait=1.3)
    time.sleep(1.2)
    b = snapshot(pr)
    others = dict(b)
    hits = []
    for base, arr in a:
        o = others.get(base)
        if o is None:
            continue
        n = (len(arr) // 4) * 4
        x = arr[:n].view('<i4').astype(np.int64)
        y = o[:n].view('<i4').astype(np.int64)
        d = y - x
        m = (x >= 0) & (x <= 3000) & (y >= 0) & (y <= 3000) & (d >= -400) & (d <= -50)
        idx = np.nonzero(m)[0]
        for i in idx[:200]:
            hits.append((base + int(i) * 4, int(x[i]), int(y[i])))
    print(f'{len(hits)} movement-like changes')
    for addr, o, n in hits[:40]:
        print(f'  {addr:#010x}: {o} -> {n} (delta {n - o})')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
