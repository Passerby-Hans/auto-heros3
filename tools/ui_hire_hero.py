# -*- coding: utf-8 -*-
"""Locate the tavern in the town screen and hire a hero through UI clicks."""
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


def cmd(ag, line, timeout=10.0):
    before = len(ag.results())
    ag.send(line)
    t0 = time.time()
    while time.time() - t0 < timeout:
        got = [ln for ln in ag.results()[before:] if ln.startswith('OK') or ln.startswith('ERR')]
        if got:
            return got[-1]
        time.sleep(0.05)
    return '<<no result>>'


def tree(ag):
    cmd(ag, 'ui_tree 6')
    time.sleep(0.3)
    try:
        return json.loads((ag.dir / 'ui_tree.json').read_text(encoding='utf-8'))
    except Exception:
        return {}


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


def window_nodes(d, i):
    ws = d.get('windows', [])
    if i >= len(ws):
        return [], ''
    out = []
    collect(ws[i], out)
    return out, (ws[i].get('root') or {}).get('cls', '')


def state(ag):
    return ag.state(10)


def main() -> int:
    ag = get_agent()
    print(cmd(ag, 'open_town 0'))
    time.sleep(2.0)
    d = tree(ag)
    ws = d.get('windows', [])
    town_idx = next((i for i in range(len(ws)) if 'CastleInterface' in window_nodes(d, i)[1]), None)
    if town_idx is None:
        print('no town window')
        return 1
    nodes, _ = window_nodes(d, town_idx)
    candidates = [n for n in nodes if n['cls'].endswith('CBuildingRect')]
    # also the left-panel clickable icons (dwellings / special buildings)
    candidates += [n for n in nodes
                   if ('CAnimImage' in n['cls'] or 'LRClickableArea' in n['cls'])
                   and n['x'] < 340 and n['y'] > 430 and n['w'] >= 20]
    print('candidates:', [(n['cls'].split('::')[-1].rstrip('E'), n['x'], n['y'], n['w'], n['h'])
                          for n in candidates])

    def extras(d):
        ws = d.get('windows', [])
        return [(i, window_nodes(d, i)[1]) for i in range(len(ws))
                if not any(k in window_nodes(d, i)[1]
                           for k in ('AdventureMapInterface', 'CCastleInterface'))]

    def close_extras():
        for _ in range(4):
            d = tree(ag)
            ex = extras(d)
            if not ex:
                return
            cmd(ag, 'close_window')
            time.sleep(0.6)

    close_extras()
    tavern_found = False
    for n in candidates:
        cx, cy = n['x'] + n['w'] // 2, n['y'] + n['h'] // 2
        cmd(ag, f'click {cx} {cy}')
        time.sleep(1.2)
        d = tree(ag)
        ex = extras(d)
        if ex:
            names = [c for _, c in ex]
            print(f'click ({cx},{cy}) -> {names}')
            if any('TavernWindow' in c for c in names):
                print(f'tavern opened at ({cx},{cy})')
                tavern_found = True
                break
            close_extras()
        else:
            print(f'click ({cx},{cy}) -> nothing')
    if not tavern_found:
        print('tavern not found')
        return 1

    d = tree(ag)
    ws = d.get('windows', [])
    tavern_idx = next(i for i in range(len(ws)) if 'TavernWindow' in window_nodes(d, i)[1])
    nodes, _ = window_nodes(d, tavern_idx)
    portraits = [n for n in nodes if 'HeroPortrait' in n['cls']]
    buttons = [n for n in nodes if n['cls'].endswith('CButton') and n['w'] >= 40]
    print('portraits:', [(n['x'], n['y'], n['w'], n['h']) for n in portraits])
    print('buttons:', [(n['x'], n['y'], n['w'], n['h']) for n in buttons])

    before = state(ag)
    g0 = before['resources']['gold']
    h0 = len(before['heroes'])
    if portraits:
        p = portraits[0]
        cmd(ag, f"click {p['x'] + p['w'] // 2} {p['y'] + p['h'] // 2}")
        time.sleep(1.6)
        now = state(ag)
        print('after portrait click: gold', g0, '->', now['resources']['gold'], 'heroes', h0, '->', len(now['heroes']))
        if len(now['heroes']) > h0:
            print('HIRED via portrait click')
            return 0
    # try the recruit (hand) button - it is the widest button on the portrait row
    if buttons:
        b = max(buttons, key=lambda n: n['w'])
        cmd(ag, f"click {b['x'] + b['w'] // 2} {b['y'] + b['h'] // 2}")
        time.sleep(2.0)
        now = state(ag)
        print('after recruit click: gold', g0, '->', now['resources']['gold'], 'heroes', h0, '->', len(now['heroes']))
        if len(now['heroes']) > h0:
            print('HIRED via recruit button')
            return 0
    print('hire failed')
    return 2


if __name__ == '__main__':
    sys.exit(main())
