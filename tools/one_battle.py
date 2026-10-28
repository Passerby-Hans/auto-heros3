# -*- coding: utf-8 -*-
"""One-shot: approach the visible enemy hero, fight one battle to the end, exit."""
from __future__ import annotations
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from vcmi_agent import VcmiAgent

agent = VcmiAgent(str(ROOT / 'var' / 'agent_mcp_v2'), log=False)
agent.session = agent._load_session()
OFFSETS = [(0, 0), (-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-2, 0), (0, 2)]


def state():
    return agent.state(timeout=10)


def run(line):
    return agent.execute(line, verify=None, result_timeout=10)


def fight(limit=360):
    end = time.time() + limit
    orders = 0
    while time.time() < end:
        st = state()
        battle = st.get('battle') or {}
        if not battle.get('active'):
            break
        if st.get('query'):
            run('answer 0')
            time.sleep(0.4)
            continue
        if battle.get('active_stack') == -1:
            run('key Return')
            time.sleep(0.6)
            continue
        mine = battle.get('my_side')
        stacks = battle.get('stacks') or []
        enemies = [s for s in stacks if s['side'] != mine]
        active = [s for s in stacks if s['id'] == battle.get('active_stack')]
        if not enemies:
            break
        if not (active and active[0]['side'] == mine):
            time.sleep(0.3)
            continue
        landed = False
        for target in sorted(enemies, key=lambda s: -s['count']):
            if ' OK ' in f' {run(f"battle_attack {target["id"]}").line} ':
                landed = True
                orders += 1
                break
        if not landed:
            ax, ay = active[0]['hex']
            near = min(enemies, key=lambda s: abs(s['hex'][0] - ax) + abs(s['hex'][1] - ay))
            tx, ty = near['hex']
            run(f'battle_move {ax + (1 if tx > ax else -1 if tx < ax else 0)} '
                f'{ay + (1 if ty > ay else -1 if ty < ay else 0)}')
        time.sleep(0.3)
    return orders


def enemy_heroes(st):
    return [o for o in st.get('objects') or []
            if o.get('kind') == 'hero' and o.get('owner') not in (0, None)]


def main() -> int:
    for _ in range(40):
        st = state()
        if (st.get('battle') or {}).get('active'):
            break
        enemies = enemy_heroes(st)
        if enemies:
            pos = enemies[0].get('visitable') or [0, 0]
            moved = False
            for dx, dy in OFFSETS:
                run('close_window')
                result = run(f'move_hero 0 {pos[0] + dx} {pos[1] + dy} 0')
                if 'no_path' not in result.line and 'ERR' not in result.line:
                    moved = True
                    break
            time.sleep(1.2)
            if not moved:
                run('close_window')
                run('end_turn')
                time.sleep(6)
        else:
            hero = (st.get('heroes') or [{}])[0]
            if int(hero.get('movement') or 0) < 150:
                run('close_window')
                run('end_turn')
                time.sleep(6)
            else:
                run('close_window')
                run('move_hero 0 9 10 0')
                time.sleep(1)

    orders = fight()
    st = state()
    battle = st.get('battle') or {}
    hero = (st.get('heroes') or [{}])[0]
    print('orders', orders, 'battle_active', battle.get('active'),
          'hero_army', sum(int(x['count']) for x in hero.get('army') or []),
          'status', st.get('status'), 'day', (st.get('date') or {}).get('day'))
    print('enemy_heroes', [(e['name'], e.get('owner')) for e in enemy_heroes(st)])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
