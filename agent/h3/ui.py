# -*- coding: utf-8 -*-
"""Background UI navigation: OCR-located PostMessage clicking.
Zero focus, zero real-cursor impact. All coordinates are game client coords.
"""
from __future__ import annotations
import time


def ocr_clicks(hwnd) -> list[tuple[str, int, int, float]]:
    """OCR the game window -> [(text, center_x, center_y, conf)]."""
    from agent.h3.capture import grab
    import numpy as np
    from agent.h3.vision import _ocr
    img = np.asarray(grab(hwnd).convert('RGB'))
    result, _ = _ocr()(img)
    out = []
    for box, text, conf in (result or []):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append((text, int(sum(xs) / 4), int(sum(ys) / 4), float(conf)))
    return out


def click_until(hwnd, keyword: str, new_keyword: str | None = None,
                attempts=4, settle=1.8) -> bool:
    """Click `keyword` until the screen changes (or `new_keyword` appears).
    The H3 menu sometimes swallows the first PostMessage click - retry."""
    from agent.h3.binput import click
    for i in range(attempts):
        before = ' '.join(screen_texts(hwnd))
        hit = next(((cx, cy) for t, cx, cy, c in ocr_clicks(hwnd) if keyword in t), None)
        if hit is None:
            raise RuntimeError(f'UI text not found: {keyword!r}')
        click(hwnd, *hit)
        time.sleep(settle)
        after = ' '.join(screen_texts(hwnd))
        if new_keyword is not None:
            if new_keyword in after and new_keyword not in before:
                return True
        elif after != before:
            return True
    return False


def click_text(hwnd, keyword: str, settle=1.6, require=True) -> bool:
    """Find any OCR hit containing `keyword`, click its center via PostMessage."""
    for t, cx, cy, c in ocr_clicks(hwnd):
        if keyword in t:
            from agent.h3.binput import click
            click(hwnd, cx, cy)
            time.sleep(settle)
            return True
    if require:
        raise RuntimeError(f'UI text not found: {keyword!r}')
    return False


def wait_screen(hwnd, keyword: str, timeout=20.0, poll=1.0):
    """Wait until `keyword` appears on screen; returns items when found."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        items = ocr_clicks(hwnd)
        for t, *_ in items:
            if keyword in t:
                return items
        time.sleep(poll)
    raise TimeoutError(f'screen with {keyword!r} not seen in {timeout}s')


def screen_texts(hwnd) -> list[str]:
    return [t for t, *_ in ocr_clicks(hwnd)]


def dump_screen(hwnd, tag='screen'):
    from PIL import Image
    from agent.h3.capture import grab
    from pathlib import Path
    p = Path('var/shots') / f'{tag}.png'
    grab(hwnd).save(p)
    return p
