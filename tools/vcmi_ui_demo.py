# -*- coding: utf-8 -*-
"""UI-driven recruitment demo (recorded).

Shows the *real* game UI: town screen -> click a dwelling icon -> recruitment window ->
click "max" -> click "recruit", and verifies the purchase through PlayerView state.
Frames are recorded and encoded to mp4 afterwards (crash-safe, reuse of vcmi_record).

Usage:  python -X utf8 tools/vcmi_ui_demo.py [--map Maps/Arrogance]
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

from vcmi_agent import VcmiAgent  # noqa: E402
import vcmi_record  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--map', default='Maps/Arrogance')
    ap.add_argument('--dir', default=str(ROOT / 'var' / 'agent_ui_demo'))
    args = ap.parse_args()

    out_dir = ROOT / 'var' / 'recordings'
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    frames_dir = out_dir / f'{stamp}-ui-frames'

    agent = VcmiAgent(args.dir, args.map)
    if not agent.start():
        print('client did not start')
        return 1
    st = agent.wait_in_game(180)
    rec = vcmi_record.FrameRecorder(frames_dir)
    rec.start(agent.hwnd)

    def cap(text: str) -> None:
        rec.set_caption(text)
        print('[ui]', text, flush=True)

    def cmd(line: str, timeout: float = 10.0) -> str:
        before = len(agent.results())
        agent.send(line)
        t0 = time.time()
        while time.time() - t0 < timeout:
            got = [ln for ln in agent.results()[before:] if ln.startswith('OK') or ln.startswith('ERR')]
            if got:
                return got[-1]
            time.sleep(0.05)
        return '<<no result>>'

    def tree() -> dict:
        cmd('ui_tree 6')
        time.sleep(0.3)
        try:
            return json.loads((agent.dir / 'ui_tree.json').read_text(encoding='utf-8'))
        except Exception:
            return {}

    def collect(node, out):
        if not isinstance(node, dict):
            return
        if 'cls' in node:
            out.append(node)
        for k, v in node.items():
            if k in ('cls', 'x', 'y', 'w', 'h', 'depth'):
                continue
            if isinstance(v, dict):
                collect(v, out)
            elif isinstance(v, list):
                for it in v:
                    collect(it, out)

    def nodes_of(d: dict, win_index: int) -> list:
        ws = d.get('windows', [])
        if win_index >= len(ws):
            return []
        out = []
        collect(ws[win_index], out)
        return out

    def root_cls(d: dict, win_index: int) -> str:
        ws = d.get('windows', [])
        if win_index >= len(ws):
            return ''
        return (ws[win_index].get('root') or {}).get('cls', '')

    try:
        cap('功能: 打开城镇界面（真实 UI）')
        print('[ui]', cmd('open_town 0'), flush=True)
        time.sleep(2.0)

        d = tree()
        ws = d.get('windows', [])
        town_idx = next((i for i in range(len(ws)) if 'CastleInterface' in root_cls(d, i)), None)
        if town_idx is None:
            cap('没有找到城镇窗口')
            raise SystemExit(1)
        town_nodes = nodes_of(d, town_idx)
        # dwelling icons are CCreaInfo widgets on the left panel
        dwell = [n for n in town_nodes if 'CCreaInfo' in n['cls'] and n['w'] > 0]
        dwell.sort(key=lambda n: (n['y'], n['x']))
        cap(f'功能: 点击兵营图标（{len(dwell)} 个可招募建筑）')
        if not dwell:
            cap('没有找到兵营图标')
            raise SystemExit(1)
        first = dwell[0]
        cx, cy = first['x'] + first['w'] // 2, first['y'] + first['h'] // 2
        print('[ui]', cmd(f'click {cx} {cy}'), flush=True)
        time.sleep(2.0)

        d = tree()
        ws = d.get('windows', [])
        rec_idx = next((i for i in range(len(ws)) if 'Recruitment' in root_cls(d, i)), None)
        if rec_idx is None:
            cap('招募窗口没有打开')
            raise SystemExit(1)
        cap('功能: 招募窗口已弹出（观众可见）')
        rec_nodes = nodes_of(d, rec_idx)
        # exclude the small slider arrow buttons (16x16) - keep the real action buttons
        buttons = sorted([n for n in rec_nodes if n['cls'].endswith('CButton')
                          and n['w'] >= 48 and n['h'] >= 24], key=lambda n: n['x'])
        slider = [n for n in rec_nodes if 'CSlider' in n['cls']]
        cap(f'窗口控件: {len(buttons)} 个按钮, {len(slider)} 个滑条')
        if len(buttons) >= 2:
            mx = buttons[0]
            cmd(f"click {mx['x'] + mx['w'] // 2} {mx['y'] + mx['h'] // 2}")   # max
            time.sleep(1.2)
            cap('功能: 点击"最大"选择购买数量')
            buy = buttons[1]
            before = agent.state(10)
            cmd(f"click {buy['x'] + buy['w'] // 2} {buy['y'] + buy['h'] // 2}")  # recruit
            cap('功能: 点击"招募"按钮（真实 UI 点击）')
            time.sleep(2.5)
            after = agent.state(10)
            g0, g1 = before['resources']['gold'], after['resources']['gold']
            gar0 = {g['creature']: g['count'] for g in before['towns'][0].get('garrison', [])}
            gar1 = {g['creature']: g['count'] for g in after['towns'][0].get('garrison', [])}
            cap(f'验证: 金币 {g0} -> {g1}, 驻军 {gar0} -> {gar1}')
            print('[ui] state change:', g0, '->', g1, gar0, '->', gar1, flush=True)
        time.sleep(1.5)

        cap('功能: 关闭招募窗口 / 返回冒险地图')
        cmd('close_top')
        time.sleep(1.0)
        cmd('close_top')
        time.sleep(1.5)
        cap('演示结束：招募完全通过界面点击完成（无直接动作 API）')
        time.sleep(3)
    finally:
        rec.stop()
        ok = vcmi_record.encode(frames_dir, out_dir / f'ui-recruit-demo-{stamp}.mp4')
        print('[ui] frames ->', frames_dir, flush=True)
        if not ok:
            print('[ui] encode failed; frames kept', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
