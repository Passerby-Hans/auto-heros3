# -*- coding: utf-8 -*-
"""S2+S3 smoke run against the CURRENT MCP contract (status/accepted, persisted events).

One real turn through the MCP tool surface, then an LLM-driven battle:
  observe -> build -> recruit -> move -> end_turn -> battle -> stop.
Every action result carries `status` (completed/submitted/rejected/unknown) and `accepted`.
This is a smoke/regression script, not a fair-play short game (debug resources are not used).

Run: .venv\\Scripts\\python.exe -X utf8 tools\\s2_s3_smoke.py
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))
import mcp_server_v2 as M  # noqa: E402

report: dict = {'started': time.strftime('%Y-%m-%d %H:%M:%S'), 'actions': [], 'battle_orders': 0}


def act(action: str, args: dict | None = None) -> dict:
    res = M.game_act(action, args)
    report['actions'].append({'action': action, 'args': args,
                              'status': res.get('status'), 'accepted': res.get('accepted'),
                              'evidence': res.get('evidence')})
    print(f'{action:12s}', res.get('status'), '|', str(res.get('evidence'))[:80])
    return res


def observe() -> dict:
    return M.game_observe()['state']


def main() -> int:
    print('START', M.session_start('Maps/Arrogance'))
    print('LEASE', M.lease_acquire())
    state = observe()
    hero = state['heroes'][0]
    town = state['towns'][0]
    print('OBSERVE day', state['day'], 'gold', (state['resources'] or {}).get('gold'),
          'hero', (hero['name'], hero['pos']), 'town', (town['name'], len(town['buildables'])))

    build_id = town['buildables'][0] if town['buildables'] else None
    if build_id is not None:
        act('build', {'town': town['i'], 'id': build_id})
    act('recruit', {'town': town['i'], 'level': 1, 'amount': 8})

    target = next((o for o in state['objects'] if o.get('kind') == 'creature'), None) \
        or next((o for o in state['objects'] if o.get('visitable')), None)
    if target:
        pos = target['visitable']
        act('move_hero', {'hero': hero['i'], 'x': pos[0], 'y': pos[1], 'z': pos[2]})
    act('end_turn')
    report['day_after_turn'] = observe()['day']

    # ---- battle: drive orders from the observed battle state (this is what the LLM does) ----
    deadline = time.time() + 120
    while time.time() < deadline:
        state = observe()
        battle = state.get('battle')
        if battle:
            break
        if M.game_events().get('decisions') and not battle:
            # queries/level-ups can block the map: answer them explicitly, then continue
            query = state.get('query')
            if query:
                act('answer', {'value': query.get('options', [{}])[0].get('index', 0)})
        time.sleep(2)
    else:
        print('NO BATTLE within 120s')
        state = observe()
        if state.get('query'):
            act('answer', {'value': 0})
        report['finished'] = time.strftime('%Y-%m-%d %H:%M:%S')
        M.session_stop()
        return 0

    print('BATTLE', [(s['id'], s['side'], s['count']) for s in state['battle']['stacks']])
    for _ in range(150):
        state = observe()
        battle = state.get('battle')
        if not battle:
            print('BATTLE ENDED after', report['battle_orders'], 'orders')
            break
        if state.get('query'):
            act('answer', {'value': 0})
            continue
        active = battle['active']
        if active == -1:
            act('confirm_deployment')
            time.sleep(0.5)
            continue
        mine = [s for s in battle['stacks']
                if s['id'] == active and s['side'] == battle['my_side']]
        enemies = [s for s in battle['stacks'] if s['side'] != battle['my_side']]
        if not mine or not enemies:
            time.sleep(1)
            continue
        target = max(enemies, key=lambda s: s['count'])
        res = act('battle_attack', {'stack': target['id']})
        report['battle_orders'] += 1
        if res.get('status') == 'rejected' and 'not_my_turn' in str(res.get('evidence')):
            time.sleep(0.4)
    report['finished'] = time.strftime('%Y-%m-%d %H:%M:%S')
    final = observe()
    report['final'] = {'day': final.get('day'), 'battle': bool(final.get('battle')),
                       'heroes': [(h['name'], h['pos']) for h in final.get('heroes') or []]}
    print('FINISH', report['final'])
    print('STOP', M.session_stop())
    out = ROOT / 'var' / 'reports' / f"s2s3-smoke-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('REPORT', out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
