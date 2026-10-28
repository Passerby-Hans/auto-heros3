# -*- coding: utf-8 -*-
"""Full-game driver: play a normal map until the game reports a win or a loss.

This is the execution half of the "LLM agent plays H3" loop; the policy here is a scripted
baseline (build economy -> recruit -> ferry troops -> capture mines -> clear weak stacks ->
find and take enemy towns).  Every action goes through the D-007 trusted protocol:
seq-matched replies, state post-conditions, unknown results reconciled once and then marked
failed (never blindly retried), off-screen window, owned-process cleanup.

Output: var/reports/full-game-<stamp>.json / .md, screenshots in var/shots/.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

from vcmi_agent import StaleStateError, VcmiAgent  # noqa: E402
from vcmi_short_game import StopRun, answer_query, fight_battle, pick_recruit, summarize  # noqa: E402

REPORTS = ROOT / 'var' / 'reports'
SHOTS = ROOT / 'var' / 'shots'

BUILD_PRIORITY = [
    ('议会', 10), ('市政厅', 9), ('物资仓库', 8), ('大本营', 8), ('市场', 7),
    ('高级', 9), ('升级', 8), ('堡垒', 7), ('要塞', 7),
    ('兵营', 6), ('训练', 6), ('塔', 6), ('营', 6), ('巢', 6), ('穴', 6), ('殿', 6),
    ('魔法行会', 4), ('铁匠', 4), ('城堡', 5), ('射手', 5), ('军械', 4),
]


def state(agent: VcmiAgent) -> dict:
    return agent.state(timeout=15)


def get_town(st):
    towns = st.get('towns') or []
    return towns[0] if towns else None


def army_total(hero) -> int:
    if not hero:
        return 0
    return sum(a['count'] for a in (hero.get('army') or []))


def main_hero(st):
    heroes = st.get('heroes') or []
    return heroes[0] if heroes else None


# Combat is NOT decided by fixed thresholds in the formal stage: when to fight is an LLM
# decision.  The driver only follows orders written by the reasoning layer into
# <dir>/orders.json, e.g. {"engage": {"creature": true, "hero": true, "town": true},
# "attack_ids": [12, 34], "avoid_ids": [7]}.  Without orders nothing is attacked.
ORDERS_FILE = 'orders.json'


def load_orders(agent):
    import json
    path = agent.dir / ORDERS_FILE
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = {}
    engage = data.get('engage') or {}
    return {'creature': bool(engage.get('creature', False)),
            'hero': bool(engage.get('hero', False)),
            'town': bool(engage.get('town', False)),
            'muster': bool(data.get('muster', False)),
            'attack_ids': set(data.get('attack_ids') or []),
            'avoid_ids': set(data.get('avoid_ids') or [])}


def build_score(option: dict) -> int:
    name = str(option.get('name') or '')
    best = 3
    for key, score in BUILD_PRIORITY:
        if key in name and score > best:
            best = score
    return best


def pick_build(st, failed):
    town = get_town(st)
    if not town:
        return None
    gold = st['resources']['gold']
    reserve = 800
    options = []
    for o in town.get('build_options') or []:
        if o.get('id') in failed:
            continue
        cost = o.get('cost') or {}
        if cost.get('gold', 0) + reserve > gold:
            continue
        if any(st['resources'].get(k, 0) < v for k, v in cost.items()):
            continue
        options.append(o)
    if not options:
        return None
    options.sort(key=lambda o: (-build_score(o), o.get('cost', {}).get('gold', 0), o['id']))
    return options[0]


def recruit_all(agent: VcmiAgent, st, log, failures) -> bool:
    town = get_town(st)
    if not town:
        return False
    did = False
    gold = st['resources']['gold']
    for dw in town.get('dwellings') or []:
        level, available = dw.get('level'), dw.get('available', 0)
        if available <= 0 or gold <= 300:
            continue
        res = agent.execute(f'recruit 0 {level} {available}',
                            verify=agent.verify_recruit(0, None, 1), verify_timeout=20)
        log.append({'kind': 'recruit', 'level': level, 'amount': available, 'result': str(res)})
        st = state(agent)
        gold = st['resources']['gold']
        if res.ok:
            did = True
        elif res.status == 'UNKNOWN':
            st2 = state(agent)
            town2 = get_town(st2)
            still = next((d for d in (town2 or {}).get('dwellings', [])
                          if d.get('level') == level), {})
            if still.get('available', 0) < available:
                did = True
            else:
                failures.setdefault('recruit', set()).add(level)
    return did


def town_threat(st, town, radius: int = 14) -> bool:
    """An enemy hero close to our town means: stop expanding, go defend the walls."""
    try:
        gate = town_gate(st, town)
    except Exception:
        gate = tuple(town.get('pos') or [])
    for o in st.get('objects') or []:
        if o.get('kind') != 'hero':
            continue
        owner = o.get('owner')
        if owner is None or owner == st.get('player'):
            continue
        v = o.get('visitable') or [0, 0]
        if abs(v[0] - gate[0]) + abs(v[1] - gate[1]) <= radius:
            return True
    return False


def town_gate(st, town):
    """Visitable tile of our town (the object's visitable position, not its anchor)."""
    tpos = tuple(town.get('pos') or [])
    for o in st.get('objects') or []:
        if o.get('kind') == 'town' and tuple(o.get('pos') or []) == tpos:
            return tuple(o['visitable'])
    return tuple(town.get('pos') or [])


def transfer_garrison_to_hero(agent: VcmiAgent, st, log, keep_min: int = 40) -> bool:
    town = get_town(st)
    heroes = st.get('heroes') or []
    if not town or not heroes:
        return False
    main = heroes[0]
    gate = town_gate(st, town)
    if tuple(main['visitable'][:2]) != gate[:2]:
        return False
    garrison = town.get('garrison') or []
    if not garrison:
        return False
    used = {a['slot'] for a in main.get('army') or []}
    free = [s for s in range(7) if s not in used]
    did = False
    # first merge garrison stacks into matching hero stacks (no free slot needed)
    hero_by_creature = {a['creature']: a['slot'] for a in main.get('army') or []}
    for g in list(garrison):
        if g['creature'] in hero_by_creature:
            res = agent.execute(f"merge town 0 {g['slot']} hero 0 {hero_by_creature[g['creature']]}",
                                verify=agent.verify_hero_army_gain(0, 1), verify_timeout=15)
            log.append({'kind': 'merge', 'creature': g['creature'], 'result': str(res)})
            if res.ok:
                did = True
    st = state(agent)
    main = (st.get('heroes') or [{}])[0]
    used = {a['slot'] for a in main.get('army') or []}
    free = [s for s in range(7) if s not in used]
    garrison_left = sum(g['count'] for g in (get_town(st).get('garrison') or []))
    for g in list(get_town(st).get('garrison') or []):
        if not free:
            break
        if garrison_left - g['count'] < keep_min:
            break                      # BASIC DEFENCE: keep a real garrison at home
        slot = free.pop(0)
        garrison_left -= g['count']
        res = agent.execute(f'transfer town 0 {g["slot"]} hero 0 {slot}',
                            verify=agent.verify_hero_army_gain(0, 1), verify_timeout=15)
        log.append({'kind': 'transfer', 'garrison_slot': g['slot'], 'hero_slot': slot,
                    'result': str(res)})
        st = state(agent)
        if res.ok:
            did = True
        else:
            break
    return did


def recruit_replacement_hero(agent, st, log) -> bool:
    """Hero died: hire a new one from the town tavern (if a tavern/hero slot is available)."""
    town = get_town(st)
    if not town:
        return False
    avail = town.get('available_heroes') or []
    for a in avail:
        res = agent.execute(f'recruit_hero 0 {a["index"]}', verify=None, result_timeout=20)
        log.append({'kind': 'recruit_hero_recovery', 'name': a.get('name'),
                    'result': str(res)})
        if res.status == 'REJECTED':
            continue
        time.sleep(1.5)
        st2 = state(agent)
        if st2.get('heroes'):
            transfer_garrison_to_hero(agent, st2, log)
            return True
    log.append({'kind': 'recruit_hero_recovery', 'result': 'no hero hired'})
    return False


def maybe_hire_scout(agent, st, log) -> bool:
    """Second hero as a long-range scout so the fighter can stay near home."""
    heroes = st.get('heroes') or []
    town = get_town(st)
    if not town or len(heroes) >= 2:
        return False
    if st['resources']['gold'] < 6000:
        return False
    avail = town.get('available_heroes') or []
    for a in avail:
        res = agent.execute(f'recruit_hero 0 {a["index"]}', verify=None, result_timeout=20)
        log.append({'kind': 'hire_scout', 'name': a.get('name'), 'result': str(res)})
        if res.status == 'REJECTED':
            continue
        time.sleep(1.2)
        return bool(state(agent).get('heroes') and len(state(agent)['heroes']) >= 2)
    return False


def move_attempt(agent, hero_idx, st, tx, ty, log, kind='move'):
    """Move one hero to (tx,ty) and return COMPLETED/REJECTED/UNKNOWN with state evidence."""
    res = agent.execute(f'move_hero {hero_idx} {tx} {ty} 0',
                        verify=agent.verify_move(hero_idx, (tx, ty)), verify_timeout=35,
                        result_timeout=45)
    log.append({'kind': kind, 'hero': hero_idx, 'dst': [tx, ty], 'result': str(res)})
    return res


def go_to_town(agent, st, log, town) -> bool:
    """Approach the town gate.  If the gate tile is occupied by another hero, use a neighbour.

    A move that returns OK but leaves the hero in place is *not* a fatal unknown: movement has
    no side effects, so we simply try another tile and carry on (D-007 only forbids blind
    retries of state-changing actions such as build/recruit).
    """
    heroes = st.get('heroes') or []
    if not heroes:
        return False
    h = heroes[0]
    gate = town_gate(st, town)
    occupied = {tuple(other['visitable'][:2]) for i, other in enumerate(heroes) if i > 0}
    cur = tuple(h['visitable'][:2])
    targets = []
    if gate[:2] not in occupied:
        targets.append(gate[:2])
    for dx, dy in ((0, 1), (1, 0), (0, -1), (-1, 0), (1, 1), (-1, 1), (1, -1), (-1, -1)):
        t = (gate[0] + dx, gate[1] + dy)
        if t not in occupied:
            targets.append(t)
    for tgt in targets:
        if cur == tgt:
            return False
        res = move_attempt(agent, 0, st, tgt[0], tgt[1], log, 'move_town')
        if res.status == 'COMPLETED':
            return True
        if res.status == 'REJECTED':
            continue
        st2 = state(agent)
        if (st2.get('battle') or {}).get('active'):
            return True
        hs = st2.get('heroes') or []
        if hs and tuple(hs[0]['visitable'][:2]) != cur:
            return True
        # unchanged: not fatal, try the next approach tile
        continue
    return False


def hero_targets(st, rejected, enemy_visible_strong: bool, orders=None):
    """Ranked adventure-map targets for the main hero."""
    heroes = st.get('heroes') or []
    if not heroes:
        return []
    hx, hy = heroes[0]['visitable'][:2]
    out = []
    for o in st.get('objects') or []:
        if o.get('id') in rejected:
            continue
        v = o.get('visitable')
        if not v:
            continue
        kind = o.get('kind') or ''
        typ = o.get('type') or ''
        owner = o.get('owner')
        dist = abs(v[0] - hx) + abs(v[1] - hy)
        orders = orders or {}
        avoid = orders.get('avoid_ids') or set()
        attack = orders.get('attack_ids') or set()
        if o.get('id') in avoid:
            continue
        engage = orders.get('engage') or {}
        if kind == 'town' and owner is not None and owner != st.get('player'):
            if o.get('id') in attack or engage.get('town'):
                out.append((-2, dist, o))       # enemy town first of all
        elif typ == 'mine' or kind == 'mine':
            if owner != st.get('player'):
                out.append((1, dist, o))
        elif typ in ('treasureChest', 'resource', 'artifact', 'scholar'):
            out.append((2, dist, o))
        elif kind == 'hero' and owner is not None and owner != st.get('player'):
            if o.get('id') in attack or engage.get('hero'):
                out.append((-2, dist, o))
        elif kind == 'creature':
            if o.get('id') in attack or engage.get('creature'):
                out.append((-1, dist, o))      # fights first: the recording must show battles
    out.sort(key=lambda t: (t[0], t[1]))
    return out


EXPLORE_OFFSETS = [(6, 0), (0, 6), (-6, 0), (0, -6), (6, 6), (-6, 6), (6, -6), (-6, -6),
                   (12, 0), (0, 12), (-12, 0), (0, -12), (12, 12), (-12, 12), (12, -12), (-12, -12)]


def move_hero(agent, st, log, targets, rejected) -> bool:
    heroes = st.get('heroes') or []
    if not heroes:
        return False
    h = heroes[0]
    if h.get('movement', 0) < 80:
        return False
    for rank, dist, o in targets[:10]:
        dst = tuple(o['visitable'])
        res = agent.execute(f'move_hero 0 {dst[0]} {dst[1]} {dst[2]}',
                            verify=agent.verify_move(0, dst[:2]), verify_timeout=45,
                            result_timeout=50)
        log.append({'kind': 'move', 'target': o.get('name'), 'rank': rank, 'dst': dst[:2],
                    'result': str(res)})
        if res.status == 'COMPLETED':
            return True
        if res.status == 'REJECTED':
            rejected.add(o.get('id'))
            continue
        st2 = state(agent)
        if (st2.get('battle') or {}).get('active'):
            return True
        if tuple(st2['heroes'][0]['visitable'][:2]) != tuple(h['visitable'][:2]):
            return True
        # no observable change: movement has no side effects, so mark the target and continue
        rejected.add(o.get('id'))
        log.append({'kind': 'move_noop', 'target': o.get('name'), 'dst': dst[:2]})
        continue
    return False


def explore(agent, st, log, step, town=None) -> bool:
    heroes = st.get('heroes') or []
    if not heroes or heroes[0].get('movement', 0) < 80:
        return False
    h = heroes[0]
    if town and step < 24:
        # systematic coverage: expanding ring around the home town
        ring = step // 8 + 1
        idx = step % 8
        offsets = [(ring * 6, 0), (0, ring * 6), (-ring * 6, 0), (0, -ring * 6),
                   (ring * 6, ring * 6), (-ring * 6, ring * 6),
                   (ring * 6, -ring * 6), (-ring * 6, -ring * 6)]
        dx, dy = offsets[idx]
        ox, oy = town['pos'][:2]
    else:
        ox, oy = h['visitable'][:2]
    for _ in range(len(EXPLORE_OFFSETS)):
        dx, dy = EXPLORE_OFFSETS[step % len(EXPLORE_OFFSETS)]
        step += 1
        tx, ty = ox + dx, oy + dy
        if not (0 <= tx < 200 and 0 <= ty < 200):
            continue
        res = agent.execute(f'move_hero 0 {tx} {ty} 0', verify=None, result_timeout=25)
        log.append({'kind': 'explore', 'dst': [tx, ty], 'result': str(res)})
        if res.status == 'COMPLETED':
            return True
        if res.status == 'REJECTED':
            continue
        st2 = state(agent)
        if tuple(st2['heroes'][0]['visitable'][:2]) != (ox, oy) or \
                (st2.get('battle') or {}).get('active'):
            return True
    return False


def explore_hero(agent, hero_idx: int, st, log, step) -> bool:
    """Send a spare hero towards expanding points around the capital (scouting)."""
    heroes = st.get('heroes') or []
    if hero_idx >= len(heroes):
        return False
    h = heroes[hero_idx]
    if h.get('movement', 0) < 80:
        return False
    town = get_town(st)
    ox, oy = (town['pos'][:2] if town else h['visitable'][:2])
    ring = step // 8 + 2
    idx = step % 8
    offsets = [(ring * 7, 0), (0, ring * 7), (-ring * 7, 0), (0, -ring * 7),
               (ring * 7, ring * 7), (-ring * 7, ring * 7),
               (ring * 7, -ring * 7), (-ring * 7, -ring * 7)]
    dx, dy = offsets[idx]
    tx, ty = ox + dx, oy + dy
    if not (0 <= tx < 200 and 0 <= ty < 200):
        return False
    before = tuple(h['visitable'][:2])
    res = agent.execute(f'move_hero {hero_idx} {tx} {ty} 0', verify=None, result_timeout=25)
    log.append({'kind': 'explore', 'hero': hero_idx, 'dst': [tx, ty], 'result': str(res)})
    if res.status == 'COMPLETED':
        return True
    if res.status == 'REJECTED':
        return False
    st2 = state(agent)
    hs = st2.get('heroes') or []
    if hero_idx < len(hs) and tuple(hs[hero_idx]['visitable'][:2]) != before:
        return True
    return bool((st2.get('battle') or {}).get('active'))


def end_turn_and_wait(agent, log, deadline) -> bool:
    st = state(agent)
    day0 = st['date']['day']
    res = agent.execute('end_turn', verify=None, result_timeout=20)
    log.append({'kind': 'end_turn', 'day': day0, 'result': str(res)})
    if res.status == 'REJECTED':
        return False
    while time.time() < deadline:
        try:
            st = state(agent)
        except StaleStateError:
            raise
        if st.get('status') in ('WINNER', 'LOSER'):
            return True
        if st['date']['day'] > day0:
            return True
        if st.get('query'):
            answer_query(agent, st, log)
            continue
        if (st.get('battle') or {}).get('active'):
            fight_battle(agent, st, log, deadline)
            continue
        time.sleep(0.5)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--map', default='Maps/Arrogance')
    ap.add_argument('--dir', default=str(ROOT / 'var' / 'agent_win'))
    ap.add_argument('--max-turns', type=int, default=200)
    ap.add_argument('--max-minutes', type=float, default=110.0)
    args = ap.parse_args()

    REPORTS.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    report_path = REPORTS / f'full-game-{stamp}.json'
    log: list = []
    report = {'stamp': stamp, 'map': args.map, 'decision_maker': 'scripted baseline (not an LLM)',
              'limits': {'max_turns': args.max_turns, 'max_minutes': args.max_minutes},
              'actions': log, 'stop_reason': None, 'errors': []}
    start_wall = time.time()

    agent = VcmiAgent(args.dir, args.map)
    try:
        if not agent.start():
            report['stop_reason'] = 'preflight: client did not start'
            return finish(report, report_path)
    except Exception as exc:
        report['stop_reason'] = f'preflight: {exc}'
        return finish(report, report_path)

    try:
        st = agent.wait_in_game(180)
        report['session'] = {'session_id': agent.session.session_id,
                             'client_pid': agent.session.client_pid, 'dir': str(agent.dir)}
        report['offscreen'] = {'rect': agent._window_rect(),
                               'is_offscreen': agent.window_is_offscreen()}
        report['start_state'] = summarize(st)
        start_day = st['date']['day']
        deadline = start_wall + args.max_minutes * 60
        turns = 0
        failures = {'build': set(), 'recruit': set(), 'move': set()}
        rejected: set = set()
        explore_step = 0
        last_progress = time.time()
        last_shot_turn = -10
        while time.time() < deadline:
            st = state(agent)
            status = st.get('status')
            if status in ('WINNER', 'LOSER'):
                report['stop_reason'] = 'victory' if status == 'WINNER' else 'defeat'
                break
            if st['date']['day'] - start_day >= args.max_turns:
                report['stop_reason'] = 'turn_limit'
                break
            if st.get('query'):
                r = answer_query(agent, st, log)
                if r == 'unsupported':
                    report['stop_reason'] = 'unsupported_query'
                    break
                continue
            if (st.get('battle') or {}).get('active'):
                r = fight_battle(agent, st, log, deadline)
                if r in ('unsupported', 'stuck'):
                    report['stop_reason'] = f'battle_{r}'
                    break
                continue
            if not st.get('making_turn'):
                time.sleep(0.6)
                continue
            town = get_town(st)
            acted = False
            if town:
                b = pick_build(st, failures['build'])
                if b:
                    res = agent.execute(f"build 0 {b['id']}",
                                        verify=agent.verify_build(0, b['id']), verify_timeout=15)
                    log.append({'kind': 'build', 'target': b, 'result': str(res)})
                    if res.ok:
                        acted = True
                    elif res.status == 'UNKNOWN':
                        failures['build'].add(b['id'])
                        st_b = state(agent)
                        built = (get_town(st_b) or {}).get('built') or []
                        log.append({'kind': 'build_reconcile', 'id': b['id'],
                                    'built': b['id'] in built})
            if recruit_all(agent, state(agent), log, failures):
                acted = True
            st = state(agent)
            if town and transfer_garrison_to_hero(agent, st, log):
                acted = True
            st = state(agent)
            # low army and troops waiting at home -> go pick them up
            orders_now = load_orders(agent)
            if town and st.get('heroes'):
                garrison_total = sum(g['count'] for g in town.get('garrison') or [])
                threatened = town_threat(st, town)
                gate = town_gate(st, town)
                heroes_now = st.get('heroes') or []
                # if the scout is standing on the gate, move it away so the fighter can enter
                if len(heroes_now) >= 2 and tuple(heroes_now[1]['visitable'][:2]) == gate[:2]:
                    if move_attempt(agent, 1, st, gate[0], gate[1] + 2, log, 'scout_clear'):
                        st = state(agent)
                        heroes_now = st.get('heroes') or []
                hero_at_gate = bool(heroes_now) and \
                    tuple(heroes_now[0]['visitable'][:2]) == gate[:2]
                if hero_at_gate and garrison_total >= 1 and (orders_now.get('muster') or threatened):
                    if transfer_garrison_to_hero(agent, st, log):
                        acted = True
                elif garrison_total >= 5 and army_total(st['heroes'][0]) < garrison_total:
                    if go_to_town(agent, st, log, town):
                        acted = True
                        st = state(agent)
                        if orders_now.get('muster') or threatened:
                            transfer_garrison_to_hero(agent, st, log)
                # a hero standing on the gate blocks tavern hiring: step aside once troops are loaded
                if len(st.get('heroes') or []) < 2 and hero_at_gate and st['resources']['gold'] >= 6000:
                    h2 = st['heroes'][0]
                    ox, oy = h2['visitable'][:2]
                    for dx, dy in ((0, 1), (1, 0), (0, -1), (-1, 0)):
                        res = agent.execute(f'move_hero 0 {ox + dx} {oy + dy} 0',
                                            verify=None, result_timeout=20)
                        if res.status == 'COMPLETED' and \
                                tuple(state(agent)['heroes'][0]['visitable'][:2]) != (ox, oy):
                            log.append({'kind': 'step_aside'})
                            st = state(agent)
                            break
            st = state(agent)
            if len(st.get('heroes') or []) < 2:
                if maybe_hire_scout(agent, st, log):
                    acted = True
                    st = state(agent)
            # defensive priority: an enemy hero near home -> return and defend
            if town and st.get('heroes') and town_threat(st, town):
                if go_to_town(agent, st, log, town):
                    acted = True
                    st = state(agent)
                    transfer_garrison_to_hero(agent, st, log)
                    st = state(agent)
            if not (st.get('heroes') or []):
                # our hero died: rebuild from the tavern and hand over the garrison
                recovered = recruit_replacement_hero(agent, st, log)
                if recovered:
                    acted = True
                st = state(agent)
            if st.get('heroes') and not (town and town_threat(st, town)):
                orders = load_orders(agent)
                # fighter (hero 0) acts on LLM orders only; scout (hero 1) explores
                targets = hero_targets(st, rejected, False, orders)
                if move_hero(agent, st, log, targets, rejected):
                    acted = True
                else:
                    if explore(agent, st, log, explore_step, town):
                        acted = True
                        explore_step += 1
                    elif len(st.get('heroes') or []) >= 2:
                        if explore_hero(agent, 1, st, log, explore_step):
                            acted = True
                            explore_step += 1
            st = state(agent)
            if st.get('status') in ('WINNER', 'LOSER'):
                continue
            hero = main_hero(st)
            if acted and hero and hero.get('movement', 0) > 80:
                last_progress = time.time()
                continue
            if time.time() - last_progress > 300:
                report['stop_reason'] = 'no_progress_300s'
                break
            # end the turn
            end_turn_and_wait(agent, log, deadline)
            turns += 1
            last_progress = time.time()
            st = state(agent)
            if turns - last_shot_turn >= 5:
                try:
                    agent.shot(SHOTS / f'full-game-{stamp}-d{st["date"]["day"]}.png')
                    last_shot_turn = turns
                except Exception as exc:
                    report['errors'].append(f'screenshot: {exc}')
            log.append({'kind': 'turn_done', 'turn': turns, 'day': st['date']['day'],
                        'gold': st['resources']['gold'],
                        'army': army_total(st['heroes'][0]) if st.get('heroes') else 0})
            report['actions'] = log
        else:
            report['stop_reason'] = 'wall_clock_limit'
        report['turns_played'] = turns
        st = state(agent)
        report['end_state'] = summarize(st)
        report['end_status'] = st.get('status')
        report['offscreen_end'] = {'rect': agent._window_rect(),
                                   'is_offscreen': agent.window_is_offscreen()}
        try:
            agent.shot(SHOTS / f'full-game-{stamp}-end.png')
            report['screenshot'] = str(SHOTS / f'full-game-{stamp}-end.png')
        except Exception as exc:
            report['errors'].append(f'screenshot: {exc}')
    except StopRun as exc:
        report['stop_reason'] = f'stop_rule: {exc}'
    except StaleStateError as exc:
        report['stop_reason'] = f'state_lost: {exc}'
    except Exception as exc:
        report['stop_reason'] = f'exception: {type(exc).__name__}: {exc}'
        report['errors'].append(traceback.format_exc())
    finally:
        try:
            if 'end_state' not in report:
                st_end = state(agent)
                report['end_state'] = summarize(st_end)
                report['end_status'] = st_end.get('status')
            if 'screenshot' not in report:
                agent.shot(SHOTS / f'full-game-{stamp}-end.png')
                report['screenshot'] = str(SHOTS / f'full-game-{stamp}-end.png')
        except Exception as exc:
            report['errors'].append(f'end capture: {exc}')
        try:
            report['shutdown'] = agent.stop()
        except Exception as exc:
            report['errors'].append(f'stop: {exc}')
        report['elapsed_seconds'] = round(time.time() - start_wall, 1)
        report['actions'] = log
    return finish(report, report_path)


def finish(report: dict, path: Path) -> int:
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    md = path.with_suffix('.md')
    lines = [f"# full game {report['stamp']}", '',
             f"- map: {report.get('map')}",
             f"- decision maker: {report.get('decision_maker')}",
             f"- stop reason: {report.get('stop_reason')}",
             f"- turns: {report.get('turns_played')}  elapsed: {report.get('elapsed_seconds')}s",
             f"- final status: {report.get('end_status')}",
             f"- session: {report.get('session')}",
             f"- offscreen start/end: {report.get('offscreen')} / {report.get('offscreen_end')}",
             f"- shutdown: {report.get('shutdown')}", '']
    end = report.get('end_state') or {}
    lines += [f"## final state (day {end.get('day')})", '',
              f"- resources: {end.get('resources')}"]
    for h in end.get('heroes') or []:
        lines.append(f"- hero {h['name']} at {h['pos']} army {h['army']} mov {h['movement']}")
    for t in end.get('towns') or []:
        lines.append(f"- town {t['name']} built {t['built']}")
    lines += ['', '## last actions']
    for a in report.get('actions', [])[-60:]:
        lines.append(f"- {json.dumps(a, ensure_ascii=False)}")
    md.write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps({k: report.get(k) for k in ('stop_reason', 'turns_played', 'end_status',
                                                 'elapsed_seconds', 'offscreen', 'shutdown')},
                     ensure_ascii=False, indent=1))
    print('report:', path)
    return 0 if report.get('stop_reason') in ('victory', 'defeat', 'turn_limit',
                                              'wall_clock_limit') else 1


if __name__ == '__main__':
    sys.exit(main())
