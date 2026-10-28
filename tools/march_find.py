# -*- coding: utf-8 -*-
"""March out of town along known-passable waypoints until an enemy hero is visible."""
from __future__ import annotations
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from vcmi_agent import VcmiAgent

agent = VcmiAgent(str(ROOT / 'var' / 'agent_mcp_v2'), log=False)
agent.session = agent._load_session()

WAYPOINTS = [(2, 8), (3, 10), (5, 10), (7, 10), (9, 10), (11, 12), (11, 14), (12, 15),
             (9, 18), (7, 19), (3, 19), (1, 14), (5, 6), (9, 8)]


def state():
    return agent.state(timeout=10)


def run(line):
    return agent.execute(line, verify=None, result_timeout=10)


def enemy_heroes(st):
    return [o for o in st.get('objects') or []
            if o.get('kind') == 'hero' and o.get('owner') not in (0, None)]


def main() -> int:
    end = time.time() + 170
    index = 0
    while time.time() < end:
        st = state()
        if (st.get('battle') or {}).get('active'):
            print('BATTLE_ACTIVE')
            break
        enemies = enemy_heroes(st)
        if enemies:
            print('ENEMY', [(e['name'], e.get('owner'), e.get('visitable')) for e in enemies])
            return 0
        hero = (st.get('heroes') or [{}])[0]
        if int(hero.get('movement') or 0) < 200:
            run('close_window')
            run('end_turn')
            time.sleep(5)
            continue
        point = WAYPOINTS[index % len(WAYPOINTS)]
        index += 1
        run('close_window')
        run(f'move_hero 0 {point[0]} {point[1]} 0')
        time.sleep(0.8)
    st = state()
    hero = (st.get('heroes') or [{}])[0]
    print('TIMEOUT pos', hero.get('visitable'), 'mov', hero.get('movement'),
          'enemies', [(e['name'], e.get('visitable')) for e in enemy_heroes(st)])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
