# -*- coding: utf-8 -*-
"""Explicit LIVE compatibility entry; never invoked by the offline runner."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TARGET = _ROOT / 'tests' / 'live' / 'test_native_recording_e2e.py'
sys.path.insert(0, str(_ROOT / 'src'))
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_TARGET.parent))
runpy.run_path(str(_TARGET), run_name='__main__')
