# -*- coding: utf-8 -*-
"""Replicate, on VCMI, the checklist we had already reached on retail H3.

   1. read hero/town/resource state        -> PlayerView (engine, fog-checked)
   2. end turn + day advances              -> endTurn
   3. move hero                            -> moveHero via IGameActionCallback
   4. recruit troops                       -> recruitCreatures
   5. build a building                     -> buildBuilding
   6. enter a battle on the map            -> moveHero onto a monster
   7. manual battle: move + attack         -> BattleActionsController::onHexLeftClicked

Coordinate convention (learned the hard way):
  * everything the agent *sees* (hero/objects `visitable`) is visitable/map space
  * `move_hero` takes visitable coordinates; the bridge converts to anchor space
  * object *anchor* tiles are often blocked; always target `visitable`

Turn discipline: actions are only legal while it is our turn.  After endTurn the AI
players act (a few seconds), then a new day starts and it is our turn again.

Run:  python -X utf8 tools/vcmi_agent_progress.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

from vcmi_agent import VcmiAgent  # noqa: E402

MAP = 'Maps/Arrogance'
BRIDGE = ROOT / 'var' / 'agent_progress'


class Check:
    def __init__(self, name):
        self.name = name
        self.ok = False
        self.detail = ''

    def __repr__(self):
        return f'[{"PASS" if self.ok else "FAIL"}] {self.name}: {self.detail}'


class Bot:
    def __init__(self, agent: VcmiAgent):
        self.agent = agent

    def cmd(self, line, timeout=10.0):
        before = len(self.agent.results())
        self.agent.send(line)
        t0 = time.time()
        while time.time() - t0 < timeout:
            lines = self.agent.results()
            if len(lines) > before:
                return lines[before]
            time.sleep(0.05)
        return '<<no result>>'

    def state(self, timeout=5.0):
        return self.agent.state(timeout)

    def wait(self, pred, timeout=15.0):
        t0 = time.time()
        last = {}
        while time.time() - t0 < timeout:
            try:
                last = self.state(3)
            except Exception:
                continue
            if pred(last):
                return last
            time.sleep(0.25)
        return last

    def move_to(self, idx, target, timeout=15.0):
        self.cmd(f'move_hero {idx} {target[0]} {target[1]} {target[2]}')
        st = self.wait(lambda s: s['heroes'] and tuple(s['heroes'][0]['visitable']) == tuple(target),
                       timeout=timeout)
        return bool(st['heroes']) and tuple(st['heroes'][0]['visitable']) == tuple(target), st


def main() -> int:
    agent = VcmiAgent(BRIDGE, MAP)
    bot = Bot(agent)
    checks = []
    try:
        if not agent.start():
            print('client did not start')
            return 1
        st = agent.wait_in_game(180)
        print('[progress] game started: date', st['date'], 'gold', st['resources']['gold'])

        c = Check('1 read state (heroes/towns/resources)')
        c.ok = bool(st['heroes']) and bool(st['towns']) and 'gold' in st['resources']
        c.detail = (f"hero={st['heroes'][0]['name']} tile={st['heroes'][0]['visitable']} "
                    f"town={st['towns'][0]['name']} gold={st['resources']['gold']}")
        checks.append(c)

        # dismiss any intro/popup window without touching the map interface
        for _ in range(2):
            bot.cmd('close_top', 3)
            time.sleep(0.3)

        # ---- 2. end turn: day advances -------------------------------------
        day0 = st['date']['day']
        bot.cmd('end_turn')
        st2 = bot.wait(lambda s: s['date']['day'] > day0, timeout=120)
        c = Check('2 end turn (day advances)')
        c.ok = st2['date']['day'] > day0
        c.detail = f'day {day0} -> {st2["date"]["day"]}'
        checks.append(c)

        # ---- 3. move hero --------------------------------------------------
        hero = st2['heroes'][0]
        vx, vy, vz = hero['visitable']
        target = None
        for dx, dy in [(0, 1), (0, -1), (1, 0), (-1, 0), (1, 1), (-1, -1)]:
            cand = (vx + dx, vy + dy, vz)
            ok, st3 = bot.move_to(0, cand)
            print(f'[progress] move -> {cand}: {ok}')
            if ok:
                target = cand
                break
        c = Check('3 move hero')
        c.ok = target is not None
        c.detail = (f'{hero["visitable"]} -> {st3["heroes"][0]["visitable"]} '
                    f'movement {hero["movement"]} -> {st3["heroes"][0]["movement"]}') if target else 'no move accepted'
        checks.append(c)

        # ---- 4. recruit ----------------------------------------------------
        town = st3['towns'][0]
        level = town['dwellings'][0]['level'] if town.get('dwellings') else 1
        gold0 = st3['resources']['gold']
        r = bot.cmd(f'recruit 0 {level} 1')
        print('[progress] recruit ->', r)
        st4 = bot.wait(lambda s: s['resources']['gold'] < gold0, timeout=25)
        c = Check('4 recruit troops')
        c.ok = st4['resources']['gold'] < gold0
        c.detail = f'result={r} gold {gold0} -> {st4["resources"]["gold"]}'
        checks.append(c)

        # ---- 5. build ------------------------------------------------------
        buildable = st4['towns'][0].get('buildable', [])
        c = Check('5 build a building')
        if buildable:
            bid = buildable[0]
            r = bot.cmd(f'build 0 {bid}')
            print('[progress] build ->', r)
            st5 = bot.wait(lambda s: bid in s['towns'][0]['built'], timeout=25)
            c.ok = bid in st5['towns'][0]['built']
            c.detail = f'result={r} building {bid} built={c.ok}'
        else:
            st5 = st4
            c.detail = 'nothing buildable'
        checks.append(c)

        # ---- 8. army stack swap (drag-and-drop equivalent) ------------------
        # done before the battle: afterwards the army may be down to a single stack.
        # Prefers the hero's army, falls back to the town garrison.
        c = Check('8 army stack swap (ArrangeStacks)')
        st_swap = bot.state(5)
        owner, idx, slots = None, 0, []
        if st_swap.get('heroes') and len(st_swap['heroes'][0]['army']) >= 2:
            owner, idx, slots = 'hero', 0, st_swap['heroes'][0]['army']
        elif st_swap.get('towns') and len(st_swap['towns'][0].get('garrison', [])) >= 2:
            owner, idx, slots = 'town', 0, st_swap['towns'][0]['garrison']
        if owner:
            a, b = slots[0], slots[1]
            r = bot.cmd(f'swap_stacks {owner} {idx} {a["slot"]} {b["slot"]}')
            print('[progress] swap_stacks ->', r)
            if owner == 'hero':
                st_swap2 = bot.wait(lambda s: s['heroes'] and s['heroes'][0]['army'] and
                                    s['heroes'][0]['army'][0]['creature'] == b['creature'], timeout=20)
                new_first = st_swap2['heroes'][0]['army'][0]['creature'] if st_swap2.get('heroes') else None
            else:
                st_swap2 = bot.wait(lambda s: s['towns'] and s['towns'][0].get('garrison') and
                                    s['towns'][0]['garrison'][0]['creature'] == b['creature'], timeout=20)
                new_first = st_swap2['towns'][0]['garrison'][0]['creature'] if st_swap2.get('towns') else None
            c.ok = new_first == b['creature']
            c.detail = f'{owner} slots {a["slot"]}<->{b["slot"]}: first creature {a["creature"]} -> {new_first}'
        else:
            c.detail = 'neither hero army nor garrison has 2+ stacks'
        checks.append(c)

        # ---- 6. enter battle -----------------------------------------------
        st6 = st5
        creatures = [o for o in st6.get('objects', []) if o.get('kind') == 'creature']
        c = Check('6 enter battle')
        battle_state = {}
        if creatures:
            hero = st6['heroes'][0]
            hx, hy, _ = hero['visitable']
            creatures.sort(key=lambda o: abs(o['visitable'][0] - hx) + abs(o['visitable'][1] - hy))
            st7 = st6
            for tgt in creatures[:6]:
                print('[progress] trying creature', tgt['visitable'], 'count', tgt['count'])
                started = False
                for attempt in range(4):
                    r = bot.cmd(f'move_hero 0 {tgt["visitable"][0]} {tgt["visitable"][1]} {tgt["visitable"][2]}')
                    print(f'[progress]   approach #{attempt + 1} ->', r)
                    if r.startswith('ERR'):
                        break  # unreachable, try the next creature
                    st7 = bot.wait(lambda s: s.get('battle', {}).get('active'), timeout=20)
                    if st7.get('battle', {}).get('active'):
                        started = True
                        break
                    cur = st7['heroes'][0]['visitable'] if st7.get('heroes') else None
                    print('[progress]   hero now at', cur)
                if started:
                    break
            battle_state = st7.get('battle', {})
            c.ok = bool(battle_state.get('active'))
            c.detail = f'battle active={c.ok} stacks={len(battle_state.get("stacks", []))}'
        else:
            st7 = st6
            c.detail = 'no creature near the hero'
        checks.append(c)

        # ---- 7. manual battle ----------------------------------------------
        # Uses BattleAction (makeMove/makeMeleeAttack/makeDefend/makeWait) sent through
        # IBattleCallback::battleMakeUnitAction - the exact packet the UI produces.
        c = Check('7 manual battle (move + attack until resolution)')
        if battle_state.get('active'):
            acts = 0
            result = {}
            last_sig = None
            stall = 0
            for _ in range(80):
                b = bot.wait(lambda s: (not s.get('battle', {}).get('active')) or
                             any(x['id'] == s['battle'].get('active_stack', -1) and x['side'] == 0
                                 for x in s['battle'].get('stacks', [])), timeout=60)
                bb = b.get('battle', {})
                if not bb.get('active'):
                    result = {'ended': True}
                    break
                stacks = bb.get('stacks', [])
                active = bb.get('active_stack')
                mine = [x for x in stacks if x['id'] == active and x['side'] == 0]
                enemies = [x for x in stacks if x['side'] == 1]
                if not mine or not enemies:
                    # battle is decided; the victory/defeat window keeps battleInt alive
                    bot.cmd('battle_result_ok', 5)
                    time.sleep(1.0)
                    if not bot.state(5).get('battle', {}).get('active'):
                        result = {'ended': True, 'result_window_ok': True}
                        break
                    time.sleep(0.5)
                    continue
                me = mine[0]
                sig = (active, tuple(me['hex']), me['count'], sum(e['count'] for e in enemies), len(stacks))
                if sig == last_sig:
                    stall += 1
                else:
                    stall = 0
                last_sig = sig
                if stall >= 2:
                    # cannot make progress (e.g. enemy out of reach) -> end this unit's turn
                    r = bot.cmd('battle_defend', 5)
                    acts += 1
                    result = {'last': r, 'stacks': len(stacks)}
                    stall = 0
                    time.sleep(1.2)
                    continue
                enemies.sort(key=lambda e: abs(e['hex'][0] - me['hex'][0]) + abs(e['hex'][1] - me['hex'][1]))
                r = bot.cmd(f'battle_attack {enemies[0]["id"]}')
                acts += 1
                result = {'last': r, 'stacks': len(stacks)}
                time.sleep(1.6)
            if not result.get('ended'):
                bot.cmd('battle_result_ok', 5)
                time.sleep(1.0)
                if not bot.state(5).get('battle', {}).get('active'):
                    result['ended'] = True
            c.ok = acts > 0 and bool(result.get('ended'))
            c.detail = f'{acts} battle actions, result={result}'
        else:
            c.detail = 'battle never started'
        checks.append(c)

        agent.shot(ROOT / 'var' / 'shots' / 'vcmi_progress.png')
    finally:
        print('\n===== checklist =====')
        for c in checks:
            print(c)
        agent.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
