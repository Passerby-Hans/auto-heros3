# -*- coding: utf-8 -*-
"""Compatibility entry shim (B1.1): the suite lives in tests/offline/test_knowledge_sources.py.

The canonical copy is ``tests/offline/test_knowledge_sources.py``; this shim keeps the
historical ``python tools/test_knowledge_sources.py`` command working.  New commands should
call the tests/ path (or the unified ``tools/run_offline_suite.py`` entry, once the Lead
adds the suite to the runner whitelist).
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TARGET = _ROOT / "tests" / "offline" / "test_knowledge_sources.py"
sys.path.insert(0, str(_ROOT / "tools"))
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_TARGET.parent))
runpy.run_path(str(_TARGET), run_name="__main__")
