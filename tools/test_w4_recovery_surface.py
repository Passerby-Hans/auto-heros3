# -*- coding: utf-8 -*-
"""T31/P-A offline tests: MCP recovery & observation surface (info + last_action surface).

No game, no client, no network: reuses the fake harness from test_action_contract (Env fixes
STATE_DIR/SAVES_DIR to a temp dir and replaces _agent()/_require_lease()).  Nothing here starts
VCMI or acquires a lease.

Covered:
  A  recent_info projection (the bridge already writes state.json "info"): present / missing /
     empty / capped at 8 / consecutive duplicates collapsed / long text truncated / non-string
     items skipped; exposed by BOTH game_observe and game_brief
  B  game_brief recovery surface after a context compaction:
       last_action     newest ledger entry summary (completed actions included)
       recent_actions  up to 5 summaries, newest first, last_action == recent_actions[0]
       fields          action/action_id/request_id/status/seq/ts/evidence/args/age_s
       robustness      game_note skipped, malformed lines ignored, evidence/args truncated,
                       request_id omitted when absent, pending_action unchanged
       making_turn     passthrough (None when the bridge omits it)
       read-only       neither tool ever sends a bridge command
  C  compatibility: old/partial states keep the previous digest shape; observe keeps returning
     decisions; the brief keeps its compact existing keys

Run: python -X utf8 tools/test_w4_recovery_surface.py
Exit code 0 only when every check passes.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))
import mcp_server_v2 as M  # noqa: E402
import test_action_contract as T  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition, detail: str = '') -> None:
    RESULTS.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


def write_ledger(env: T.Env, entries: list) -> None:
    """Replace <state-dir>/actions.jsonl with the given entries (offline fixture)."""
    path = M._dir() / 'actions.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + '\n')


def append_raw(env: T.Env, text: str) -> None:
    path = M._dir() / 'actions.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(text + '\n')


def stamp(seconds_ago: float = 0.0) -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(time.time() - seconds_ago))


def with_info(items) -> dict:
    st = T.state()
    st['info'] = items
    return st


# ------------------------------------------------------------------ A: recent_info projection
def test_recent_info_projection() -> None:
    st = with_info(['第一周：松鼠之周', '你找到了宝物 A', '马厩：提高旅行的速度'])
    digest = M._digest(st)
    check('A1 digest exposes the newest Info-window lines as recent_info',
          digest.get('recent_info') == ['第一周：松鼠之周', '你找到了宝物 A',
                                        '马厩：提高旅行的速度'], str(digest.get('recent_info')))
    check('A1b recent_info keeps the bridge order (oldest first)',
          digest['recent_info'][0] == '第一周：松鼠之周'
          and digest['recent_info'][-1] == '马厩：提高旅行的速度',
          str(digest['recent_info']))

    old = M._digest(T.state())
    check('A2 a state without info yields an empty recent_info and keeps the old fields',
          old.get('recent_info') == [] and old.get('day') == 1 and 'heroes' in old
          and 'battle_active' in old, str({k: old.get(k) for k in ('recent_info', 'day')}))

    empty = M._digest(with_info([]))
    check('A2b an empty info list yields an empty recent_info', empty.get('recent_info') == [],
          str(empty.get('recent_info')))

    many = M._digest(with_info([f'msg{i}' for i in range(12)]))
    check('A3 only the newest 8 info lines are kept',
          many.get('recent_info') == [f'msg{i}' for i in range(4, 12)],
          str(many.get('recent_info')))

    repeated = M._digest(with_info(['A', 'A', 'B', 'B', 'B', 'C']))
    check('A4 consecutive duplicate info lines are collapsed',
          repeated.get('recent_info') == ['A', 'B', 'C'], str(repeated.get('recent_info')))

    long_text = M._digest(with_info(['x' * 400]))
    kept = (long_text.get('recent_info') or [''])
    check('A5 an overlong info line is truncated with an explicit marker',
          len(kept) == 1 and kept[0].endswith('…') and len(kept[0]) == M._RECENT_INFO_TEXT_MAX + 1,
          f'len={len(kept[0]) if kept else None}')

    junk = M._digest(with_info([None, 42, {'a': 1}, '   ', 'ok', ['x']]))
    check('A5b non-string / blank info entries are skipped instead of crashing',
          junk.get('recent_info') == ['ok'], str(junk.get('recent_info')))

    rich = with_info(['星轴：占星家教你提高施法能力', '马厩：每星期一回来换马'])
    rich['making_turn'] = True
    with T.Env() as env:
        fake = env.agent(T.FakeAgent([rich]))
        observed = M.game_observe()
        check('A6 game_observe returns recent_info inside state',
              observed.get('ok') is True
              and (observed.get('state') or {}).get('recent_info')
              == ['星轴：占星家教你提高施法能力', '马厩：每星期一回来换马'],
              str((observed.get('state') or {}).get('recent_info')))
        brief = M.game_brief()
        check('A6b game_brief returns the same recent_info',
              brief.get('ok') is True
              and brief.get('recent_info') == observed['state']['recent_info'],
              str(brief.get('recent_info')))
        check('A6c observing/briefing is read-only (no bridge command was sent)',
              fake.sent == [], str(fake.sent))


# ------------------------------------------------------------------ B: recovery surface
def test_brief_recovery_surface() -> None:
    with T.Env() as env:
        env.agent(T.FakeAgent([T.state()]))
        brief = M.game_brief()
        check('B1 no ledger -> last_action is None and recent_actions is empty',
              brief.get('last_action') is None and brief.get('recent_actions') == [],
              f"last={brief.get('last_action')} recent={brief.get('recent_actions')}")
        check('B1b the new keys are always present in the brief',
              'last_action' in brief and 'recent_actions' in brief
              and 'recent_info' in brief and 'making_turn' in brief,
              str(sorted(set(brief) & {'last_action', 'recent_actions', 'recent_info',
                                       'making_turn'})))

    with T.Env() as env:
        env.agent(T.FakeAgent([T.state()]))
        write_ledger(env, [{'ts': stamp(), 'action': 'game_note', 'status': 'OK',
                            'action_id': 'note1'}])
        brief = M.game_brief()
        check('B2 game_note entries are not part of the recovery tail',
              brief.get('last_action') is None and brief.get('recent_actions') == [],
              str(brief.get('recent_actions')))

    with T.Env() as env:
        env.agent(T.FakeAgent([T.state()]))
        write_ledger(env, [
            {'ts': stamp(120), 'action': 'move_hero', 'status': 'completed', 'seq': 1,
             'action_id': 'a1', 'request_id': 'r1', 'evidence': 'hero 0 tile [1,2]->[3,4]',
             'args': {'hero': 0, 'x': 3, 'y': 4, 'z': 0}},
            {'ts': stamp(60), 'action': 'build', 'status': 'completed', 'seq': 2,
             'action_id': 'a2', 'request_id': 'r2', 'evidence': 'town 0: building 8 appeared',
             'args': {'town': 0, 'id': 8}},
        ])
        brief = M.game_brief()
        last = brief.get('last_action') or {}
        recent = brief.get('recent_actions') or []
        check('B3 last_action is the newest ledger entry',
              last.get('action') == 'build' and last.get('action_id') == 'a2'
              and last.get('request_id') == 'r2' and last.get('status') == 'completed'
              and last.get('seq') == 2, str(last))
        check('B3b recent_actions is newest-first and last_action == recent_actions[0]',
              [item.get('action') for item in recent] == ['build', 'move_hero']
              and recent[0] == last, str([item.get('action') for item in recent]))
        check('B3c recent rows carry ts and a numeric age',
              isinstance(last.get('ts'), str) and isinstance(last.get('age_s'), float)
              and last['age_s'] >= 0, f"ts={last.get('ts')} age={last.get('age_s')}")
        check('B3d evidence and args are included for duplicate detection',
              'building 8 appeared' in str(last.get('evidence'))
              and '"id": 8' in str(last.get('args')), str(last.get('args')))

    with T.Env() as env:
        env.agent(T.FakeAgent([T.state()]))
        write_ledger(env, [{'ts': stamp(10 * (8 - i)), 'action': f'act{i}',
                            'status': 'completed', 'seq': i, 'action_id': f'a{i}'}
                           for i in range(8)])
        recent = M.game_brief().get('recent_actions') or []
        check('B4 recent_actions is capped at 5 rows (newest first)',
              len(recent) == M._ACTION_TAIL_MAX
              and [item.get('action') for item in recent]
              == ['act7', 'act6', 'act5', 'act4', 'act3'],
              str([item.get('action') for item in recent]))

    with T.Env() as env:
        env.agent(T.FakeAgent([T.state()]))
        write_ledger(env, [{'ts': stamp(), 'action': 'end_turn', 'status': 'completed',
                            'seq': 3, 'action_id': 'a3'}])
        last = M.game_brief().get('last_action') or {}
        check('B5 request_id is omitted when the ledger entry has none',
              'request_id' not in last, str(last))

    with T.Env() as env:
        env.agent(T.FakeAgent([T.state()]))
        write_ledger(env, [{'ts': stamp(), 'action': 'recruit', 'status': 'completed',
                            'seq': 4, 'action_id': 'a4',
                            'evidence': 'e' * 500,
                            'args': {'town': 0, 'level': 7, 'amount': 3,
                                     'note': 'n' * 500}}])
        last = M.game_brief().get('last_action') or {}
        check('B6 overlong evidence is truncated with a marker',
              str(last.get('evidence', '')).endswith('…')
              and len(str(last.get('evidence', ''))) == M._ACTION_EVIDENCE_MAX + 1,
              f"len={len(str(last.get('evidence', '')))}")
        check('B6b overlong args are truncated with a marker',
              str(last.get('args', '')).endswith('…')
              and len(str(last.get('args', ''))) == M._ACTION_ARGS_MAX + 1,
              f"len={len(str(last.get('args', '')))}")

    with T.Env() as env:
        env.agent(T.FakeAgent([T.state()]))
        write_ledger(env, [{'ts': stamp(), 'action': 'move_hero', 'status': 'completed',
                            'seq': 5, 'action_id': 'a5'}])
        append_raw(env, '{not valid json')
        append_raw(env, json.dumps({'ts': stamp(), 'action': 'build', 'status': 'submitted',
                                    'seq': 6, 'action_id': 'a6'}))
        brief = M.game_brief()
        check('B7 malformed ledger lines are ignored, newest valid entry wins',
              (brief.get('last_action') or {}).get('action_id') == 'a6',
              str(brief.get('last_action')))
        check('B7b pending_action still surfaces the recent submitted entry',
              (brief.get('pending_action') or {}).get('action_id') == 'a6',
              str(brief.get('pending_action')))

    with T.Env() as env:
        env.agent(T.FakeAgent([T.state()]))
        write_ledger(env, [{'ts': stamp(5), 'action': 'build', 'status': 'completed',
                            'seq': 1, 'action_id': 'done1'}])
        brief = M.game_brief()
        check('B8 a completed-only tail does not raise pending_action',
              'pending_action' not in brief, str(brief.get('pending_action')))

    with T.Env() as env:
        env.agent(T.FakeAgent([T.state()]))
        # exercise the REAL ledger writer so the format the tools write is what the brief reads
        M._ledger({'action': 'move_hero', 'args': {'hero': 0, 'x': 6, 'y': 7, 'z': 0},
                   'seq': 9, 'bridge_status': 'COMPLETED', 'status': 'completed',
                   'accepted': True, 'evidence': 'hero 0 tile [5,5]->[6,7]',
                   'action_id': 'real1', 'request_id': 'req-real1'})
        last = M.game_brief().get('last_action') or {}
        check('B9 the real _ledger format is readable by last_action',
              last.get('action') == 'move_hero' and last.get('action_id') == 'real1'
              and last.get('request_id') == 'req-real1' and last.get('status') == 'completed'
              and isinstance(last.get('age_s'), float), str(last))

    with T.Env() as env:
        st = T.state()
        st['making_turn'] = True
        env.agent(T.FakeAgent([st]))
        check('B10 making_turn is passed through', M.game_brief().get('making_turn') is True, '')
    with T.Env() as env:
        env.agent(T.FakeAgent([T.state()]))
        check('B10b an old state without making_turn reports None',
              M.game_brief().get('making_turn') is None, '')

    with T.Env() as env:
        fake = env.agent(T.FakeAgent([T.state()]))
        M.game_brief()
        check('B11 game_brief is read-only (no bridge command was sent)', fake.sent == [],
              str(fake.sent))


# ------------------------------------------------------------------ C: compatibility
def test_compatibility() -> None:
    old = T.state(day=3, heroes=[T.make_hero(0, (2, 2))],
                  battle={'active': 5, 'my_side': 0, 'active_stack': 5,
                          'stacks': [T.make_stack(5, 0, 10, (1, 1))]})
    with T.Env() as env:
        env.agent(T.FakeAgent([old]))
        observed = M.game_observe()
        state = observed.get('state') or {}
        check('C1 an old state keeps the previous observe shape',
              observed.get('ok') is True and state.get('day') == 3
              and state.get('recent_info') == [] and state.get('heroes')[0].get('i') == 0,
              str({k: state.get(k) for k in ('day', 'recent_info')}))

    query_state = T.state(query={'id': 7, 'type': 'level_up', 'text': 'hero levelled up',
                                 'can_cancel': False, 'primary': 'ATTACK',
                                 'options': [{'index': 0, 'type': 'SEC_SKILL', 'sub': 6}]})
    with T.Env() as env:
        env.agent(T.FakeAgent([query_state]))
        observed = M.game_observe()
        check('C2 observe still returns the current decision points',
              any(point.get('type') == 'query_opened'
                  for point in observed.get('decisions') or []),
              str([point.get('type') for point in observed.get('decisions') or []]))
        brief = M.game_brief()
        check('C3 the brief keeps its compact existing keys',
              {'ok', 'day', 'status', 'gold', 'heroes', 'towns', 'battle', 'decisions',
               'players', 'enemies', 'objects', 'observed', 'identity', 'memory'}
              <= set(brief), str(sorted(brief)))

    with T.Env() as env:
        env.agent(T.FakeAgent([T.state(in_game=True)]))
        digest = M._digest(T.state())
        check('C4 no-info state stays byte-shape compatible for old consumers',
              set(digest) >= {'day', 'status', 'resources', 'query', 'battle_active',
                              'heroes', 'towns', 'objects', 'recent_info'},
              str(sorted(digest)))


def main() -> int:
    test_recent_info_projection()
    test_brief_recovery_surface()
    test_compatibility()
    failed = [item for item in RESULTS if not item[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
