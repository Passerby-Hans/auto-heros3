# -*- coding: utf-8 -*-
"""Hunt the nearest visible enemy hero, attack it and fight the battle to the end.

Standing order for this debug run: "attack the biggest enemy stack with whoever is active".
The 1080p60 overlay recorder runs in its own process and captures the whole hunt.
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

PATROL = [(2, 8), (3, 10), (5, 10), (7, 10), (9, 10), (12, 10), (11, 13), (12, 15),
          (9, 18), (7, 19), (3, 19), (1, 14)]
patrol_index = 0
started = time.time()


def state():
    return agent.state(timeout=10)


def run(line, timeout=20):
    return agent.execute(line, verify=None, result_timeout=timeout)


def enemy_heroes(st):
    return [o for o in st.get('objects') or []
            if o.get('kind') == 'hero' and o.get('owner') not in (0, None)]


def act(line):
    # A leftover town/screen window blocks map actions; closing it first is cheap.
    run('close_window')
    return run(line)


def battle_loop(limit_seconds=240):
    end = time.time() + limit_seconds
    orders = 0
    while time.time() < end:
        st = state()
        battle = st.get('battle') or {}
        if not battle.get('active'):
            break
        if st.get('query'):
            run('answer 0')
            time.sleep(0.6)
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
        time.sleep(0.55)
    return orders


def main() -> int:
    global patrol_index
    battles = 0
    for step in range(160):
        st = state()
        day = (st.get('date') or {}).get('day')
        hero = (st.get('heroes') or [{}])[0]
        army = sum(int(x['count']) for x in hero.get('army') or [])
        if (st.get('battle') or {}).get('active'):
            orders = battle_loop()
            battles += 1
            st = state()
            enemies = enemy_heroes(st)
            print(f'[hunt] battle#{battles} orders={orders} army={army} '
                  f'enemy_heroes_left={[(e["name"], e.get("owner")) for e in enemies]}',
                  flush=True)
            if not enemies:
                break
            continue

        enemies = enemy_heroes(st)
        if enemies:
            if int(hero.get('movement') or 0) < 200:
                run('close_window')
                run('end_turn')
                time.sleep(6)
                continue
            target = enemies[0]
            pos = target.get('visitable') or [0, 0]
            print(f'[hunt] day={day} ATTACK {target["name"]} at {pos[:2]} army={army}',
                  flush=True)
            act(f'move_hero 0 {pos[0]} {pos[1]} 0')
            time.sleep(1.5)
            continue

        movement = int(hero.get('movement') or 0)
        if movement < 120:
            act('end_turn')
            time.sleep(6)
            continue
        point = PATROL[patrol_index % len(PATROL)]
        patrol_index += 1
        act(f'move_hero 0 {point[0]} {point[1]} 0')
        time.sleep(0.8)
        if step % 12 == 0:
            print(f'[hunt] patrol day={day} pos={hero.get("visitable")} point={point} '
                  f'army={army} mov={movement}', flush=True)

    st = state()
    enemies = enemy_heroes(st)
    hero = (st.get('heroes') or [{}])[0]
    army = sum(int(x['count']) for x in hero.get('army') or [])
    print('[hunt] RESULT', 'day', (st.get('date') or {}).get('day'), 'army', army,
          'battles', battles, 'enemy_heroes', [(e['name'], e.get('owner')) for e in enemies],
          'elapsed', round(time.time() - started, 1), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
