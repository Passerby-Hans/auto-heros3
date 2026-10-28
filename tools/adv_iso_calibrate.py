# -*- coding: utf-8 -*-
"""Deterministic adventure-map clicks without vision coordinates.

Idea: H3 recenters the camera on the selected hero (session 7 finding), so the
hero's tile sits at a fixed screen point.  With the isometric basis (32,16) /
(-32,16) (measured earlier: ±64/±32 at 1600x1200), any tile offset from the hero
has a computable pixel, and after every move the anchor is restored automatically.

This tool locates the hero's pixel by hover probe, then validates the mapping by
clicking `hero + offset` and measuring the resulting map-content shift through
template matching (shift == -(hero displacement)).
"""
import sys

import numpy as np

sys.path.insert(0, '.')
from agent.h3 import lab

B1 = (32, 16)      # +1 tile in map-x
B2 = (-32, 16)     # +1 tile in map-y


def hero_pixel(pr, name_hint='查看'):
    """Scan a small grid around the viewport centre for the hero's info text."""
    best = []
    for y in range(240, 341, 10):
        for x in range(340, 461, 10):
            lab.shover(x, y, 0.35)
            t = lab.status_bar_text()
            if t and '查看' in t and '信息' in t:
                best.append((x, y, t))
    return best


def grab_gray():
    return np.asarray(lab.grab(lab.hwnd()).convert('L')).astype(np.int32)


def shift_of(a, b, patch=(500, 300, 60, 40), radius=160):
    px, py, pw, ph = patch
    tpl = a[py:py + ph, px:px + pw]
    best, bestpos = None, None
    for yy in range(py - radius, py + radius):
        for xx in range(px - radius, px + radius):
            if xx < 0 or yy < 0 or yy + ph > b.shape[0] or xx + pw > b.shape[1]:
                continue
            d = np.abs(b[yy:yy + ph, xx:xx + pw] - tpl).mean()
            if best is None or d < best:
                best, bestpos = d, (xx, yy)
    return (bestpos[0] - px, bestpos[1] - py), best


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    pr = lab.proc()
    lab.sclick(692, 346, wait=1.6)              # center on hero
    hits = hero_pixel(pr)
    print(f'{len(hits)} hover hits for the hero text; first 6:')
    for h in hits[:6]:
        print('   ', h)
    if not hits:
        raise SystemExit('hero pixel not found')
    hx = int(np.mean([h[0] for h in hits]))
    hy = int(np.mean([h[1] for h in hits]))
    print(f'hero screen anchor ~= ({hx},{hy})')

    off = (2, 0)                                  # two tiles along +x
    tx = hx + off[0] * B1[0] + off[1] * B2[0]
    ty = hy + off[0] * B1[1] + off[1] * B2[1]
    lab.shover(tx, ty, 0.5)
    print('target pixel', (tx, ty), 'hint:', repr(lab.status_bar_text()))
    before = grab_gray()
    lab.sclick(tx, ty, wait=1.2)
    lab.sclick(tx, ty, wait=2.0)
    after = grab_gray()
    sh, score = shift_of(before, after)
    print(f'content shift {sh} (score {score:.2f}) -> implied hero displacement '
          f'{(-sh[0], -sh[1])} px')
    lab.snap('adv_iso_validation')
