# -*- coding: utf-8 -*-
"""Compatibility entry for the overlay recorder (B0.4b module-alias shim).

Implementation: ``src/auto_heros3/recording/overlay_recorder.py``.  The MCP server spawns THIS
path as a separate process (so reloading the MCP server never interrupts a recording), and the
offline recording suites import the module and patch its attributes; importing this module
therefore yields the very same implementation module object (module alias, not a name copy),
while executing it forwards argv to the implementation ``main()``.

Run:            python -X utf8 tools/vcmi_overlay_recorder.py --dir <state> --out <dir> ...
Implementation: src/auto_heros3/recording/overlay_recorder.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT, _ROOT / "tools", _ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

if __name__ == "__main__":
    from auto_heros3.recording.overlay_recorder import main
    raise SystemExit(main())
else:
    from auto_heros3.recording import overlay_recorder as _implementation
    sys.modules[__name__] = _implementation
