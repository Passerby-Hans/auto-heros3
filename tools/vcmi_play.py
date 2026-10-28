# -*- coding: utf-8 -*-
"""Autonomous play session for the VCMI agent bridge.

Purpose: (a) exercise every bridge feature end-to-end on a real game, (b) learn/record how
the game reacts (this is the "play a few games" part of the task), (c) be a starting point
for the actual decision layer.

What the bot does each day:
  * answers all pending server queries (level-up / chest / teleport / select)
  * builds the cheapest available building when we can afford it
  * recruits the cheapest affordable creature stack into the town garrison
  * walks the hero to the nearest visible object (resource/chest/monster/mine...)
  * fights any battle that starts, using BattleAction moves/attacks
  * ends the turn, waits for the new day

Usage:
    python -X utf8 tools/vcmi_play.py --days 5 --map Maps/Arrogance
    python -X utf8 tools/vcmi_play.py --days 5 --keep     # leave the client running
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


class Player:
    def __init__(self, agent: VcmiAgent, verbose: bool = True):
        self.a = agent
        self.verbose = verbose
        self.log_lines: list[str] = []

    # ---------------- io ----------------
    def say(self, msg: str) -> None:
        self.log_lines.append(msg)
        if self.verbose:
            print(f'[play] {msg}')

    def cmd(self, line: str, timeout: float = 10.0) -> str:
        before = len(self.a.results())
        self.a.send(line)
        t0 = time.time()
        while time.time() - t0 < timeout:
            lines = self.a.results()
            new = lines[before:]
            # queries/info are logged too; they are not the command's own result
            for ln in new:
                if ln.startswith('OK') or ln.startswith('ERR'):
                    return ln
            time.sleep(0.05)
        return '<<no result>>'

    def state(self, timeout: float = 5.0) -> dict:
        return self.a.state(timeout)

    def wait(self, pred, timeout: float = 20.0) -> dict:
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

    # ---------------- query answering ----------------
    def answer_queries(self, st: dict | None = None) -> bool:
        st = st or self.state()
        q = st.get('query')
        if not q:
            return False
        qtype = q.get('type')
        opts = q.get('options', [])
        choice = 1
        if qtype == 'level_up':
            # policy v0: take the first offered secondary skill; expose options in the log
            choice = 0
            self.say(f'level_up: primary={q.get("primary")} choose secondary option {choice} '
                     f'of {[o.get("sub") for o in opts]}')
        elif qtype == 'select':
            # policy v0: prefer experience over gold, then first option
            exp = [o for o in opts if o.get('type') == 'EXPERIENCE']
            choice = exp[0]['index'] if exp else 1
            self.say(f'select: "{q.get("text", "")[:60]}" -> option {choice} ({opts and [o.get("type") for o in opts]})')
        elif qtype == 'yes_no':
            choice = 1
            self.say(f'yes_no: "{q.get("text", "")[:60]}" -> yes')
        elif qtype == 'teleport':
            choice = 1
            self.say(f'teleport: {len(opts)} exits -> first')
        r = self.cmd(f'answer {choice}')
        self.say(f'  answered -> {r}')
        return r.startswith('OK')


    def reinforce_hero(self, st: dict) -> None:
        """Move recruited garrison stacks onto the hero (needs the hero in the town)."""
        if not st.get('heroes') or not st.get('towns'):
            return
        hero = st['heroes'][0]
        used = {a['slot'] for a in hero['army']}
        free = [s for s in range(7) if s not in used]
        for town in st['towns']:
            garrison = town.get('garrison', [])
            for g in garrison:
                if not free:
                    return
                slot = free.pop(0)
                r = self.cmd(f'transfer town {town["index"]} {g["slot"]} hero 0 {slot}')
                self.say(f'town {town["name"]}: garrison slot {g["slot"]} ({g["count"]}x{g["creature"]}) '
                         f'-> hero slot {slot}: {r}')
                if r.startswith('ERR'):
                    return

    # ---------------- economy ----------------
    def town_actions(self, st: dict) -> None:
        for town in st.get('towns', []):
            idx = town['index']
            buildable = town.get('buildable', [])
            gold = st['resources']['gold']
            if buildable:
                bid = buildable[0]
                r = self.cmd(f'build {idx} {bid}')
                self.say(f'town {town["name"]}: build {bid} -> {r}')
            for dw in town.get('dwellings', []):
                if dw['available'] > 0:
                    amount = min(dw['available'], max(1, gold // 200)) if dw['level'] <= 2 else 1
                    r = self.cmd(f'recruit {idx} {dw["level"]} {amount}')
                    self.say(f'town {town["name"]}: recruit L{dw["level"]} x{amount} -> {r}')
                    break

    # ---------------- battle ----------------
    def fight(self, budget_s: float = 420.0) -> bool:
        """Play our stacks; patiently wait while the enemy AI moves."""
        acts = 0
        last_sig = None
        stall = 0
        t0 = time.time()
        while time.time() - t0 < budget_s:
            st = self.state()
            if st.get('query'):
                self.answer_queries(st)
                continue
            b = st.get('battle', {})
            if not b.get('active'):
                return acts > 0 or True
            active = b.get('active_stack', -1)
            stacks = b.get('stacks', [])
            if active < 0:                      # animation / deployment
                time.sleep(0.5)
                continue
            my_side = b.get('my_side', 0)
            mine = [x for x in stacks if x['id'] == active and x['side'] == my_side]
            enemies = [x for x in stacks if x['side'] != my_side]
            if not enemies:                      # battle decided, result window pending
                self.cmd('battle_result_ok', 5)
                time.sleep(0.8)
                continue
            if not mine:                         # enemy AI is acting - just wait
                time.sleep(1.5)
                continue
            me = mine[0]
            sig = (active, tuple(me['hex']), me['count'], sum(e['count'] for e in enemies))
            stall = stall + 1 if sig == last_sig else 0
            last_sig = sig
            if stall >= 3:
                self.cmd('battle_defend', 5)
                stall = 0
                acts += 1
                time.sleep(1.2)
                continue
            enemies.sort(key=lambda e: abs(e['hex'][0] - me['hex'][0]) + abs(e['hex'][1] - me['hex'][1]))
            r = self.cmd(f'battle_attack {enemies[0]["id"]}')
            acts += 1
            if r.startswith('ERR'):
                self.cmd('battle_defend', 5)
            time.sleep(1.2)
        return acts > 0

    # ---------------- adventure ----------------
    def army_size(self, st: dict) -> int:
        hero = st['heroes'][0] if st.get('heroes') else None
        if not hero:
            return 0
        return sum(a['count'] for a in hero['army'])

    def walk_to_nearest_object(self, st: dict) -> str:
        hero = st['heroes'][0]
        hx, hy, hz = hero['visitable']
        objs = [o for o in st.get('objects', []) if tuple(o['visitable']) != (hx, hy, hz)]
        if not objs:
            return 'nothing visible to visit'
        # heuristics v0: prefer non-creature objects; only fight creatures that look weak
        ours = self.army_size(st)
        def score(o):
            d = abs(o['visitable'][0] - hx) + abs(o['visitable'][1] - hy)
            if o['kind'] == 'creature':
                cnt = o.get('count', 999)
                if cnt * 4 > ours + 2:
                    return None          # too strong for our army, skip
                return d + 20                # deprioritise fights
            return d
        scored = [(score(o), o) for o in objs]
        scored = [(sc, o) for sc, o in scored if sc is not None]
        scored.sort(key=lambda t: t[0])
        for _, o in scored[:8]:
            tx, ty, tz = o['visitable']
            # walk one tile at a time: the pathfinder only gives this-turn paths, so pick the
            # neighbour of the hero that is closest to the target and reachable
            for attempt in range(8):
                st_now = self.state()
                if not st_now.get('heroes'):
                    break
                hx, hy, hz = st_now['heroes'][0]['visitable']
                if (hx, hy, hz) == (tx, ty, tz):
                    return f'reached {o["visitable"]}'
                neigh = [(hx + dx, hy + dy, hz)
                         for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                         if not (dx == 0 and dy == 0)]
                neigh.sort(key=lambda p: abs(p[0] - tx) + abs(p[1] - ty))
                moved = False
                for cand in neigh:
                    r = self.cmd(f'move_hero 0 {cand[0]} {cand[1]} {cand[2]}', timeout=6)
                    if r.startswith('ERR'):
                        continue
                    # a join-army / object question can freeze the hero until answered
                    if self.state().get('query'):
                        self.answer_queries()
                    st2 = self.wait(lambda s: s.get('heroes') and
                                    tuple(s['heroes'][0]['visitable']) != (hx, hy, hz), timeout=8)
                    after = tuple(st2['heroes'][0]['visitable']) if st2.get('heroes') else None
                    self.say(f'walk toward {o["visitable"]} step{attempt + 1}: {r} '
                             f'{(hx, hy, hz)}->{after}')
                    if st2.get('battle', {}).get('active'):
                        self.fight()
                        return f'fought near {o["visitable"]}'
                    if after != (hx, hy, hz):
                        moved = True
                        break
                if not moved:
                    break
        return 'no reachable object'

    # ---------------- turn loop ----------------
    def play_day(self) -> str:
        st = self.wait(lambda s: s.get('in_game') and s.get('date'), timeout=120)
        if not st.get('in_game'):
            return 'not in game (timeout)'
        self.answer_queries(st)
        st = self.state()
        if not st.get('in_game'):
            return 'not in game'
        if not st.get('heroes'):
            if not st.get('towns'):
                return 'GAME OVER: DEFEAT (no heroes, no towns)'
            return 'no heroes left (cannot continue: no tavern command yet)'
        day = st['date']['day']
        self.say(f'--- day {day} (week {st["date"]["week"]}) gold={st["resources"]["gold"]} '
                 f'hero={st["heroes"][0]["name"]} at {st["heroes"][0]["visitable"]} mov={st["heroes"][0]["movement"]}')
        self.town_actions(st)
        self.reinforce_hero(self.state())
        self.walk_to_nearest_object(self.state())
        self.answer_queries()
        for _ in range(3):
            if not self.state().get('battle', {}).get('active'):
                break
            self.say('battle still active -> resolving first')
            self.fight(budget_s=300.0)
            time.sleep(1.0)
        r = self.cmd('end_turn')
        # while waiting for the new day, keep handling incoming battles and blocking queries:
        # a defensive battle would otherwise block the day forever (R06).
        st2 = self.state()
        deadline = time.time() + 240
        while time.time() < deadline:
            st2 = self.state()
            if st2.get('date', {}).get('day', -1) > day:
                break
            if st2.get('query'):
                self.answer_queries(st2)
                continue
            if (st2.get('battle') or {}).get('active'):
                self.fight(budget_s=300)
                continue
            time.sleep(0.5)
        if st2.get('date', {}).get('day', day) <= day:
            # the turn did not advance: the server is waiting on something we missed
            self.say(f'STALL detected (end_turn -> {r}); trying recovery')
            for _ in range(3):
                self.answer_queries()
                self.cmd('battle_result_ok', 4)
                self.cmd('close_top', 4)
                time.sleep(1.0)
                st2 = self.state()
                if st2.get('date', {}).get('day', day) > day:
                    break
            if st2.get('date', {}).get('day', day) <= day:
                self.say('STALL not recovered')
                return f'STALLED at day {day}'
        self.say(f'end_turn -> {r}; day now {st2["date"]["day"]}')
        self.answer_queries(st2)
        return f'day {day} -> {st2["date"]["day"]}'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=5)
    ap.add_argument('--map', default='Maps/Arrogance')
    ap.add_argument('--dir', default=str(ROOT / 'var' / 'agent_play'))
    ap.add_argument('--keep', action='store_true')
    args = ap.parse_args()

    agent = VcmiAgent(args.dir, args.map)
    player = Player(agent)
    try:
        if not agent.start():
            print('client did not start')
            return 1
        st = agent.wait_in_game(180)
        player.say(f'game started: {json.dumps(st["date"])} gold={st["resources"]["gold"]}')
        for _ in range(args.days):
            result = player.play_day()
            player.say(f'day result: {result}')
            if 'GAME OVER' in result or 'cannot continue' in result:
                player.say(f'session ended: {result}')
                break
            if not player.wait(lambda s: s.get('in_game'), timeout=15).get('in_game'):
                player.say('game ended (client reports no game)')
                break
        agent.shot(ROOT / 'var' / 'shots' / 'vcmi_play_final.png')
        out = ROOT / 'var' / 'logs' / 'vcmi-play.log'
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text('\n'.join(player.log_lines), encoding='utf-8')
        final = {}
        try:
            final = player.state(5)
        except Exception:
            pass
        summary = {
            'map': args.map,
            'days_played': final.get('date', {}).get('day'),
            'final_date': final.get('date'),
            'gold': final.get('resources', {}).get('gold'),
            'resources': final.get('resources'),
            'heroes': final.get('heroes'),
            'towns': final.get('towns'),
            'in_game': final.get('in_game'),
            'result_line': player.log_lines[-1] if player.log_lines else None,
            'log': str(out),
        }
        sess = ROOT / 'var' / 'logs' / 'vcmi-play-result.json'
        sess.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'[play] result -> {sess}')
    finally:
        if not args.keep:
            agent.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
