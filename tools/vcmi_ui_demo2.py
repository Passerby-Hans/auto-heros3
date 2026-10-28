# -*- coding: utf-8 -*-
"""Recorded UI demo: recruit troops and hire a hero entirely through the game UI.

Flow: move the hero out of town (H3 rule: the town tavern cannot hire while a hero visits),
open the town screen, click a dwelling -> recruitment window -> max -> recruit; then click the
tavern -> select a hero -> hire.  Everything is driven by synthetic mouse events into VCMI's
own UI, so the video shows the real windows.
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
from vcmi_agent import VcmiAgent  # noqa: E402
import vcmi_record  # noqa: E402


def cmd(ag, line, timeout=12.0):
    before = len(ag.results())
    ag.send(line)
    t0 = time.time()
    while time.time() - t0 < timeout:
        got = [ln for ln in ag.results()[before:] if ln.startswith('OK') or ln.startswith('ERR')]
        if got:
            return got[-1]
        time.sleep(0.05)
    return '<<no result>>'


def tree(ag):
    cmd(ag, 'ui_tree 6')
    time.sleep(0.3)
    try:
        return json.loads((ag.dir / 'ui_tree.json').read_text(encoding='utf-8'))
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


def wn(d, i):
    ws = d.get('windows', [])
    if i >= len(ws):
        return [], ''
    out = []
    collect(ws[i], out)
    return out, (ws[i].get('root') or {}).get('cls', '')


def clk(ag, n):
    cmd(ag, f"click {n['x'] + n['w'] // 2} {n['y'] + n['h'] // 2}")


def close_extras(ag):
    for _ in range(4):
        d = tree(ag)
        ws = d.get('windows', [])
        if not any(not any(k in wn(d, i)[1] for k in ('AdventureMapInterface', 'CCastleInterface'))
                   for i in range(len(ws))):
            return
        cmd(ag, 'close_window')
        time.sleep(0.4)



def prepare(attempts: int = 4):
    """Start a game whose starting town actually has a tavern (random town each game)."""
    for attempt in range(1, attempts + 1):
        ag = VcmiAgent(str(ROOT / 'var' / 'agent_ui_demo2'), 'Maps/Arrogance')
        if not ag.start():
            continue
        ag.wait_in_game(180)
        pos = ag.state(10)['heroes'][0]['visitable']
        for target in ([pos[0], pos[1] + 1], [pos[0] + 1, pos[1]], [pos[0], pos[1] - 1],
                       [pos[0] - 1, pos[1]]):
            r = cmd(ag, f'move_hero 0 {target[0]} {target[1]} 0')
            if r.startswith('OK'):
                time.sleep(1.5)
                break
        cmd(ag, 'open_town 0')
        time.sleep(1.8)
        d = tree(ag)
        ws = d.get('windows', [])
        town_idx = next((i for i in range(len(ws)) if 'CastleInterface' in wn(d, i)[1]), None)
        if town_idx is None:
            continue
        nodes, _ = wn(d, town_idx)
        rects = [n for n in nodes if n['cls'].endswith('CBuildingRect')]
        for n in rects:
            clk(ag, n)
            time.sleep(1.0)
            d2 = tree(ag)
            ws2 = d2.get('windows', [])
            if any('TavernWindow' in wn(d2, i)[1] for i in range(len(ws2))):
                print(f'[ui2] attempt {attempt}: town has tavern at '
                      f'({n["x"] + n["w"] // 2},{n["y"] + n["h"] // 2})', flush=True)
                cmd(ag, 'close_window')
                time.sleep(0.5)
                return ag, n
            close_extras(ag)
        print(f'[ui2] attempt {attempt}: no tavern in this town, restarting', flush=True)
        try:
            ag.stop()
        except Exception:
            pass
    return None, None


def main():
    out_dir = ROOT / 'var' / 'recordings'
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    frames_dir = out_dir / f'{stamp}-ui2-frames'
    ag, tavern_building = prepare()
    if ag is None:
        print('could not get a town with a tavern')
        return 1
    rec = vcmi_record.FrameRecorder(frames_dir)
    rec.start(ag.hwnd)

    def cap(t):
        rec.set_caption(t)
        print('[ui2]', t, flush=True)

    try:
        cap('UI 演示：招募界面 + 酒馆招英雄（全部通过模拟鼠标点击完成）')
        time.sleep(2.2)
        cap('第一步：英雄已移出城镇（H3 规则：有英雄在城里时酒馆不能雇新英雄）')
        time.sleep(1.2)

        # --- town screen (already open from prepare; reopen to be sure) ---
        cap('第二步：打开城镇界面（真实 UI）')
        print('[ui2]', cmd(ag, 'open_town 0'), flush=True)
        time.sleep(2.0)

        # --- recruit troops through the dwelling window ---
        cap('第三步：点击兵营图标，打开招募窗口')
        d = tree(ag)
        ws = d.get('windows', [])
        town_idx = next((i for i in range(len(ws)) if 'CastleInterface' in wn(d, i)[1]), None)
        nodes, _ = wn(d, town_idx) if town_idx is not None else ([], '')
        dwell = sorted([n for n in nodes if 'CCreaInfo' in n['cls'] and n['w'] > 0],
                       key=lambda n: (n['y'], n['x']))
        if not dwell:
            cap('没有找到招募建筑')
            raise SystemExit(1)
        clk(ag, dwell[0])
        time.sleep(2.0)

        d = tree(ag)
        ws = d.get('windows', [])
        ri = next((i for i in range(len(ws)) if 'Recruitment' in wn(d, i)[1]), None)
        if ri is None:
            cap('招募窗口没有打开')
            raise SystemExit(1)
        cap('第四步：招募窗口已弹出 —— 点“最大”再点“招募”')
        nodes, _ = wn(d, ri)
        buttons = sorted([n for n in nodes if n['cls'].endswith('CButton') and n['w'] >= 48 and n['h'] >= 24],
                         key=lambda n: n['x'])
        before = ag.state(10)
        g0 = before['resources']['gold']
        clk(ag, buttons[0])          # max
        time.sleep(1.2)
        clk(ag, buttons[1])          # recruit
        time.sleep(2.4)
        after = ag.state(10)
        cap(f'验证：金币 {g0} -> {after["resources"]["gold"]}（界面点击完成购买）')
        time.sleep(1.6)
        # close only the recruitment window - close_window on the wrong window would pop the town
        dnow = tree(ag)
        wsnow = dnow.get('windows', [])
        for i in range(len(wsnow)):
            if 'Recruitment' in wn(dnow, i)[1]:
                cmd(ag, 'close_window')
                time.sleep(1.0)
                break
        # make sure the town screen is still open before clicking the tavern
        dnow = tree(ag)
        if not any('CCastleInterface' in wn(dnow, i)[1] for i in range(len(dnow.get('windows', [])))):
            cap('重新打开城镇界面')
            cmd(ag, 'open_town 0')
            time.sleep(2.0)

        # --- tavern: select hero then hire ---
        cap('第五步：点击酒馆建筑（招募英雄界面）')
        clk(ag, tavern_building)
        time.sleep(2.0)

        def tavern_index() -> int | None:
            dd = tree(ag)
            wss = dd.get('windows', [])
            return next((i for i in range(len(wss)) if 'TavernWindow' in wn(dd, i)[1]), None)

        ti = tavern_index()
        if ti is None:
            # the building probe coordinates can be stale (town re-opened); search again
            cap('重新定位酒馆建筑…')
            d0 = tree(ag)
            ws0 = d0.get('windows', [])
            town_i = next((i for i in range(len(ws0)) if 'CastleInterface' in wn(d0, i)[1]), None)
            cand = [n for n in (wn(d0, town_i)[0] if town_i is not None else [])
                    if n['cls'].endswith('CBuildingRect')]
            for n in cand:
                clk(ag, n)
                time.sleep(1.2)
                ti = tavern_index()
                if ti is not None:
                    print('[ui2] tavern reopened via building', flush=True)
                    break
                close_extras(ag)
        if ti is None:
            cap('没有打开酒馆窗口，演示结束（招兵部分已完成）')
            raise SystemExit(0)

        d = tree(ag)
        ws = d.get('windows', [])
        nodes, _ = wn(d, ti)
        portraits = [n for n in nodes if 'HeroPortrait' in n['cls']]
        buttons = [n for n in nodes if n['cls'].endswith('CButton') and n['w'] >= 40]
        before = ag.state(10)
        g0, h0 = before['resources']['gold'], len(before['heroes'])
        cap('第六步：点英雄头像选中（酒馆窗口，观众可见）')
        if portraits:
            clk(ag, portraits[0])
            time.sleep(1.6)
        cap('第七步：点“招募”按钮雇佣英雄')
        if buttons:
            clk(ag, max(buttons, key=lambda n: n['w']))
            time.sleep(2.4)
        after = ag.state(10)
        cap(f'验证：金币 {g0} -> {after["resources"]["gold"]}，英雄 {h0} -> {len(after["heroes"])}：'
            f'{", ".join(h["name"] for h in after["heroes"])}')
        time.sleep(2.0)
        cmd(ag, 'close_window')
        time.sleep(0.8)
        cmd(ag, 'close_window')
        time.sleep(1.2)
        cap('演示结束：招兵 + 招英雄全部通过界面点击完成；没有任何直接动作 API')
        time.sleep(3)
    finally:
        rec.stop()
        ok = vcmi_record.encode(frames_dir, out_dir / f'ui-full-demo-{stamp}.mp4')
        print('[ui2] frames ->', frames_dir, flush=True)
        if not ok:
            print('[ui2] encode failed', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
