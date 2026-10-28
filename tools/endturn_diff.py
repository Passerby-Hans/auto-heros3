# -*- coding: utf-8 -*-
"""End-turn memory diff: locate the date/day counter and live game-state region.

1. snapshot all readable memory
2. PostMessage-click the end-turn button, wait until the on-screen date changes
3. snapshot again
4. diff -> addresses whose value went 1->2 (day), plus change statistics
"""
import sys
sys.stdout.reconfigure(encoding='utf-8')
import time
import numpy as np
from agent.h3.mem import Process
from agent.h3.capture import h3_hwnds, grab
from agent.h3.binput import click
from agent.h3.vision import _ocr


def ocr_date(hwnd):
    img = np.asarray(grab(hwnd).convert('RGB'))
    result, _ = _ocr()(img)
    for box, text, conf in (result or []):
        if '月' in text and '日' in text:
            return text
    return None


def snapshot(p):
    out = {}
    for base, arr in p.scan_regions(chunk=0x800000):
        out[base] = arr.copy()
    return out


def main():
    hwnd = h3_hwnds()[0][0]
    p = Process.attach()
    d0 = ocr_date(hwnd)
    print('date before:', d0)
    s1 = snapshot(p)
    print(f'  regions snapshotted: {len(s1)}, {sum(len(a) for a in s1.values())/1e6:.0f} MB')

    click(hwnd, 1500, 1130)   # end-turn button (1600x1200 layout)
    d1 = None
    for _ in range(20):
        time.sleep(1.0)
        d1 = ocr_date(hwnd)
        if d1 and d1 != d0:
            break
    print('date after :', d1)
    if d1 == d0:
        print('date did not change - end-turn click may have missed; abort')
        return
    s2 = snapshot(p)

    print('--- diff: values 1->2 (day counter candidates) ---')
    n = 0
    for base in s1:
        a, b = s1[base], s2.get(base)
        if b is None or len(a) != len(b):
            continue
        diff_idx = np.nonzero(a != b)[0]
        for i in diff_idx:
            # aligned int32 slots only
            if i % 4:
                continue
            v1 = int(np.frombuffer(a[i:i+4].tobytes(), dtype=np.int32)[0])
            v2 = int(np.frombuffer(b[i:i+4].tobytes(), dtype=np.int32)[0])
            if v1 == 1 and v2 == 2:
                va = base + int(i)
                ctx = b[max(0, i-16):i+20].hex()
                print(f'  [{va:#x}] 1->2  ctx={ctx}')
                n += 1
                if n > 60:
                    break
        if n > 60:
            break
    print('--- region change stats (top 12) ---')
    stats = []
    for base in s1:
        a, b = s2.get(base), s1[base]
        if b is None or len(a) != len(b):
            continue
        stats.append((int((a != b).sum()), base, len(a)))
    stats.sort(reverse=True)
    for cnt, base, size in stats[:12]:
        print(f'  region {base:#x} ({size//1024}KB): {cnt} bytes changed')


if __name__ == '__main__':
    main()
