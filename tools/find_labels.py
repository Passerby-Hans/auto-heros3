# -*- coding: utf-8 -*-
"""Detect the unit count labels (the small framed boxes under each stack).

H3 draws every stack's count in a little box at the stack's feet.  The box colour
is distinctive, so colour-segmentation gives precise, animation-proof unit
positions — the missing piece for calibrating the hex lattice.
"""
import sys

import numpy as np
from PIL import Image


def labels(path, tol=18):
    img = np.asarray(Image.open(path).convert('RGB')).astype(int)
    # sample the label frame colour from a known label if asked
    h, w, _ = img.shape
    r, g, b = img[:, :, 0], img[:, :, 1], img[:, :, 2]
    # purple frame: strong blue+red, low green
    mask = (b > 90) & (r > 40) & (r < 180) & (g < 90) & (b - g > 40)
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return []
    # greedy clustering by distance
    pts = np.stack([xs, ys], axis=1)
    used = np.zeros(len(pts), bool)
    out = []
    for i in range(len(pts)):
        if used[i]:
            continue
        cx, cy = pts[i]
        sel = (np.abs(pts[:, 0] - cx) < 30) & (np.abs(pts[:, 1] - cy) < 18) & ~used
        grp = pts[sel]
        used |= sel
        if len(grp) >= 25:
            out.append((int(grp[:, 0].mean()), int(grp[:, 1].mean()), len(grp)))
    return sorted(out, key=lambda t: t[1])


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    for path in sys.argv[1:]:
        print(path)
        for cx, cy, n in labels(path):
            print(f'   label at ({cx},{cy}) px={n}')
