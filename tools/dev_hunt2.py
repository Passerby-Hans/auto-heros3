# -*- coding: utf-8 -*-
"""Approach the enemy hero with stepwise pathing, then fight the battle to the end.

- Bridge guard now rejects battle actions when it is not our turn (ERR not_my_turn),
  so this loop only ever injects legal orders.
- Direct pathing to a tile occupied by a hero returns no_path, so try neighbour offsets.
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from vcmi_agent import VcmiAgent

agent = VcmiAgent(str(ROOT / 'var' / 'agent_mcp_v2'), log=False)
agent.session = agent._load_session()

OFFSETS = [(0, 0), (-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-2, 0), (0, 2),
           (2, 2), (-2, -2), (2, -2), (-2, 2)]


def state():
    return agent.state(timeout=10)


def run(line, timeout=20):
    return agent.execute(line, verify=None, result_timeout=timeout)


def enemy_heroes(st):
    return [o for o in st.get('objects') or []
            if o.get('kind') == 'hero' and o.get('owner') not in (0, None)]


def fight(limit=300):
    end = time.time() + limit
    orders = 0
    while time.time() < end:
        st = state()
        battle = st.get('battle') or {}
        if not battle.get('active'):
            break
        if st.get('query'):
            run('answer 0')
            time.sleep(0.5)
            continue
        if battle.get('active_stack') == -1:
            run('key Return')
            time.sleep(0.8)
            continue
        mine = battle.get('my_side')
        active_id = battle.get('active_stack')
        stacks = battle.get('stacks') or []
        enemies = [s for s in stacks if s['side'] != mine]
        if not enemies:
            break
        active = [s for s in stacks if s['id'] == active_id]
        if active and active[0]['side'] == mine:
            target = max(enemies, key=lambda s: s['count'])['id']
            run(f'battle_attack {target}')
            orders += 1
            time.sleep(0.45)
        else:
            time.sleep(0.35)
    return orders


def main() -> int:
    battles = 0
    for step in range(150):
        st = state()
        if (st.get('battle') or {}).get('active'):
            orders = fight()
            battles += 1
            st = state()
            enemies = enemy_heroes(st)
            hero = (st.get('heroes') or [{}])[0]
            print(f'[v2] battle#{battles} orders={orders} army='
                  f'{sum(int(x["count"]) for x in hero.get("army") or [])} '
                  f'enemy_left={[(e["name"], e.get("owner")) for e in enemies]}', flush=True)
            if not enemies:
                break
            continue

        enemies = enemy_heroes(st)
        hero = (st.get('heroes') or [{}])[0]
        movement = int(hero.get('movement') or 0)
        if enemies:
            pos = enemies[0].get('visitable') or [0, 0]
            moved = False
            for dx, dy in OFFSETS:
                run('close_window')
                result = run(f'move_hero 0 {pos[0] + dx} {pos[1] + dy} 0')
                if 'no_path' not in result.line and 'ERR' not in result.line:
                    moved = True
                    break
            st = state()
            if (st.get('battle') or {}).get('active'):
                continue
            if not moved:
                run('close_window')
                run('end_turn')
                time.sleep(6)
            else:
                time.sleep(1.2)
            continue

        if movement < 150:
            run('close_window')
            run('end_turn')
            time.sleep(6)
        else:
            run('close_window')
            run('move_hero 0 9 10 0')
            time.sleep(1)

    st = state()
    enemies = enemy_heroes(st)
    hero = (st.get('heroes') or [{}])[0]
    print('[v2] RESULT day', (st.get('date') or {}).get('day'),
          'army', sum(int(x['count']) for x in hero.get('army') or []),
          'battles', battles,
          'enemy_heroes', [(e['name'], e.get('owner')) for e in enemies], flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
