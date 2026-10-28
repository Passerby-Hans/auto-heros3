# -*- coding: utf-8 -*-
"""Minimal deterministic battle resolver for the diagnostic shroud-walk demo.

The explorer (`fog_path.py`) stops when a battle starts.  This small driver finishes that
battle with plain `battle_actions` / `battle_attack` / `battle_approach` / `battle_wait`
bridge commands (the same actions the MCP exposes), then optionally dismisses the result
window.  It is a *diagnostic* helper: the LLM still decides whether to fight; this only
executes the fight the explorer walked into.

Usage:
  python -X utf8 tools/fog_battle.py --dir var/fog-explore-<ts> [--max-actions 120]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import fog_path as F  # noqa: E402
from vcmi_agent import VcmiAgent  # noqa: E402


def read_battle_actions(agent: VcmiAgent, directory: Path) -> dict:
    result = agent.execute('battle_actions', verify=None, result_timeout=15)
    if result.status != 'COMPLETED':
        return {}
    try:
        return json.loads((directory / 'battle_actions.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def choose_target(data: dict, stacks: list) -> tuple[int | None, str]:
    """Pick an enemy stack: shooters first, then melee, then approach, then the nearest one."""
    targets = [tgt for tgt in (data.get('targets') or []) if isinstance(tgt, dict)]
    for key in ('can_shoot', 'can_melee', 'walk'):
        for tgt in targets:
            if tgt.get(key):
                return int(tgt['id']), key
    enemy = [stack for stack in stacks if stack.get('side') != data.get('my_side')]
    if enemy and targets:
        return int(targets[0]['id']), 'any'
    return None, 'none'


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--dir', required=True, help='bridge/state dir of the running client')
    parser.add_argument('--hero', type=int, default=1)
    parser.add_argument('--max-actions', type=int, default=120)
    args = parser.parse_args()

    directory = Path(args.dir)
    if not directory.is_absolute():
        directory = ROOT / directory
    agent = VcmiAgent(directory)
    summary = {'bridge_dir': str(directory), 'actions': [], 'started_at':
               time.strftime('%Y-%m-%dT%H:%M:%S')}
    for step in range(args.max_actions):
        state = agent.state(timeout=10)
        battle = state.get('battle') or {}
        if not battle.get('active'):
            summary['ended'] = 'battle_finished'
            break
        data = read_battle_actions(agent, directory)
        if not data or data.get('active') is None:
            # not our stack's turn (AI stack acting / animation) - give the client a moment
            time.sleep(0.6)
            continue
        target_id, why = choose_target(data, battle.get('stacks') or [])
        if target_id is None:
            result = agent.execute('battle_wait', verify=None, result_timeout=15)
            summary['actions'].append({'step': step, 'cmd': 'battle_wait',
                                       'status': result.status, 'line': result.line[:120]})
            time.sleep(0.4)
            continue
        line = f'battle_attack {target_id}'
        result = agent.execute(line, verify=None, result_timeout=15)
        command = line
        if result.status == 'REJECTED':
            line = f'battle_approach {target_id}'
            result = agent.execute(line, verify=None, result_timeout=15)
            command = line
            if result.status == 'REJECTED':
                command = 'battle_wait'
                result = agent.execute(command, verify=None, result_timeout=15)
        summary['actions'].append({'step': step, 'cmd': command, 'why': why,
                                   'status': result.status, 'line': result.line[:120]})
        print(f'[{step}] {command} -> {result.status} {result.line[:80]}')
        time.sleep(0.5)
    else:
        summary['ended'] = 'max_actions'

    state = agent.state(timeout=10)
    summary['end_battle_active'] = bool((state.get('battle') or {}).get('active'))
    summary['end_hero_army'] = [stack for stack in
                                (state.get('heroes') or [{}])[args.hero].get('army', [])] \
        if state.get('heroes') else []
    # dismiss the battle result window if one is left open
    if not summary['end_battle_active']:
        result = agent.execute('battle_result_ok', verify=None, result_timeout=10)
        summary['result_window'] = {'status': result.status, 'line': result.line[:120]}
        time.sleep(0.5)
    out = ROOT / 'var' / 'reports' / 'fog-explore' / (
        time.strftime('%Y%m%d-%H%M%S') + '-battle.json')
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: summary.get(k) for k in
                      ('ended', 'end_battle_active', 'end_hero_army', 'result_window')},
                     ensure_ascii=False))
    print('report:', out)
    return 0 if not summary.get('end_battle_active') else 1


if __name__ == '__main__':
    raise SystemExit(main())
