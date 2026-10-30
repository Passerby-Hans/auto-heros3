# -*- coding: utf-8 -*-
"""Compatibility wrapper for the fog-frontier exploration module (B0.3 module-alias shim).

Implementation: ``src/auto_heros3/exploration/fog_path.py``.  Importing this module aliases it,
so ``import fog_path`` inside the MCP ``explore_toward`` tool, the offline test suite
(``tools/test_fog_path.py``) and the live CLIs (``tools/fog_path_live.py``, ``tools/fog_battle.py``)
returns the very same module object.  ``fog_path`` itself has no CLI entry point.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT, _ROOT / "tools", _ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from auto_heros3.exploration import fog_path as _implementation  # noqa: E402

sys.modules[__name__] = _implementation
