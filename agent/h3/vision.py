# -*- coding: utf-8 -*-
"""Vision helpers: screenshot OCR (rapidocr, zh) for UI reading."""
from __future__ import annotations
from functools import lru_cache


@lru_cache(maxsize=1)
def _ocr():
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR()


def ocr_image(path_or_array):
    """-> list of (text, x, y, conf) top-left coords."""
    result, _ = _ocr()(path_or_array)
    return [(t, int(b[0][0]), int(b[0][1]), float(c)) for b, t, c in (result or [])]


def ocr_screen(hwnd):
    from agent.h3.capture import grab
    import numpy as np
    img = np.asarray(grab(hwnd).convert('RGB'))
    result, _ = _ocr()(img)
    return [(t, int(b[0][0]), int(b[0][1]), float(c)) for b, t, c in (result or [])]


def find_text(items, keyword):
    """First OCR hit whose text contains keyword -> (x, y)."""
    for t, x, y, c in items:
        if keyword in t:
            return x, y
    return None


if __name__ == '__main__':
    from agent.h3.capture import h3_hwnds
    hwnd = h3_hwnds()[0][0]
    for t, x, y, c in ocr_screen(hwnd):
        print(f'y={y:3d} x={x:3d} conf={c:.2f}  {t}')
