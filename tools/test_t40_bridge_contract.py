# -*- coding: utf-8 -*-
r"""T40/C11 offline contract tests: recruit_hero deterministic refusal, anchor/tile fields,
formal open_hero MCP action.

OFFLINE ONLY - no game client, no bridge process: every case drives mcp_server_v2 with a
scripted FakeAgent (see test_action_contract.py) and an isolated state dir (T.Env).

Covered:
  A  bridge command shape: ACTION_SPECS['open_hero'] -> `open_hero <hero>`, validation
  B  recruit_hero: the bridge's new ERR recruit_hero_refused <reason> is reported as rejected
     (and the MCP precondition still refuses a visiting hero before sending anything)
  C  anchor/tile: digest/brief pass the canonical `tile` through and keep the old shape for
     fixtures that predate it; interactions (position helpers) prefer tile over pos
  D  open_hero verifier: ui_tree CHeroWindow -> completed, window LEFT OPEN (no close command);
     no CHeroWindow -> rejected; bridge ERR -> rejected; ui_tree unavailable -> unknown;
     T42/F1: an UNKNOWN/REJECTED query or a NOT-REFRESHED ui_tree.json is unknown (never
     completed from a stale file left by an older command)
"""
from __future__ import annotations

import json
import sys
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


def write_ui_tree(root: Path, classes: list) -> None:
    """Fake bridge ui_tree.json in the isolated state dir."""
    path = root / 'state' / 'ui_tree.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    windows = [{'root': {'cls': cls, 'x': 0, 'y': 0, 'w': 800, 'h': 600, 'children': []}}
               for cls in classes]
    path.write_text(json.dumps({'windows': windows}), encoding='utf-8')


def town_fixture(**extra) -> dict:
    town = {'index': 0, 'id': 500, 'name': 'Home', 'pos': [4, 4, 0], 'visitable': [4, 5, 0],
            'anchor': [4, 4, 0], 'tile': [4, 5, 0], 'built': [7], 'build_options': [],
            'dwellings': [], 'garrison': [],
            'available_heroes': [{'index': 0, 'name': 'Caitlin', 'hero_type': 3}],
            'hero_visiting': False}
    town.update(extra)
    return town


# ------------------------------------------------------------------ A: command shape
def test_open_hero_spec() -> None:
    check('T40-A1 open_hero is a whitelisted action with a single hero arg',
          M.ACTION_SPECS.get('open_hero') == ('open_hero', ('hero',)),
          str(M.ACTION_SPECS.get('open_hero')))
    check('T40-A2 open_hero builds the bridge command line',
          M._bridge_line('open_hero', {'hero': 2}) == 'open_hero 2',
          M._bridge_line('open_hero', {'hero': 2}))
    with T.Env() as env:
        fake = env.agent(T.FakeAgent([T.state()]))
        bad = M.game_act('open_hero', {})
        check('T40-A3 open_hero without hero is rejected before the bridge',
              bad.get('ok') is False and bad.get('sent') is False
              and 'missing args' in str(bad.get('error')) and fake.sent == [],
              str(bad.get('error')))
        bad = M.game_act('open_hero', {'hero': 16})
        check('T40-A4 hero index out of range is rejected before the bridge',
              bad.get('ok') is False and bad.get('sent') is False
              and 'range' in str(bad.get('error')), str(bad.get('error')))


# ------------------------------------------------------------------ B: recruit_hero refusals
def test_recruit_hero_refusals() -> None:
    state = T.state(gold=12000,
                    heroes=[T.make_hero(0, (1, 1))],
                    towns=[town_fixture()])
    with T.Env() as env:
        fake = env.agent(T.FakeAgent(
            [state, state],
            results=[T.bridge_err(1, 'recruit_hero_refused visiting_hero name=Caitlin')]))
        res = M.game_act('recruit_hero', {'town': 0, 'index': 0})
        check('T40-B1 bridge ERR recruit_hero_refused becomes status=rejected',
              res.get('status') == 'rejected' and res.get('ok') is False
              and 'recruit_hero_refused visiting_hero' in str(res.get('evidence')), str(res)[:240])
        check('T40-B2 the refusal evidence keeps the bridge line',
              'recruit_hero_refused' in str(res.get('bridge_line')), str(res.get('bridge_line')))
        check('T40-B3 the refused command was actually sent (precondition passed)',
              fake.sent == ['recruit_hero 0 0'], str(fake.sent))

    visiting = T.state(gold=12000,
                       heroes=[T.make_hero(0, (4, 5))],
                       towns=[town_fixture(hero_visiting=True)])
    with T.Env() as env:
        fake = env.agent(T.FakeAgent([visiting, visiting]))
        res = M.game_act('recruit_hero', {'town': 0, 'index': 0})
        check('T40-B4 the MCP precondition still refuses a visiting hero before sending',
              res.get('status') == 'rejected' and res.get('sent') is False
              and 'visiting hero' in str(res.get('evidence')) and fake.sent == [],
              str(res.get('evidence'))[:200])

    no_tavern = T.state(gold=12000, heroes=[T.make_hero(0, (1, 1))],
                        towns=[town_fixture(hero_visiting=False)])
    with T.Env() as env:
        env.agent(T.FakeAgent([no_tavern, no_tavern],
                              results=[T.bridge_err(1, 'recruit_hero_refused no_tavern')]))
        res = M.game_act('recruit_hero', {'town': 0, 'index': 0})
        check('T40-B5 any recruit_hero_refused reason is a deterministic rejection',
              res.get('status') == 'rejected' and 'no_tavern' in str(res.get('evidence')),
              str(res.get('evidence'))[:200])

    # Success path keeps the old async verification semantics (gold spent -> submitted; a new
    # hero -> completed).  The bridge now also reports cost/gold_before on the OK line.
    before = T.state(gold=12000, heroes=[T.make_hero(0, (1, 1))], towns=[town_fixture()])
    after = T.state(gold=9500, heroes=[T.make_hero(0, (1, 1)),
                                       {**T.make_hero(1, (4, 5)), 'name': 'Caitlin'}],
                    towns=[town_fixture()])
    with T.Env() as env:
        env.agent(T.FakeAgent(
            [before, after],
            results=[T.bridge_ok(1, 'recruit_hero town=0 index=0 name=Caitlin cost=2500 '
                                     'gold_before=12000')]))
        res = M.game_act('recruit_hero', {'town': 0, 'index': 0})
        check('T40-B6 a successful hire is verified from the new hero + gold diff',
              res.get('status') == 'completed' and 'Caitlin' in str(res.get('evidence')),
              str(res.get('evidence'))[:200])


# ------------------------------------------------------------------ C: anchor/tile
def test_anchor_tile_fields() -> None:
    hero = {'index': 0, 'name': 'Adela', 'pos': [34, 33, 0], 'visitable': [32, 33, 0],
            'anchor': [34, 33, 0], 'tile': [32, 33, 0], 'movement': 1500, 'army': []}
    digest = (M._digest(T.state(heroes=[hero])).get('heroes') or [{}])[0]
    check('T40-C1 digest passes the canonical hero tile through',
          digest.get('tile') == [32, 33, 0] and digest.get('pos') == [34, 33, 0],
          str(digest))
    brief_hero = (M._hero_brief(digest))
    check('T40-C2 game_brief hero view carries the compact tile',
          brief_hero.get('tile') == [32, 33, 0] and brief_hero.get('pos') == [34, 33, 0],
          str(brief_hero)[:200])
    old = {'index': 1, 'name': 'OldHero', 'visitable': [2, 2], 'movement': 900, 'army': []}
    old_digest = M._hero_digest(old)
    check('T40-C3 an old/partial hero digest keeps its exact previous shape',
          old_digest == {'i': 1, 'name': 'OldHero', 'pos': [2, 2], 'visitable': [2, 2],
                         'movement': 900, 'army': []}, str(old_digest))

    objects = [{'id': 500, 'kind': 'town', 'type': 'town', 'name': 'Home',
                'pos': [4, 4, 0], 'visitable': [4, 5, 0],
                'anchor': [4, 4, 0], 'tile': [4, 5, 0]}]
    st = T.state(heroes=[hero], towns=[town_fixture()])
    st['objects'] = objects
    digest = M._digest(st)
    obj = (digest.get('objects') or [{}])[0]
    town = (digest.get('towns') or [{}])[0]
    check('T40-C4 object digest carries anchor+tile next to pos/visitable',
          obj.get('anchor') == [4, 4, 0] and obj.get('tile') == [4, 5, 0]
          and obj.get('pos') == [4, 4, 0] and obj.get('visitable') == [4, 5, 0], str(obj))
    check('T40-C5 town digest carries the interaction tile',
          town.get('tile') == [4, 5, 0], str(town))
    old_obj_state = T.state()
    old_obj_state['objects'] = [{'id': 8, 'kind': 'object', 'type': 'resource',
                                 'name': 'Gold', 'pos': [9, 9, 0], 'visitable': [9, 8, 0]}]
    old_obj = (M._digest(old_obj_state).get('objects') or [{}])[0]
    check('T40-C6 an old object digest does not invent anchor/tile keys',
          'anchor' not in old_obj and 'tile' not in old_obj, str(old_obj))

    # interaction helpers use the canonical tile first (same value as visitable on T40 clients).
    pos = M._hero_position(T.state(heroes=[hero]), 0)
    town_pos = M._town_position(st, 0)
    check('T40-C8 hero/town interaction helpers prefer tile (== visitable)',
          pos == (32, 33, 0) and town_pos == (4, 5, 0), f'hero={pos} town={town_pos}')

    # tile-only fixture (future nodes): helpers still resolve it.
    tile_only = {'index': 0, 'name': 'H', 'tile': [7, 8, 0], 'movement': 100, 'army': []}
    check('T40-C9 a tile-only hero resolves its interaction tile',
          M._hero_position(T.state(heroes=[tile_only]), 0) == (7, 8, 0), '')


# ------------------------------------------------------------------ D: open_hero verifier
def test_open_hero_verifier() -> None:
    st = T.state(heroes=[T.make_hero(0, (5, 5))])

    with T.Env() as env:
        holder = {'classes': ['class AdventureMapInterface', 'class CHeroWindow']}

        def on_execute(line: str) -> None:
            if line == 'ui_tree':
                write_ui_tree(env.root, holder['classes'])

        fake = env.agent(T.FakeAgent([st, st], results=[
            T.bridge_ok(1, 'open_hero 0 name=Adela'),
            T.bridge_ok(2, 'ui_tree windows=2 (see ui_tree.json)')],
            on_execute=on_execute))
        res = M.game_act('open_hero', {'hero': 0})
        check('T40-D1 open_hero verifies CHeroWindow through ui_tree -> completed',
              res.get('status') == 'completed' and res.get('ok') is True
              and 'CHeroWindow' in str(res.get('evidence')), str(res)[:240])
        check('T40-D2 the hero window is LEFT OPEN (verifier never pops it)',
              'close_window' not in ' '.join(fake.sent) and 'key Escape' not in ' '.join(fake.sent)
              and 'left open' in str(res.get('evidence')), str(fake.sent))
        check('T40-D3 only the command + one read-only ui_tree were sent',
              fake.sent == ['open_hero 0', 'ui_tree'], str(fake.sent))

    with T.Env() as env:
        fake = env.agent(T.FakeAgent(
            [st, st],
            results=[T.bridge_ok(1, 'open_hero 0 name=Adela'),
                     T.bridge_ok(2, 'ui_tree windows=1')],
            on_execute=lambda line: write_ui_tree(env.root, ['class AdventureMapInterface'])
            if line == 'ui_tree' else None))
        res = M.game_act('open_hero', {'hero': 0})
        check('T40-D4 a missing CHeroWindow is an honest rejection',
              res.get('status') == 'rejected' and 'no CHeroWindow' in str(res.get('evidence')),
              str(res.get('evidence'))[:200])

    with T.Env() as env:
        env.agent(T.FakeAgent([st, st],
                              results=[T.bridge_err(1, 'bad_hero_index')]))
        res = M.game_act('open_hero', {'hero': 3})
        check('T40-D5 a bridge ERR for open_hero stays rejected (terminal)',
              res.get('status') == 'rejected' and 'bad_hero_index' in str(res.get('evidence')),
              str(res.get('evidence'))[:200])

    with T.Env() as env:
        def broken_ui_tree(line: str) -> None:
            if line == 'ui_tree':
                raise RuntimeError('bridge gone')

        env.agent(T.FakeAgent([st, st], results=[
            T.bridge_ok(1, 'open_hero 0 name=Adela')],
            on_execute=broken_ui_tree))
        res = M.game_act('open_hero', {'hero': 0})
        check('T40-D6 an unreadable widget tree is unknown, never fabricated completed',
              res.get('status') == 'unknown' and res.get('ok') is False,
              str(res)[:200])


# ------------------------------------------------ D7-D10: T42/F1 stale ui_tree observation
def test_open_hero_stale_observation() -> None:
    """Audit F1: the verifier must bind the observation to the *current* ui_tree query."""
    st = T.state(heroes=[T.make_hero(0, (5, 5))])

    # A pre-existing file with CHeroWindow must not satisfy a failed query.
    with T.Env() as env:
        write_ui_tree(env.root, ['class AdventureMapInterface', 'class CHeroWindow'])
        path = env.root / 'state' / 'ui_tree.json'
        before = (path.stat().st_mtime_ns, path.read_bytes())
        fake = env.agent(T.FakeAgent([st, st], results=[
            T.bridge_ok(1, 'open_hero 0 name=Adela'),
            T.bridge_unknown(2)]))
        res = M.game_act('open_hero', {'hero': 0})
        after = (path.stat().st_mtime_ns, path.read_bytes())
        check('T40-D7 an UNKNOWN ui_tree query is unknown (old file not accepted)',
              res.get('status') == 'unknown' and res.get('ok') is False
              and 'UNKNOWN' in str(res.get('evidence'))
              and before == after, str(res.get('evidence'))[:220])
        check('T40-D7b the failed observation creates an unresolved gate',
              (env.root / 'state' / 'unresolved.json').exists())
        check('T40-D7c the open window is not closed on an unknown verdict',
              fake.sent == ['open_hero 0', 'ui_tree'], str(fake.sent))

    with T.Env() as env:
        write_ui_tree(env.root, ['class AdventureMapInterface', 'class CHeroWindow'])
        env.agent(T.FakeAgent([st, st], results=[
            T.bridge_ok(1, 'open_hero 0 name=Adela'),
            T.bridge_err(2, 'not_available')]))
        res = M.game_act('open_hero', {'hero': 0})
        check('T40-D8 a REJECTED ui_tree query is unknown (old file not accepted)',
              res.get('status') == 'unknown' and res.get('ok') is False
              and 'rejected' in str(res.get('evidence')).lower(), str(res.get('evidence'))[:220])

    # Bridge says OK but this command did not refresh ui_tree.json -> stale, never completed.
    with T.Env() as env:
        write_ui_tree(env.root, ['class AdventureMapInterface', 'class CHeroWindow'])
        env.agent(T.FakeAgent([st, st], results=[
            T.bridge_ok(1, 'open_hero 0 name=Adela'),
            T.bridge_ok(2, 'ui_tree windows=2')]))   # no on_execute -> file is not rewritten
        saved_wait = M._UI_TREE_REFRESH_WAIT
        M._UI_TREE_REFRESH_WAIT = 0.3
        try:
            res = M.game_act('open_hero', {'hero': 0})
        finally:
            M._UI_TREE_REFRESH_WAIT = saved_wait
        check('T40-D9 an OK query that left the ui_tree file unchanged is unknown (stale)',
              res.get('status') == 'unknown' and res.get('ok') is False
              and 'not refreshed' in str(res.get('evidence')), str(res.get('evidence'))[:220])

    # Fresh file (real refresh) with CHeroWindow -> completed (contrast for D9).
    with T.Env() as env:
        def refresh(line: str) -> None:
            if line == 'ui_tree':
                write_ui_tree(env.root, ['class AdventureMapInterface', 'class CHeroWindow'])

        env.agent(T.FakeAgent([st, st], results=[
            T.bridge_ok(1, 'open_hero 0 name=Adela'),
            T.bridge_ok(2, 'ui_tree windows=2')], on_execute=refresh))
        res = M.game_act('open_hero', {'hero': 0})
        check('T40-D10 a refreshed ui_tree with CHeroWindow is still completed',
              res.get('status') == 'completed' and 'CHeroWindow' in str(res.get('evidence')),
              str(res.get('evidence'))[:220])


def main() -> int:
    test_open_hero_spec()
    test_recruit_hero_refusals()
    test_anchor_tile_fields()
    test_open_hero_verifier()
    test_open_hero_stale_observation()
    failed = [item for item in RESULTS if not item[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
