# -*- coding: utf-8 -*-
"""Full UI flow: move the hero out of town, open the town screen, click the tavern, hire a hero.

H3 rule (server): the town tavern cannot hire while a hero is visiting the town
(HeroPoolProcessor::hireHero -> "There is visiting hero - no place!"), so the hero must leave first.
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
from vcmi_agent import VcmiAgent  # noqa: E402


def cmd(ag, line, timeout=12.0):
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


def wn(d, i):
    ws = d.get('windows', [])
    if i >= len(ws):
        return [], ''
    out = []
    collect(ws[i], out)
    return out, (ws[i].get('root') or {}).get('cls', '')


def clk(ag, n):
    cmd(ag, f"click {n['x'] + n['w'] // 2} {n['y'] + n['h'] // 2}")


def close_extras(ag):
    for _ in range(4):
        d = tree(ag)
        ws = d.get('windows', [])
        if not any(not any(k in wn(d, i)[1] for k in ('AdventureMapInterface', 'CCastleInterface'))
                   for i in range(len(ws))):
            return
        cmd(ag, 'close_window')
        time.sleep(0.5)


def main():
    ag = VcmiAgent(str(ROOT / 'var' / 'agent_ui'), 'Maps/Arrogance')
    if not ag.start():
        print('start failed')
        return 1
    ag.wait_in_game(180)
    st = ag.state(10)
    pos = st['heroes'][0]['visitable']
    print('hero at', pos)

    # 1) move the hero out of town (try surrounding tiles; move_hero reports no_path otherwise)
    moved = False
    for target in ([pos[0] + 1, pos[1]], [pos[0] - 1, pos[1]], [pos[0], pos[1] + 1],
                   [pos[0], pos[1] - 1], [pos[0] + 1, pos[1] + 1], [pos[0] - 1, pos[1] - 1]):
        r = cmd(ag, f'move_hero 0 {target[0]} {target[1]} 0')
        print('move', target, '->', r)
        if r.startswith('OK'):
            time.sleep(1.5)
            st = ag.state(10)
            if st['heroes'][0]['visitable'][:2] != pos[:2]:
                moved = True
                break
    if not moved:
        print('hero did not leave town')
    print('hero now at', ag.state(10)['heroes'][0]['visitable'])

    # 2) open town, find the tavern building by probing
    print(cmd(ag, 'open_town 0'))
    time.sleep(1.8)
    d = tree(ag)
    ws = d.get('windows', [])
    town_idx = next((i for i in range(len(ws)) if 'CastleInterface' in wn(d, i)[1]), None)
    if town_idx is None:
        print('no town window')
        return 1
    nodes, _ = wn(d, town_idx)
    rects = [n for n in nodes if n['cls'].endswith('CBuildingRect')]
    tavern_at = None
    for n in rects:
        clk(ag, n)
        time.sleep(1.2)
        d = tree(ag)
        ws = d.get('windows', [])
        if any('TavernWindow' in wn(d, i)[1] for i in range(len(ws))):
            tavern_at = (n['x'] + n['w'] // 2, n['y'] + n['h'] // 2)
            print('tavern opened at', tavern_at)
            break
        close_extras(ag)
    if not tavern_at:
        print('tavern not found')
        return 1

    # 3) hire through the tavern window
    d = tree(ag)
    ws = d.get('windows', [])
    ti = next(i for i in range(len(ws)) if 'TavernWindow' in wn(d, i)[1])
    nodes, _ = wn(d, ti)
    portraits = [n for n in nodes if 'HeroPortrait' in n['cls']]
    print('portraits:', [(n['x'], n['y'], n['w'], n['h']) for n in portraits])
    before = ag.state(10)
    g0, h0 = before['resources']['gold'], len(before['heroes'])
    for p in portraits:
        clk(ag, p)
        time.sleep(1.8)
        now = ag.state(10)
        print('after portrait click: gold', g0, '->', now['resources']['gold'],
              'heroes', h0, '->', len(now['heroes']))
        if len(now['heroes']) > h0:
            print('HIRED hero:', [h['name'] for h in now['heroes']])
            return 0
    # fall back to the widest button (the hand/recruit button)
    buttons = [n for n in nodes if n['cls'].endswith('CButton') and n['w'] >= 40]
    if buttons:
        clk(ag, max(buttons, key=lambda n: n['w']))
        time.sleep(2.0)
        now = ag.state(10)
        print('after recruit button: gold', g0, '->', now['resources']['gold'],
              'heroes', h0, '->', len(now['heroes']))
        if len(now['heroes']) > h0:
            print('HIRED hero:', [h['name'] for h in now['heroes']])
            return 0
    print('hire failed')
    return 2


if __name__ == '__main__':
    sys.exit(main())
