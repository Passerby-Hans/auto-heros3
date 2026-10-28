# -*- coding: utf-8 -*-
"""Print widgets of one window within a rectangle (for locating town-screen icons)."""
import io
import json
import sys

path = sys.argv[1] if len(sys.argv) > 1 else r'E:\Hans_Files\auto-heros3\var\agent_ui\ui_tree.json'
win = int(sys.argv[2]) if len(sys.argv) > 2 else 1
x0, y0, x1, y1 = (int(v) for v in (sys.argv[3:7] if len(sys.argv) >= 7 else (0, 440, 340, 720)))

d = json.load(io.open(path, encoding='utf-8'))
windows = d.get('windows', [])
if win >= len(windows):
    print('no such window')
    raise SystemExit(1)


def collect(node, out):
    if not isinstance(node, dict):
        return
    if 'cls' in node:
        out.append(node)
    for k, v in node.items():
        if k in ('cls', 'x', 'y', 'w', 'h', 'depth'):
            continue
        if isinstance(v, dict):
            collect(v, out)
        elif isinstance(v, list):
            for it in v:
                collect(it, out)


nodes = []
collect(windows[win], nodes)
roots = [n for n in nodes if n.get('depth') == 0]
print('window', win, 'root', roots[0]['cls'] if roots else '?', 'nodes', len(nodes))
for n in sorted(nodes, key=lambda n: (n['y'], n['x'])):
    if x0 <= n['x'] <= x1 and y0 <= n['y'] <= y1 and n.get('w', 0) > 0:
        print(f"  {n['cls']}  {n['x']},{n['y']} {n['w']}x{n['h']} d={n['depth']}")
