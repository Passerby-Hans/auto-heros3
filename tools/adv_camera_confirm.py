# -*- coding: utf-8 -*-
"""Confirm the adventure-map camera fields and dump their owner object.

Candidate (from the linear-response test, delta +40 per 4 minimap px):
  0x0fef001c / 0x0fef0020  and a few copies.
Here: recenter on the hero, read them, pan DOWN, read again -> the y field must
move while x stays; then dump the surrounding structure to find the map object
(tile arrays, hero tile, ...).
"""
import struct
import sys

sys.path.insert(0, '.')
from agent.h3 import lab

CAMS = [0x0fef001c, 0x0fef0020, 0x0fef07e0, 0x0fef0b28, 0x12570218]


def read(pr):
    out = {}
    for a in CAMS:
        d = pr.try_read(a, 4)
        out[a] = struct.unpack('<i', d)[0] if d else None
    return out


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    pr = lab.proc()
    lab.sclick(692, 346, wait=1.6)        # 下一个英雄 -> center on hero
    print('after recenter:', {hex(k): v for k, v in read(pr).items()})
    lab.sclick(700, 84, wait=1.6)         # pan down 4 px
    print('after pan down:', {hex(k): v for k, v in read(pr).items()})
    lab.sclick(704, 84, wait=1.6)         # pan right 4 px
    print('after pan right:', {hex(k): v for k, v in read(pr).items()})
    print('\n--- dump 0x0fef0000 .. 0x0fef0200 ---')
    base = 0x0fef0000
    d = pr.try_read(base, 0x200)
    if d:
        for off in range(0, len(d), 16):
            vals = struct.unpack('<4i', d[off:off + 16])
            print(f'{base + off:#010x}  {d[off:off+16].hex(" ")}  {vals}')
