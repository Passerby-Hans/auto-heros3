# -*- coding: utf-8 -*-
"""MCP-like gateway: the reasoning layer (LLM) takes over the running game directly.

Minimal tool server over the D-007 bridge.  Every call prints one JSON object, so an external
agent (or a human shell) can drive the game step by step, like an MCP tool call:

    python -X utf8 tools/agent_mcp.py start   --map Maps/Arrogance --dir var/agent_mcp
    python -X utf8 tools/agent_mcp.py tools
    python -X utf8 tools/agent_mcp.py call {"tool":"state"}
    python -X utf8 tools/agent_mcp.py call {"tool":"act","args":{"commands":["build 0 8"]}}
    python -X utf8 tools/agent_mcp.py call {"tool":"orders","args":{"engage":{"creature":true}}}
    python -X utf8 tools/agent_mcp.py call {"tool":"shot"}
    python -X utf8 tools/agent_mcp.py call {"tool":"stop"}

Tools (all player-visible / player-equivalent, server validated):
  status  -> {in_game, day, status, session, offscreen}
  state   -> compact player-visible snapshot (heroes/towns/resources/objects/query/battle)
  act     -> [{cmd, seq, status, detail}] ; OK alone means submitted, COMPLETED is proven
  orders  -> write <dir>/orders.json (LLM decides when/whom to fight; driver follows)
  shot    -> save a screenshot of the off-screen window
  stop    -> stop the owned client, report pids/leftovers
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

from vcmi_agent import VcmiAgent  # noqa: E402


def session() -> VcmiAgent:
    agent = VcmiAgent(str(ROOT / 'var' / 'agent_mcp'))
    try:
        import vcmi_shot
        wins = vcmi_shot.find_window()
        if wins:
            agent.hwnd = wins[0][0]
    except Exception:
        pass
    return agent


def compact(st: dict) -> dict:
    return {
        'in_game': st.get('in_game'), 'day': (st.get('date') or {}).get('day'),
        'status': st.get('status'), 'session_id': st.get('session_id'),
        'resources': st.get('resources'), 'query': st.get('query'),
        'battle': st.get('battle'),
        'heroes': [{'i': h['index'], 'name': h['name'], 'pos': h['visitable'],
                    'movement': h['movement'], 'army': h['army']} for h in st.get('heroes') or []],
        'towns': [{'i': t['index'], 'name': t['name'], 'pos': t['pos'],
                   'built': t.get('built'), 'buildables': [o['id'] for o in t.get('build_options', [])],
                   'garrison': t.get('garrison'), 'heroes': t.get('available_heroes')}
                  for t in st.get('towns') or []],
        'objects': [{k: o.get(k) for k in ('id', 'kind', 'type', 'name', 'visitable', 'owner',
                                           'hover') if o.get(k) is not None}
                    for o in st.get('objects') or []],
        'info': (st.get('info') or [])[-6:],
    }


def verify(agent: VcmiAgent, cmd: str):
    parts = cmd.split()
    if not parts:
        return None
    op = parts[0]
    try:
        if op == 'build':
            return agent.verify_build(int(parts[1]), int(parts[2]))
        if op == 'recruit':
            return agent.verify_recruit(int(parts[1]), None, 1)
        if op == 'move_hero':
            idx, tx, ty = int(parts[1]), int(parts[2]), int(parts[3])
            target = (tx, ty)

            def _mv():
                st = agent.state(timeout=5)
                if (st.get('battle') or {}).get('active'):
                    return True, 'battle started'
                pos = agent.hero_position(st, idx)
                return pos == target, f'hero {idx} at {pos}'
            return _mv
        if op in ('transfer', 'merge'):
            return agent.verify_hero_army_gain(0, 1)
        if op == 'recruit_hero':
            def _h():
                st = agent.state(timeout=5)
                return len(st.get('heroes') or []) > 1, 'hero count'
            return _h
    except (IndexError, ValueError):
        return None
    return None


def call(req: dict) -> dict:
    tool = req.get('tool')
    args = req.get('args') or {}
    agent = session()
    if tool == 'status':
        st = agent.state(timeout=10)
        return {'ok': True, 'result': {
            'in_game': st.get('in_game'), 'day': (st.get('date') or {}).get('day'),
            'status': st.get('status'), 'session': agent.session.session_id,
            'offscreen': agent.window_is_offscreen(), 'rect': agent._window_rect()}}
    if tool == 'state':
        return {'ok': True, 'result': compact(agent.state(timeout=10))}
    if tool == 'act':
        out = []
        for cmd in args.get('commands') or []:
            res = agent.execute(cmd, verify=verify(agent, cmd), verify_timeout=25,
                                result_timeout=40)
            out.append({'cmd': cmd, 'seq': res.seq, 'status': res.status,
                        'detail': res.detail or res.line})
        return {'ok': all(o['status'] == 'COMPLETED' for o in out), 'result': out}
    if tool == 'advance':
        from vcmi_short_game import pump_events
        out = pump_events(agent, timeout=float(args.get('timeout', 180)))
        return {'ok': out.get('reason') in ('timeout', 'day_changed'), 'result': out}
    if tool == 'end_turn':
        from vcmi_short_game import pump_events
        st = agent.state(timeout=10)
        day0 = (st.get('date') or {}).get('day')
        res = agent.execute('end_turn', verify=None, result_timeout=20)
        out = pump_events(agent, stop_when_day_after=day0,
                          timeout=float(args.get('timeout', 240)))
        return {'ok': out.get('reason') == 'day_changed',
                'result': {'seq': res.seq, 'status': res.status, 'day': out.get('day'),
                           'pump': out.get('reason')}}
    if tool == 'deliver':
        from vcmi_win import transfer_garrison_to_hero
        st = agent.state(timeout=10)
        log: list = []
        did = transfer_garrison_to_hero(agent, st, log)
        st2 = agent.state(timeout=10)
        return {'ok': True, 'result': {
            'moved': did, 'log': log,
            'heroes': [{'name': h['name'], 'army': h['army']} for h in st2.get('heroes') or []],
            'garrison': (st2.get('towns') or [{}])[0].get('garrison')}}
    if tool == 'orders':
        path = agent.dir / 'orders.json'
        data = {k: args[k] for k in ('engage', 'attack_ids', 'avoid_ids') if k in args}
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        return {'ok': True, 'result': {'orders': data, 'path': str(path)}}
    if tool == 'shot':
        path = agent.shot(args.get('path') or ROOT / 'var' / 'shots' / 'mcp.png')
        return {'ok': True, 'result': {'path': str(path)}}
    if tool == 'stop':
        return {'ok': True, 'result': agent.stop()}
    return {'ok': False, 'error': f'unknown tool {tool!r}'}


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        print(json.dumps({'ok': False, 'error': 'usage: start|tools|call'}))
        return 1
    if argv[0] == 'tools':
        print(json.dumps({'ok': True, 'tools': ['status', 'state', 'act', 'advance', 'end_turn', 'deliver',
                              'orders', 'shot', 'stop']}))
        return 0
    if argv[0] == 'start':
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument('--map', default='Maps/Arrogance')
        ap.add_argument('--dir', default=str(ROOT / 'var' / 'agent_mcp'))
        a = ap.parse_args(argv[1:])
        agent = VcmiAgent(a.dir, a.map)
        try:
            ok = agent.start()
            st = agent.wait_in_game(180) if ok else {}
            print(json.dumps({'ok': bool(ok), 'session': getattr(agent.session, 'session_id', None),
                              'client_pid': getattr(agent.session, 'client_pid', None),
                              'day': (st.get('date') or {}).get('day'),
                              'offscreen': agent.window_is_offscreen()}, ensure_ascii=False))
        except Exception as exc:
            print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
            return 1
        return 0
    if argv[0] == 'call':
        try:
            req = json.loads(argv[1])
        except (IndexError, ValueError):
            print(json.dumps({'ok': False, 'error': 'expected: call <json>'}))
            return 1
        try:
            print(json.dumps(call(req), ensure_ascii=False))
        except Exception as exc:
            print(json.dumps({'ok': False, 'error': f'{type(exc).__name__}: {exc}'},
                             ensure_ascii=False))
            return 1
        return 0
    print(json.dumps({'ok': False, 'error': f'unknown command {argv[0]!r}'}))
    return 1


if __name__ == '__main__':
    sys.exit(main())
