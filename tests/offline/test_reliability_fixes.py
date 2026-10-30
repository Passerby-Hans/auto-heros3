# -*- coding: utf-8 -*-
"""B0.5 reliability: the duplicated `_file_signature` is split into UI / save helpers.

Why: two definitions shared one name in the MCP server (full-file tuple for UI freshness,
64 KiB head-hash dict for engine save-file stability).  The later definition silently shadowed
the earlier one, so UI freshness actually used the save semantics (and a large ui_tree dump
could change beyond the head while being treated as unchanged).  The helpers are now named
`_ui_tree_signature` (full-file sha256) and `_save_file_signature` (cheap head hash), and this
suite guards the split with a >64 KiB file.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mcp_server_v2 as M  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition, detail: str = '') -> None:
    RESULTS.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix='reliability-signature-') as tmp:
        path = Path(tmp) / 'ui_tree.json'
        check('R1 missing UI/save files have no signature',
              M._ui_tree_signature(path) is None and M._save_file_signature(path) is None)
        payload = bytearray(b'A' * 70_000)
        path.write_bytes(payload)
        ui_before = M._ui_tree_signature(path)
        save_before = M._save_file_signature(path)
        payload[68_000] = ord('B')  # beyond the 64 KiB head that save signatures hash
        path.write_bytes(payload)
        ui_after = M._ui_tree_signature(path)
        save_after = M._save_file_signature(path)
        check('R2 the UI signature hashes the whole file (change beyond 64 KiB is detected)',
              ui_before is not None and ui_after is not None and ui_before != ui_after,
              f'{ui_before} -> {ui_after}')
        check('R3 the save signature stays a cheap head hash (same head despite the change)',
              save_before is not None and save_after is not None
              and save_before.get('sha256_head') == save_after.get('sha256_head'),
              f"{save_before.get('sha256_head')} -> {save_after.get('sha256_head')}")
        check('R4 the duplicated `_file_signature` name is gone and helpers are explicit',
              not hasattr(M, '_file_signature')
              and callable(M._ui_tree_signature) and callable(M._save_file_signature))
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f'\n{passed}/{len(RESULTS)} checks passed')
    return 0 if passed == len(RESULTS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
