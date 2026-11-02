# -*- coding: utf-8 -*-
"""Process DPI awareness for capture.

The recorder reads client geometry and WGC frames in the same process.  ``vcmi_shot`` calls the
legacy ``SetProcessDPIAware`` (system-DPI-aware) at import; on a mixed-DPI host (primary 150%,
VDD 96%) that virtualizes the window/client by 1.5x while WGC reports physical pixels, so the
crop contract sees 1536x1152 against a 1028x815 frame and falls back to GDI.

This helper must run BEFORE importing vcmi_shot / creating windows.  It is a no-op on non-Windows
or when the API is unavailable; callers must treat a False result as a diagnostic, not fatal.
"""
from __future__ import annotations


def enable_per_monitor_v2() -> bool:
    try:
        import ctypes

        user32 = ctypes.WinDLL('user32', use_last_error=True)
        fn = getattr(user32, 'SetProcessDpiAwarenessContext', None)
        if fn is not None:
            fn.argtypes = [ctypes.c_void_p]
            fn.restype = ctypes.c_bool
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
            if fn(ctypes.c_void_p(-4)):
                return True
    except Exception:
        pass
    try:
        import ctypes

        shcore = ctypes.WinDLL('shcore', use_last_error=True)
        fn = getattr(shcore, 'SetProcessDpiAwareness', None)
        if fn is not None:
            fn.argtypes = [ctypes.c_int]
            fn.restype = ctypes.c_long
            # PROCESS_PER_MONITOR_DPI_AWARE = 2
            return fn(2) == 0
    except Exception:
        pass
    return False
