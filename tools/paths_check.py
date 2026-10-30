# -*- coding: utf-8 -*-
"""Compatibility wrapper for the repository path self-check (B0.2 module-alias shim).

Implementation: ``src/auto_heros3/paths.py``.  Importing this module aliases it, so
``import paths_check`` in the offline suites returns the very same module object (patching
and identity checks keep working).  The CLI stays available at the historical path.

    python -X utf8 tools/paths_check.py [--json]
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT, _ROOT / "tools", _ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

if __name__ == "__main__":
    from auto_heros3.paths import main
    raise SystemExit(main())
else:
    from auto_heros3 import paths as _implementation
    sys.modules[__name__] = _implementation
