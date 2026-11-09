# -*- coding: utf-8 -*-
"""Compatibility shim: canonical suite lives in tests/offline/test_native_stop.py."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TARGET = _ROOT / 'tests' / 'offline' / 'test_native_stop.py'
sys.path.insert(0, str(_ROOT / 'src'))
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_TARGET.parent))
runpy.run_path(str(_TARGET), run_name='__main__')
