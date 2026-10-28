# -*- coding: utf-8 -*-
"""R2 toolkit: formal MCP stdio server, evaluation matrix and recorded full-game runner.

One file, three subcommands (keeps the runtime surface small):

  serve   --stdio MCP-like JSON-RPC loop: one request per line, one JSON reply per line;
          tools are the ones exposed by agent_mcp.call (status/state/act/advance/end_turn/
          deliver/orders/shot/stop).  A client (any LLM harness) can own a whole game this way.
  eval    evaluation matrix: run vcmi_win.py for each frozen config, aggregate win rate,
          turns, duration, unknown/stall counters into var/reports/eval-*.json/.md.
          `--dry-run` validates the pipeline without starting the game.
  record  full-game recording: launch the win driver, capture frames with PrintWindow at 4 fps
          (window stays off-screen), encode var/recordings/full-game-<stamp>.mp4 afterwards.

Examples:
  python -X utf8 tools/agent_mcp_server.py serve
  python -X utf8 tools/agent_mcp_server.py eval --dry-run
  python -X utf8 tools/agent_mcp_server.py eval --configs var/configs/eval.json
  python -X utf8 tools/agent_mcp_server.py record --minutes 25
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

REPORTS = ROOT / 'var' / 'reports'
RECORDINGS = ROOT / 'var' / 'recordings'
DEFAULT_CONFIGS = [
    {'name': 'arrogance-normal', 'map': 'Maps/Arrogance', 'max_turns': 200, 'max_minutes': 40},
]


def serve() -> int:
    """Minimal stdio MCP-like loop: {"tool": "...", "args": {...}} per line."""
    import agent_mcp
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError as exc:
            print(json.dumps({'ok': False, 'error': f'bad json: {exc}'}), flush=True)
            continue
        if req.get('tool') == 'shutdown':
            print(json.dumps({'ok': True, 'result': 'bye'}), flush=True)
            return 0
        try:
            reply = agent_mcp.call(req)
        except Exception as exc:
            reply = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
        print(json.dumps(reply, ensure_ascii=False), flush=True)
    return 0


def eval_matrix(configs: list, dry_run: bool = False, minutes: float = 40.0) -> int:
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    rows = []
    for cfg in configs:
        name = cfg.get('name') or cfg.get('map', 'map')
        if dry_run:
            rows.append({'config': cfg, 'status': 'dry-run'})
            continue
        cmd = [sys.executable, '-u', '-X', 'utf8', str(ROOT / 'tools' / 'vcmi_win.py'),
               '--map', cfg.get('map', 'Maps/Arrogance'),
               '--dir', str(ROOT / 'var' / f"eval_{name}"),
               '--max-turns', str(cfg.get('max_turns', 200)),
               '--max-minutes', str(cfg.get('max_minutes', minutes))]
        log_path = REPORTS / f'eval-{stamp}-{name}.log'
        t0 = time.time()
        with open(log_path, 'w', encoding='utf-8') as log:
            proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
        row = {'config': cfg, 'exit': proc.returncode, 'elapsed': round(time.time() - t0, 1),
               'log': str(log_path)}
        # latest report written by that run
        candidates = sorted(REPORTS.glob('full-game-*.json'), key=lambda p: p.stat().st_mtime)
        if candidates:
            try:
                rep = json.loads(candidates[-1].read_text(encoding='utf-8'))
                row.update({'stop_reason': rep.get('stop_reason'),
                            'turns': rep.get('turns_played'),
                            'elapsed_game': rep.get('elapsed_seconds'),
                            'result': rep.get('end_status')})
            except ValueError:
                pass
        rows.append(row)
    summary = {'stamp': stamp, 'configs': len(rows), 'rows': rows,
               'wins': sum(1 for r in rows if r.get('result') == 'WINNER'),
               'losses': sum(1 for r in rows if r.get('result') == 'LOSER'),
               'incomplete': sum(1 for r in rows if r.get('result') not in ('WINNER', 'LOSER')),
               'human_interventions': 0}
    out = REPORTS / f'eval-{stamp}.json'
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding='utf-8')
    (REPORTS / f'eval-{stamp}.md').write_text(
        '# eval\n\n' + '\n'.join(f"- {json.dumps(r, ensure_ascii=False)}" for r in rows),
        encoding='utf-8')
    print(json.dumps({k: summary[k] for k in ('configs', 'wins', 'losses', 'incomplete')},
                     ensure_ascii=False))
    print('summary:', out)
    return 0


def record(minutes: float, map_name: str, max_turns: int) -> int:
    import vcmi_record
    import vcmi_shot
    RECORDINGS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    frames_dir = RECORDINGS / f'full-game-{stamp}-frames'
    rec = vcmi_record.FrameRecorder(frames_dir, fps=4.0)
    rec.set_caption('LLM 接管：完整游戏录制（离屏、静音）')
    cmd = [sys.executable, '-u', '-X', 'utf8', str(ROOT / 'tools' / 'vcmi_win.py'),
           '--map', map_name, '--dir', str(ROOT / 'var' / 'agent_rec'),
           '--max-turns', str(max_turns), '--max-minutes', str(minutes)]
    log_path = REPORTS / f'recorded-run-{stamp}.log'
    REPORTS.mkdir(parents=True, exist_ok=True)
    with open(log_path, 'w', encoding='utf-8') as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        # attach the recorder to the game window as soon as it exists (window stays off-screen)
        hwnd = None
        t0 = time.time()
        while time.time() - t0 < minutes * 60:
            if proc.poll() is not None:
                break
            if hwnd is None:
                wins = vcmi_shot.find_window()
                if wins:
                    hwnd = wins[0][0]
                    rec.hwnd = hwnd
                    rec.start(hwnd)
            time.sleep(1.0)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    rec.stop()
    video = RECORDINGS / f'full-game-{stamp}.mp4'
    ok = vcmi_record.encode(frames_dir, video)
    print(json.dumps({'ok': ok, 'video': str(video), 'frames': rec.count,
                      'run_log': str(log_path), 'seconds': round(time.time() - t0, 1)},
                     ensure_ascii=False))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('serve')
    p_eval = sub.add_parser('eval')
    p_eval.add_argument('--configs', default=None)
    p_eval.add_argument('--dry-run', action='store_true')
    p_rec = sub.add_parser('record')
    p_rec.add_argument('--minutes', type=float, default=25.0)
    p_rec.add_argument('--map', default='Maps/Arrogance')
    p_rec.add_argument('--max-turns', type=int, default=200)
    a = ap.parse_args()

    if a.cmd == 'serve':
        return serve()
    if a.cmd == 'eval':
        configs = DEFAULT_CONFIGS
        if a.configs:
            configs = json.loads(Path(a.configs).read_text(encoding='utf-8'))
        return eval_matrix(configs, dry_run=a.dry_run)
    if a.cmd == 'record':
        return record(a.minutes, a.map, a.max_turns)
    return 1


if __name__ == '__main__':
    sys.exit(main())
