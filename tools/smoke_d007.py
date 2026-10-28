# -*- coding: utf-8 -*-
"""Targeted smoke for the D-007 fixes (short, bounded, no gameplay budget consumed)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
from vcmi_agent import VcmiAgent  # noqa: E402

agent = VcmiAgent(str(ROOT / 'var' / 'agent_smoke2'), 'Maps/Arrogance')
try:
    assert agent.start(), 'client did not start'
    st = agent.wait_in_game(180)
    print('session', agent.session.session_id, 'client', agent.session.client_pid)
    print('offscreen', agent.window_is_offscreen(), 'rect', agent._window_rect())
    town = st['towns'][0]
    ids = [o['id'] for o in town.get('build_options', [])]
    print('build_options ids', ids)
    print('cost-less specials filtered:', 26 not in ids and 27 not in ids)
    names = [o.get('name') for o in town.get('build_options', [])]
    print('names', names[:5])
    shot = agent.shot(ROOT / 'var' / 'shots' / 'd007-smoke.png')
    print('shot', shot, shot.exists())
    res = agent.execute('ping', verify=None, result_timeout=10)
    print('ping', res)
finally:
    print('stop', json.dumps(agent.stop(), ensure_ascii=False))
