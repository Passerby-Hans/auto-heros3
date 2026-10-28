# -*- coding: utf-8 -*-
"""Probe town-screen buildings: click each candidate, report which window opens."""
import io
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
from vcmi_agent import VcmiAgent  # noqa: E402

SESSION = ROOT / 'var' / 'agent_console' / 'session.json'


def get_agent() -> VcmiAgent:
    sess = json.loads(SESSION.read_text(encoding='utf-8'))
    ag = VcmiAgent(sess['dir'])
    ag.hwnd = sess.get('hwnd')
    return ag


def cmd(ag: VcmiAgent, line: str, timeout: float = 10.0) -> str:
    before = len(ag.results())
    ag.send(line)
    t0 = time.time()
    while time.time() - t0 < timeout:
        got = [ln for ln in ag.results()[before:] if ln.startswith('OK') or ln.startswith('ERR')]
        if got:
            return got[-1]
        time.sleep(0.05)
    return '<<no result>>'


def ui_roots(ag: VcmiAgent) -> list:
    cmd(ag, 'ui_tree 2')
    time.sleep(0.4)
    path = ag.dir / 'ui_tree.json'
    try:
        d = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return []
    out = []
    for w in d.get('windows', []):
        root = w.get('root') or {}
        out.append(root.get('cls', '?'))
    return out


def main() -> int:
    ag = get_agent()
    candidates = [
        (606, 325), (279, 279), (931, 335), (512, 426), (797, 470),
        (381, 475), (728, 442), (685, 511), (956, 514),
        (210, 543), (265, 556), (307, 556),
        (204, 607), (259, 607), (314, 607),
    ]
    print('before:', ui_roots(ag))
    found = {}
    for (x, y) in candidates:
        cmd(ag, f'click {x} {y}')
        time.sleep(1.2)
        roots = ui_roots(ag)
        new = [r for r in roots if r not in ('class AdventureMapInterface', 'class CCastleInterface')]
        print(f'click {x},{y} -> windows={roots}')
        if new:
            found[(x, y)] = new
        # close anything that opened on top of the town screen
        for _ in range(3):
            roots = ui_roots(ag)
            top_extra = [r for r in roots if r not in ('class AdventureMapInterface', 'class CCastleInterface')]
            if not top_extra:
                break
            cmd(ag, 'close_top')
            time.sleep(0.6)
    print('\n=== mapping ===')
    for k, v in found.items():
        print(k, '->', v)
    return 0


if __name__ == '__main__':
    sys.exit(main())
