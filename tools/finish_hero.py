# -*- coding: utf-8 -*-
"""Finish off the weakened enemy hero: move onto its tile, then fight the battle."""
from __future__ import annotations
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from vcmi_agent import VcmiAgent

agent = VcmiAgent(str(ROOT / 'var' / 'agent_mcp_v2'), log=False)
agent.session = agent._load_session()


def state():
    return agent.state(timeout=10)


def run(line):
    return agent.execute(line, verify=None, result_timeout=10)


def fight(seconds=90):
    end = time.time() + seconds
    while time.time() < end:
        st = state()
        battle = st.get('battle') or {}
        if not battle.get('active'):
            return
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
        if not enemies or not (active and active[0]['side'] == mine):
            time.sleep(0.3)
            continue
        landed = False
        for target in sorted(enemies, key=lambda s: -s['count']):
            if ' OK ' in f' {run(f"battle_attack {target["id"]}").line} ':
                landed = True
                break
        if not landed:
            ax, ay = active[0]['hex']
            near = min(enemies, key=lambda s: abs(s['hex'][0] - ax) + abs(s['hex'][1] - ay))
            tx, ty = near['hex']
            run(f'battle_move {ax + (1 if tx > ax else -1 if tx < ax else 0)} '
                f'{ay + (1 if ty > ay else -1 if ty < ay else 0)}')
        time.sleep(0.3)


st = state()
enemies = [o for o in st.get('objects') or []
           if o.get('kind') == 'hero' and o.get('owner') not in (0, None)]
hero = (st.get('heroes') or [{}])[0]
print('start pos', hero.get('visitable'), 'mov', hero.get('movement'),
      'army', sum(int(x['count']) for x in hero.get('army') or []),
      'enemies', [(e['name'], e.get('visitable')) for e in enemies])

if enemies:
    if int(hero.get('movement') or 0) < 200:
        for _ in range(6):
            st = state()
            if not st.get('query'):
                break
            run('answer 0')
            time.sleep(0.8)
        run('close_window')
        run('end_turn')
        time.sleep(8)
        st = state()
        hero = (st.get('heroes') or [{}])[0]
        enemies = [o for o in st.get('objects') or []
                   if o.get('kind') == 'hero' and o.get('owner') not in (0, None)]
        print('after end_turn day', (st.get('date') or {}).get('day'),
              'mov', hero.get('movement'),
              'enemies', [(e['name'], e.get('visitable')) for e in enemies])
if enemies:
    pos = enemies[0].get('visitable') or [0, 0]
    run('close_window')
    run(f'move_hero 0 {pos[0]} {pos[1]} 0')
    time.sleep(1.5)
    fight(120)

st = state()
hero = (st.get('heroes') or [{}])[0]
enemies = [o for o in st.get('objects') or []
           if o.get('kind') == 'hero' and o.get('owner') not in (0, None)]
print('RESULT day', (st.get('date') or {}).get('day'),
      'army', sum(int(x['count']) for x in hero.get('army') or []),
      'status', st.get('status'),
      'enemy_heroes', [(e['name'], e.get('owner')) for e in enemies])
