# -*- coding: utf-8 -*-
"""B0.1 offline guards: the refactor baseline that later batches must not break.

Covers four things the B0 refactor can silently damage:

  P  repository path resolution - root by markers, tracked paths present, nothing escapes;
  C  the pinned engine contract still parses and keeps its shape;
  T  the MCP tool surface the LLM client depends on - stdio handshake + 33 tool names and
     each input JSON schema compared with tools/tool_surface_baseline.json;
  I  isolation - this test starts only the MCP server process (no VCMI client, no game
     action) with TEMPORARY state/saves/recordings/memory paths, and verifies the formal
     evidence ledger was not touched.

Run:
    .venv\\Scripts\\python.exe -X utf8 tools\\test_b0_guards.py

This suite is in the offline whitelist of tools/run_offline_suite.py.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import paths_check  # noqa: E402  (tools/paths_check.py -> module alias of auto_heros3.paths)
import mcp_server_v2 as mcp_entry  # noqa: E402  (tools/mcp_server_v2.py -> alias of the impl)
import fog_path as fog_entry  # noqa: E402  (tools/fog_path.py -> alias of the impl)

from auto_heros3 import paths as paths_impl  # noqa: E402
from auto_heros3.mcp import server as server_impl  # noqa: E402
from auto_heros3.domain import action_specs as action_specs_impl  # noqa: E402
from auto_heros3.exploration import fog_path as fog_impl  # noqa: E402
from auto_heros3.observation import digest as digest_impl  # noqa: E402

BASELINE_FILE = ROOT / "tools" / "tool_surface_baseline.json"
CONTRACT_FILE = ROOT / "tools" / "bridge_contract.json"
FORMAL_LEDGER = ROOT / "var" / "agent_mcp_v2" / "actions.jsonl"
SERVER = ROOT / "tools" / "mcp_server_v2.py"
SERVER_IMPL = ROOT / "src" / "auto_heros3" / "mcp" / "server.py"
FOG_ENTRY = ROOT / "tools" / "fog_path.py"
FOG_IMPL = ROOT / "src" / "auto_heros3" / "exploration" / "fog_path.py"
SHA256_RE = re.compile(r"[0-9a-f]{64}")

RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: object = "") -> None:
    ok = bool(ok)
    RESULTS.append((label, ok, str(detail) if detail else ""))
    print(f"{'PASS' if ok else 'FAIL'} {label}" + (f"  ({detail})" if detail and not ok else ""))


# ------------------------------------------------------------------ MCP stdio snapshot
def _get(obj, *names, default=None):
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _tool_entry(tool) -> tuple[str | None, dict | None]:
    data = tool.model_dump() if hasattr(tool, "model_dump") else {}
    name = data.get("name") or _get(tool, "name")
    schema = (data.get("input_schema") or data.get("inputSchema")
              or _get(tool, "input_schema", "inputSchema"))
    return name, schema


def capture_tool_surface() -> tuple[dict, list[tuple[str | None, dict | None]], str, str]:
    """Spawn the MCP server over stdio, initialize + tools/list only.

    Returns (server_info, tools, error_text, temp_dir).  The server gets temporary
    state/saves/recordings/memory directories so nothing formal can be touched.
    """
    from mcp import StdioServerParameters
    from mcp.client.session import ClientSession
    from mcp.client.stdio import stdio_client

    tmp = tempfile.mkdtemp(prefix="b0-guards-")
    tmp_path = Path(tmp)
    env = dict(os.environ)
    env.pop("AUTO_HEROS3_SAVE_SUBDIR", None)
    env.update({
        "PYTHONIOENCODING": "utf8",
        "AUTO_HEROS3_STATE_DIR": str(tmp_path / "state"),
        "AUTO_HEROS3_SAVES_DIR": str(tmp_path / "saves"),
        "AUTO_HEROS3_RECORDINGS_DIR": str(tmp_path / "recordings"),
        "AUTO_HEROS3_MEMORY_ROOT": str(tmp_path / "player"),
    })

    async def _run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-X", "utf8", str(SERVER)],
            env=env,
            cwd=str(ROOT),
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                init = await asyncio.wait_for(session.initialize(), 120)
                tools = await asyncio.wait_for(session.list_tools(), 120)
        return init, tools

    try:
        init, listed = asyncio.run(asyncio.wait_for(_run(), 180))
        server_info = _get(init, "server_info", "serverInfo")
        if hasattr(server_info, "model_dump"):
            server_info = server_info.model_dump()
        return (server_info or {}), [_tool_entry(t) for t in listed.tools], "", tmp
    except Exception as exc:  # noqa: BLE001 - the guard must report, never crash the suite
        return {}, [], f"{type(exc).__name__}: {exc}", tmp


def read_formal_ledger() -> str:
    try:
        return FORMAL_LEDGER.read_text(encoding="utf-8")
    except OSError:
        return "<absent>"


def main() -> int:
    # ---------------------------------------------------------------- P: path resolution
    try:
        root = paths_check.repo_root()
        root_error = ""
    except RuntimeError as exc:
        root, root_error = None, str(exc)
    check("B0-P1 repo root resolves by markers", root == ROOT, f"{root} != {ROOT} {root_error}")
    report = paths_check.check_paths()
    check("B0-P2 path self-check reports no structural error", report["ok"], report["errors"])
    missing = sorted(k for k, v in report["paths"].items() if v["tracked"] and not v["exists"])
    check("B0-P3 every tracked runtime path exists", not missing, missing)
    escaping = sorted(k for k, v in report["paths"].items() if not v["inside_root"])
    check("B0-P4 every resolved runtime path stays inside the repo", not escaping, escaping)

    # ---------------------------------------------------------------- M: module-alias shims
    # B0.2 moved the implementation into src/ and kept the historical entry paths as shims.
    # These checks pin the contract that matters for the offline suites and the DSH entry:
    # importing the historical module name must yield the very same module object.
    check("B0-P5 tools/paths_check.py is a module alias of auto_heros3.paths",
          paths_check is paths_impl, f"{paths_check.__name__} != {paths_impl.__name__}")
    check("B0-M1 tools/mcp_server_v2.py is a module alias of auto_heros3.mcp.server",
          mcp_entry is server_impl, f"{mcp_entry.__name__} != {server_impl.__name__}")
    try:
        impl_file = Path(mcp_entry.__file__).resolve()
    except (OSError, TypeError):  # pragma: no cover - only when the import failed
        impl_file = None
    check("B0-M2 the imported MCP entry resolves to src/auto_heros3/mcp/server.py",
          impl_file == SERVER_IMPL.resolve(), impl_file)
    entry_size = SERVER.stat().st_size if SERVER.exists() else 0
    impl_size = SERVER_IMPL.stat().st_size if SERVER_IMPL.exists() else 0
    check("B0-M3 the historical entry is a thin shim and the implementation is the large file",
          entry_size < 5_000 and impl_size > 200_000, f"entry={entry_size} impl={impl_size}")
    check("B0-M4 tools/fog_path.py is a module alias of auto_heros3.exploration.fog_path",
          fog_entry is fog_impl, f"{fog_entry.__name__} != {fog_impl.__name__}")
    fog_entry_size = FOG_ENTRY.stat().st_size if FOG_ENTRY.exists() else 0
    fog_impl_size = FOG_IMPL.stat().st_size if FOG_IMPL.exists() else 0
    check("B0-M5 the fog_path entry is a thin shim and the implementation is the large file",
          fog_entry_size < 5_000 and fog_impl_size > 10_000,
          f"entry={fog_entry_size} impl={fog_impl_size}")
    # B0.3 pure-module extraction contract: the server must expose the very same objects the
    # new modules define (call sites and the offline suites rely on these identities).
    check("B0-M6 the server action table is the auto_heros3.domain.action_specs object",
          mcp_entry.ACTION_SPECS is action_specs_impl.ACTION_SPECS
          and mcp_entry.ACTIONS is action_specs_impl.ACTIONS)
    check("B0-M7 the server digest helpers are the auto_heros3.observation.digest objects",
          mcp_entry._digest is digest_impl._digest
          and mcp_entry._hero_digest is digest_impl._hero_digest
          and mcp_entry._truncate is digest_impl._truncate)

    # ---------------------------------------------------------------- C: engine contract
    try:
        contract = json.loads(CONTRACT_FILE.read_text(encoding="utf-8"))
        contract_error = ""
    except (OSError, ValueError) as exc:
        contract, contract_error = {}, f"{type(exc).__name__}: {exc}"
    client = (contract or {}).get("client") or {}
    check("B0-C1 bridge contract parses", bool(contract) and not contract_error, contract_error)
    check("B0-C2 contract pins protocol 2", client.get("protocol") == 2, client.get("protocol"))
    pinned = str(client.get("sha256") or "")
    check("B0-C3 contract pins a full client sha256", bool(SHA256_RE.fullmatch(pinned)),
          pinned[:16] + "..." if pinned else "missing")

    # ---------------------------------------------------------------- I: formal ledger guard
    ledger_before = read_formal_ledger()

    # ---------------------------------------------------------------- T: MCP tool surface
    baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    expected = {item["name"]: item["input_schema"] for item in baseline["tools"]}
    server_info, tools, error, temp_dir = capture_tool_surface()
    check("B0-T1 MCP stdio handshake + tools/list succeeds", not error, error)
    check("B0-T2 server identifies as auto-heros3",
          str(server_info.get("name") or "") == "auto-heros3", server_info.get("name"))
    actual = {}
    for name, schema in tools:
        if name is not None:
            actual[name] = schema
    check("B0-T3 tool count matches baseline", len(tools) == baseline["tool_count"],
          f"{len(tools)} != {baseline['tool_count']}")
    check("B0-T4 tool name set matches baseline", set(actual) == set(expected),
          sorted(set(actual) ^ set(expected)))
    for name in sorted(expected):
        check(f"B0-T5/{name} input schema matches baseline",
              name in actual and actual[name] == expected[name],
              "tool missing or schema changed")
    check("B0-T6 no unexpected tools beyond the baseline",
          not (set(actual) - set(expected)), sorted(set(actual) - set(expected)))

    # ---------------------------------------------------------------- I: isolation
    ledger_after = read_formal_ledger()
    check("B0-I1 the formal actions ledger was not modified by this test",
          ledger_before == ledger_after,
          f"{len(ledger_before)} -> {len(ledger_after)} bytes")
    shutil.rmtree(temp_dir, ignore_errors=True)
    check("B0-I2 temporary isolation directory was cleaned up",
          not Path(temp_dir).exists(), temp_dir)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
