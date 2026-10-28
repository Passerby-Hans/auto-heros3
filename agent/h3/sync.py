# -*- coding: utf-8 -*-
"""Action/screen synchronization (user-raised issue: memory changes instantly,
the rendered frame lags). Primitives:
- frames_stable(): consecutive identical screenshots == animation finished
- mem_stable(): watched memory values stopped changing
- settle(): both, before reading the screen or issuing the next input
"""
from __future__ import annotations
import hashlib, time


def _digest(img_bytes: bytes) -> str:
    return hashlib.md5(img_bytes).hexdigest()


def grab_digest(hwnd) -> str:
    import numpy as np
    from agent.h3.capture import grab
    return _digest(np.asarray(grab(hwnd).convert('L')).tobytes())


def frames_stable(hwnd, need: int = 3, interval: float = 0.3, timeout: float = 10.0) -> bool:
    """True if `need` consecutive frames are identical (animation/render settled)."""
    t0 = time.time()
    same = 0
    last = None
    while time.time() - t0 < timeout:
        d = grab_digest(hwnd)
        if d == last:
            same += 1
            if same >= need:
                return True
        else:
            same = 0
            last = d
        time.sleep(interval)
    return False


def mem_stable(proc, addrs, need: int = 3, interval: float = 0.15, timeout: float = 5.0) -> bool:
    t0 = time.time()
    same = 0
    last = None
    while time.time() - t0 < timeout:
        cur = [proc.i32(a) for a in addrs]
        if cur == last:
            same += 1
            if same >= need:
                return True
        else:
            same = 0
            last = cur
        time.sleep(interval)
    return False


def settle(hwnd, proc, addrs=(), timeout: float = 10.0) -> bool:
    """Wait for memory quiescence AND frame stability before the next step."""
    if addrs:
        mem_stable(proc, list(addrs), timeout=timeout)
    return frames_stable(hwnd, timeout=timeout)
