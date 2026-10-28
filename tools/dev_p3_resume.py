# -*- coding: utf-8 -*-
"""P3 resume probe: a FRESH process that only uses the memory + MCP surface to continue.

Simulates "context was compacted / new chat": it is a separate Python process with no access
to the previous in-memory history.  It reads game_brief (identity/memory index) and
CURRENT.md, then performs the next step recorded in the notes and verifies the effect.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import mcp_server_v2 as M  # noqa: E402

print('RESUME process started (no previous context)')
brief = M.game_brief()
if not brief.get('ok'):
    print('RESUME FAILED: cannot read the running game:', brief.get('error'))
    raise SystemExit(1)
print('RESUME identity', brief.get('identity'), 'day', brief.get('day'))
print('RESUME memory', brief.get('memory'))
if brief.get('pending_action'):
    print('RESUME pending_action (needs attention):', brief['pending_action'])

doc = M.memory_read('game', 'current')
print('RESUME current.md revision', doc.get('revision'), 'updated_at', doc.get('updated_at'))
body = doc.get('body') or ''
if '# 下一步' not in body:
    print('RESUME FAILED: CURRENT.md has no next-step section')
    raise SystemExit(1)
print('RESUME notes-next-step-found: yes')

state = M.game_observe()['state']
# A new MCP process has no lease token (by design): reconnect = re-acquire before acting.
lease = M.lease_acquire()
print('RESUME reconnect lease ok:', lease.get('ok'))
if state.get('query'):
    print('RESUME answering pending query', M.game_act('answer', {'value': 0},
                                                        request_id='p3-resume-answer-1'))
before = M.game_brief().get('day')
result = M.game_act('end_turn', None, request_id='p3-resume-endturn-1')
print('RESUME end_turn ->', result.get('status'), '|', result.get('evidence'))
day = before
deadline = time.time() + 25
while time.time() < deadline:
    day = M.game_brief().get('day')
    if day != before:
        break
    time.sleep(1)
print(f'RESUME_DONE day {before} -> {day} (end_turn status={result.get("status")})')
raise SystemExit(0 if day != before else 1)
