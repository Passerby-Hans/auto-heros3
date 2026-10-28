# -*- coding: utf-8 -*-
"""Adventure-map work: find the hero object's tile coordinates by name search +
one-step move diff, and find the camera scroll anchor by minimap-click diff."""
import struct
import sys
import time

import numpy as np

sys.path.insert(0, '.')
from agent.h3 import lab


def find_hero_objects(pr, name: str):
    raw = name.encode('gbk')
    out = []
    for base, arr in pr.scan_regions():
        b = arr.tobytes()
        off = 0
        while True:
            i = b.find(raw, off)
            if i < 0:
                break
            out.append(base + i)
            off = i + 1
    cands = []
    for h in out:
        obj = h - 0x445           # hero struct: name at +0x445 (see HANDOVER)
        st = pr.try_read(obj + 0x406, 4)
        if st and all(0 <= v <= 30 for v in st):
            cands.append((obj, tuple(st)))
    return cands


def dump(pr, base, size=0x800):
    return pr.read(base, size)


def diff(a, b, label=''):
    print(f'--- {label} changed dwords (|delta|<=64) ---')
    for off in range(0, len(a) - 3, 4):
        va = struct.unpack('<i', a[off:off + 4])[0]
        vb = struct.unpack('<i', b[off:off + 4])[0]
        if va != vb and abs(va - vb) <= 64 and -10000 <= va <= 100000 and -10000 <= vb <= 100000:
            print(f'  +{off:#05x}: {va} -> {vb}')


if __name__ == '__main__':
    pr = lab.proc()
    name = sys.argv[1] if len(sys.argv) > 1 else '拉克斯'
    cands = find_hero_objects(pr, name)
    print(f'hero candidates for {name}: {[(hex(o), s) for o, s in cands]}')
    if not cands:
        raise SystemExit(1)
    obj = cands[0][0]
    a = dump(pr, obj)
    # move: click a nearby open tile twice (H3 eats the first click sometimes)
    lab.shover(360, 330, 0.4)
    print('hover:', repr(lab.status_bar_text()))
    lab.sclick(360, 330, wait=1.2)
    lab.sclick(360, 330, wait=2.0)
    time.sleep(1.0)
    b = dump(pr, obj)
    diff(a, b, 'after move click')
    print('hero obj', hex(obj), 'stats', tuple(pr.read(obj + 0x406, 4)))
    lab.snap('adv_01_moved')
