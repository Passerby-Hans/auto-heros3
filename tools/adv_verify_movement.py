# -*- coding: utf-8 -*-
"""Verify the hero movement-points candidate and dump its neighbours.

0x00b123a4 went 1667 -> 1586 across one small move.  If that is the hero's
remaining movement, two more moves must each subtract a plausible cost, and the
surrounding struct very likely also holds the hero's tile/position.
"""
import struct
import sys
import time

sys.path.insert(0, '.')
from agent.h3 import lab

CAND = 0x00b123a4


def val(pr, a):
    d = pr.try_read(a, 4)
    return struct.unpack('<i', d)[0] if d else None


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    pr = lab.proc()
    print('candidate now:', val(pr, CAND))
    for step, (x, y) in enumerate([(400, 380), (360, 400), (430, 320)], 1):
        lab.shover(x, y, 0.4)
        h = lab.status_bar_text()
        before = val(pr, CAND)
        for _ in range(2):
            lab.sclick(x, y, wait=1.3)
        time.sleep(0.8)
        after = val(pr, CAND)
        print(f'move {step} to ({x},{y}) hint={h!r}: {before} -> {after} (delta {None if after is None or before is None else after - before})')
    print('\n--- neighbours of the candidate ---')
    base = (CAND & ~0x3F) - 0x40
    d = pr.try_read(base, 0xC0)
    if d:
        for off in range(0, len(d), 16):
            vals = struct.unpack('<4i', d[off:off + 16])
            print(f'{base + off:#010x}  {d[off:off+16].hex(" ")}  {vals}')
