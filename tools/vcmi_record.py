# -*- coding: utf-8 -*-
"""Record an agent-driven VCMI demo covering as many bridge features as possible.

Crash-safe design: frames are written as JPEG files first, then encoded to mp4 with ffmpeg
after the run (or on the next run if the client crashed), so a client crash never destroys
the recording.

The demo deliberately avoids ending the turn (VCMI 1.7.5 can crash inside its own battle AI
during AI turns) and instead shows: off-screen capture, PlayerView state, building names and
prices, recruitment, second hero from the tavern, army transfer, upgrades, market, reachable
tile pathfinding + mine capture, query answering (treasure chest / level-up) and a manual
battle driven through BattleAction packets.

Usage:
    python -X utf8 tools/vcmi_record.py --map Maps/Arrogance
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from vcmi_agent import VcmiAgent  # noqa: E402
import vcmi_shot  # noqa: E402

FONT_CANDIDATES = [r'C:\Windows\Fonts\msyh.ttc', r'C:\Windows\Fonts\simhei.ttf']
VCMI_SETTINGS = Path.home() / 'Documents' / 'My Games' / 'vcmi' / 'config' / 'settings.json'
FFMPEG = shutil.which('ffmpeg') or r'C:\Users\Hans\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe'


def load_font(size: int):
    for f in FONT_CANDIDATES:
        if Path(f).exists():
            try:
                return ImageFont.truetype(f, size)
            except Exception:
                continue
    return ImageFont.load_default()


class FrameRecorder:
    """Saves annotated JPEG frames; encoding happens later (crash-safe)."""

    def __init__(self, frames_dir: Path, fps: float = 4.0):
        self.dir = frames_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.fps = fps
        self.caption = 'VCMI agent demo'
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.count = 0
        self.hwnd = None
        self.font = load_font(22)
        self.small = load_font(15)

    def set_caption(self, text: str) -> None:
        with self.lock:
            self.caption = text

    def start(self, hwnd: int) -> None:
        self.hwnd = hwnd
        threading.Thread(target=self._run, daemon=True).start()

    def _annotate(self, img: Image.Image, caption: str) -> Image.Image:
        img = img.convert('RGB')
        draw = ImageDraw.Draw(img, 'RGBA')
        w, h = img.size
        draw.rectangle([0, h - 62, w, h], fill=(0, 0, 0, 195))
        draw.text((14, h - 54), caption, font=self.font, fill=(255, 255, 120))
        draw.text((14, h - 27), 'VCMI agent bridge  |  window off-screen, muted, mouse untouched',
                  font=self.small, fill=(170, 215, 255))
        return img

    def _run(self) -> None:
        interval = 1.0 / self.fps
        while not self.stop_flag.is_set():
            t0 = time.time()
            try:
                img = vcmi_shot.grab(self.hwnd)
            except Exception:
                time.sleep(0.2)
                continue
            with self.lock:
                caption = self.caption
            arr = np.asarray(self._annotate(img, caption))
            frame = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            self.count += 1
            cv2.imwrite(str(self.dir / f'f_{self.count:06d}.jpg'),
                        frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            dt = time.time() - t0
            if dt < interval:
                time.sleep(interval - dt)

    def stop(self) -> None:
        self.stop_flag.set()
        time.sleep(0.4)


def encode(frames_dir: Path, out: Path, fps: float = 4.0) -> bool:
    frames = sorted(frames_dir.glob('f_*.jpg'))
    if not frames:
        print('[demo] no frames')
        return False
    cmd = [FFMPEG, '-y', '-framerate', str(fps), '-i', str(frames_dir / 'f_%06d.jpg'),
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '20', str(out)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print('[demo] ffmpeg failed:', r.stderr[-800:])
            return False
        print(f'[demo] video -> {out} ({len(frames)} frames, {len(frames) / fps:.0f}s)')
        return True
    except FileNotFoundError:
        print('[demo] ffmpeg not found')
        return False


def set_quick_combat(enabled: bool) -> None:
    import io
    data = json.load(io.open(VCMI_SETTINGS, encoding='utf-8-sig'))
    data.setdefault('adventure', {})['quickCombat'] = enabled
    io.open(VCMI_SETTINGS, 'w', encoding='utf-8').write(json.dumps(data, ensure_ascii=False, indent=4))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--map', default='Maps/Arrogance')
    ap.add_argument('--dir', default=str(ROOT / 'var' / 'agent_demo'))
    ap.add_argument('--encode-only', default=None, help='frames dir to encode only')
    args = ap.parse_args()

    out_dir = ROOT / 'var' / 'recordings'
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')

    if args.encode_only:
        d = Path(args.encode_only)
        return 0 if encode(d, out_dir / f'agent-demo-{stamp}.mp4') else 1

    frames_dir = out_dir / f'{stamp}-frames'
    states_dir = ROOT / 'doc' / 'findings' / 'demo-states'
    states_dir.mkdir(parents=True, exist_ok=True)

    was_quick = True
    try:
        was_quick = json.load(open(VCMI_SETTINGS, encoding='utf-8-sig')).get('adventure', {}).get('quickCombat', True)
    except Exception:
        pass
    set_quick_combat(False)          # manual battle, so the demo can show BattleAction control

    agent = VcmiAgent(args.dir, args.map)
    if not agent.start():
        print('client did not start')
        set_quick_combat(was_quick)
        return 1
    st = agent.wait_in_game(180)

    rec = FrameRecorder(frames_dir)
    rec.start(agent.hwnd)

    def cap(text: str) -> None:
        rec.set_caption(text)
        print(f'[demo] {text}', flush=True)

    def dump(name: str) -> None:
        try:
            data = agent.state(10)
        except Exception:
            return
        (states_dir / f'{stamp}-{name}.json').write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')

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

    def alive() -> bool:
        return agent.proc is not None and agent.proc.poll() is None

    def answer_queries(st_obj: dict | None = None) -> str:
        st_obj = st_obj or agent.state(10)
        q = st_obj.get('query')
        if not q:
            return ''
        choice = 0 if q.get('type') == 'level_up' else 1
        if q.get('type') == 'select' and q.get('options'):
            exp = [o for o in q['options'] if o.get('type') == 'EXPERIENCE']
            choice = exp[0]['index'] if exp else 1
        txt = str(q.get('text', '')).replace('\n', ' ')[:38]
        cap(f'功能 11: 查询应答（{q.get("type")}）{txt}')
        r = cmd(f'answer {choice}')
        print('[demo]  ', r, flush=True)
        return r

    def wait_hero(timeout: float = 25.0) -> dict:
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                return agent.state(3)
            except Exception:
                time.sleep(0.2)
        return {}

    def move_to(target, max_steps: int = 8) -> bool:
        """Walk to target with move_hero and only accept an *observed* state change."""
        for _ in range(max_steps):
            if not alive():
                return False
            st_now = wait_hero()
            hs = st_now.get('heroes') or []
            if not hs:
                return False
            cur = tuple(hs[0]['visitable'])
            dst = tuple(target)
            if cur[:2] == dst[:2]:
                return True
            if (st_now.get('battle') or {}).get('active'):
                cap('移动中触发战斗')
                return True
            res = agent.execute(f'move_hero 0 {dst[0]} {dst[1]} {dst[2]}',
                                verify=agent.verify_move(0, dst), verify_timeout=30,
                                result_timeout=40)
            st_after = wait_hero()
            new_pos = tuple((st_after.get('heroes') or [{}])[0].get('visitable', [-1, -1, 0]))
            if new_pos[:2] == cur[:2] and not (st_after.get('battle') or {}).get('active'):
                cap(f'移动未生效（{res.status}），不宣称前进')
                return False
            cap(f'移动: {res.status} — 英雄 {cur[:2]} -> {new_pos[:2]}')
            answer_queries()
        return False

    try:
        # ---- 1/2: environment + PlayerView ----
        cap('功能 1: 屏幕外窗口 + PrintWindow 抓帧（不抢焦点/鼠标，进程静音）')
        time.sleep(2)
        cap('功能 2: PlayerView — state.json 只含玩家可见信息（资源/英雄/城镇/可见对象）')
        st = agent.state(10)
        dump('state-day1')
        time.sleep(2)

        hero = st['heroes'][0]
        town = st['towns'][0]
        cap(f'局面: day {st["date"]["day"]} | 英雄 {hero["name"]}{hero["visitable"]} | 城镇 {town["name"]} | 金币 {st["resources"]["gold"]}')
        time.sleep(2)

        # ---- 3: build with names/costs ----
        opts = town.get('build_options', [])
        pick = next((o for o in opts if '大本营' in str(o.get('name', ''))), None) or \
               (min(opts, key=lambda o: o.get('cost', {}).get('gold', 0)) if opts else None)
        if pick:
            cap(f'功能 3: 建造（名字+造价来自引擎）— {pick.get("name")} {pick.get("cost")}')
            print('[demo]  ', cmd(f'build 0 {pick["id"]}'), flush=True)
            time.sleep(2)

        # ---- 4: recruit ----
        for dw in town.get('dwellings', [])[:3]:
            if not alive():
                break
            cap(f'功能 4: 招募 {dw["level"]} 级兵 x{min(dw["available"], 10)}')
            print('[demo]  ', cmd(f'recruit 0 {dw["level"]} {min(dw["available"], 10)}'), flush=True)
            time.sleep(1.2)

        # ---- 5: tavern second hero ----
        st = agent.state(10)
        avail = st['towns'][0].get('available_heroes', [])
        if avail:
            cap(f'功能 5: 酒馆第二英雄 — {avail[0].get("name")}')
            print('[demo]  ', cmd(f'recruit_hero 0 {avail[0]["index"]}'), flush=True)
            time.sleep(2)

        # ---- 6: transfer ----
        st = agent.state(10)
        used = {a['slot'] for a in st['heroes'][0]['army']}
        free = [s for s in range(7) if s not in used]
        for g in st['towns'][0].get('garrison', [])[:len(free)]:
            if not free or not alive():
                break
            slot = free.pop(0)
            cap(f'功能 6: 驻军→英雄 — {g["creature"]}x{g["count"]} → 槽 {slot}')
            print('[demo]  ', cmd(f'transfer town 0 {g["slot"]} hero 0 {slot}'), flush=True)
            time.sleep(1.2)

        # ---- 7: upgrade ----
        st = agent.state(10)
        if st['heroes'][0]['army']:
            slot = st['heroes'][0]['army'][0]['slot']
            creature = st['heroes'][0]['army'][0]['creature']
            cap(f'功能 7: 兵种升级（槽 {slot}，服务器校验）')
            print('[demo]  ', cmd(f'upgrade hero 0 {slot} {creature}'), flush=True)
            time.sleep(1.5)

        # ---- 8: market ----
        cap('功能 8: 市场交易 — 木材→金币 x3')
        print('[demo]  ', cmd('market_trade 0 0 0 6 3'), flush=True)
        time.sleep(1.5)

        # ---- 9: move to a mine, claim only what is verified ----
        st = agent.state(10)
        mines = [o for o in st.get('objects', []) if o.get('type') == 'mine']
        if mines:
            hero_pos = st['heroes'][0]['visitable']
            mines.sort(key=lambda o: abs(o['visitable'][0] - hero_pos[0]) + abs(o['visitable'][1] - hero_pos[1]))
            cap(f'功能 9: 走向资源点 → {mines[0]["name"]} {mines[0]["visitable"]}')
            reached = move_to(mines[0]['visitable'], max_steps=8)
            st_now = wait_hero()
            mine_now = next((o for o in (st_now.get('objects') or [])
                             if o.get('id') == mines[0].get('id')), {})
            my_player = st_now.get('player')
            captured = bool(reached and mine_now.get('owner') == my_player)
            if captured:
                cap(f'占领资源点: {mines[0]["name"]}（owner={my_player} 已验证）')
            else:
                cap(f'未能确认占领 {mines[0]["name"]}（owner={mine_now.get("owner")}, '
                    f'player={my_player}）— 不写成功字幕')
            time.sleep(2)

        # ---- 11: query answering via treasure chest ----
        st = agent.state(10)
        chests = [o for o in st.get('objects', []) if o.get('type') == 'treasureChest']
        if chests:
            hero_pos = st['heroes'][0]['visitable']
            chests.sort(key=lambda o: abs(o['visitable'][0] - hero_pos[0]) + abs(o['visitable'][1] - hero_pos[1]))
            cap(f'功能 11: 走向宝箱 {chests[0]["visitable"]}（会弹出金/经验选择）')
            move_to(chests[0]['visitable'], max_steps=10)
            time.sleep(1.5)
            answer_queries()
            time.sleep(2)

        # ---- 12: manual battle via BattleAction packets ----
        st = agent.state(10)
        creeps = [o for o in st.get('objects', []) if o.get('kind') == 'creature']
        if creeps:
            hero_pos = st['heroes'][0]['visitable']
            creeps.sort(key=lambda o: (o.get('count', 999),
                                       abs(o['visitable'][0] - hero_pos[0]) + abs(o['visitable'][1] - hero_pos[1])))
            target = creeps[0]
            cap(f'功能 12: 手动战斗 — 走向 {target["name"]} x{target["count"]}')
            move_to(target['visitable'], max_steps=10)
            for _ in range(40):
                if not alive():
                    break
                bst = agent.state(10)
                q = bst.get('query')
                if q:
                    answer_queries(bst)
                    continue
                b = bst.get('battle', {})
                if not b.get('active'):
                    break
                active = b.get('active_stack', -1)
                stacks = b.get('stacks', [])
                my_side = b.get('my_side', 0)
                mine = [x for x in stacks if x['id'] == active and x['side'] == my_side]
                enemies = [x for x in stacks if x['side'] != my_side]
                if not mine:
                    cap('功能 12: 手动战斗 — 等待敌方行动')
                    time.sleep(1.2)
                    continue
                if not enemies:
                    cmd('battle_result_ok')
                    time.sleep(1)
                    continue
                me = mine[0]
                enemies.sort(key=lambda e: abs(e['hex'][0] - me['hex'][0]) + abs(e['hex'][1] - me['hex'][1]))
                cap(f'功能 12: 手动战斗 — BattleAction 攻击 stack {enemies[0]["id"]} '
                    f'(我方 {me["count"]} vs 敌 {enemies[0]["count"]})')
                print('[demo]  ', cmd(f'battle_attack {enemies[0]["id"]}'), flush=True)
                time.sleep(1.4)
            cap('功能 13: 战斗结果窗口自动关闭（BattleResultWindow::bExitf）')
            cmd('battle_result_ok')
            time.sleep(2)

        dump('final-state')
        cap('演示结束 — 所有操作都是玩家同层接口，最终由服务器校验；无 GM / 无开雾 / 无存档修改')
        time.sleep(4)
    finally:
        rec.stop()
        ok = encode(frames_dir, out_dir / f'agent-demo-{stamp}.mp4')
        try:
            set_quick_combat(was_quick)
        except Exception:
            pass
        print(f'[demo] frames -> {frames_dir} ({rec.count})', flush=True)
        print(f'[demo] states -> {states_dir}', flush=True)
        if not ok:
            print('[demo] encode failed; frames are kept for retry', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
