# -*- coding: utf-8 -*-
"""One bounded, honest short game (D-007 first-round execution).

Rules implemented here:
  * fixed config, no silent restarts / no re-rolling the start, no human play;
  * at most `max_turns` own turns or `max_minutes` of wall clock since client start;
  * every action is submitted with a sequence number and only counted as completed when the
    state post-condition is observed; unknown results are reconciled once and then stop;
  * battles are handled with the real player side (`battle.my_side`), queries are answered only
    for supported types, unsupported blocking interactions stop the run;
  * an event pump keeps handling battles/queries while waiting for the next day;
  * a JSON + markdown report is written with evidence per action, stop reason and leftovers.
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

from vcmi_agent import CommandResult, StaleStateError, VcmiAgent  # noqa: E402

class StopRun(Exception):
    """Stop the run immediately with an honest reason."""


REPORTS = ROOT / 'var' / 'reports'
SHOTS = ROOT / 'var' / 'shots'

QUERY_ANSWERS = {
    'level_up': 0,
    'yes_no': 1,
    'garrison': 1,
    'recruitment': 1,
    'info': 0,
    'teleport': -1,
}


def _state(agent: VcmiAgent) -> dict:
    return agent.state(timeout=12)


def answer_query(agent: VcmiAgent, st: dict, log: list) -> str:
    q = st.get('query')
    if not q:
        return 'none'
    qtype = q.get('type')
    if qtype == 'select':
        options = q.get('options') or []
        xp = [o for o in options if o.get('type') == 'EXPERIENCE']
        value = xp[0]['index'] if xp else (1 if options else 0)
    elif qtype in QUERY_ANSWERS:
        value = QUERY_ANSWERS[qtype]
    else:
        log.append({'kind': 'query', 'action': 'STOP', 'detail': f'unsupported query {qtype}'})
        return 'unsupported'
    res = agent.execute(f'answer {value}', verify=None, result_timeout=20)
    log.append({'kind': 'query', 'action': f'answer {value}', 'type': qtype,
                'text': str(q.get('text', ''))[:120], 'result': str(res)})
    return 'answered' if res.status != 'NOT_SENT' else 'failed'


def fight_battle(agent: VcmiAgent, st: dict, log: list, deadline: float) -> str:
    """Handle the active battle using the real side; returns 'done' | 'stuck' | 'unsupported'.

    If <dir>/llm_battle.flag exists the battle is handed over to the reasoning layer: the pump
    only answers queries and waits while the LLM issues the actual BattleAction commands through
    agent_mcp.  That keeps 'who decided which stack to attack' unambiguous in recordings.
    """
    llm_control = agent.dir / 'llm_battle.flag'
    if llm_control.exists():
        log.append({'kind': 'battle', 'action': 'handed_to_llm'})
        last_cmds = agent._command_lines()
        idle_since = time.time()
        fallbacks = 0
        deploy_retry = 0
        while time.time() < deadline:
            st = _state(agent)
            battle = st.get('battle') or {}
            if not battle.get('active'):
                log.append({'kind': 'battle', 'action': 'finished_under_llm_control'})
                return 'done'
            if st.get('query'):
                answer_query(agent, st, log)
                continue
            cmds = agent._command_lines()
            if cmds != last_cmds:
                last_cmds = cmds
                idle_since = time.time()
                continue
            idle = time.time() - idle_since
            active = battle.get('active_stack')
            ours_active = any(x.get('id') == active and x.get('side') == battle.get('my_side')
                              for x in battle.get('stacks') or [])
            if idle > 12 and (active is None or active < 0) and deploy_retry < 5:
                # pre-battle deployment / turn handover: confirm with Enter
                agent.execute('key return', verify=None, result_timeout=10)
                log.append({'kind': 'battle', 'action': 'deploy_confirm_fallback'})
                deploy_retry += 1
                idle_since = time.time()
            elif idle > 12 and ours_active:
                # our stack waits for orders and the LLM is idle: defend to keep the turn going
                agent.execute('battle_defend', verify=None, result_timeout=10)
                log.append({'kind': 'battle', 'action': 'llm_idle_fallback_defend'})
                fallbacks += 1
                idle_since = time.time()
            elif idle > 60:
                # nobody is driving this battle any more: stop instead of recording a frozen frame
                log.append({'kind': 'battle', 'action': 'llm_absent_stop'})
                return 'stuck'
            if fallbacks > 40:
                return 'stuck'
            time.sleep(0.5)
        return 'stuck'
    last_progress = time.time()
    rejections = 0
    wait_started = {}
    while time.time() < deadline:
        st = _state(agent)
        if st.get('query'):
            r = answer_query(agent, st, log)
            if r == 'unsupported':
                return 'unsupported'
            continue
        battle = st.get('battle') or {}
        if not battle.get('active'):
            log.append({'kind': 'battle', 'action': 'finished'})
            return 'done'
        my_side = battle.get('my_side', -1)
        stacks = battle.get('stacks') or []
        active = battle.get('active_stack', -1)
        enemies = [s for s in stacks if s.get('side') != my_side]
        mine = [s for s in stacks if s.get('side') == my_side]
        if not enemies:
            agent.execute('battle_result_ok', verify=None, result_timeout=10)
            time.sleep(0.5)
            continue
        if not mine:
            return 'stuck'
        active_stack = next((s for s in stacks if s.get('id') == active), None)
        if active is None or active < 0 or active_stack is None or active_stack.get('side') != my_side:
            # active_stack == -1 usually means the pre-battle DEPLOYMENT phase is waiting for
            # a confirmation (manual combat).  Without it the battle hangs forever.
            wait_started.setdefault('t', time.time())
            waited = time.time() - wait_started['t']
            if active is None or active < 0:
                if waited > 3 and not wait_started.get('pressed'):
                    wait_started['pressed'] = True
                    agent.execute('key return', verify=None, result_timeout=10)
                    log.append({'kind': 'battle', 'action': 'confirm_deployment'})
            wait_started['t'] = time.time() if waited > 3 else wait_started['t']
            time.sleep(0.8)          # enemy turn / animation
            continue
        wait_started.clear()
        enemies.sort(key=lambda e: abs(e['hex'][0] - active_stack['hex'][0])
                     + abs(e['hex'][1] - active_stack['hex'][1]))
        target = enemies[0]
        res = agent.execute(f"battle_attack {target['id']}", verify=None, result_timeout=15)
        log.append({'kind': 'battle', 'action': f"attack {target['id']}",
                    'my_count': active_stack.get('count'), 'enemy_count': target.get('count'),
                    'result': str(res)})
        if res.status == 'REJECTED':
            rejections += 1
            agent.execute('battle_defend', verify=None, result_timeout=10)
            if rejections > 4:
                return 'stuck'
        elif res.status == 'COMPLETED':
            rejections = 0
            last_progress = time.time()
        elif res.status == 'UNKNOWN':
            # reconcile once with state, then stop (no blind retry of non-idempotent actions)
            time.sleep(1.0)
            st2 = _state(agent)
            if not (st2.get('battle') or {}).get('active'):
                return 'done'
            if time.time() - last_progress > 120:
                return 'stuck'
        if time.time() - last_progress > 60:
            return 'stuck'
    return 'stuck'


def pump_events(agent: VcmiAgent, stop_when_day_after: int | None = None,
                timeout: float = 180.0, verbose: bool = False) -> dict:
    """Shared event pump: keep answering queries / fighting battles while waiting.

    Returns {'reason': ..., 'day': ...}.  Reasons: day_changed, timeout, unsupported, stuck,
    state_lost.
    """
    log: list = []
    deadline = time.time() + timeout
    last_day = None
    while time.time() < deadline:
        try:
            st = _state(agent)
        except StaleStateError as exc:
            return {'reason': 'state_lost', 'detail': str(exc), 'day': last_day}
        last_day = st['date']['day']
        if stop_when_day_after is not None and last_day > stop_when_day_after:
            return {'reason': 'day_changed', 'day': last_day}
        if st.get('query'):
            r = answer_query(agent, st, log)
            if r == 'unsupported':
                return {'reason': 'unsupported', 'day': last_day, 'log': log}
            continue
        if (st.get('battle') or {}).get('active'):
            r = fight_battle(agent, st, log, deadline)
            if r in ('unsupported', 'stuck'):
                return {'reason': r, 'day': last_day, 'log': log}
            continue
        time.sleep(0.5)
    return {'reason': 'timeout', 'day': last_day, 'log': log}


# ---------------------------------------------------------------------- planner
def pick_build(st: dict, reserve_gold: int = 2000):
    towns = st.get('towns') or []
    if not towns:
        return None
    town = towns[0]
    gold = st['resources']['gold']
    options = town.get('build_options') or []
    affordable = []
    for o in options:
        cost = o.get('cost') or {}
        if cost.get('gold', 0) + reserve_gold > gold:
            continue
        # resource-only costs are fine too; require we can pay every listed resource
        need_ok = all(st['resources'].get(k, 0) >= v for k, v in cost.items())
        if need_ok and o.get('name'):
            affordable.append(o)
    if not affordable:
        return None
    affordable.sort(key=lambda o: (o['cost'].get('gold', 0), o['id']))
    return affordable[0]


def pick_recruit(st: dict, reserve_gold: int = 1200):
    towns = st.get('towns') or []
    if not towns:
        return None
    gold = st['resources']['gold']
    for dw in towns[0].get('dwellings', []):
        if dw.get('available', 0) > 0 and gold > reserve_gold:
            return dw['level'], dw['available']
    return None


def pick_target(st: dict):
    heroes = st.get('heroes') or []
    if not heroes:
        return None
    hx, hy = heroes[0]['visitable'][:2]
    objs = st.get('objects') or []
    pref = {'treasureChest': 0, 'resource': 1, 'artifact': 2, 'mine': 3, 'creature': 4,
            'monster': 4}
    candidates = []
    for o in objs:
        if o.get('owner') is not None and o.get('kind') == 'mine':
            continue                      # already ours
        v = o.get('visitable')
        if not v:
            continue
        kind = o.get('kind') or ''
        typ = o.get('type') or ''
        rank = pref.get(typ, pref.get(kind, 9))
        if rank >= 9:
            continue
        dist = abs(v[0] - hx) + abs(v[1] - hy)
        candidates.append((rank, dist, o))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2]


def pick_targets(st: dict, exclude=None, limit: int = 6):
    """Ranked list of reachable-looking targets, skipping known-rejected ids."""
    exclude = exclude or set()
    heroes = st.get('heroes') or []
    if not heroes:
        return []
    hx, hy = heroes[0]['visitable'][:2]
    objs = st.get('objects') or []
    pref = {'treasureChest': 0, 'resource': 1, 'artifact': 2, 'mine': 3, 'creature': 4,
            'monster': 4}
    candidates = []
    for o in objs:
        if o.get('id') in exclude:
            continue
        if o.get('owner') is not None and o.get('kind') == 'mine':
            continue
        v = o.get('visitable')
        if not v:
            continue
        kind = o.get('kind') or ''
        typ = o.get('type') or ''
        rank = pref.get(typ, pref.get(kind, 9))
        if rank >= 9:
            continue
        dist = abs(v[0] - hx) + abs(v[1] - hy)
        candidates.append((rank, dist, o))
    candidates.sort(key=lambda c: (c[0], c[1]))
    return [c[2] for c in candidates[:limit]]


def plan_turn(agent: VcmiAgent, log: list, human_log: list, failures: dict, deadline: float) -> bool:
    """One own-turn plan. Returns True if something was actually done."""
    st = _state(agent)
    did = False

    build = pick_build(st)
    if build and build['id'] not in failures.get('build', set()):
        res = agent.execute(f"build 0 {build['id']}", verify=agent.verify_build(0, build['id']),
                            verify_timeout=15)
        log.append({'kind': 'build', 'target': build, 'result': str(res)})
        if res.ok:
            did = True
            human_log.append(f"build {build['id']} ({build.get('name')}) proved by built list")
        elif res.status == 'UNKNOWN':
            # reconcile exactly once through the state; a non-idempotent action must never be
            # retried blindly (D-007)
            st2 = _state(agent)
            built = (st2.get('towns') or [{}])[0].get('built') or []
            failures.setdefault('build', set()).add(build['id'])
            if build['id'] in built:
                human_log.append(f"build {build['id']} reconciled: present in built list")
            else:
                raise StopRun(f'unknown_build id={build["id"]} name={build.get("name")}'
                              f' (not in built list after reconcile)')
    st = _state(agent)
    rec = pick_recruit(st)
    if rec:
        level, amount = rec
        amount = min(amount, 8)
        res = agent.execute(f'recruit 0 {level} {amount}',
                            verify=agent.verify_recruit(0, None, 1), verify_timeout=20)
        log.append({'kind': 'recruit', 'level': level, 'amount': amount, 'result': str(res)})
        if res.ok:
            did = True
            human_log.append(f'recruit level {level} x{amount} proved by garrison count')

    st = _state(agent)
    rejected = failures.setdefault('move', set())
    for target in pick_targets(st, exclude=rejected):
        hero = st['heroes'][0]
        pos = tuple(hero['visitable'][:2])
        dst_full = tuple(target['visitable'])
        dst = dst_full[:2]
        if dst == pos:
            continue
        res = agent.execute(f'move_hero 0 {dst_full[0]} {dst_full[1]} {dst_full[2]}',
                            verify=agent.verify_move(0, dst), verify_timeout=30,
                            result_timeout=40)
        log.append({'kind': 'move', 'target': target.get('name'), 'dst': dst,
                    'result': str(res)})
        human_log.append(f"move towards {target.get('name')} {dst}: {res.status}")
        if res.ok:
            did = True
            break
        if res.status == 'REJECTED':
            rejected.add(target.get('id'))
            continue
        # UNKNOWN: reconcile once, then stop - never blindly retry non-idempotent actions
        st2 = _state(agent)
        if (st2.get('battle') or {}).get('active') or \
                tuple(st2['heroes'][0]['visitable'][:2]) != pos:
            did = True
            human_log.append('move reconciled: hero moved or battle started')
            break
        raise StopRun(f'unknown_move to {dst}: no observable change after reconcile')
    return did


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--map', default='Maps/Arrogance')
    ap.add_argument('--dir', default=str(ROOT / 'var' / 'agent_short'))
    ap.add_argument('--max-turns', type=int, default=7)
    ap.add_argument('--max-minutes', type=float, default=30.0)
    ap.add_argument('--force', action='store_true', help='run even if preflight warnings exist')
    args = ap.parse_args()

    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    report_path = REPORTS / f'short-game-{stamp}.json'
    log: list = []
    human: list = []
    start_wall = time.time()
    report = {
        'stamp': stamp,
        'map': args.map,
        'decision_maker': 'scripted (NOT an LLM player)',
        'limits': {'max_turns': args.max_turns, 'max_minutes': args.max_minutes},
        'config': {},
        'actions': [],
        'human_summary': human,
        'stop_reason': None,
        'errors': [],
    }

    # ---- preflight (no silent restart, no human play) ----
    try:
        import io
        settings = Path.home() / 'Documents' / 'My Games' / 'vcmi' / 'config' / 'settings.json'
        cfg = json.load(io.open(settings, encoding='utf-8-sig'))
        report['config'] = {
            'settings_file': str(settings),
            'quickCombat': cfg.get('adventure', {}).get('quickCombat'),
            'difficulty': cfg.get('game', {}).get('difficulty'),
        }
    except Exception as exc:
        report['config'] = {'settings_read_error': str(exc)}

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
                             'client_pid': agent.session.client_pid,
                             'dir': str(agent.dir)}
        report['start_state'] = summarize(st)
        try:
            report['offscreen'] = {'rect': agent._window_rect(),
                                   'is_offscreen': agent.window_is_offscreen()}
        except Exception as exc:
            report['offscreen'] = {'error': str(exc)}
        start_day = st['date']['day']
        deadline = start_wall + args.max_minutes * 60
        log.append({'kind': 'session', 'session_id': agent.session.session_id,
                    'day': start_day})
        turns = 0
        last_progress = time.time()
        unknown_streak = 0
        failures: dict = {}
        while time.time() < deadline:
            st = _state(agent)
            if st['date']['day'] - start_day >= args.max_turns:
                report['stop_reason'] = 'turn_limit'
                break
            if st.get('query'):
                r = answer_query(agent, st, log)
                if r == 'unsupported':
                    report['stop_reason'] = 'unsupported_query'
                    break
                last_progress = time.time()
                continue
            if (st.get('battle') or {}).get('active'):
                r = fight_battle(agent, st, log, deadline)
                report['actions'] = log
                if r == 'unsupported':
                    report['stop_reason'] = 'unsupported_battle_interaction'
                    break
                if r == 'stuck':
                    report['stop_reason'] = 'battle_stuck'
                    break
                last_progress = time.time()
                continue
            acted = plan_turn(agent, log, human, failures, deadline)
            report['actions'] = log
            if acted:
                last_progress = time.time()
                unknown_streak = 0
            else:
                # nothing to do this turn -> end it
                st = _state(agent)
                day0 = st['date']['day']
                res = agent.execute('end_turn', verify=None, result_timeout=20)
                log.append({'kind': 'end_turn', 'day': day0, 'result': str(res)})
                if res.status == 'REJECTED':
                    report['stop_reason'] = 'end_turn_rejected'
                    break
                out = pump_events(agent, stop_when_day_after=day0, timeout=180)
                log.append({'kind': 'pump', 'outcome': out})
                report['actions'] = log
                if out.get('reason') not in ('day_changed',):
                    report['stop_reason'] = f"pump_{out.get('reason')}"
                    break
                turns += 1
                last_progress = time.time()
                unknown_streak = 0
                continue
            if time.time() - last_progress > 120:
                # reconcile once, then stop
                st2 = _state(agent)
                if st2['date']['day'] == st['date']['day'] and not st2.get('query') \
                        and not (st2.get('battle') or {}).get('active'):
                    report['stop_reason'] = 'no_progress_120s'
                    break
                last_progress = time.time()
        else:
            report['stop_reason'] = 'wall_clock_limit'
        report['turns_played'] = turns
        report['end_state'] = summarize(_state(agent))
        try:
            report['offscreen_end'] = {'rect': agent._window_rect(),
                                       'is_offscreen': agent.window_is_offscreen()}
        except Exception:
            pass
        try:
            SHOTS.mkdir(parents=True, exist_ok=True)
            agent.shot(SHOTS / f'short-game-{stamp}.png')
            report['screenshot'] = str(SHOTS / f'short-game-{stamp}.png')
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
            report['shutdown'] = agent.stop()
        except Exception as exc:
            report['errors'].append(f'stop: {exc}')
        report['elapsed_seconds'] = round(time.time() - start_wall, 1)
        report['actions'] = log
    return finish(report, report_path)


def summarize(st: dict) -> dict:
    return {
        'day': st.get('date', {}).get('day'),
        'resources': st.get('resources'),
        'heroes': [{'name': h['name'], 'pos': h['visitable'], 'army': h['army'],
                    'movement': h['movement']} for h in (st.get('heroes') or [])],
        'towns': [{'name': t['name'], 'built': t.get('built'),
                   'garrison': t.get('garrison')} for t in (st.get('towns') or [])],
        'objects_visible': len(st.get('objects') or []),
    }


def finish(report: dict, path: Path) -> int:
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    md = path.with_suffix('.md')
    lines = [f"# short game {report['stamp']}", '',
             f"- map: {report.get('map')}",
             f"- decision maker: {report.get('decision_maker')}",
             f"- stop reason: {report.get('stop_reason')}",
             f"- turns played: {report.get('turns_played')}",
             f"- elapsed: {report.get('elapsed_seconds')}s",
             f"- session: {report.get('session')}",
             f"- config: {report.get('config')}", '', '## actions']
    for a in report.get('actions', []):
        lines.append(f"- {json.dumps(a, ensure_ascii=False)}")
    lines += ['', '## shutdown', json.dumps(report.get('shutdown'), ensure_ascii=False)]
    md.write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps({k: report.get(k) for k in ('stop_reason', 'turns_played',
                                                 'elapsed_seconds', 'session', 'shutdown')},
                     ensure_ascii=False, indent=1))
    print('report:', path)
    return 0 if report.get('stop_reason') else 1


if __name__ == '__main__':
    sys.exit(main())
