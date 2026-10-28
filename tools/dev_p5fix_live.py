# -*- coding: utf-8 -*-
"""Live sanity for the P5 fixes: transfer precondition (hero in town) + move_near fallback."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import mcp_server_v2 as M  # noqa: E402

print('start', M.session_start('Maps/Arrogance').get('ok'))
M.lease_acquire()
brief = M.game_brief()
hero = brief['heroes'][0]
town = brief['towns'][0]
print('day', brief.get('day'), 'hero', hero['name'], hero['pos'], 'town', town['name'])

recruited = M.game_act('recruit', {'town': town['i'], 'level': 1, 'amount': 14},
                       request_id='p5fix-recruit')
print('recruit ->', recruited.get('status'), recruited.get('evidence'))

transfer = M.game_act('transfer', {'src': 'town', 'src_index': town['i'], 'src_slot': 0,
                                   'dst': 'hero', 'dst_index': hero['i'], 'dst_slot': 1},
                      request_id='p5fix-transfer')
print('transfer ->', transfer.get('status'), '|', transfer.get('evidence'))

moved = M.move_near(hero['i'], 6, 6, radius=1)
print('move_near ->', moved.get('status'), 'target', moved.get('target'),
      'attempts', [(a['target'], a['status']) for a in moved.get('attempts') or []])

after = M.game_brief()
print('final hero army', after['heroes'][0]['army'], 'pos', after['heroes'][0]['pos'])
M.session_stop()
