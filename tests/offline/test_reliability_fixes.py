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

import json
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mcp_server_v2 as M  # noqa: E402
from test_action_contract import Env, state  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition, detail: str = '') -> None:
    RESULTS.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


def test_battle_actions_concurrent_cache() -> None:
    """F5: two concurrent battle_actions queries must not exchange the shared cache file."""
    with Env() as env:
        class ConcurrentBattleAgent:
            def __init__(self):
                self.calls = 0
                self.first_written = threading.Event()
                self.call_lock = threading.Lock()

            def state(self, timeout: float = 10.0) -> dict:
                return state(battle={'active': True})

            def execute(self, line: str, verify=None, result_timeout: float = 20.0):
                with self.call_lock:
                    self.calls += 1
                    call = self.calls
                active = 11 if call == 1 else 222
                path = M._dir() / 'battle_actions.json'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({'active': active,
                                            'targets': [{'id': active}]}), encoding='utf-8')
                if call == 1:
                    self.first_written.set()
                    time.sleep(0.2)  # give the second query a chance to overwrite without a lock
                return types.SimpleNamespace(seq=call, status='COMPLETED',
                                             line=f'seq={call} OK battle_actions', detail='')

        agent = ConcurrentBattleAgent()
        env.agent(agent)
        results: dict = {}

        def query(name: str) -> None:
            try:
                results[name] = M.battle_actions()
            except Exception as exc:  # noqa: BLE001 - reported below
                results[name] = {'error': repr(exc)}

        first = threading.Thread(target=query, args=('a',), name='reader-a')
        first.start()
        assert agent.first_written.wait(5)
        second = threading.Thread(target=query, args=('b',), name='reader-b')
        second.start()
        first.join(5)
        second.join(5)
        check('R5 concurrent battle_actions queries cannot exchange cache payloads',
              results.get('a', {}).get('active') == 11
              and results.get('b', {}).get('active') == 222,
              str({key: value.get('active') if isinstance(value, dict) else value
                   for key, value in results.items()}))


def main() -> int:
    test_battle_actions_concurrent_cache()
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
