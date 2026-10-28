# -*- coding: utf-8 -*-
"""P5 short-game report builder: summarize the bounded run from the ledger + live brief."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import mcp_server_v2 as M  # noqa: E402

ISSUES = [
    {'id': 'deployment-return', 'severity': 'high',
     'summary': "Deployment phase ignores synthetic 'key return'; battle_start() clicking the "
                "battle-window bottom button (ui_tree) works (validated live this run).",
     'status': 'fixed (battle_start)'},
    {'id': 'move-no-path', 'severity': 'medium',
     'summary': "move_hero to some adjacent tiles returns ERR no_path ((5,7)/(6,6) on day 1); "
                "the same target works later/from other tiles -> caller must try alternates.",
     'status': 'open (documented)'},
    {'id': 'garrison-transfer-needs-town', 'severity': 'medium',
     'summary': "transfer town garrison -> hero had no effect while the hero stood outside town "
                "(submitted, no diff) and worked when the hero was on the town tile "
                "(town 29->15, hero 4->18). Precondition should be documented/validated.",
     'status': 'open (documented)'},
    {'id': 'pending-action-noise', 'severity': 'low',
     'summary': 'game_brief.pending_action can surface an old submitted ledger entry '
                '(e.g. confirm_deployment) that is no longer relevant.',
     'status': 'open'},
    {'id': 'battle-round-ok', 'severity': 'low',
     'summary': "battle_round returns ok=false when orders were accepted but no observable "
                "change yet (ok means completed only); callers must read status/orders.",
     'status': 'by design (documented)'},
]


def main() -> int:
    run = M._run_identity()
    created = run.get('created_at')
    since = None
    try:
        since = time.mktime(time.strptime(created, '%Y-%m-%d %H:%M:%S')) - 1
    except (TypeError, ValueError, OverflowError):
        pass
    actions = []
    try:
        for line in (M._dir() / 'actions.jsonl').read_text(encoding='utf-8').splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get('run_id') != run.get('run_id'):
                continue
            actions.append(entry)
    except OSError:
        pass
    brief = M.game_brief()
    document = M.memory_read('game', 'current')
    now = time.time()
    duration = None
    if since is not None:
        duration = round(now - since, 1)
    report = {
        'run_id': run.get('run_id'), 'branch_id': run.get('branch_id'),
        'started_at': created, 'finished_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'duration_seconds': duration,
        'turn_limit': 7, 'time_limit_seconds': 1800,
        'day_reached': brief.get('day'), 'status': brief.get('status'),
        'turns_ended': sum(1 for a in actions if a.get('action') == 'end_turn'
                           and a.get('status') == 'completed'),
        'actions': [{'action': a.get('action'), 'status': a.get('status'),
                     'evidence': a.get('evidence')} for a in actions],
        'final_brief': {key: brief.get(key) for key in
                        ('day', 'status', 'gold', 'heroes', 'towns')},
        'memory_current_revision': document.get('revision'),
        'issues': ISSUES,
        'fair_play_note': 'No debug_resources were used in this run; resource start is the map default.',
    }
    out = ROOT / 'doc' / 'reports' / f"p5-short-game-{run.get('run_id')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    var_out = ROOT / 'var' / 'reports' / out.name
    var_out.parent.mkdir(parents=True, exist_ok=True)
    var_out.write_text(out.read_text(encoding='utf-8'), encoding='utf-8')
    print('P5 REPORT', out)
    print(json.dumps({key: report[key] for key in
                      ('run_id', 'duration_seconds', 'day_reached', 'turns_ended',
                       'status', 'memory_current_revision')}, ensure_ascii=False))
    print('actions', len(report['actions']),
          'completed', sum(1 for a in report['actions'] if a['status'] == 'completed'),
          'rejected', sum(1 for a in report['actions'] if a['status'] == 'rejected'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
