# -*- coding: utf-8 -*-
"""Drive the current battle with legal, bridge-guard-compliant orders."""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from vcmi_agent import VcmiAgent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--seconds', type=int, default=60)
    args = ap.parse_args()
    agent = VcmiAgent(str(ROOT / 'var' / 'agent_mcp_v2'), log=False)
    agent.session = agent._load_session()
    end = time.time() + args.seconds
    good = 0
    while time.time() < end:
        st = agent.state(timeout=10)
        battle = st.get('battle') or {}
        if not battle.get('active'):
            break
        if st.get('query'):
            agent.execute('answer 0', verify=None, result_timeout=8)
            time.sleep(0.5)
            continue
        if battle.get('active_stack') == -1:
            agent.execute('key Return', verify=None, result_timeout=8)
            time.sleep(0.6)
            continue
        mine = battle.get('my_side')
        active_id = battle.get('active_stack')
        stacks = battle.get('stacks') or []
        enemies = [s for s in stacks if s['side'] != mine]
        if not enemies:
            break
        active = [s for s in stacks if s['id'] == active_id]
        if not (active and active[0]['side'] == mine):
            time.sleep(0.3)
            continue
        landed = False
        for target in sorted(enemies, key=lambda s: -s['count']):
            result = agent.execute(f"battle_attack {target['id']}", verify=None,
                                   result_timeout=8)
            if ' OK ' in f' {result.line} ':
                landed = True
                good += 1
                break
        if not landed:
            ax, ay = active[0]['hex']
            nearest = min(enemies, key=lambda s: abs(s['hex'][0] - ax) + abs(s['hex'][1] - ay))
            tx, ty = nearest['hex']
            nx = ax + (1 if tx > ax else -1 if tx < ax else 0)
            ny = ay + (1 if ty > ay else -1 if ty < ay else 0)
            agent.execute(f'battle_move {nx} {ny}', verify=None, result_timeout=8)
        time.sleep(0.3)
    st = agent.state(timeout=10)
    battle = st.get('battle') or {}
    mine = battle.get('my_side')
    hero = (st.get('heroes') or [{}])[0]
    print('good_orders', good, 'battle', battle.get('active'),
          'ours', [s['count'] for s in battle.get('stacks') or [] if s['side'] == mine],
          'enemies', [(s['id'], s['count']) for s in battle.get('stacks') or []
                      if s['side'] != mine])
    print('hero_army', sum(int(x['count']) for x in hero.get('army') or []),
          'status', st.get('status'), 'day', (st.get('date') or {}).get('day'))
    print('enemy_heroes', [(o['name'], o.get('owner')) for o in st.get('objects') or []
                           if o.get('kind') == 'hero' and o.get('owner') not in (0, None)])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
