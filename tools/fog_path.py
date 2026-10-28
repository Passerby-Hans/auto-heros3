# -*- coding: utf-8 -*-
"""Fog-frontier exploration pathfinding ("walk the border of the black shroud").

Why this exists
---------------
The adventure-map pathfinder that the pinned client uses for BOTH the hover arrows and the
move orders is ``CPlayerInterface::getPathsInfo`` -> ``CPathfinder`` with
``PathfinderUtil::evaluateAccessibility``, whose first rule is::

    if(!fow[pos])
        return EPathAccessibility::BLOCKED;

``fow`` is the player-team ``fogOfWarMap``: 0 = tile was never revealed (black shroud),
1 = revealed (permanently - ``CGameState::initFogOfWar`` / ``GameStatePackVisitor`` only ever
set it to 1).  Consequences that matter for planning:

  * an unrevealed tile is NOT pathable at all -> ``probe`` answers ``reachable=0`` and
    ``move_hero`` refuses with ``ERR no_path``;
  * ``probe`` also answers ``reachable=0`` for the hero's own tile (``CPathsInfo::getPath``
    refuses a node without predecessor), so the hero tile is seeded into the reachable set;
  * therefore ``reachable=0`` for a far target proves **nothing**: the target may simply
    still be behind the shroud, or a guard monster may stand on the only corridor
    (BLOCKVIS tiles can be attacked but never walked through);
  * the only way to open the shroud is to walk the hero to revealed tiles that touch the
    shroud border: the hero's sight (``getSightRadius``) reveals the next ring, the border
    moves, repeat.

That is the "brute force only when stuck" search the player asked for: traverse the tiles
around the black shroud, step on the ones closest to the goal, and let each step reveal the
next ring of shroud until the goal becomes genuinely reachable (or the border is exhausted).

The algorithm
-------------
::

    repeat (move budget / turn budget / battle / stuck)
        region   = probe(whole level, ONE read-only command)   # complete reachable set
        if target in region and reachable:                     # a real path exists now
            move(target) ; done
        else:
            frontier = reachable tiles touching a non-reachable tile
            take the UNVISITED border tile nearest to the target
            move(it)                 # stepping there lets the sight radius reveal the ring
    until target reached / border exhausted / budget used

Two lessons from the live runs are baked in:

  * probe the WHOLE level.  A sliding window can leave the shroud border just outside it,
    and the returned "reachable" set is then only the part of the component inside the window.
  * the visited border set must survive end-of-turn.  A purely distance-ordered walk parks
    in front of a dead end and repeats the same tour every day; the corner tile that opens
    the passage (live: the east corner of the underground pocket) is only reached by
    traversing the border.

The module stays bridge-agnostic: ``probe`` / ``move`` / ``observe`` are injected callables,
so the same code is covered by offline unit tests, used by the MCP tool
``explore_toward`` and by the live CLI ``tools/fog_path_live.py``.
"""
from __future__ import annotations

import re

# 8-neighbourhood: heroes move diagonally on the adventure map, so path distance (and the
# frontier adjacency test) must use it.
ADJ8 = ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1))

MAX_RADIUS_CAP = 16          # hard cap: (2r+1)^2 probe targets grows quadratically
DEFAULT_RADIUS = 8
DEFAULT_MAX_STEPS = 8
# One probe command can carry the whole level (measured: 1296 tiles in 0.1 s on the pinned
# client).  That makes the `reachable` set *complete*, which is what the border traversal
# needs - a sliding window can hide the shroud border just outside it.
FULL_LEVEL_PROBE_TILES = 8192

_TILE_RE = re.compile(r'(-?\d+),(-?\d+),(-?\d+)=([01])')
_FACT_RE = re.compile(r'(turns|cost|len|teleport|embark)=(-?\d+)')
_PROBE_FACTS = ('turns', 'cost', 'len', 'teleport', 'embark')

MOVE_ACCEPTED = ('completed', 'submitted')

# Status values returned by FogExplorer.run(); 'direct'/'frontier' mean "one action accepted,
# re-observe and continue", they are NOT a claim that the target was reached.
STATUS_DIRECT = 'direct'
STATUS_REACHED = 'reached'
STATUS_BATTLE = 'battle'
STATUS_STUCK = 'stuck'
STATUS_MAX_STEPS = 'max_steps'
STATUS_NO_STATE = 'no_state'
STATUS_NO_HERO = 'no_hero'
STATUS_MOVE_FAILED = 'move_failed'
STATUS_NEED_TURN = 'need_turn'
STATUS_END_TURN_FAILED = 'end_turn_failed'
STATUS_QUERY = 'query_pending'
STATUS_QUERY_FAILED = 'query_failed'


# ---------------------------------------------------------------------- parsing helpers
def parse_probe_reply(reply: str) -> dict[tuple[int, int, int], dict]:
    """Parse a bridge ``probe`` reply line into ``{tile: {reachable, turns, cost, ...}}``.

    Example reply::

        seq=7 OK probe 11,25,1=1 turns=0 cost=141 len=2 teleport=0 embark=0 12,24,1=0
    """
    text = str(reply or '')
    # Tolerate the "seq=N OK probe" prefix as well as a bare "OK probe ..." body.
    tokens = text.split()
    out: dict[tuple[int, int, int], dict] = {}
    current = None
    for token in tokens:
        tile_match = _TILE_RE.fullmatch(token)
        if tile_match:
            tile = (int(tile_match.group(1)), int(tile_match.group(2)), int(tile_match.group(3)))
            current = {'tile': tile, 'reachable': tile_match.group(4) == '1'}
            for fact in _PROBE_FACTS:
                current[fact] = 0 if fact in ('teleport', 'embark') else None
            out[tile] = current
            continue
        fact_match = _FACT_RE.fullmatch(token)
        if fact_match and current is not None:
            current[fact_match.group(1)] = int(fact_match.group(2))
    return out


def probe_command(hero: int, tiles) -> str:
    """Build ONE single-line bridge command probing every tile (the bridge accepts a list)."""
    parts = [f'probe {int(hero)}']
    for tile in tiles:
        parts.append(f'{int(tile[0])} {int(tile[1])} {int(tile[2])}')
    return ' '.join(parts)


# ---------------------------------------------------------------------- geometry helpers
def manhattan(a, b) -> int:
    return abs(int(a[0]) - int(b[0])) + abs(int(a[1]) - int(b[1]))


def neighbours8(tile):
    x, y, z = int(tile[0]), int(tile[1]), int(tile[2])
    for dx, dy in ADJ8:
        yield (x + dx, y + dy, z)


def observed_bounds(state: dict) -> tuple[int, int, int, int]:
    """Map box ``(minx, miny, maxx, maxy)`` that is safe to probe.

    The pinned engine indexes ``fogOfWarMap`` WITHOUT bounds checks
    (``MapTilesStorage::getTileIndex`` only asserts in debug builds), so probing a tile
    outside the map is undefined behaviour.  Coordinates that the state already reports
    (heroes/towns/objects). 0 is a valid minimum on every H3 map, so the box never extends
    into negative coordinates.  Callers that know the scenario dimensions should pass
    ``map_w``/``map_h`` explicitly instead of relying on this inference.
    """
    xs: list[int] = []
    ys: list[int] = []

    def _take(obj) -> None:
        for key in ('pos', 'visitable', 'tile', 'anchor'):
            value = obj.get(key) if isinstance(obj, dict) else None
            if isinstance(value, (list, tuple)) and len(value) >= 2:
                try:
                    xs.append(int(value[0]))
                    ys.append(int(value[1]))
                except (TypeError, ValueError):
                    continue

    for section in ('heroes', 'towns', 'objects'):
        for obj in state.get(section) or []:
            _take(obj)
    if not xs or not ys:
        # Nothing observed yet: a 1x1 box is the only provably safe answer.
        return (0, 0, 0, 0)
    return (0, 0, max(xs), max(ys))


def bounds_from_map(map_w: int, map_h: int) -> tuple[int, int, int, int]:
    """Explicit map box for a scenario of known dimensions (0-based inclusive)."""
    if int(map_w) <= 0 or int(map_h) <= 0:
        raise ValueError('map_w/map_h must be positive')
    return (0, 0, int(map_w) - 1, int(map_h) - 1)


def window_tiles(center, radius: int, bounds: tuple[int, int, int, int]):
    """All tiles of the hero's layer inside the square window AND inside the safe map box."""
    minx, miny, maxx, maxy = bounds
    cx, cy, cz = int(center[0]), int(center[1]), int(center[2])
    radius = max(0, int(radius))
    tiles = []
    for x in range(cx - radius, cx + radius + 1):
        if x < minx or x > maxx:
            continue
        for y in range(cy - radius, cy + radius + 1):
            if y < miny or y > maxy:
                continue
            tiles.append((x, y, cz))
    return tiles


def bfs_depth(start, walkable) -> dict:
    """Breadth-first step distance inside the reachable set (the BFS of the border search)."""
    depth = {start: 0}
    queue = [start]
    head = 0
    while head < len(queue):
        current = queue[head]
        head += 1
        for neighbour in neighbours8(current):
            if neighbour in walkable and neighbour not in depth:
                depth[neighbour] = depth[current] + 1
                queue.append(neighbour)
    return depth


# ---------------------------------------------------------------------- frontier search
def reachable_tiles(region: dict) -> set:
    return {tile for tile, data in region.items() if data.get('reachable')}


def frontier_candidates(region: dict, hero_tile, target,
                        blocked=frozenset()) -> list[dict]:
    """Rank the shroud-border tiles: closest to the target first, then cheapest, then most fog.

    A candidate is a REACHABLE tile with at least one probed neighbour that is not reachable
    (unrevealed shroud, wall, water, or a monster standing on the corridor).  Neighbours
    outside the probed window are ignored - their state is unknown, not "shroud".
    """
    walkable = reachable_tiles(region)
    # Engine quirk (CGPathNode.cpp:78): getPath() returns false when the node has no
    # predecessor, so `probe` reports reachable=0 for the hero's OWN tile.  The hero is
    # obviously standing there, so it must be part of the reachable graph.
    if hero_tile in region and not region[hero_tile].get('reachable'):
        region[hero_tile]['reachable'] = True
    walkable.add(hero_tile)
    if not walkable:
        return []
    depth = bfs_depth(hero_tile, walkable)
    candidates = []
    for tile in sorted(walkable):
        if tile in blocked:
            continue
        shroud_contact = 0
        for neighbour in neighbours8(tile):
            if neighbour not in region:
                continue
            if not region[neighbour].get('reachable'):
                shroud_contact += 1
        if shroud_contact == 0:
            continue
        candidates.append({
            'tile': tile,
            'distance': manhattan(tile, target),
            'depth': depth.get(tile, 1 << 30),
            'shroud_contact': shroud_contact,
            'turns': region[tile].get('turns'),
        })
    # Same-turn tiles first: a multi-turn destination only walks the first leg, so a greedy
    # order that prefers "closer to the target" over "arrives this turn" would burn moves.
    # distance is the primary key (the player's "prioritise the target direction"), BFS depth
    # breaks ties toward cheaper steps, then the strongest shroud contact.
    candidates.sort(key=lambda cand: (cand['turns'] not in (None, 0),
                                      cand['distance'], cand['depth'],
                                      -cand['shroud_contact'], cand['tile']))
    return candidates


def hero_tile(state: dict, hero: int):
    """Interaction tile of the hero (``tile``/``visitable``; ``pos`` is only the sprite anchor)."""
    heroes = (state or {}).get('heroes') or []
    if not isinstance(hero, int) or hero < 0 or hero >= len(heroes):
        return None
    entry = heroes[hero]
    for key in ('tile', 'visitable', 'pos'):
        value = entry.get(key)
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            return (int(value[0]), int(value[1]), int(value[2]))
    return None


def hero_movement(state: dict, hero: int):
    heroes = (state or {}).get('heroes') or []
    if isinstance(hero, int) and 0 <= hero < len(heroes):
        try:
            return int(heroes[hero].get('movement'))
        except (TypeError, ValueError):
            return None
    return None


def battle_active(state: dict) -> bool:
    return bool(((state or {}).get('battle') or {}).get('active'))


def move_accepted(result) -> bool:
    """True when the move command was accepted (completed or at least submitted)."""
    if not isinstance(result, dict):
        return False
    if result.get('ok') is False and result.get('status') not in MOVE_ACCEPTED:
        return False
    return str(result.get('status') or '').lower() in MOVE_ACCEPTED


# ---------------------------------------------------------------------- explorer driver
class FogExplorer:
    """Brute-force shroud-border explorer used only when normal progress is stuck.

    Parameters are injected callables so tests never touch a client:

    ``probe(tiles)``
        send ONE ``probe`` command for every tile and return ``parse_probe_reply``'s dict.
        Read-only.
    ``move(tile)``
        issue exactly ONE movement order and return a dict with a ``status`` field
        (``completed``/``submitted`` = accepted, anything else = not accepted).
    ``observe()``
        return the current bridge state dict (``state.json`` body).
    ``end_turn()`` (optional)
        end the turn so the search can continue on the next day (bounded by ``max_turns``);
        return a dict with a ``status`` field like ``move``.
    """

    def __init__(self, *, probe, move, observe, hero: int, target,
                 bounds: tuple[int, int, int, int] | None = None,
                 radius: int = DEFAULT_RADIUS, max_radius: int = 12,
                 max_steps: int = DEFAULT_MAX_STEPS, log=None,
                 end_turn=None, max_turns: int = 0, answer_query=None):
        self.probe = probe
        self.move = move
        self.observe = observe
        self.end_turn = end_turn
        self.answer_query = answer_query
        self.max_turns = max(0, int(max_turns))
        self.hero = int(hero)
        self.target = (int(target[0]), int(target[1]),
                       int(target[2]) if len(target) > 2 else 0)
        self.bounds = bounds or (0, 0, 0, 0)
        self.radius = max(1, min(int(radius), MAX_RADIUS_CAP))
        self.max_radius = max(self.radius, min(int(max_radius), MAX_RADIUS_CAP))
        self.max_steps = max(1, int(max_steps))
        self.log = log or (lambda _msg: None)
        # Brute-force bookkeeping: candidates that already failed, tiles the hero already
        # stood on, and the last probe statistics (surfaced in the result for auditing).
        self.failed: set = set()
        self.visited: set = set()
        self.revealed: set = set()
        self.stats = {'probes': 0, 'probe_tiles': 0, 'region_reachable': 0,
                      'revealed_tiles': 0, 'region_growth': [], 'frontier_seen': 0,
                      'moves_sent': 0}

    # -- one probe round ------------------------------------------------------------
    def region_tiles(self, hero_tile):
        """Tiles to probe: the whole level when it fits in one command, else a window."""
        minx, miny, maxx, maxy = self.bounds
        area = max(0, maxx - minx + 1) * max(0, maxy - miny + 1)
        if area <= FULL_LEVEL_PROBE_TILES:
            minz = maxz = int(hero_tile[2])
            return [(x, y, minz) for x in range(minx, maxx + 1)
                    for y in range(miny, maxy + 1)]
        return window_tiles(hero_tile, max(self.radius, 12), self.bounds)

    def _probe_region(self, hero_tile) -> dict:
        region = self.probe(self.region_tiles(hero_tile))
        self.stats['probes'] += 1
        self.stats['probe_tiles'] += len(region)
        reachable = reachable_tiles(region)
        self.stats['region_reachable'] = len(reachable)
        self.revealed |= reachable
        return region

    # -- main loop -------------------------------------------------------------------
    def run(self, max_moves: int | None = None, max_turns: int | None = None) -> dict:
        """Walk the shroud border until the target is reachable, the border is exhausted or
        the move/turn budget runs out.  ``max_turns`` > 0 (and an ``end_turn`` callable) lets
        the search continue on later days when the hero's movement points are spent."""
        move_budget = self.max_steps if max_moves is None else max(1, int(max_moves))
        turn_budget = self.max_turns if max_turns is None else max(0, int(max_turns))
        trace: list[dict] = []
        recent: list = []
        moves = 0
        turns = 0
        last_region_size = None
        while moves < move_budget:
            state = self.observe()
            if not isinstance(state, dict) or not state.get('heroes'):
                return {'ok': False, 'status': STATUS_NO_STATE, 'trace': trace,
                        'stats': dict(self.stats)}
            if battle_active(state):
                battle = state.get('battle') or {}
                return {'ok': False, 'status': STATUS_BATTLE, 'trace': trace,
                        'battle_round': battle.get('round'), 'stats': dict(self.stats),
                        'hint': 'battle started; decide the fight before exploring again'}
            query = state.get('query')
            if isinstance(query, dict) and query.get('id') is not None:
                # A UI query (chest/quest/monster prompt) blocks every movement order: the
                # live run froze exactly here with a 宝箱 "gold or experience" dialog.
                if self.answer_query is None:
                    return {'ok': False, 'status': STATUS_QUERY, 'trace': trace,
                            'query': query, 'turns': turns, 'stats': dict(self.stats),
                            'hint': 'answer the open query (game_act answer) before exploring'}
                result = self.answer_query(query)
                trace.append({'step': len(trace), 'kind': 'answer_query',
                              'query_id': query.get('id'), 'move': result})
                if not move_accepted(result):
                    return {'ok': False, 'status': STATUS_QUERY_FAILED,
                            'query': query, 'move': result, 'trace': trace,
                            'turns': turns, 'stats': dict(self.stats)}
                continue
            current = hero_tile(state, self.hero)
            if current is None:
                return {'ok': False, 'status': STATUS_NO_HERO, 'trace': trace,
                        'stats': dict(self.stats)}
            if current == self.target:
                return {'ok': True, 'status': STATUS_REACHED, 'hero_tile': current,
                        'turns': turns, 'trace': trace, 'stats': dict(self.stats)}

            movement = hero_movement(state, self.hero)
            if movement == 0:
                # No movement points left: the border cannot be walked any further today.
                if self.end_turn is None or turns >= turn_budget:
                    return {'ok': False, 'status': STATUS_NEED_TURN, 'hero_tile': current,
                            'turns': turns, 'movement': movement, 'trace': trace,
                            'stats': dict(self.stats),
                            'hint': 'movement exhausted; end the turn (or pass max_turns) '
                                    'to keep walking the shroud border'}
                result = self.end_turn()
                turns += 1
                trace.append({'step': len(trace), 'kind': 'end_turn', 'turn': turns,
                              'move': result})
                if not move_accepted(result):
                    return {'ok': False, 'status': STATUS_END_TURN_FAILED,
                            'hero_tile': current, 'turns': turns, 'move': result,
                            'trace': trace, 'stats': dict(self.stats)}
                recent = []                   # a new day; keep `visited` - the border walked
                continue                      # yesterday must not be walked again

            region = self._probe_region(current)
            size = len(reachable_tiles(region))
            growth = None if last_region_size is None else size - last_region_size
            self.stats['region_growth'].append(growth)
            self.stats['revealed_tiles'] = len(self.revealed)
            last_region_size = size
            target_probe = region.get(self.target)
            if target_probe and target_probe.get('reachable'):
                result = self.move(self.target)
                self.stats['moves_sent'] += 1
                trace.append({'step': len(trace), 'kind': 'direct', 'from': current,
                              'to': self.target, 'move': result})
                if move_accepted(result):
                    return {'ok': True, 'status': STATUS_DIRECT, 'hero_tile': current,
                            'target': self.target, 'move': result, 'turns': turns,
                            'trace': trace, 'stats': dict(self.stats)}
                self.failed.add(self.target)
                return {'ok': False, 'status': STATUS_MOVE_FAILED, 'hero_tile': current,
                        'target': self.target, 'move': result, 'turns': turns,
                        'trace': trace, 'stats': dict(self.stats)}

            candidates = frontier_candidates(region, current, self.target, blocked=self.failed)
            self.stats['frontier_seen'] = len(candidates)
            # Never "step on" the tile the hero is already standing on: the sight ring is
            # already revealed from there, so it cannot open anything new.
            candidates = [cand for cand in candidates if cand['tile'] != current]
            # Border traversal: every border tile has to be stepped on once - a greedy
            # "closest to the target" walk alone can park in front of a dead end and never
            # try the corner that actually opens a passage (observed live: the north border
            # of the underground pocket was exhausted while the east corner (9,33) was what
            # revealed the next corridor).  So: unvisited border tiles first, target
            # direction as the ordering inside that set, and the visited set survives
            # end-of-turn so a new day does not replay the same tour.
            pool = [cand for cand in candidates if cand['tile'] not in self.visited]
            if not pool:
                return {'ok': False, 'status': STATUS_STUCK, 'hero_tile': current,
                        'reason': 'every shroud-border tile in the probed area was visited',
                        'turns': turns, 'trace': trace, 'stats': dict(self.stats),
                        'hint': 'the border is exhausted (walls/void) or the target is in '
                                'another walkable component; try another hero or entrance'}
            pool.sort(key=lambda cand: (cand['tile'] in recent,
                                        cand['distance'], cand['depth'],
                                        -cand['shroud_contact'], cand['tile']))

            moved = None
            interrupted = False
            for cand in pool[:4]:
                result = self.move(cand['tile'])
                self.stats['moves_sent'] += 1
                accepted = move_accepted(result)
                trace.append({'step': len(trace), 'kind': 'frontier', 'from': current,
                              'to': cand['tile'],
                              'score': {'distance': cand['distance'], 'depth': cand['depth'],
                                        'shroud_contact': cand['shroud_contact'],
                                        'turns': cand.get('turns')},
                              'move': result})
                if accepted:
                    moved = cand
                    break
                self.failed.add(cand['tile'])
                # A chest/quest dialog or a battle can appear *during* the move.  While a
                # query is open every further move_hero is silently swallowed (live: 3x12 s
                # wasted on "reply received but effect not observed"), so stop here and let
                # the outer loop answer it / report the battle.
                fresh = self.observe()
                if battle_active(fresh) or (isinstance(fresh, dict) and fresh.get('query')):
                    interrupted = True
                    break
            if moved is None and interrupted:
                continue
            if moved is None:
                return {'ok': False, 'status': STATUS_STUCK, 'hero_tile': current,
                        'reason': 'every shroud-border move was refused',
                        'turns': turns, 'trace': trace, 'stats': dict(self.stats)}

            moves += 1
            recent = ([current] + recent)[:2]
            self.visited.add(current)
            self.visited.add(moved['tile'])
            # The loop re-observes and re-probes: stepping on the border made the hero's
            # sight reveal the next ring of shroud, so the window content changes.

        state = self.observe()
        return {'ok': False, 'status': STATUS_MAX_STEPS,
                'hero_tile': hero_tile(state, self.hero) if isinstance(state, dict) else None,
                'turns': turns, 'moves': moves, 'trace': trace, 'stats': dict(self.stats)}
