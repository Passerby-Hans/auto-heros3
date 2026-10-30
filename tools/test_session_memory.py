# -*- coding: utf-8 -*-
"""Compatibility entry shim (B0.5): the suite moved to tests/live/test_session_memory.py.

The kanonical copy lives in `tests/live/test_session_memory.py`; this shim keeps the historical
`python tools/test_session_memory.py` command working.  New commands should call the tests/ path (or the
unified `tools/run_offline_suite.py` entry).
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TARGET = _ROOT / "tests" / "live" / "test_session_memory.py"
sys.path.insert(0, str(_ROOT / "tools"))
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_TARGET.parent))
runpy.run_path(str(_TARGET), run_name="__main__")
