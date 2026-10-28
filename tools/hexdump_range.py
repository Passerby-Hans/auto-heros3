# -*- coding: utf-8 -*-
"""Hexdump a memory range of the running game (exploration helper)."""
import struct
import sys

sys.path.insert(0, '.')
from agent.h3 import lab


def dump(pr, base, size=0x400):
    d = pr.try_read(base, size)
    if not d:
        print('unreadable', hex(base)); return
    for off in range(0, len(d), 16):
        ch = d[off:off + 16]
        vals = struct.unpack('<4i', ch) if len(ch) == 16 else ()
        print(f'{base + off:#010x}  {ch.hex(" ")}  {vals}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    base = int(sys.argv[1], 0) if len(sys.argv) > 1 else 0xe81e600
    size = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0x400
    dump(lab.proc(), base, size)
