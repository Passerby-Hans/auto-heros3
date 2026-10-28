import json
import io
import sys

path = sys.argv[1] if len(sys.argv) > 1 else r'E:\Hans_Files\auto-heros3\var\agent_ui\ui_tree.json'
d = json.load(io.open(path, encoding='utf-8'))
print('windows:', len(d.get('windows', [])))


def collect(node):
    if not isinstance(node, dict):
        return
    if 'cls' in node:
        yield node
    for k, v in node.items():
        if k in ('cls', 'x', 'y', 'w', 'h', 'depth'):
            continue
        if isinstance(v, dict):
            yield from collect(v)
        elif isinstance(v, list):
            for it in v:
                yield from collect(it)


for w in d.get('windows', []):
    print('--- window')
    for n in collect(w):
        pad = '  ' * int(n.get('depth', 0))
        print(f"{pad}{n['cls']}  {n['x']},{n['y']} {n['w']}x{n['h']}")
