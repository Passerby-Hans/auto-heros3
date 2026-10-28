# -*- coding: utf-8 -*-
"""Agent console for VCMI: read the player-visible state, issue player actions, end turns.

Protocol v2: every command gets a sequence number from the bridge and the console matches
that sequence number to the reply.  OK means "submitted"; for state-changing actions the
console additionally waits for the state post-condition before printing COMPLETED.

Usage:
    python -X utf8 tools/vcmi_act.py start --map Maps/Arrogance [--dir var/agent_me]
    python -X utf8 tools/vcmi_act.py state
    python -X utf8 tools/vcmi_act.py do "build 0 8" "recruit 0 1 15" "move_hero 0 7 6 0"
    python -X utf8 tools/vcmi_act.py endturn [--days 1]
    python -X utf8 tools/vcmi_act.py shot var/shots/x.png
    python -X utf8 tools/vcmi_act.py stop
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

from vcmi_agent import CommandResult, VcmiAgent  # noqa: E402

STATE_FILE = ROOT / 'var' / 'agent_console' / 'session.json'


def save_session(agent: VcmiAgent) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({'dir': str(agent.dir)}), encoding='utf-8')


def load_session() -> dict | None:
    if not STATE_FILE.exists():
        return None
    return json.loads(STATE_FILE.read_text(encoding='utf-8'))


def get_agent() -> VcmiAgent:
    sess = load_session()
    if not sess:
        raise SystemExit('no session; run: vcmi_act.py start --map Maps/...')
    return VcmiAgent(sess['dir'])


def compact_state(st: dict) -> dict:
    """What the agent needs to reason about the day: only player-visible information."""
    out = {
        'in_game': st.get('in_game'),
        'session_id': st.get('session_id'),
        'cmd_seq': st.get('cmd_seq'),
        'date': st.get('date'),
        'resources': st.get('resources'),
        'query': st.get('query'),
        'battle': st.get('battle'),
        'heroes': [],
        'towns': [],
        'objects': [],
        'info': (st.get('info') or [])[-5:],
    }
    for h in st.get('heroes', []):
        out['heroes'].append({
            'index': h['index'], 'id': h.get('id'), 'name': h['name'], 'level': h['level'],
            'exp': h['exp'], 'mana': h['mana'], 'movement': h['movement'],
            'pos': h['visitable'], 'primary': h['primary'],
            'army': [{'slot': a['slot'], 'creature': a['creature'], 'count': a['count']}
                     for a in h['army']],
        })
    for t in st.get('towns', []):
        out['towns'].append({
            'index': t['index'], 'name': t['name'], 'pos': t['pos'],
            'built': t['built'], 'buildable': t.get('buildable', []),
            'build_options': t.get('build_options', []),
            'dwellings': t.get('dwellings', []),
            'garrison': t.get('garrison', []),
            'available_heroes': t.get('available_heroes', []),
        })
    for o in st.get('objects', []):
        entry = {k: o.get(k) for k in ('id', 'kind', 'type', 'name', 'visitable', 'owner',
                                       'creature', 'creature_name', 'hover')}
        out['objects'].append({k: v for k, v in entry.items() if v is not None})
    return out


def verify_for(agent: VcmiAgent, line: str):
    """Return a post-condition checker for the commands whose effect can be observed."""
    parts = line.split()
    if not parts:
        return None
    op = parts[0]
    try:
        if op == 'move_hero':
            idx, x, y = int(parts[1]), int(parts[2]), int(parts[3])
            target = (x, y)

            def _move():
                st = agent.state(timeout=5)
                if (st.get('battle') or {}).get('active'):
                    return True, 'battle started'
                pos = agent.hero_position(st, idx)
                if pos is None:
                    return False, f'hero {idx} missing'
                return pos == target, f'hero {idx} at {pos}'
            return _move
        if op == 'build':
            return agent.verify_build(int(parts[1]), int(parts[2]))
        if op == 'recruit':
            return agent.verify_recruit(int(parts[1]), None, 1)
        if op == 'recruit_hero':
            def _hero():
                st = agent.state(timeout=5)
                return len(st.get('heroes') or []) > 1, f'heroes={len(st.get("heroes") or [])}'
            return _hero
        if op == 'end_turn':
            def _day():
                st = agent.state(timeout=5)
                return st['date']['day'] > base_day[0], f"day={st['date']['day']}"
            base_day = [agent.state(timeout=5)['date']['day']]
            return _day
        if op in ('swap_stacks', 'transfer', 'upgrade', 'market_trade', 'cast_spell'):
            def _changed():
                st = agent.state(timeout=5)
                key = json.dumps([st.get('resources'), st.get('heroes'), st.get('towns')],
                                 sort_keys=True)
                if base[0] is None:
                    base[0] = key
                    return False, 'baseline'
                return key != base[0], 'state changed'
            base = [None]
            return _changed
    except (IndexError, ValueError):
        return None
    return None


def run_command(agent: VcmiAgent, line: str) -> CommandResult:
    verify = verify_for(agent, line)
    timeout = 40.0 if line.startswith('move_hero') else 20.0
    return agent.execute(line, verify=verify, verify_timeout=25.0, result_timeout=timeout)



def fight_battle(agent: VcmiAgent, max_actions: int = 60) -> str:
    """Mechanical executor for the agent's standing battle policy: focus-fire the weakest
    enemy stack, shooting with shooters.  Target selection is the agent's decision."""
    actions = 0
    for _ in range(max_actions):
        st = agent.state(5)
        b = st.get('battle') or {}
        if not b.get('active'):
            return f'battle over after {actions} actions'
        active = b.get('active_stack', -1)
        stacks = b.get('stacks', [])
        mine = [x for x in stacks if x['id'] == active and x['side'] == 0]
        enemies = [x for x in stacks if x['side'] == 1]
        if not enemies:
            agent.execute('battle_result_ok', result_timeout=8)
            time.sleep(0.8)
            continue
        if not mine:
            time.sleep(1.0)
            continue
        me = mine[0]
        enemies.sort(key=lambda e: (e['count'], abs(e['hex'][0] - me['hex'][0]) + abs(e['hex'][1] - me['hex'][1])))
        tgt = enemies[0]
        agent.execute(f"battle_attack {tgt['id']}", result_timeout=8)
        actions += 1
        time.sleep(1.2)
    return f'battle still active after {actions} actions'


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)

    p_start = sub.add_parser('start')
    p_start.add_argument('--map', default='Maps/Arrogance')
    p_start.add_argument('--dir', default=str(ROOT / 'var' / 'agent_me'))

    sub.add_parser('state')
    p_fight = sub.add_parser('fight')
    p_fight.add_argument('--max', type=int, default=60)
    sub.add_parser('stop')
    sub.add_parser('reachable')

    p_do = sub.add_parser('do')
    p_do.add_argument('commands', nargs='+')

    p_end = sub.add_parser('endturn')
    p_end.add_argument('--days', type=int, default=1)

    p_shot = sub.add_parser('shot')
    p_shot.add_argument('path', nargs='?', default=str(ROOT / 'var' / 'shots' / 'vcmi_me.png'))

    args = ap.parse_args()

    if args.cmd == 'start':
        agent = VcmiAgent(args.dir, args.map)
        try:
            if not agent.start():
                print('client did not start')
                return 1
            st = agent.wait_in_game(180)
        except Exception as exc:
            print(f'start failed: {exc}')
            return 1
        save_session(agent)
        print(json.dumps(compact_state(st), ensure_ascii=False, indent=1))
        return 0

    agent = get_agent()

    if args.cmd == 'stop':
        result = agent.stop()
        try:
            STATE_FILE.unlink()
        except OSError:
            pass
        print(json.dumps(result, ensure_ascii=False))
        return 0

    if args.cmd == 'fight':
        print(fight_battle(agent, args.max))
        return 0

    if args.cmd == 'state':
        print(json.dumps(compact_state(agent.state(10)), ensure_ascii=False, indent=1))
        return 0

    if args.cmd == 'reachable':
        print(json.dumps({'error': 'reachable_disabled_use_move_hero'}, ensure_ascii=False))
        return 0

    if args.cmd == 'do':
        bad = 0
        for line in args.commands:
            res = run_command(agent, line)
            print(f'{line}  ->  {res}')
            if not res.ok:
                bad += 1
        return 1 if bad else 0

    if args.cmd == 'endturn':
        for _ in range(args.days):
            st = agent.state(10)
            day0 = st['date']['day']
            res = agent.execute('end_turn', verify=None, result_timeout=20)
            print(f'end_turn -> {res}')
            # pump events while waiting for the new day: battles and queries must be handled
            from vcmi_short_game import pump_events  # local import to avoid cycles
            outcome = pump_events(agent, stop_when_day_after=day0, timeout=180.0, verbose=True)
            print(f'day {day0} -> {outcome.get("day")} ({outcome.get("reason")})')
        return 0

    if args.cmd == 'shot':
        agent.shot(args.path)
        return 0

    return 0


if __name__ == '__main__':
    sys.exit(main())
