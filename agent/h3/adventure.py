# -*- coding: utf-8 -*-
"""Adventure-map pixel <-> tile, done deterministically.

Why this design survives variable map sizes and surface/underground layers:
  * we never assume a map size: everything is expressed as *tile offsets from the
    selected hero*, and H3 recenters the camera on that hero after each action,
    so the anchor re-establishes itself after every move / layer switch;
  * the layer (surface vs underground) only changes *which* tile array is drawn —
    the hero is still the anchor, and the iso basis is identical.

Deterministic inputs only:
  * the hero's screen anchor comes from a hover *text probe* (scan pixels, read the
    status bar), never from a vision model's coordinate guess;
  * the iso basis (32,16)/(-32,16) is the engine's fixed projection;
  * movement is verified by measuring the map-content shift (template match), which
    must equal minus the hero's pixel displacement.
"""
from __future__ import annotations

import numpy as np

# iso projection constants (client pixels per tile step)
B_MAPX = (32, 16)     # +1 tile east  (map x + 1)
B_MAPY = (-32, 16)    # +1 tile south (map y + 1)

VIEW = (30, 60, 600, 540)   # adventure-map viewport in the 800x600 client


def tile_offset_to_pixel(anchor, dx: int, dy: int) -> tuple[int, int]:
    """Pixel for the tile `dx` east / `dy` south of the anchor tile."""
    return (anchor[0] + dx * B_MAPX[0] + dy * B_MAPY[0],
            anchor[1] + dx * B_MAPX[1] + dy * B_MAPY[1])


def pixel_to_tile_offset(anchor, px: int, py: int) -> tuple[float, float]:
    """Inverse: fractional tile offset (dx, dy) of a pixel from the anchor."""
    vx, vy = px - anchor[0], py - anchor[1]
    # solve [32 -32; 16 16] * (dx,dy) = (vx,vy)
    dx = (vx / 32.0 + vy / 16.0) / 2.0
    dy = (vy / 16.0 - vx / 32.0) / 2.0
    return dx, dy


def find_hero_anchor(name: str, region=(280, 240, 460, 380), step=8):
    """Scan pixels for the hover text carrying the hero's name -> hero anchor.

    Returns (anchor, hits).  `anchor` is the tile-centre pixel of the hero (taken
    as the median of the hits, which is robust against sprite vs tile offsets).
    """
    from agent.h3 import lab
    hits = []
    x0, y0, x1, y1 = region
    for y in range(y0, y1, step):
        for x in range(x0, x1, step):
            lab.shover(x, y, 0.28)
            t = lab.status_bar_text()
            if t and name in t:
                hits.append((x, y, t))
    if not hits:
        return None, hits
    ax = int(np.median([h[0] for h in hits]))
    ay = int(np.median([h[1] for h in hits]))
    return (ax, ay), hits


def _gray(img):
    return np.asarray(img.convert('L')).astype(np.int32)


def find_hint(name: str, region=(40, 70, 600, 530), step=24, max_hits=12,
              contains=True):
    """Scan pixels and return those whose rollover text mentions `name`.

    This is the *deterministic* hit-test for the adventure map: H3's rollover bar
    is the engine telling us what is under a pixel, so we never have to trust a
    vision model's coordinate estimate.  Cheap because the search is coarse
    (default 24 px) and refines only around hits.
    """
    from agent.h3 import lab
    x0, y0, x1, y1 = region
    hits = []
    for y in range(y0, y1, step):
        for x in range(x0, x1, step):
            lab.shover(x, y, 0.22)
            t = lab.status_bar_text()
            if not t or not t.strip():
                continue
            if (name in t) if contains else (t.strip() == name):
                hits.append((x, y, t))
                if len(hits) >= max_hits:
                    return hits
    return hits


def refine_hint(name: str, seed, radius=24, step=4):
    """Local refinement around a seed hit -> the densest pixel for that object."""
    from agent.h3 import lab
    x0, y0 = seed
    best = []
    for y in range(y0 - radius, y0 + radius + 1, step):
        for x in range(x0 - radius, x0 + radius + 1, step):
            lab.shover(x, y, 0.20)
            t = lab.status_bar_text()
            if t and name in t:
                best.append((x, y))
    if not best:
        return seed
    ax = int(np.median([b[0] for b in best]))
    ay = int(np.median([b[1] for b in best]))
    return (ax, ay)


def click_hint(name: str, region=(40, 70, 600, 530), step=24, tries=2, refine=True):
    """Click the object whose rollover text mentions `name`.

    Returns (pixel, hits) or (None, []) when the object is not in view.
    """
    from agent.h3 import lab
    hits = find_hint(name, region, step)
    if not hits:
        return None, []
    px, py, _ = hits[0]
    if refine:
        px, py = refine_hint(name, (px, py))
    for _ in range(tries):
        lab.sclick(px, py, wait=1.2)
    return (px, py), hits


def content_shift(before, after, patch=(330, 300, 70, 44), radius=200):
    """Template-match a map patch: returns (dx,dy) of the map content."""
    a, b = _gray(before), _gray(after)
    px, py, pw, ph = patch
    tpl = a[py:py + ph, px:px + pw]
    best, bestpos = None, None
    for yy in range(max(0, py - radius), min(b.shape[0] - ph, py + radius)):
        for xx in range(max(0, px - radius), min(b.shape[1] - pw, px + radius)):
            d = np.abs(b[yy:yy + ph, xx:xx + pw] - tpl).mean()
            if best is None or d < best:
                best, bestpos = d, (xx, yy)
    return (bestpos[0] - px, bestpos[1] - py), best
