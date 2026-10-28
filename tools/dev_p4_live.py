# -*- coding: utf-8 -*-
"""P4 live validation: reconnect (lease), recording singleton/recovery, save/load confirmation."""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import mcp_server_v2 as M  # noqa: E402

print('lease', M.lease_acquire().get('ok'))
brief = M.game_brief()
print('day before', brief.get('day'), 'status', brief.get('status'))
result = M.game_act('end_turn')
print('end_turn ->', result.get('status'), '|', result.get('evidence'))
deadline = time.time() + 25
day = brief.get('day')
while time.time() < deadline:
    day = M.game_brief().get('day')
    if day != brief.get('day'):
        break
    time.sleep(1)
print('day after', day)

first = M.recording_start(fps=10, height=768)
print('recording_start #1', first.get('ok'), first.get('dir'))
second = M.recording_start(fps=10, height=768)
print('recording_start #2 (should be recording_active)', second.get('ok'), second.get('error'))
print('recording_status', {key: M.recording_status().get(key)
                           for key in ('recording', 'dir', 'pid')})
time.sleep(3)
stopped = M.recording_stop()
print('recording_stop', stopped.get('ok'), 'frames', stopped.get('frames'),
      'bytes', stopped.get('bytes'), 'recovered', stopped.get('recovered'))
print('video', stopped.get('video'))

saved = M.save_game('p4-live')
print('save_game ->', saved.get('status'), '|', saved.get('evidence'))
loaded = M.load_game('p4-live')
print('load_game ->', loaded.get('status'), '|', loaded.get('evidence'))
print('final day', M.game_brief().get('day'))
