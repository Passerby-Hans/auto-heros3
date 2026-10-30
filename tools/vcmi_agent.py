# -*- coding: utf-8 -*-
"""Compatibility entry for the VCMI client driver (B0.4 module-alias shim).

Implementation: ``src/auto_heros3/runtime/vcmi_agent.py``.  The historical import name and the
documented command line must keep working, and the offline suites patch this module's globals
(``vcmi_agent.subprocess.Popen``, ``vcmi_agent.EXE`` ...), so importing this module yields the
very same module object as the implementation (module alias, not a name copy).

Run:            python -X utf8 tools/vcmi_agent.py --map Maps/Arrogance
Implementation: src/auto_heros3/runtime/vcmi_agent.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT, _ROOT / "tools", _ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

if __name__ == "__main__":
    from auto_heros3.runtime.vcmi_agent import main
    raise SystemExit(main())
else:
    from auto_heros3.runtime import vcmi_agent as _implementation
    sys.modules[__name__] = _implementation
