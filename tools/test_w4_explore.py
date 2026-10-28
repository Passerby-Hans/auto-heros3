# -*- coding: utf-8 -*-
"""Offline regression tests for the W4 observation/exploration batch.

Covered:
  W4-1 game_brief keeps full XYZ, lists visible enemy heroes/towns, reports observation age
  W4-2 game_brief filters: hero origin, radius, limit, kinds
  W4-3 path_preview parses the bridge probe (reachable/turns/cost/len/teleport/embark) and is
       read-only: no lease needed, no ledger entry, no unresolved marker
  W4-4 move verifier compares full XYZ (same XY on another layer is NOT completed) and reports
       the actual from->to positions
  W4-5 source guards: rich probe reply, brief enemies/observed, path_preview tool, kill fallback
Run: python -X utf8 tools/test_w4_explore.py
Exit code 0 only when every check passes.
"""
from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))
import mcp_server_v2 as M  # noqa: E402
from test_action_contract import Env, FakeAgent, bridge_ok, state  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition, detail: str = '') -> None:
    RESULTS.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


def explore_state() -> dict:
    st = state(
        heroes=[{'index': 0, 'name': 'Hero', 'pos': [5, 5, 1], 'visitable': [4, 5, 1],
                 'movement': 1000, 'army': [{'slot': 0, 'creature': 1, 'count': 3}]}],
        towns=[{'index': 0, 'name': 'Home', 'pos': [4, 4, 1], 'built': [],
                'build_options': [], 'dwellings': [], 'garrison': [],
                'available_heroes': []}])
    st['player'] = 0
    st['ts_wall'] = int(time.time() * 1000) - 1500
    st['objects'] = [
        {'id': 1, 'kind': 'creature', 'type': 'monster', 'name': 'Wolf',
         'pos': [6, 5, 1], 'visitable': [5, 5, 1], 'hover': '很少（1~2）'},
        {'id': 2, 'kind': 'hero', 'type': 'hero', 'name': 'EnemyHero',
         'pos': [8, 7, 0], 'visitable': [7, 7, 0], 'owner': 1},
        {'id': 3, 'kind': 'town', 'type': 'town', 'name': 'EnemyTown',
         'pos': [9, 9, 0], 'visitable': [9, 8, 0], 'owner': 1},
        {'id': 4, 'kind': 'object', 'type': 'resource', 'name': 'Gold',
         'pos': [20, 19, 0], 'visitable': [20, 20, 0]},
    ]
    return st


def probe_reply(line: str):
    return types.SimpleNamespace(seq=1, status='COMPLETED', line=line, detail='')


# ------------------------------------------------------------------ W4-1/2: brief
def test_brief_observation() -> None:
    with Env() as env:
        env.agent(FakeAgent([explore_state()]))
        brief = M.game_brief()
        check('W4-1a hero positions keep x,y,z (actual pos, not visitable offset)',
              brief['heroes'][0]['pos'] == [5, 5, 1],
              str(brief['heroes'][0]['pos']))
        check('W4-1a2 the interaction tile (visitable) is exposed separately',
              brief['heroes'][0]['visitable'] == [4, 5, 1],
              str(brief['heroes'][0].get('visitable')))
        check('W4-1b town positions are present with z',
              brief['towns'][0]['pos'] == [4, 4, 1], str(brief['towns'][0].get('pos')))
        enemy_names = {enemy['name'] for enemy in brief.get('enemies') or []}
        check('W4-1c visible enemy hero/town are reported with owner+pos',
              enemy_names == {'EnemyHero', 'EnemyTown'}
              and all(len(enemy['pos']) == 3 for enemy in brief.get('enemies') or [])
              and {e['name']: e['pos'] for e in brief.get('enemies') or []}.get('EnemyHero')
              == [8, 7, 0],
              str(brief.get('enemies')))
        check('W4-1d observation timestamp/age are reported',
              isinstance((brief.get('observed') or {}).get('ts_wall'), int)
              and (brief.get('observed') or {}).get('age_s') is not None
              and (brief['observed']['age_s'] or 0) >= 0.5,
              str(brief.get('observed')))
        monster = next((o for o in brief['objects'] if o['id'] == 1), None)
        check('W4-1e object positions keep z and dz', monster is not None
              and monster['pos'] == [6, 5, 1] and monster['dz'] == 0, str(monster))

    with Env() as env:
        env.agent(FakeAgent([explore_state()]))
        near = M.game_brief(radius=3)
        check('W4-2a radius filters objects by live distance',
              [o['id'] for o in near['objects']] == [1],
              str([o['id'] for o in near['objects']]))
        only_monsters = M.game_brief(kinds='monster')
        check('W4-2b kinds filter on kind/type',
              [o['kind'] for o in only_monsters['objects']] == ['creature'],
              str([o['kind'] for o in only_monsters['objects']]))
        limited = M.game_brief(limit=1)
        check('W4-2c limit clamps the object list but keeps the full count',
              len(limited['objects']) == 1 and limited.get('object_count', 0) > 1,
              f"objects={len(limited['objects'])} count={limited.get('object_count')}")
        bad = M.game_brief(limit=0)
        check('W4-2d invalid filters are rejected', bad.get('ok') is False,
              str(bad.get('error')))


# ------------------------------------------------------------------ W4-3: path preview
def test_path_preview() -> None:
    with Env() as env:
        env.agent(FakeAgent([state()], results=[
            probe_reply('seq=1 OK probe 5,6,0=1 turns=2 cost=300 len=5 teleport=1 embark=0')]))
        res = M.path_preview(hero=0, x=5, y=6, z=0)
        check('W4-3a reachable preview parses turns/cost/len/teleport',
              res.get('ok') is True and res.get('reachable') is True
              and res.get('turns') == 2 and res.get('cost') == 300
              and res.get('path_len') == 5 and res.get('teleport') is True
              and res.get('embark') is False, str(res)[:240])
    with Env() as env:
        env.agent(FakeAgent([state()], results=[
            probe_reply('seq=1 OK probe 9,9,0=0')]))
        res = M.path_preview(hero=0, x=9, y=9, z=0)
        check('W4-3b unreachable preview is reported as reachable=False',
              res.get('ok') is True and res.get('reachable') is False
              and res.get('turns') is None, str(res)[:200])
    with Env() as env:
        res = M.path_preview(hero=0, x=-1, y=6, z=0)
        check('W4-3c invalid coordinates rejected before touching the agent',
              res.get('ok') is False and 'range' in str(res.get('error')),
              str(res.get('error')))
    with Env() as env:
        env.restore_real_lease()
        M._lease_token = None                      # no lease on purpose
        env.agent(FakeAgent([state()], results=[
            probe_reply('seq=1 OK probe 5,6,0=1 turns=1 cost=100 len=2 teleport=0 embark=0')]))
        res = M.path_preview(hero=0, x=5, y=6, z=0)
        check('W4-3d read-only preview needs no lease', res.get('ok') is True, str(res)[:160])
        check('W4-3e preview writes no ledger entry / unresolved marker',
              not (env.root / 'state' / 'actions.jsonl').exists()
              and not (env.root / 'state' / 'unresolved.json').exists(),
              'no side effects')


# ------------------------------------------------------------------ W4-4: move verifier XYZ
def hero_at(x, y, z, visitable=None) -> dict:
    visitable = visitable or [x, y, z]
    return {'index': 0, 'name': 'Hero', 'pos': [x, y, z], 'visitable': visitable,
            'movement': 900, 'army': [{'slot': 0, 'creature': 1, 'count': 3}]}


def test_move_verifier_xyz() -> None:
    with Env() as env:
        # same `visitable` offset in both states; only the actual pos layer differs
        env.agent(FakeAgent([state(heroes=[hero_at(5, 5, 0, [4, 5, 0])]),
                             state(heroes=[hero_at(5, 5, 1, [4, 5, 1])])]))
        res = M.game_act('move_hero', {'hero': 0, 'x': 5, 'y': 5, 'z': 0})
        check('W4-4a same XY on another layer is NOT completed',
              res.get('status') == 'submitted'
              and 'target [5, 5, 0]' in str(res.get('evidence')),
              str(res.get('evidence')))
    with Env() as env:
        env.agent(FakeAgent([state(heroes=[hero_at(5, 5, 1, [4, 5, 1])]),
                             state(heroes=[hero_at(7, 5, 1, [6, 5, 1])])]))
        res = M.game_act('move_hero', {'hero': 0, 'x': 6, 'y': 5, 'z': 1})
        check('W4-4b exact XYZ is completed and the evidence shows tile from->to',
              res.get('status') == 'completed'
              and 'tile [4, 5, 1]->[6, 5, 1]' in str(res.get('evidence')),
              str(res.get('evidence')))


# ------------------------------------------------------------------ W4-5: source guards
def test_source_guards() -> None:
    bridge = (ROOT / 'platform' / 'vcmi-patch' / 'client' / 'AgentBridge.cpp').read_text(
        encoding='utf-8')
    server = (ROOT / 'tools' / 'mcp_server_v2.py').read_text(encoding='utf-8')
    agent = (ROOT / 'tools' / 'vcmi_agent.py').read_text(encoding='utf-8')
    check('W4-5a probe reply carries turns/cost/len/teleport/embark',
          '" turns="' in bridge and '" cost="' in bridge and '" teleport="' in bridge
          and '" embark="' in bridge and 'path.lastNode()' in bridge,
          'rich probe reply')
    check('W4-5b brief exposes enemies/observed and a hero filter',
          "'enemies': []" in server and "'observed': {'ts_wall'" in server
          and 'def game_brief(hero:' in server,
          'brief enrichment')
    check('W4-5c path_preview tool exists and is registered',
          'def path_preview(' in server and 'agent.execute(f\'probe {hero}' in server,
          'path_preview')
    check('W4-5d move verifier compares full XYZ',
          "position[:3] == target[:3]" in server
          and "int(args.get('z') or 0)" in server,
          'XYZ comparison')
    check('W4-5e stop falls back to a psutil kill when taskkill fails',
          'fallback_used' in agent and 'child.kill()' in agent and 'p.kill()' in agent,
          'kill fallback')


def main() -> int:
    test_brief_observation()
    test_path_preview()
    test_move_verifier_xyz()
    test_source_guards()
    failed = [item for item in RESULTS if not item[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
