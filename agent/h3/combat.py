# -*- coding: utf-8 -*-
"""Battlefield geometry for H3 combat: hex lattice <-> screen pixels.

Everything here was measured live on the CN SoD 3.1 build (session 13,
2026-10-21), see doc/findings/combat-hexmap.md:

* every stack draws its troop count in a small framed box (the "label") at its
  feet; the label's position is animation-proof and colour-segmentable, so it is
  a reliable marker of *which hex* a stack is standing on;
* the battlefield hex lattice (in client pixels, 800x600):

      centre(c, r) = anchor + c*(44, 0) + r*(22, -42)

  i.e. column pitch 44 px, row pitch 42 px, odd rows shifted by half a column.
  Measured: a one-hex move of our pikeman moved its label by exactly (22, -42);
  a 3-hex walk of an enemy stack moved its label by (-132, 0).

Because the label is drawn at a fixed offset *inside* the stack's own hex, the
label position itself is a valid click target for that hex.  That removes the
need to know the absolute lattice phase: clicking `label + v` (one lattice step)
is guaranteed to land inside the neighbouring hex.

Public API
----------
labels(screen=None)      -> [Label(x, y, weight)] sorted right-to-left
split_sides(labels)      -> (ours, theirs) split by screen x (we deploy at left)
Lattice(dx1, dy1, dx2, dy2) with .center(), .step(), .hex_of(pixel)
"""
from __future__ import annotations

import dataclasses

import numpy as np

# --- measured lattice constants (client pixels) -------------------------------
STEP_EAST = (44, 0)      # one column to the right
STEP_NORTH_EAST = (22, -42)   # next column, one row up
STEP_SOUTH_EAST = (22, 42)
STEP_WEST = (-44, 0)
STEP_NORTH_WEST = (-22, -42)
STEP_SOUTH_WEST = (-22, 42)

BATTLE_TOP_BAR = 0          # battlefield occupies the whole top area
BATTLE_BOTTOM = 500         # bottom command bar starts around y=500


@dataclasses.dataclass
class Label:
    x: int
    y: int
    weight: int

    def shifted(self, step) -> tuple[int, int]:
        return self.x + step[0], self.y + step[1]


def _rgb(screen):
    """Accept a PIL image / path / numpy array; return HxWx3 int array."""
    if isinstance(screen, np.ndarray):
        arr = screen
        if arr.ndim == 2:
            arr = np.stack([arr] * 3, axis=-1)
        return arr.astype(int)
    if isinstance(screen, str):
        from PIL import Image
        return np.asarray(Image.open(screen).convert('RGB')).astype(int)
    return np.asarray(screen.convert('RGB')).astype(int)


def labels(screen, min_px: int = 40, cell: int = 6) -> list[Label]:
    """Find the troop-count boxes (purple-framed) -> stack positions.

    Connected components on a coarse grid: the boxes are ~28x14 px and may
    fragment in the colour mask, so grouping must be generous.  The centroid is
    computed over the mask pixels themselves, which keeps it animation-proof.
    """
    img = _rgb(screen)
    r, g, b = img[:, :, 0], img[:, :, 1], img[:, :, 2]
    mask = (b > 90) & (r > 40) & (r < 180) & (g < 90) & (b - g > 40)
    mask[:BATTLE_TOP_BAR, :] = False
    mask[BATTLE_BOTTOM:, :] = False
    if not mask.any():
        return []

    # coarse occupancy grid, then BFS over it
    gh, gw = mask.shape[0] // cell + 1, mask.shape[1] // cell + 1
    grid = np.zeros((gh, gw), bool)
    ys, xs = np.nonzero(mask)
    grid[ys // cell, xs // cell] = True

    seen = np.zeros_like(grid)
    comps = []
    for sy in range(gh):
        for sx in range(gw):
            if not grid[sy, sx] or seen[sy, sx]:
                continue
            stack = [(sy, sx)]
            seen[sy, sx] = True
            cells = []
            while stack:
                cy, cx = stack.pop()
                cells.append((cy, cx))
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < gh and 0 <= nx < gw and grid[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            stack.append((ny, nx))
            comps.append(cells)

    out = []
    for cells in comps:
        sel = np.zeros(len(xs), bool)
        for cy, cx in cells:
            sel |= (ys // cell == cy) & (xs // cell == cx)
        if int(sel.sum()) < min_px:
            continue
        out.append(Label(int(xs[sel].mean()), int(ys[sel].mean()), int(sel.sum())))
    return sorted(out, key=lambda l: l.x)


def split_sides(ls: list[Label], mid: int = 400):
    """H3 always deploys the defender (us) on the left half."""
    return [l for l in ls if l.x < mid], [l for l in ls if l.x >= mid]


class Lattice:
    """Lattice anchored on one label; gives pixel<->(col,row) conversions."""

    def __init__(self, anchor: Label,
                 v1=STEP_EAST, v2=STEP_SOUTH_EAST):
        self.anchor = anchor
        self.v1 = v1
        self.v2 = v2

    def center(self, c: int, r: int) -> tuple[int, int]:
        """Pixel of lattice cell (c, r): anchor + c*v1 + r*v2."""
        return (self.anchor.x + c * self.v1[0] + r * self.v2[0],
                self.anchor.y + c * self.v1[1] + r * self.v2[1])

    def hex_of(self, px: int, py: int) -> tuple[int, int]:
        """Inverse mapping (floats rounded to the nearest cell)."""
        import numpy as np
        a = np.array([[self.v1[0], self.v2[0]], [self.v1[1], self.v2[1]]], dtype=float)
        d = np.array([px - self.anchor.x, py - self.anchor.y], dtype=float)
        c, r = np.linalg.solve(a, d)
        return int(round(c)), int(round(r))

    def step_click(self, step) -> tuple[int, int]:
        """Pixel to click in order to move one cell along `step`."""
        return self.anchor.shifted(step)
