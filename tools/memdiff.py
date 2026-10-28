# -*- coding: utf-8 -*-
"""Whole-process memory diff around ONE action.

The general tool for "what did that click actually change?".
It snapshots all readable memory, takes a control snapshot after an idle pause
(animations/timers = noise), then performs the action and diffs again, reporting
only dwords whose value changed by a small delta and that were NOT part of the
idle noise set.

Usage:
    python -X utf8 tools/memdiff.py click 350 300 [--seconds 2.5]
    python -X utf8 tools/memdiff.py idle                # just measure noise
"""
from __future__ import annotations
import argparse
import sys
import time

import numpy as np

sys.path.insert(0, '.')
from agent.h3.mem import Process
from agent.h3 import lab

LIMIT = 3000


def snapshot(pr):
    return [(base, arr.copy()) for base, arr in pr.scan_regions(chunk=0x800000)]


def dword_changes(a, b, maxdelta):
    if a.shape != b.shape:
        return None, None, None
    n = (len(a) // 4) * 4
    x = a[:n].view('<i4')
    y = b[:n].view('<i4')
    m = (x != y) & (np.abs(x.astype(np.int64) - y.astype(np.int64)) <= maxdelta) \
        & (np.abs(x) < 100_000_000) & (np.abs(y) < 100_000_000)
    idx = np.nonzero(m)[0]
    return idx[:LIMIT], x, y


def diff(s1, s2, maxdelta=5000):
    out = {}
    other = dict(s2)
    for base, arr in s1:
        o = other.get(base)
        if o is None:
            continue
        idx, x, y = dword_changes(arr, o, maxdelta)
        if idx is None:
            continue
        for i in idx:
            out[base + int(i) * 4] = (int(x[i]), int(y[i]))
    return out


def annotate(pr, addr):
    d = addr - pr.exe.base
    if 0 <= d < pr.exe.size:
        return f' (exe+{d:#x})'
    for name, m in pr.modules.items():
        if m.base <= addr < m.base + m.size:
            return f' ({name}+{addr - m.base:#x})'
    return ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['idle', 'click', 'key'])
    ap.add_argument('aa', nargs='?', type=int)
    ap.add_argument('bb', nargs='?', type=int)
    ap.add_argument('--seconds', type=float, default=2.5)
    ap.add_argument('--maxdelta', type=int, default=5000)
    args = ap.parse_args()

    pr = Process.attach()
    print('snapshot 1 ...'); s1 = snapshot(pr)
    time.sleep(2.0)
    print('snapshot 2 (idle) ...'); s2 = snapshot(pr)
    noise = diff(s1, s2, args.maxdelta)
    print(f'idle noise: {len(noise)} dwords')
    del s1

    time.sleep(0.3)
    print('snapshot 3 ...'); s3 = snapshot(pr)
    if args.action == 'click':
        lab.hover(args.aa, args.bb, 0.3)
        lab.click(args.aa, args.bb, wait=args.seconds)
    elif args.action == 'key':
        from agent.h3.binput import tap
        tap(lab.hwnd(), args.aa, hold=0.12)
        time.sleep(args.seconds)
    else:
        time.sleep(args.seconds)
    print('snapshot 4 ...'); s4 = snapshot(pr)

    changed = diff(s3, s4, args.maxdelta)
    print(f'changes: {len(changed)} dwords')
    real = {a: v for a, v in changed.items() if a not in noise}
    print(f'changes minus idle noise: {len(real)} dwords')
    for a, (o, n) in sorted(real.items())[:150]:
        print(f'  {a:#010x}{annotate(pr, a)}: {o} -> {n}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
