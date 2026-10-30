# -*- coding: utf-8 -*-
"""Compatibility entry shim (B0.5): the suite moved to tests/offline/test_reliability_fixes.py.

The canonical copy lives in `tests/offline/test_reliability_fixes.py`; this shim keeps the
historical `python tools/test_*.py` command working.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TARGET = _ROOT / "tests" / "offline" / "test_reliability_fixes.py"
sys.path.insert(0, str(_ROOT / "tools"))
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_TARGET.parent))
runpy.run_path(str(_TARGET), run_name="__main__")
