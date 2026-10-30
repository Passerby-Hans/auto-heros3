# -*- coding: utf-8 -*-
"""Repository path resolver + self check (single source of truth for the refactor).

Why this exists
---------------
Historically every script computed ``ROOT = Path(__file__).resolve().parents[1]`` (69 files
under tools/ + agent/) and injected it into ``sys.path`` (127 places).  Moving a file one
level deeper does not raise: it silently changes ROOT and can redirect runtime state,
evidence, recordings, manual search and vendor/contract lookups to the wrong directory.
A refactor therefore needs one explicit resolver plus an assertion that every path the
runtime uses still lives inside this repository.

B0.2 moved the implementation here (``src/auto_heros3/paths.py``) and turned
``tools/paths_check.py`` into a module-alias shim, so the historical import/CLI keeps working.
The MCP server implementation resolves ROOT through :func:`repo_root` instead of ``parents[N]``.

This module is read-only (it never creates directories) and importable without side effects.
The CLI behaviour is unchanged.

Usage
-----
    python -X utf8 tools/paths_check.py        # human-readable summary, exit 1 on error
    python -X utf8 tools/paths_check.py --json # machine-readable result
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# Files that must all exist in a checkout: used both as the root marker set and as the
# tracked-path existence check.  A checkout missing one of these is structurally broken.
ROOT_MARKERS = (
    "pyproject.toml",
    "tools/mcp_server_v2.py",
    "tools/vcmi_agent.py",
    "tools/vcmi_overlay_recorder.py",
    "tools/fog_path.py",
    "tools/vcmi_shot.py",
    "tools/bridge_contract.json",
    "platform/vcmi-patch/apply.py",
    "platform/vcmi-patch/client/AgentBridge.cpp",
    "platform/vcmi-patch/client/AgentBridge.h",
    "agent/__init__.py",
    "src/auto_heros3/__init__.py",
    "src/auto_heros3/paths.py",
    "src/auto_heros3/mcp/server.py",
    "src/auto_heros3/domain/action_specs.py",
    "src/auto_heros3/observation/digest.py",
    "src/auto_heros3/exploration/fog_path.py",
)

# Every path the runtime resolves from ROOT.  ``tracked=True`` means it must exist in a
# checkout; ``tracked=False`` paths are local-only (git-ignored dirs / a built client) and
# are reported as warnings when absent (they are never errors).
KEY_PATHS = {
    "tools_dir": ("tools", True),
    "mcp_server": ("tools/mcp_server_v2.py", True),
    "vcmi_agent": ("tools/vcmi_agent.py", True),
    "overlay_recorder": ("tools/vcmi_overlay_recorder.py", True),
    "fog_path": ("tools/fog_path.py", True),
    "vcmi_shot": ("tools/vcmi_shot.py", True),
    "bridge_contract": ("tools/bridge_contract.json", True),
    "src_dir": ("src", True),
    "src_pkg": ("src/auto_heros3", True),
    "paths_module": ("src/auto_heros3/paths.py", True),
    "mcp_server_impl": ("src/auto_heros3/mcp/server.py", True),
    "action_specs": ("src/auto_heros3/domain/action_specs.py", True),
    "digest_module": ("src/auto_heros3/observation/digest.py", True),
    "fog_path_impl": ("src/auto_heros3/exploration/fog_path.py", True),
    "agent_pkg": ("agent", True),
    "agent_h3": ("agent/h3", True),
    "knowledge_module": ("agent/knowledge/manual.py", True),
    "platform_patch": ("platform/vcmi-patch", True),
    "patch_apply": ("platform/vcmi-patch/apply.py", True),
    "bridge_cpp": ("platform/vcmi-patch/client/AgentBridge.cpp", True),
    "bridge_h": ("platform/vcmi-patch/client/AgentBridge.h", True),
    "doc_dir": ("doc", False),
    "doc_player": ("doc/player", False),
    "knowledge_docs": ("doc/knowledge", False),
    "var_dir": ("var", False),
    "state_dir": ("var/agent_mcp_v2", False),
    "recordings_dir": ("var/recordings", False),
    "vendor_dir": ("vendor", False),
    "client_exe": ("vendor/vcmi-src/build/bin/VCMI_client.exe", False),
}

RESOLVED_KEYS = tuple(KEY_PATHS)


def repo_root(start: Path | None = None) -> Path:
    """Resolve the repository root by markers, starting at *start* (default: this file).

    Raises RuntimeError when no ancestor contains all ROOT_MARKERS - a half-moved tree must
    fail loudly instead of guessing a directory.
    """
    here = Path(start or __file__).resolve()
    for candidate in (here, *here.parents):
        if all((candidate / marker).exists() for marker in ROOT_MARKERS):
            return candidate
    raise RuntimeError(
        "could not locate the auto-heros3 repository root: no ancestor of "
        f"{here} contains all markers {ROOT_MARKERS}"
    )


def check_paths(start: Path | None = None) -> dict:
    """Verify ROOT and every runtime path.

    Returns a dict::

        {"ok": bool, "root": str, "paths": {name: {path, exists, tracked, inside_root}},
         "errors": [str], "warnings": [str]}

    Errors: a marker/tracked path is missing, or any resolved path escapes the repository.
    Warnings: an optional local path (doc/, var/, vendor/) is absent - expected on a fresh
    clone and never a structural failure.
    """
    root = repo_root(start)
    result: dict = {"ok": True, "root": str(root), "paths": {}, "errors": [], "warnings": []}
    root_l = root
    for name, (rel, tracked) in KEY_PATHS.items():
        path = (root / rel).resolve()
        inside = path.is_relative_to(root_l)
        exists = path.exists()
        result["paths"][name] = {
            "path": str(path),
            "rel": rel,
            "exists": exists,
            "tracked": tracked,
            "inside_root": inside,
        }
        if not inside:
            result["errors"].append(f"{name}: {path} escapes the repository root {root}")
        elif tracked and not exists:
            result["errors"].append(f"{name}: tracked path is missing: {rel}")
        elif not tracked and not exists:
            result["warnings"].append(f"{name}: optional local path is absent: {rel}")
    result["ok"] = not result["errors"]
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Resolve and verify the auto-heros3 repository paths")
    ap.add_argument("--json", action="store_true", help="print the machine-readable result")
    a = ap.parse_args(argv)
    try:
        result = check_paths()
    except RuntimeError as exc:  # cannot even locate the root
        print(f"ERROR: {exc}")
        return 1
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(f"root: {result['root']}")
        for name, info in result["paths"].items():
            state = "ok" if info["exists"] else ("warn" if not info["tracked"] else "MISSING")
            print(f"  {name:18s} {state:7s} {info['rel']}")
        for warning in result["warnings"]:
            print(f"WARN: {warning}")
        for error in result["errors"]:
            print(f"ERROR: {error}")
        print("RESULT:", "OK" if result["ok"] else "ERROR")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
