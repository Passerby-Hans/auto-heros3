# -*- coding: utf-8 -*-
"""Live verification for tools/fog_path.py (shroud-border BFS walk on the real client).

Starts the pinned VCMI client on a save (via tools/vcmi_agent.py, off-screen), then walks the
hero along the black-shroud border toward a target that the normal pathfinder reports as
"unreachable".  Every step is printed as one line and the whole run is recorded to
``var/reports/fog-explore/<timestamp>/`` (run.json, steps.jsonl, screenshots).

Run:
  python -X utf8 tools/fog_path_live.py --save Saves/agent/probe-ug18 --hero 1 \
      --target 12,26,1 --map-size 36x36 --radius 8 --max-steps 25

The save slot is passed to the pinned client's ``--agent-start``; this is a diagnostic
resume (续档) - it is NOT part of the clean-run evidence and must be labelled as such.
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


def _parse_target(text: str):
    parts = [int(part) for part in str(text).replace(':', ',').split(',') if part.strip()]
    if len(parts) == 2:
        parts.append(0)
    if len(parts) != 3:
        raise argparse.ArgumentTypeError('target must be x,y[,z]')
    return tuple(parts)


def parse_map_size(text: str):
    parts = [int(part) for part in str(text).lower().replace('*', 'x').split('x') if part.strip()]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError('map size must be WxH (e.g. 36x36)')
    return (parts[0], parts[1])


class LiveBridge:
    """Thin adapter: fog_path injectables -> VcmiAgent bridge commands."""

    def __init__(self, agent: VcmiAgent, hero: int, log: list, steps_path: Path,
                 shot_dir: Path | None = None):
        self.agent = agent
        self.hero = hero
        self.log = log
        self.steps_path = steps_path
        self.shot_dir = shot_dir
        self.turns = 0
        self.counter = 0

    def _append(self, entry: dict) -> None:
        self.counter += 1
        entry['n'] = self.counter
        entry['ts'] = time.strftime('%Y-%m-%dT%H:%M:%S')
        self.log.append(entry)
        with open(self.steps_path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + '\n')

    def observe(self) -> dict:
        return self.agent.state(timeout=10)

    def probe(self, tiles):
        line = F.probe_command(self.hero, tiles)
        result = self.agent.execute(line, verify=None, result_timeout=30)
        kind = result.status
        body = result.line
        if kind != 'COMPLETED' or 'OK probe' not in body:
            self._append({'kind': 'probe_error', 'status': kind, 'line': body[:200],
                          'tiles': len(tiles)})
            raise RuntimeError(f'probe failed: {kind} {body[:200]}')
        parsed = F.parse_probe_reply(body)
        if len(parsed) != len(tiles):
            # Some tiles may be outside the clamped box?  Not an error: report it once.
            self._append({'kind': 'probe_partial', 'asked': len(tiles), 'got': len(parsed)})
        self._append({'kind': 'probe', 'tiles': len(tiles), 'reachable':
                      sum(1 for data in parsed.values() if data['reachable'])})
        return parsed

    def answer_query(self, query):
        """Deterministic policy for the diagnostic run: experience > resource > first option;
        option-less dialogs (yes_no/info/garrison/recruitment) answer 0 (decline)."""
        options = [opt for opt in (query.get('options') or []) if isinstance(opt, dict)]
        chosen = None
        for preferred in ('EXPERIENCE', 'PRIMARY_SKILL', 'RESOURCE'):
            for opt in options:
                if str(opt.get('type')) == preferred:
                    chosen = opt
                    break
            if chosen is not None:
                break
        if chosen is None and options:
            chosen = options[0]
        index = int(chosen.get('index')) if chosen is not None else 0
        answer_type = str((chosen or {}).get('type') or query.get('type') or '')

        def _cleared():
            state = self.agent.state(timeout=5)
            return (state.get('query') is None), f"query={state.get('query')}"

        result = self.agent.execute(f'answer {int(index)}', verify=_cleared,
                                    verify_timeout=12.0, result_timeout=30)
        status = result.status.lower()
        self._append({'kind': 'answer_query', 'query_id': query.get('id'), 'option': index,
                      'type': answer_type, 'value': (chosen or {}).get('value'),
                      'status': status})
        print(f"[query] {str(query.get('text'))[:36]!r} -> option {index} "
              f"({answer_type} {(chosen or {}).get('value')}) {status}")
        return {'ok': status in ('completed', 'submitted'), 'status': status,
                'detail': str(result.line)[:200]}

    def move(self, tile):
        x, y, z = int(tile[0]), int(tile[1]), int(tile[2])
        before = F.hero_tile(self.agent.state(timeout=5), self.hero)

        def _moved():
            state = self.agent.state(timeout=5)
            current = F.hero_tile(state, self.hero)
            return (current is not None and current != before), f'hero {before} -> {current}'

        result = self.agent.execute(f'move_hero {self.hero} {x} {y} {z}',
                                    verify=_moved, verify_timeout=12.0,
                                    result_timeout=30)
        status = result.status.lower()  # completed / rejected / unknown
        entry = {'kind': 'move', 'to': [x, y, z], 'status': status,
                 'detail': str(result.detail or result.line)[:200]}
        self._append(entry)
        after = F.hero_tile(self.agent.state(timeout=5), self.hero)
        print(f'[move] {before} -> {after}  (asked {x},{y},{z}) {status}')
        return {'ok': status in ('completed', 'submitted'), 'status': status,
                'detail': entry['detail']}

    def end_turn(self):
        state_before = self.agent.state(timeout=10)
        day_before = (state_before.get('date') or {}).get('day')

        def _next_turn():
            state = self.agent.state(timeout=5)
            day = (state.get('date') or {}).get('day')
            hero = F.hero_tile(state, self.hero)
            return (day is not None and day != day_before), f'day {day_before} -> {day}, hero {hero}'

        result = self.agent.execute('end_turn', verify=_next_turn, verify_timeout=180.0,
                                    result_timeout=60)
        status = result.status.lower()
        state = self.agent.state(timeout=10)
        self._append({'kind': 'end_turn', 'status': status,
                      'day': (state.get('date') or {}).get('day'),
                      'movement': F.hero_movement(state, self.hero)})
        print(f'[turn] day {(state.get("date") or {}).get("day")} '
              f'movement={F.hero_movement(state, self.hero)} {status}')
        if self.shot_dir is not None and self.agent.hwnd:
            try:
                self.turns += 1
                self.agent.shot(self.shot_dir / f'turn{self.turns:02d}-day'
                                f'{(state.get("date") or {}).get("day")}.png')
            except Exception as exc:                           # pragma: no cover
                print(f'[turn] screenshot skipped: {exc}')
        return {'ok': status in ('completed', 'submitted'), 'status': status,
                'detail': str(result.detail or result.line)[:200]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--save', default='Saves/agent/probe-ug18')
    parser.add_argument('--dir', default=None, help='bridge/state dir (default: var/fog-explore-<ts>)')
    parser.add_argument('--hero', type=int, default=1)
    parser.add_argument('--target', type=_parse_target, default=(12, 26, 1))
    parser.add_argument('--map-size', default=None, help='WxH; omit to infer from observed state')
    parser.add_argument('--radius', type=int, default=8)
    parser.add_argument('--max-radius', type=int, default=12)
    parser.add_argument('--max-steps', type=int, default=20)
    parser.add_argument('--max-turns', type=int, default=0,
                        help='extra turns to end automatically when movement runs out')
    parser.add_argument('--auto-answer', action='store_true',
                        help='answer UI queries automatically (diagnostic runs only)')
    parser.add_argument('--keep', action='store_true', help='leave the client running at the end')
    parser.add_argument('--attach', action='store_true',
                        help='continue on the client of an existing bridge dir instead of '
                             'starting/reloading one (use with --dir)')
    parser.add_argument('--no-screenshot', action='store_true')
    args = parser.parse_args()

    stamp = time.strftime('%Y%m%d-%H%M%S')
    run_dir = ROOT / 'var' / 'reports' / 'fog-explore' / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    bridge_dir = Path(args.dir) if args.dir else ROOT / 'var' / f'fog-explore-{stamp}'
    steps_path = run_dir / 'steps.jsonl'
    steps_path.write_text('', encoding='utf-8')

    summary = {
        'tool': 'fog_path_live.py',
        'started_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'save': args.save,
        'bridge_dir': str(bridge_dir),
        'hero': args.hero,
        'target': list(args.target),
        'radius': args.radius,
        'max_radius': args.max_radius,
        'max_steps': args.max_steps,
        'note': ('diagnostic resume (续档) of a test save - NOT clean-run evidence; '
                 'the saved game is loaded, not replayed'),
    }
    print(json.dumps({'run_dir': str(run_dir), 'bridge_dir': str(bridge_dir)},
                     ensure_ascii=False))

    agent = VcmiAgent(bridge_dir, args.save, offscreen=True)
    attached = bool(args.attach)
    try:
        if attached:
            state = agent.state(timeout=20)
            summary['client_pid'] = 'attached'
            try:
                import vcmi_shot
                wins = vcmi_shot.find_window()
                agent.hwnd = wins[0][0] if wins else None
            except Exception:                                  # pragma: no cover
                agent.hwnd = None
        else:
            if not agent.start():
                summary['error'] = 'client_start_failed'
                (run_dir / 'run.json').write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
                return 2
            state = agent.wait_in_game()
            summary['client_pid'] = agent.session.client_pid if agent.session else None
        summary['start_day'] = (state.get('date') or {}).get('day')
        summary['start_hero_tile'] = F.hero_tile(state, args.hero)
        print(f"[live] client pid={summary['client_pid']} day={summary['start_day']} "
              f"hero{args.hero} tile={summary['start_hero_tile']}")
        if not args.no_screenshot and agent.hwnd:
            agent.shot(run_dir / '01-start.png')

        bounds = (F.bounds_from_map(*parse_map_size(args.map_size)) if args.map_size
                  else F.observed_bounds(state))
        summary['bounds'] = list(bounds)
        print(f'[live] probe bounds={bounds}')

        bridge = LiveBridge(agent, args.hero, [], steps_path,
                            shot_dir=None if args.no_screenshot else run_dir)
        explorer = F.FogExplorer(probe=bridge.probe, move=bridge.move,
                                 observe=bridge.observe, hero=args.hero,
                                 target=args.target, bounds=bounds,
                                 radius=args.radius, max_radius=args.max_radius,
                                 max_steps=args.max_steps,
                                 end_turn=bridge.end_turn, max_turns=args.max_turns,
                                 answer_query=bridge.answer_query if args.auto_answer else None,
                                 log=lambda msg: print('[live] ' + msg))
        started = time.time()
        result = explorer.run(max_moves=args.max_steps, max_turns=args.max_turns)
        summary['elapsed_s'] = round(time.time() - started, 2)
        summary['result_status'] = result.get('status')
        summary['result_ok'] = result.get('ok')
        summary['turns_ended'] = result.get('turns')
        summary['stats'] = result.get('stats')
        summary['trace_len'] = len(result.get('trace') or [])
        summary['end_hero_tile'] = (F.hero_tile(agent.state(timeout=10), args.hero)
                                    if not result.get('status') == 'no_state' else None)
        summary['end_movement'] = (F.hero_movement(agent.state(timeout=10), args.hero)
                                   if not result.get('status') == 'no_state' else None)
        print(json.dumps({'status': result.get('status'), 'ok': result.get('ok'),
                          'turns': result.get('turns'), 'stats': result.get('stats'),
                          'end_hero_tile': summary['end_hero_tile']}, ensure_ascii=False))
        for entry in (result.get('trace') or [])[-6:]:
            print(json.dumps(entry, ensure_ascii=False)[:300])
        if not args.no_screenshot and agent.hwnd:
            shot = agent.shot(run_dir / '02-end.png')
            summary['end_screenshot'] = str(shot)
        (run_dir / 'run.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                          encoding='utf-8')
        return 0 if result.get('ok') else 1
    finally:
        if args.keep or attached:
            print('[live] client kept running; bridge dir ' + str(bridge_dir))
        else:
            try:
                agent.stop()
                print('[live] client stopped')
            except Exception as exc:                     # pragma: no cover - best effort
                print(f'[live] stop failed: {exc}')


if __name__ == '__main__':
    raise SystemExit(main())
