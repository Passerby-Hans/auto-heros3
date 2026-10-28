# -*- coding: utf-8 -*-
"""Relocate the adventure-map object layer (drifts every launch).

Session 6 pinned it at 0x3143d20 with w,h then tile-array pointers, but those are
heap addresses.  The map dimensions are also mirrored statically at [0x807f30].
This tool scans for the (w,h) pair and dumps each candidate so the header can be
identified by its pointer neighbourhood.
"""
import struct
import sys

sys.path.insert(0, '.')
from agent.h3 import lab

if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    pr = lab.proc()
    w, h = struct.unpack('<ii', pr.read(0x807f30, 8))
    print(f'static map dims @0x807f30: {w} x {h}')
    hits = pr.find_i32_seq([w, h], limit=60)
    print(f'{len(hits)} candidates for ({w},{h})')
    for addr in hits[:20]:
        d = pr.try_read(addr - 8, 0x30)
        if not d:
            continue
        vals = struct.unpack('<' + 'i' * 12, d)
        print(f'{addr:#010x}  ' + ' '.join(f'{v:#x}' for v in vals))
