# -*- coding: utf-8 -*-
"""Locate a unit sprite in a screenshot by template matching against a crop.

Used for battlefield calibration: measure the exact pixel displacement of our
stack after one move -> that displacement is one lattice basis vector.
"""
import sys

import numpy as np
from PIL import Image


def load(path):
    return np.asarray(Image.open(path).convert('L')).astype(np.int32)


def find(template, scene, center, radius=120):
    """Best match of `template` inside a search window around `center`."""
    th, tw = template.shape
    cx, cy = center
    x0, y0 = max(0, cx - radius), max(0, cy - radius)
    x1 = min(scene.shape[1], cx + radius + tw)
    y1 = min(scene.shape[0], cy + radius + th)
    win = scene[y0:y1, x0:x1]
    best, bestpos = None, None
    for yy in range(0, win.shape[0] - th + 1):
        for xx in range(0, win.shape[1] - tw + 1):
            d = np.abs(win[yy:yy + th, xx:xx + tw] - template).mean()
            if best is None or d < best:
                best, bestpos = d, (x0 + xx, y0 + yy)
    return bestpos, best


if __name__ == '__main__':
    before = load(sys.argv[1])
    after = load(sys.argv[2])
    # template = crop of the unit from the "before" shot: x,y,w,h given on cmdline
    x, y, w, h = (int(v) for v in sys.argv[3:7])
    tpl = before[y:y + h, x:x + w]
    pos, score = find(tpl, after, (x + w // 2, y + h // 2), radius=int(sys.argv[7]) if len(sys.argv) > 7 else 160)
    print(f'template at ({x},{y}) matched in after at {pos} score={score:.2f}')
    print(f'displacement: dx={pos[0] - x}, dy={pos[1] - y}')
