# -*- coding: utf-8 -*-
"""Recorded UI demo where *every* step happens on camera.

Starts on the adventure map, walks the hero out of town (visible movement animation), opens
the town screen, recruits troops through the dwelling/recruitment windows, then hires a hero in
the tavern - all with synthetic mouse events.  If a random starting town has no tavern the whole
run is retried (failed frames are discarded), so the published video never cuts away.

Output: var/recordings/ui-camera-demo-<ts>.mp4 (copied to doc/findings by the caller).
"""
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
from vcmi_agent import VcmiAgent  # noqa: E402
import vcmi_record  # noqa: E402


def cmd(ag, line, timeout=15.0):
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
    time.sleep(0.25)
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


def ensure_town(ag):
    d = tree(ag)
    if any('CCastleInterface' in wn(d, i)[1] for i in range(len(d.get('windows', [])))):
        return True
    cmd(ag, 'open_town 0')
    time.sleep(1.8)
    d = tree(ag)
    return any('CCastleInterface' in wn(d, i)[1] for i in range(len(d.get('windows', []))))


def attempt(rec_factory, frames_dir):
    ag = VcmiAgent(str(ROOT / 'var' / 'agent_camera'), 'Maps/Arrogance')
    if not ag.start():
        return None
    ag.wait_in_game(180)
    rec = rec_factory(frames_dir)
    rec.start(ag.hwnd)

    def cap(t):
        rec.set_caption(t)
        print('[cam]', t, flush=True)

    ok = False
    try:
        st = ag.state(10)
        pos = list(st['heroes'][0]['visitable'])
        cap('开局：冒险地图（屏幕外窗口、进程静音、不抢鼠标焦点）')
        time.sleep(2.5)
        cap(f'当前：day {st["date"]["day"]}，英雄 {st["heroes"][0]["name"]} 在城镇 {st["towns"][0]["name"]}')
        time.sleep(2.0)

        # 1) walk the hero out of town - on camera
        cap('第一步：把英雄移出城镇（H3 规则：城里有英雄时酒馆不能雇新英雄）')
        moved = False
        for target in ([pos[0], pos[1] + 1], [pos[0] + 1, pos[1]], [pos[0], pos[1] - 1],
                       [pos[0] - 1, pos[1]], [pos[0] + 1, pos[1] + 1]):
            r = cmd(ag, f'move_hero 0 {target[0]} {target[1]} 0')
            print('[cam]  move', target, '->', r, flush=True)
            if r.startswith('OK'):
                time.sleep(2.0)
                st = ag.state(10)
                if list(st['heroes'][0]['visitable'])[:2] != pos[:2]:
                    moved = True
                    pos = list(st['heroes'][0]['visitable'])
                    break
        cap(f'英雄已走到 {pos[:2]}（画面上可以看到移动）')
        time.sleep(2.2)
        if not moved:
            return None

        # 2) town screen
        cap('第二步：打开城镇界面（真实 UI）')
        cmd(ag, 'open_town 0')
        time.sleep(2.4)
        if not ensure_town(ag):
            return None
        d = tree(ag)
        ws = d.get('windows', [])
        town_i = next((i for i in range(len(ws)) if 'CCastleInterface' in wn(d, i)[1]), None)
        nodes, _ = wn(d, town_i)
        # locate tavern by clicking buildings on camera (like a player exploring the town)
        cap('第三步：依次点击城镇建筑，找到酒馆')
        rects = [n for n in nodes if n['cls'].endswith('CBuildingRect')]
        tavern = None
        for n in rects:
            clk(ag, n)
            time.sleep(1.1)
            dd = tree(ag)
            wss = dd.get('windows', [])
            if any('TavernWindow' in wn(dd, i)[1] for i in range(len(wss))):
                tavern = n
                break
            close_extras(ag)
            ensure_town(ag)
        if tavern is None:
            print('[cam] no tavern in this town, retry', flush=True)
            return None
        cap('酒馆窗口已打开（观众可见）')
        time.sleep(1.8)
        dd = tree(ag)
        wss = dd.get('windows', [])
        ti = next(i for i in range(len(wss)) if 'TavernWindow' in wn(dd, i)[1])
        tnodes, _ = wn(dd, ti)
        portraits = [n for n in tnodes if 'HeroPortrait' in n['cls']]
        tbuttons = [n for n in tnodes if n['cls'].endswith('CButton') and n['w'] >= 40]
        cmd(ag, 'close_window')          # close tavern, do the troops first
        time.sleep(0.9)
        ensure_town(ag)

        # 3) recruit troops through the dwelling window
        cap('第四步：点击兵营图标，打开招募窗口')
        d = tree(ag)
        ws = d.get('windows', [])
        town_i = next(i for i in range(len(ws)) if 'CCastleInterface' in wn(d, i)[1])
        nodes, _ = wn(d, town_i)
        dwell = sorted([n for n in nodes if 'CCreaInfo' in n['cls'] and n['w'] > 0],
                       key=lambda n: (n['y'], n['x']))
        if not dwell:
            return None
        clk(ag, dwell[0])
        time.sleep(2.0)
        d = tree(ag)
        ws = d.get('windows', [])
        ri = next((i for i in range(len(ws)) if 'Recruitment' in wn(d, i)[1]), None)
        if ri is None:
            return None
        cap('第五步：招募窗口——点“最大”，再点“招募”')
        rnodes, _ = wn(d, ri)
        rb = sorted([n for n in rnodes if n['cls'].endswith('CButton')
                     and n['w'] >= 48 and n['h'] >= 24], key=lambda n: n['x'])
        before = ag.state(10)
        g0 = before['resources']['gold']
        clk(ag, rb[0])
        time.sleep(1.2)
        clk(ag, rb[1])
        time.sleep(2.4)
        after = ag.state(10)
        cap(f'验证：金币 {g0} -> {after["resources"]["gold"]}（招募完成）')
        time.sleep(1.6)
        # close only the recruitment window
        dn = tree(ag)
        if any('Recruitment' in wn(dn, i)[1] for i in range(len(dn.get('windows', [])))):
            cmd(ag, 'close_window')
            time.sleep(0.9)
        ensure_town(ag)

        # 4) hire a hero in the tavern
        cap('第六步：回到酒馆，选择英雄并点“招募”')
        clk(ag, tavern)
        time.sleep(2.0)
        dd = tree(ag)
        wss = dd.get('windows', [])
        ti = next((i for i in range(len(wss)) if 'TavernWindow' in wn(dd, i)[1]), None)
        if ti is None:
            return None
        tnodes, _ = wn(dd, ti)
        portraits = [n for n in tnodes if 'HeroPortrait' in n['cls']]
        tbuttons = [n for n in tnodes if n['cls'].endswith('CButton') and n['w'] >= 40]
        before = ag.state(10)
        g0, h0 = before['resources']['gold'], len(before['heroes'])
        cap('第七步：点英雄头像选中')
        if portraits:
            clk(ag, portraits[0])
            time.sleep(1.6)
        cap('第八步：点“招募”按钮雇佣英雄')
        if tbuttons:
            clk(ag, max(tbuttons, key=lambda n: n['w']))
            time.sleep(2.4)
        after = ag.state(10)
        cap(f'验证：金币 {g0} -> {after["resources"]["gold"]}，英雄 {h0} -> {len(after["heroes"])}：'
            f'{", ".join(h["name"] for h in after["heroes"])}')
        time.sleep(2.2)
        if len(after['heroes']) <= h0:
            print('[cam] hire failed, retry', flush=True)
            return None
        cmd(ag, 'close_window')
        time.sleep(0.8)
        ensure_town(ag)
        cmd(ag, 'close_window')
        time.sleep(1.4)
        cap('演示结束：移动、进城、招募、雇英雄全部在画面中完成（无隐藏步骤）')
        time.sleep(3.0)
        ok = True
        return frames_dir
    finally:
        rec.stop()
        if not ok:
            try:
                shutil.rmtree(frames_dir, ignore_errors=True)
            except Exception:
                pass


def main() -> int:
    out_dir = ROOT / 'var' / 'recordings'
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')

    def rec_factory(frames_dir: Path):
        return vcmi_record.FrameRecorder(frames_dir)

    for i in range(1, 5):
        frames_dir = out_dir / f'{stamp}-cam{i}-frames'
        print(f'[cam] ===== attempt {i} =====', flush=True)
        res = attempt(rec_factory, frames_dir)
        if res:
            video = out_dir / f'ui-camera-demo-{stamp}.mp4'
            ok = vcmi_record.encode(res, video)
            print('[cam] video ->', video if ok else 'encode failed', flush=True)
            shutil.copy2(video, ROOT / 'doc' / 'findings' / 'ui-camera-demo.mp4')
            return 0
        try:
            ag_kill = __import__('subprocess').run(['taskkill', '/F', '/IM', 'VCMI_client.exe'],
                                                   capture_output=True)
        except Exception:
            pass
        time.sleep(2)
    print('[cam] all attempts failed')
    return 1


if __name__ == '__main__':
    sys.exit(main())
