# -*- coding: utf-8 -*-
"""Compatibility wrapper for the off-screen window capture helpers (B0.4 module-alias shim).

Implementation: ``src/auto_heros3/runtime/vcmi_shot.py``.  Importing this module aliases it, so
``import vcmi_shot`` in the MCP server, the recorder, the exploration CLIs and the offline tests
returns the very same module object (attribute patches keep working).  The historical
``python tools/vcmi_shot.py [out.png]`` debug entry is preserved.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT, _ROOT / "tools", _ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

if __name__ == "__main__":
    import runpy
    runpy.run_module("auto_heros3.runtime.vcmi_shot", run_name="__main__", alter_sys=False)
else:
    from auto_heros3.runtime import vcmi_shot as _implementation
    sys.modules[__name__] = _implementation
