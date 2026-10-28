# -*- coding: utf-8 -*-
"""Scenario runner: launch -> new game (Arrogance) -> find a neutral stack -> attack.

Purpose: getting into a battle is the expensive part of every combat session; this
script does the menu walk and a hover-scan for monsters, then attacks the first
stack it finds and reports whether the combat screen came up.

Usage:
    python -X utf8 tools/run_battle.py            # full run (launches the game)
    python -X utf8 tools/run_battle.py --running  # game already launched
    python -X utf8 tools/run_battle.py --find-only
"""
from __future__ import annotations
import sys
import time

sys.path.insert(0, '.')
from agent.h3 import lab

COMBAT_MARKERS = ('自动战斗', '等待', '防御', '撤退')
MONSTER_WORDS = ('盗贼', '野猪', '农民', '半人马', '狼', '妖精', '精灵', '矮人',
                 '游魂', '骷髅', '僵尸', '玛各', '火精灵', '气元素', '土元素')


def is_combat() -> bool:
    txt = lab.screen_text()
    return sum(1 for k in COMBAT_MARKERS if k in txt) >= 2


def boot(running: bool):
    if not running:
        from agent.h3.launch import main
        main()


def new_game():
    lab.click_until(648, 70, '单场', tries=4)          # 新游戏
    lab.click_until(648, 60, '开始', tries=4)          # 单场场景 (scenario screen)
    lab.click_until(452, 540, '月', tries=5, wait=2.5)  # 开始 -> map
    lab.click(400, 430, wait=1.5)                      # ✓ dismiss intro narration
    lab.click(400, 300, wait=0.6)


def find_monsters(step=40):
    found = []
    for y in range(120, 480, step):
        for x in range(120, 540, step):
            lab.hover(x, y, 0.30)
            t = lab.status_bar_text()
            if t and any(w in t for w in MONSTER_WORDS):
                found.append((x, y, t))
    return found


def attack(x, y, tries=3):
    for i in range(tries):
        lab.hover(x, y, 0.5)
        lab.click(x, y, wait=1.5)
        if is_combat():
            return i + 1
    return -1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    boot('--running' in sys.argv)
    if '--find-only' not in sys.argv:
        new_game()
        print('screen:', lab.screen_text()[:100])
    monsters = find_monsters()
    for x, y, t in monsters[:10]:
        print('monster', (x, y), t)
    if not monsters:
        print('no monster found in the scanned viewport'); raise SystemExit(0)
    if '--find-only' in sys.argv:
        raise SystemExit(0)
    x, y, name = monsters[0]
    print(f'attacking {name} at {(x, y)}')
    n = attack(x, y)
    print('combat reached' if n > 0 else 'attack did not start a combat', f'(clicks={n})')
    lab.snap('battle_start')
