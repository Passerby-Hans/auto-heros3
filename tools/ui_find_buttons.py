# -*- coding: utf-8 -*-
"""Summarise a ui_tree.json dump: per-window root class + clickable widgets with rects."""
import io
import json
import sys

path = sys.argv[1] if len(sys.argv) > 1 else r'E:\Hans_Files\auto-heros3\var\agent_ui\ui_tree.json'
d = json.load(io.open(path, encoding='utf-8'))
windows = d.get('windows', [])
print('windows:', len(windows))


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


for i, w in enumerate(windows):
    nodes = []
    collect(w, nodes)
    roots = [n for n in nodes if n.get('depth') == 0]
    root_cls = roots[0]['cls'] if roots else '?'
    print(f'--- window {i}: root={root_cls} nodes={len(nodes)}')
    for n in nodes:
        cls = n['cls']
        if any(k in cls for k in ('Button', 'Slider', 'Selectable', 'TextBox', 'Label', 'Area')):
            print(f"    {cls}  {n['x']},{n['y']} {n['w']}x{n['h']} d={n['depth']}")
