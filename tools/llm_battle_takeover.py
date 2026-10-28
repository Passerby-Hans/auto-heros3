# -*- coding: utf-8 -*-
"""LLM battle takeover helper: wait for the battle, then execute the LLM's standing orders.

Standing order used here (chosen by the reasoning layer): focus the enemy stack with the fewest
creatures; if the attack is rejected, defend to keep the turn moving.  Every command is logged
with its seq so the recording can be audited order-by-order.

Usage: python -X utf8 tools/llm_battle_takeover.py [--dir var/agent_rec] [--poll 480]
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

from vcmi_agent import VcmiAgent  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default=str(ROOT / 'var' / 'agent_rec'))
    ap.add_argument('--poll', type=float, default=480.0)
    ap.add_argument('--max-orders', type=int, default=80)
    args = ap.parse_args()

    agent = VcmiAgent(args.dir, log=False)
    agent.session = agent._load_session()
    if agent.session is None:
        print('no session in', args.dir, flush=True)
        return 1

    state = {}
    battle = {}
    deadline = time.time() + args.poll
    while time.time() < deadline:
        try:
            state = agent.state(timeout=10)
        except Exception as exc:
            print('state:', exc, flush=True)
            time.sleep(2)
            continue
        battle = state.get('battle') or {}
        if battle.get('active'):
            break
        time.sleep(5)
    if not battle.get('active'):
        print('NO BATTLE within poll window; day', (state.get('date') or {}).get('day'), flush=True)
        return 0

    print('BATTLE day', state['date']['day'], 'my_side', battle.get('my_side'), flush=True)
    print('stacks', [(x['id'], x['side'], x['creature'], x['count'], x['hex'])
                     for x in battle.get('stacks') or []], flush=True)

    issued = 0
    for _ in range(args.max_orders):
        try:
            state = agent.state(timeout=10)
        except Exception as exc:
            print('state:', exc, flush=True)
            time.sleep(1)
            continue
        battle = state.get('battle') or {}
        if not battle.get('active'):
            print('BATTLE FINISHED after', issued, 'orders', flush=True)
            return 0
        my_side = battle.get('my_side')
        active = battle.get('active_stack')
        stacks = battle.get('stacks') or []
        mine = [x for x in stacks if x['id'] == active and x['side'] == my_side]
        enemies = [x for x in stacks if x['side'] != my_side]
        if not mine or not enemies:
            time.sleep(1.2)
            continue
        target = min(enemies, key=lambda e: e['count'])
        res = agent.execute(f"battle_attack {target['id']}", verify=None, result_timeout=15)
        issued += 1
        print('ORDER attack', target['id'], 'count', target['count'], '->', res.status,
              'seq', res.seq, flush=True)
        if res.status == 'REJECTED':
            r2 = agent.execute('battle_defend', verify=None, result_timeout=12)
            print('ORDER defend ->', r2.status, 'seq', r2.seq, flush=True)
        time.sleep(1.6)
    print('orders issued:', issued, flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
