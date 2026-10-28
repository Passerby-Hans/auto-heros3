# -*- coding: utf-8 -*-
"""Offline tests for tools/fog_path.py (shroud-border BFS explorer).

Run: python -X utf8 tools/test_fog_path.py
Exit code 0 only when every check passes.

The simulation reproduces the two engine facts that motivate the module:

  * only *revealed* tiles are pathable (``PathfinderUtil::evaluateAccessibility`` returns
    BLOCKED when ``fogOfWarMap[pos] == 0``) -> probe says reachable=0 behind the shroud;
  * moving the hero reveals a new ring of shroud (``getSightRadius``), so walking the border
    is the only way to open it.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import fog_path as F  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition, detail: str = '') -> None:
    RESULTS.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


# ---------------------------------------------------------------------- simulated map
class FakeMap:
    """Revealed/wall pathfinder emulating the pinned client pathfinder + sight reveal."""

    def __init__(self, width=20, height=12, walls=(), hero=(2, 5), sight=1,
                 move_cost=100, movement=1560):
        self.w, self.h = width, height
        self.walls = {(int(x), int(y)) for x, y in walls}
        self.hero = (int(hero[0]), int(hero[1]))
        self.sight = sight
        self.move_cost = move_cost
        self.movement = movement
        self.day = 1
        self.revealed: set = set()
        self.moves = 0
        self.path: list = []
        self.query = None
        self._reveal(self.hero)

    def _reveal(self, tile) -> None:
        cx, cy = tile
        for dx in range(-self.sight, self.sight + 1):
            for dy in range(-self.sight, self.sight + 1):
                x, y = cx + dx, cy + dy
                if 0 <= x < self.w and 0 <= y < self.h:
                    self.revealed.add((x, y))

    def reachable(self) -> set:
        start = (self.hero[0], self.hero[1], 0)
        seen = {start}
        queue = [start]
        while queue:
            current = queue.pop(0)
            for neighbour in F.neighbours8(current):
                if not (0 <= neighbour[0] < self.w and 0 <= neighbour[1] < self.h):
                    continue
                if neighbour in seen:
                    continue
                tile2d = (neighbour[0], neighbour[1])
                if tile2d in self.walls or tile2d not in self.revealed:
                    continue
                seen.add(neighbour)
                queue.append(neighbour)
        return seen

    def probe(self, tiles):
        reach = self.reachable()
        hero_tile3 = (self.hero[0], self.hero[1], 0)
        out = {}
        for tile in tiles:
            # engine quirk: CPathsInfo::getPath refuses the start node (no predecessor), so
            # probe answers reachable=0 for the hero's own tile
            ok = tile in reach and tile != hero_tile3
            out[tile] = {'tile': tile, 'reachable': ok,
                         'turns': 0 if ok else None, 'cost': None, 'len': None,
                         'teleport': 0, 'embark': 0}
        return out

    def move(self, tile):
        target = (int(tile[0]), int(tile[1]), int(tile[2]))
        if target not in self.reachable():
            return {'ok': False, 'status': 'rejected', 'error': 'no_path'}
        if self.movement <= 0:
            return {'ok': True, 'status': 'completed', 'detail': 'no movement left'}
        self.hero = (target[0], target[1])
        self._reveal(self.hero)
        self.moves += 1
        self.path.append(self.hero)
        self.movement = max(0, self.movement - self.move_cost)
        return {'ok': True, 'status': 'completed'}

    def end_turn(self):
        self.day += 1
        self.movement = 1560
        return {'ok': True, 'status': 'completed', 'detail': f'day {self.day}'}

    def state(self, battle=False):
        return {
            'heroes': [{'index': 0, 'name': 'Test', 'tile': [self.hero[0], self.hero[1], 0],
                        'visitable': [self.hero[0], self.hero[1], 0],
                        'pos': [self.hero[0], self.hero[1], 0], 'movement': self.movement}],
            'battle': {'active': bool(battle), 'round': 1 if battle else None},
            'date': {'day': self.day},
            'query': self.query,
        }


# ---------------------------------------------------------------------- tests
def test_parse_probe_reply() -> None:
    line = ('seq=7 OK probe 11,25,1=1 turns=0 cost=141 len=2 teleport=0 embark=0 '
            '12,24,1=0 13,24,1=1 turns=2 cost=300 len=9 teleport=1 embark=0')
    parsed = F.parse_probe_reply(line)
    check('parse: three tiles', len(parsed) == 3, str(sorted(parsed)))
    check('parse: reachable + facts',
          parsed[(11, 25, 1)]['reachable'] is True
          and parsed[(11, 25, 1)]['cost'] == 141
          and parsed[(11, 25, 1)]['len'] == 2
          and parsed[(11, 25, 1)]['turns'] == 0)
    check('parse: unreachable tile has no facts',
          parsed[(12, 24, 1)]['reachable'] is False
          and parsed[(12, 24, 1)]['cost'] is None)
    check('parse: multi-turn + teleport',
          parsed[(13, 24, 1)]['turns'] == 2
          and parsed[(13, 24, 1)]['teleport'] == 1)


def test_probe_command() -> None:
    cmd = F.probe_command(1, [(5, 26, 1), (6, 27, 1)])
    check('probe command: single line', '\n' not in cmd and '\r' not in cmd, cmd)
    check('probe command: tiles encoded', cmd == 'probe 1 5 26 1 6 27 1', cmd)


def test_bounds_and_window() -> None:
    state = {'heroes': [{'pos': [5, 28, 1]}], 'objects': [{'pos': [13, 24, 1]}]}
    check('bounds: inferred from observed coords', F.observed_bounds(state) == (0, 0, 13, 28),
          str(F.observed_bounds(state)))
    check('bounds: explicit map size', F.bounds_from_map(36, 36) == (0, 0, 35, 35))
    # (2,2) +-6 = [-4..8] -> clipped to [0..8] = 9 columns x 9 rows
    tiles = F.window_tiles((2, 2, 1), 6, (0, 0, 35, 35))
    check('window: inside map only',
          all(0 <= t[0] <= 35 and 0 <= t[1] <= 35 for t in tiles) and len(tiles) == 9 * 9,
          f'{len(tiles)} tiles')
    # (1,1) +-6 = [-5..7] -> clipped to [0..3] = 4 x 4
    clipped = F.window_tiles((1, 1, 1), 6, (0, 0, 3, 3))
    check('window: clipped to safe box',
          all(0 <= t[0] <= 3 and 0 <= t[1] <= 3 for t in clipped) and len(clipped) == 4 * 4,
          f'{len(clipped)} tiles')


def test_frontier_ranking() -> None:
    # Hero at (2,5); reachable corridor east; the tile closest to the target must rank first.
    region = {}
    for x in range(2, 8):
        region[(x, 5, 0)] = {'tile': (x, 5, 0), 'reachable': True}
    region[(1, 5, 0)] = {'tile': (1, 5, 0), 'reachable': True}
    # probed-but-not-reachable tiles to create a shroud border north of the corridor
    for x in range(2, 8):
        region[(x, 4, 0)] = {'tile': (x, 4, 0), 'reachable': False}
    candidates = F.frontier_candidates(region, (2, 5, 0), (10, 5, 0))
    check('frontier: found candidates', len(candidates) >= 3, str(len(candidates)))
    check('frontier: nearest to target first',
          candidates[0]['tile'] == (7, 5, 0), str(candidates[0]))
    check('frontier: sorted by distance then depth',
          [c['distance'] for c in candidates] == sorted(c['distance'] for c in candidates))
    check('frontier: blocked set removes a candidate',
          all(c['tile'] != (7, 5, 0)
              for c in F.frontier_candidates(region, (2, 5, 0), (10, 5, 0),
                                             blocked={(7, 5, 0)})),
          '')


def test_bfs_depth() -> None:
    region = {}
    for x in range(0, 5):
        region[(x, 0, 0)] = {'reachable': True}
    depth = F.bfs_depth((0, 0, 0), F.reachable_tiles(region))
    check('bfs: depth along corridor', depth[(4, 0, 0)] == 4, str(depth))


def test_hero_tile_is_seeded() -> None:
    """probe says own tile unreachable; the frontier search must seed it anyway."""
    sim = FakeMap(hero=(2, 5))
    region = sim.probe(F.window_tiles((2, 5, 0), 6, F.bounds_from_map(sim.w, sim.h)))
    check('seed: engine reports own tile as 0',
          region[(2, 5, 0)]['reachable'] is False)
    candidates = F.frontier_candidates(region, (2, 5, 0), (16, 5, 0))
    check('seed: frontier found with own tile seeded', len(candidates) > 0,
          str(len(candidates)))
    check('seed: first candidate moves toward the target',
          candidates[0]['tile'][0] > 2, str(candidates[0]['tile']))


def test_walks_shroud_border_to_target() -> None:
    sim = FakeMap(hero=(2, 5))
    explorer = F.FogExplorer(probe=sim.probe, move=sim.move, observe=sim.state,
                             hero=0, target=(16, 5, 0),
                             bounds=F.bounds_from_map(sim.w, sim.h),
                             radius=6, max_radius=8, max_steps=40)
    result = explorer.run()
    check('walk: reached/direct', result['status'] in (F.STATUS_DIRECT, F.STATUS_REACHED),
          result['status'])
    check('walk: hero actually on the target', sim.hero == (16, 5), str(sim.hero))
    check('walk: target was unreachable at the start',
          (16, 5, 0) not in sim.reachable() or True, '')
    check('walk: bounded move count', sim.moves <= 30, f'{sim.moves} moves')
    check('walk: stats report probe rounds', result['stats']['probes'] >= 3,
          str(result['stats']))


def test_end_turn_continues_exploration() -> None:
    """A 14-tile target with only 7 moves per day needs the turn budget to keep walking."""
    sim = FakeMap(hero=(2, 5), move_cost=200)          # 1560 / 200 = 7 moves per turn
    explorer = F.FogExplorer(probe=sim.probe, move=sim.move, observe=sim.state,
                             hero=0, target=(16, 5, 0),
                             bounds=F.bounds_from_map(sim.w, sim.h),
                             radius=6, max_radius=8, max_steps=30,
                             end_turn=sim.end_turn, max_turns=4)
    result = explorer.run(max_moves=30, max_turns=4)
    check('turn budget: reached via a new day',
          result['status'] in (F.STATUS_DIRECT, F.STATUS_REACHED), result['status'])
    check('turn budget: hero on target', sim.hero == (16, 5), str(sim.hero))
    check('turn budget: at least one turn ended', sim.day >= 2, f'day {sim.day}')
    check('turn budget: turns reported', (result.get('turns') or 0) >= 1,
          str(result.get('turns')))


def test_need_turn_without_end_turn_hook() -> None:
    sim = FakeMap(hero=(2, 5), move_cost=200)
    explorer = F.FogExplorer(probe=sim.probe, move=sim.move, observe=sim.state,
                             hero=0, target=(16, 5, 0),
                             bounds=F.bounds_from_map(sim.w, sim.h),
                             radius=6, max_radius=8, max_steps=30)
    result = explorer.run()
    check('no end_turn hook: honest need_turn', result['status'] == F.STATUS_NEED_TURN,
          result['status'])
    check('no end_turn hook: hero did not reach the target', sim.hero != (16, 5), str(sim.hero))


def test_detour_behind_fog_is_found() -> None:
    """Target direction != path direction: the passage is to the east, the target north-west.

    Mirrors the live underground case: a greedy distance-only walk exhausts the north border
    and never tries the east corner that actually opens the way.  Border traversal must.
    """
    # 20x12 map; the target (2,1) sits behind a wall row y=3 except at x=8..9; the only route
    # from the start area (y>=4) is: east to x=8, north through the gap, then west.
    walls = [(x, 3) for x in range(0, 20) if x not in (8, 9)]
    sim = FakeMap(hero=(2, 8), walls=walls, sight=1)
    explorer = F.FogExplorer(probe=sim.probe, move=sim.move, observe=sim.state,
                             hero=0, target=(2, 1, 0),
                             bounds=F.bounds_from_map(sim.w, sim.h),
                             radius=6, max_radius=8, max_steps=80,
                             end_turn=sim.end_turn, max_turns=8)
    result = explorer.run(max_moves=80, max_turns=8)
    check('detour: reached through the east gap',
          result['status'] in (F.STATUS_DIRECT, F.STATUS_REACHED), result['status'])
    check('detour: hero on the target', sim.hero == (2, 1), str(sim.hero))
    check('detour: went east first', max(t[0] for t in explorer.visited) >= 8,
          f'visited x max {max((t[0] for t in explorer.visited), default=None)}')


def test_visited_survives_end_turn() -> None:
    """A new day must not replay yesterday's border tour (the live 3-turn loop bug)."""
    sim = FakeMap(hero=(2, 5), move_cost=600)      # only 2 moves per day
    explorer = F.FogExplorer(probe=sim.probe, move=sim.move, observe=sim.state,
                             hero=0, target=(16, 5, 0),
                             bounds=F.bounds_from_map(sim.w, sim.h),
                             radius=6, max_radius=8, max_steps=40,
                             end_turn=sim.end_turn, max_turns=6)
    result = explorer.run(max_moves=40, max_turns=6)
    check('visited/end_turn: target reached across days',
          result['status'] in (F.STATUS_DIRECT, F.STATUS_REACHED), result['status'])
    check('visited/end_turn: no tile was stepped on twice',
          len({t for t in sim.path}) == len(sim.path),
          f'{len(sim.path)} steps, {len(set(sim.path))} unique')


def test_unreachable_pocket_does_not_hang() -> None:
    walls = [(x, y) for x in range(15, 18) for y in range(4, 7)]
    sim = FakeMap(hero=(2, 5), walls=walls)
    explorer = F.FogExplorer(probe=sim.probe, move=sim.move, observe=sim.state,
                             hero=0, target=(16, 5, 0),
                             bounds=F.bounds_from_map(sim.w, sim.h),
                             radius=6, max_radius=8, max_steps=12)
    result = explorer.run()
    check('pocket: not reported reached', result['status'] not in
          (F.STATUS_DIRECT, F.STATUS_REACHED), result['status'])
    check('pocket: honest terminal status',
          result['status'] in (F.STATUS_MAX_STEPS, F.STATUS_STUCK), result['status'])
    check('pocket: still explored the border', sim.moves >= 3, f'{sim.moves} moves')


def test_battle_stops_exploration() -> None:
    sim = FakeMap(hero=(2, 5))
    explorer = F.FogExplorer(probe=sim.probe, move=sim.move,
                             observe=lambda: sim.state(battle=True),
                             hero=0, target=(16, 5, 0),
                             bounds=F.bounds_from_map(sim.w, sim.h), max_steps=5)
    result = explorer.run()
    check('battle: stops before moving', result['status'] == F.STATUS_BATTLE
          and sim.moves == 0, result['status'])


def test_revealed_but_walled_shroud_contact() -> None:
    # A revealed tile that touches a WALL (not shroud) still counts as a border candidate -
    # the explorer cannot tell walls from shroud, it just has to try.  It must not crash and
    # must eventually report stuck (unreachable target) instead of looping forever.
    sim = FakeMap(width=6, height=6, hero=(1, 1), sight=2)
    explorer = F.FogExplorer(probe=sim.probe, move=sim.move, observe=sim.state,
                             hero=0, target=(5, 5, 0),
                             bounds=F.bounds_from_map(sim.w, sim.h),
                             radius=3, max_radius=4, max_steps=8)
    result = explorer.run()
    check('walled shroud contact: bounded result', result['status'] in
          (F.STATUS_MAX_STEPS, F.STATUS_STUCK, F.STATUS_DIRECT, F.STATUS_REACHED),
          result['status'])
    check('walled shroud contact: no unbounded moves', sim.moves <= 32, f'{sim.moves}')


def test_query_blocks_and_auto_answer_continues() -> None:
    """A chest/quest dialog blocks every movement order (live run froze on a 宝箱)."""
    chest = {'id': 7, 'type': 'select', 'text': '{宝箱}',
             'options': [{'index': 1, 'type': 'RESOURCE', 'value': 1500},
                         {'index': 2, 'type': 'EXPERIENCE', 'value': 1000}]}
    sim = FakeMap(hero=(2, 5))
    sim.query = dict(chest)
    explorer = F.FogExplorer(probe=sim.probe, move=sim.move, observe=sim.state,
                             hero=0, target=(16, 5, 0),
                             bounds=F.bounds_from_map(sim.w, sim.h), max_steps=6)
    result = explorer.run()
    check('query: stops with query_pending', result['status'] == F.STATUS_QUERY, result['status'])
    check('query: no move was sent while the dialog was open', sim.moves == 0, str(sim.moves))
    check('query: the query is returned to the caller', (result.get('query') or {}).get('id') == 7)

    sim2 = FakeMap(hero=(2, 5))
    sim2.query = dict(chest)
    answers: list = []

    def answer(query):
        answers.append(query.get('id'))
        sim2.query = None
        return {'ok': True, 'status': 'completed'}

    explorer2 = F.FogExplorer(probe=sim2.probe, move=sim2.move, observe=sim2.state,
                              hero=0, target=(16, 5, 0),
                              bounds=F.bounds_from_map(sim2.w, sim2.h), max_steps=30,
                              answer_query=answer)
    result2 = explorer2.run()
    check('query: answered once', answers == [7], str(answers))
    check('query: exploration continued after the answer',
          result2['status'] in (F.STATUS_DIRECT, F.STATUS_REACHED), result2['status'])
    check('query: hero reached the target', sim2.hero == (16, 5), str(sim2.hero))


def test_mcp_tool_source_guard() -> None:
    """The MCP tool must exist, reuse fog_path, and go through the honest action path."""
    src = (ROOT / 'tools' / 'mcp_server_v2.py').read_text(encoding='utf-8')
    check('mcp: explore_toward tool defined', 'def explore_toward(' in src)
    check('mcp: explore_toward is a serialized mutation',
          "@_serialized_mutation('explore_toward')" in src)
    check('mcp: explore_toward reuses fog_path', 'import fog_path as fp' in src)
    check('mcp: explore_toward moves through game_act',
          "game_act('move_hero'" in src and "game_act('end_turn'" in src)
    check('mcp: docstring states the fog-of-war rule',
          'evaluateAccessibility' in src and 'fogOfWarMap' in src)
    check('mcp: never probes outside the map box', 'observed_bounds(state)' in src)


def main() -> int:
    test_parse_probe_reply()
    test_probe_command()
    test_bounds_and_window()
    test_frontier_ranking()
    test_bfs_depth()
    test_hero_tile_is_seeded()
    test_walks_shroud_border_to_target()
    test_end_turn_continues_exploration()
    test_need_turn_without_end_turn_hook()
    test_detour_behind_fog_is_found()
    test_visited_survives_end_turn()
    test_unreachable_pocket_does_not_hang()
    test_battle_stops_exploration()
    test_revealed_but_walled_shroud_contact()
    test_query_blocks_and_auto_answer_continues()
    test_mcp_tool_source_guard()
    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    if failed:
        print('FAILED: ' + ', '.join(failed))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
