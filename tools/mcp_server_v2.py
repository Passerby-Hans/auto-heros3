# -*- coding: utf-8 -*-
"""Compatibility entry point for the MCP server (B0.2 module-alias shim).

The implementation lives in ``src/auto_heros3/mcp/server.py``.  The DSH MCP registration and
every existing command still launch THIS path, and the offline test suites
``import mcp_server_v2`` and monkeypatch its module globals, so this file behaves as a module
alias: after import, ``sys.modules['mcp_server_v2']`` IS the implementation module (not a copy
of its names).  That keeps ``M._agent`` / ``M._require_lease`` / ``M._lease_token`` patches
effective exactly as before.

Run:            python -X utf8 tools/mcp_server_v2.py            (stdio MCP server)
Self test:      python -X utf8 tools/mcp_server_v2.py --selftest
Implementation: src/auto_heros3/mcp/server.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT, _ROOT / "tools", _ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

if __name__ == "__main__":
    from auto_heros3.mcp.server import main
    raise SystemExit(main())
else:
    from auto_heros3.mcp import server as _implementation
    sys.modules[__name__] = _implementation
