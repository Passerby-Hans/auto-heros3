# -*- coding: utf-8 -*-
"""Standard MCP server (official Python SDK v2) for LLM takeover of VCMI.

First principle: **no LLM command -> the game does not move.**  Nothing here runs a driver,
auto-plays, auto-answers dialogs or auto-fights.  Tools only observe, execute one requested
action, report decision points, and keep the runtime alive.

Run (stdio):      python -X utf8 tools/mcp_server_v2.py
Self test:        python -X utf8 tools/mcp_server_v2.py --selftest

Tools:
  session_start(map, dir) -> start the owned client (explicit business action)
  session_stop()          -> stop only the owned client, report leftovers
  game_status()           -> session/day/status/offscreen
  game_observe()          -> player-visible digest (heroes/towns/objects/query/battle)
  game_act(action)        -> ONE action (whitelist), returns status/seq/evidence/decision point
  game_events()           -> persisted decision points (battle/query/game-over/level-up)
  battle_round(target)    -> lead ONE battle round (refuses auto-answer by default)
  save_game()/load_game() -> save slots, verified against file/state before/after observation
  lease_acquire()/release()-> single-writer lease with an in-memory token (restart => re-acquire)

Honest result contract (S0 / D-007):
  status   completed | submitted | rejected | unknown | interrupted
  ok       True ONLY when status == 'completed' (the post-condition was observed)
  accepted True when the engine bridge replied OK to the command; it is NOT a claim that the
           action took effect (averifier can still reject/keep it submitted)
  A bridge REJECTED/ERR reply and a bridge UNKNOWN/timeout are terminal: the post-condition
  verifier can never upgrade them to completed.
"""
from __future__ import annotations

import argparse
import base64
import functools
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))


# ---------------------------------------------------------------- test-isolation path injection
# The AUTO_HEROS3_*_DIR / AUTO_HEROS3_SAVE_SUBDIR variables below are a TEST-ONLY escape hatch:
# a live/debug test must redirect every writable path BEFORE importing this module, because the
# path constants are resolved at import time (see the N0 guard in tools/test_session_memory.py).
# Formal runs never set them; with no env variable each default below is exactly the path this
# server has always used.
def _env_path(name: str, default: Path) -> Path:
    """Path from a test-isolation env var, or `default` when the variable is unset/empty."""
    raw = os.environ.get(name)
    return Path(raw) if raw else default


# MCP Python SDK v2 renamed FastMCP to MCPServer; keep a v1 fallback for older installs.
try:  # pragma: no cover - depends on the installed SDK
    from mcp.server.mcpserver import MCPServer
    mcp = MCPServer('auto-heros3')
    try:
        from mcp.server.mcpserver import Image  # type: ignore
    except Exception:
        Image = None  # type: ignore
except Exception:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP('auto-heros3')
    try:
        from mcp.server.fastmcp import Image  # type: ignore
    except Exception:
        Image = None  # type: ignore

from vcmi_agent import VcmiAgent  # noqa: E402
STATE_DIR = _env_path('AUTO_HEROS3_STATE_DIR', ROOT / 'var' / 'agent_mcp_v2')
# action -> (bridge command, ordered argument names).  Tool args arrive as a JSON object, so the
# command line must be built from this spec instead of relying on dict insertion order.
ACTION_SPECS = {
    'move': ('move_hero', ('hero', 'x', 'y', 'z')),
    'move_hero': ('move_hero', ('hero', 'x', 'y', 'z')),
    'build': ('build', ('town', 'id')),
    'recruit': ('recruit', ('town', 'level', 'amount')),
    'recruit_hero': ('recruit_hero', ('town', 'index')),
    # T40/C11: open the real hero screen (CPlayerInterface::openHeroWindow -> CHeroWindow).  The
    # window is left OPEN so a demo can show the equipment/paper-doll; close it with close_window
    # (the bridge accepts CHeroWindow in its close_window_safe list).
    'open_hero': ('open_hero', ('hero',)),
    'transfer': ('transfer', ('src', 'src_index', 'src_slot', 'dst', 'dst_index', 'dst_slot')),
    'merge': ('merge', ('src', 'src_index', 'src_slot', 'dst', 'dst_index', 'dst_slot')),
    'swap_stacks': ('swap_stacks', ('owner', 'index', 'slot_a', 'slot_b')),
    'split': ('split', ('owner', 'index', 'slot', 'amount')),
    'upgrade': ('upgrade', ('owner', 'index', 'slot', 'creature')),
    'market_trade': ('market_trade', ('town', 'trade_mode', 'sell', 'buy', 'amount')),
    'answer': ('answer', ('value',)),
    'end_turn': ('end_turn', ()),
    # T35: hero magic.  cast_spell is the adventure-map spellbook request (optional x,y,z target);
    # battle_cast_spell is the battle spellbook request (optional unit stack or hex target).
    'cast_spell': ('cast_spell', ('hero', 'spell')),
    'battle_cast_spell': ('battle_cast_spell', ('spell',)),
    'battle_attack': ('battle_attack', ('stack',)),
    # Explicit move-towards-without-attacking (the old battle_attack silently did this).
    'battle_approach': ('battle_approach', ('target',)),
    'battle_move': ('battle_move', ('x', 'y')),
    'battle_defend': ('battle_defend', ()),
    'battle_wait': ('battle_wait', ()),
    'battle_result_ok': ('battle_result_ok', ()),
    'confirm_deployment': ('key return', ()),
    'close_window': ('close_window_safe', ()),
    'center_hero': ('center_hero', ()),
    # UI clicks for client-side dialogs the bridge does not expose as server queries
    # (shipyard, quest windows, battle dialogs).  Coordinates are the same event-dispatcher
    # coordinates the ui_tree dump uses.  It is a plain single action with no verifier.
    'click': ('click', ('x', 'y')),
}
ACTIONS = set(ACTION_SPECS)
_recorder = {'proc': None, 'dir': None, 'started': 0.0}
_OVERLAY_MAX = 600
# N5: the on-video panel must keep the model's reasoning.  A plain "last N lines" trim evicted
# thoughts almost immediately (they are the most frequent line kind), so the panel keeps the last
# _OVERLAY_THOUGHT_KEEP reasoning lines plus the most recent other lines; the complete feed is
# additionally appended to overlay_history.jsonl (rotated once at 4 MB).
_OVERLAY_THOUGHT_KEEP = 120
_OVERLAY_TRIM_BYTES = 512 * 1024
_OVERLAY_HISTORY_MAX_BYTES = 4 * 1024 * 1024
# Save slots written by save_game live under <Documents>/My Games/vcmi/Saves/<SAVE_SUBDIR>.
# AUTO_HEROS3_SAVES_DIR / AUTO_HEROS3_SAVE_SUBDIR are test-isolation overrides (formal runs do
# not set them).  The subdir must be a plain directory name ([A-Za-z0-9_-]+): an absolute path or
# a value containing separators would escape the saves root, so it is rejected at import time.
SAVES_DIR = _env_path('AUTO_HEROS3_SAVES_DIR',
                      Path.home() / 'Documents' / 'My Games' / 'vcmi' / 'Saves')
_SAVE_SUBDIR_ENV = os.environ.get('AUTO_HEROS3_SAVE_SUBDIR')
if _SAVE_SUBDIR_ENV is not None and not re.fullmatch(r'[A-Za-z0-9_-]+', _SAVE_SUBDIR_ENV):
    raise RuntimeError(
        'AUTO_HEROS3_SAVE_SUBDIR must match [A-Za-z0-9_-]+ (a plain save directory name; '
        f'empty values, absolute paths and path separators are refused); got {_SAVE_SUBDIR_ENV!r}')
SAVE_SUBDIR = _SAVE_SUBDIR_ENV if _SAVE_SUBDIR_ENV is not None else 'agent'
# Recorder clips land under ROOT/'var'/'recordings' by default.  AUTO_HEROS3_RECORDINGS_DIR is a
# test-isolation override; recording_start() keeps following ROOT when no override is set so the
# existing offline tests that monkeypatch ROOT stay isolated.
_RECORDINGS_ENV = os.environ.get('AUTO_HEROS3_RECORDINGS_DIR')
RECORDINGS_DIR = Path(_RECORDINGS_ENV) if _RECORDINGS_ENV else ROOT / 'var' / 'recordings'


def _recordings_root() -> Path:
    """Recorder output root: env override for isolated live tests, else follow (patched) ROOT."""
    return RECORDINGS_DIR if _RECORDINGS_ENV else ROOT / 'var' / 'recordings'


def _safe_save_name(name: str) -> str:
    return ''.join(ch for ch in (name or '') if ch.isalnum() or ch in '-_.')[:48]


def _saves_meta_path() -> Path:
    return _dir() / 'saves.json'


def _load_saves_meta() -> dict:
    try:
        data = json.loads(_saves_meta_path().read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _dir() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR


def _agent() -> VcmiAgent:
    a = VcmiAgent(str(_dir()), log=False)
    a.session = a._load_session()
    try:
        import vcmi_shot
        wins = vcmi_shot.find_window()
        if wins:
            a.hwnd = wins[0][0]
    except Exception:
        pass
    return a


LEASE_TTL = 600.0        # seconds; every accepted action renews the lease
# The lease token lives in module memory and is mirrored to <dir>/lease.json.  Writing the lease
# file alone is not enough: a server restart loses the in-memory token, so a stale lease file can
# never be used by a new process - it must call lease_acquire() again.
_lease_token: str | None = None


def _lease_record(token: str) -> dict:
    return {'owner': 'llm', 'token': token, 'expires_at': time.time() + LEASE_TTL}


def _require_lease() -> None:
    global _lease_token
    if not _lease_token:
        raise RuntimeError('no lease: call lease_acquire first '
                           '(lease tokens do not survive a server restart)')
    path = _dir() / 'lease.json'
    try:
        lease = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise RuntimeError('no lease: call lease_acquire first')
    if not isinstance(lease, dict) or lease.get('owner') != 'llm':
        raise RuntimeError('lease not held by this session')
    if lease.get('token') != _lease_token:
        raise RuntimeError('lease token mismatch: call lease_acquire again')
    try:
        expired = float(lease.get('expires_at') or 0) < time.time()
    except (TypeError, ValueError):
        expired = True
    if expired:
        raise RuntimeError('lease expired: call lease_acquire again')
    # renew on every accepted action so long play sessions do not lose control mid-turn
    try:
        path.write_text(json.dumps(_lease_record(_lease_token)), encoding='utf-8')
    except OSError:
        pass


# ---------------------------------------------------------------- unresolved prior actions (R2)
# A mutating action whose outcome could not be observed (bridge timeout, unreadable after-state)
# must not be bypassed by a new request_id / helper.  The marker blocks every further mutating
# action until the caller has inspected the game and explicitly closed it with
# resolve_unresolved(); that tool records the observation and never turns unknown into success.
def _unresolved_path() -> Path:
    return _dir() / 'unresolved.json'


def _load_unresolved() -> dict | None:
    try:
        data = json.loads(_unresolved_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _set_unresolved(entry: dict) -> None:
    _atomic_write_json(_unresolved_path(), entry)


def _unresolved_error(request: str | None = None) -> dict | None:
    """Error dict when a previous mutating action is still unresolved, else None."""
    unresolved = _load_unresolved()
    if unresolved is None:
        return None
    same_request = bool(request) and request == unresolved.get('request_id')
    return {'ok': False, 'sent': False,
            'error': 'request_pending' if same_request else 'unresolved_prior_action',
            'request_id': request,
            'unresolved': {key: unresolved.get(key) for key in
                           ('action', 'args', 'action_id', 'request_id', 'seq', 'status',
                            'evidence', 'ts') if key in unresolved},
            'hint': ('inspect the game (game_observe/game_action_status), then close it with '
                     'resolve_unresolved(action_id=<id>, observed=<what you saw>) before any '
                     'new action')}


# --------------------------------------------------- D-014 mutation serialization / UI gate
# The client can issue tool calls concurrently (observed in the third batch: five `recruit` calls
# in one reply -> an open CGarrisonDialogQuery went stale and VCMI_client crashed).  A mutating
# tool therefore owns a single process-wide slot from BEFORE it reads the pre-state until its
# verified result is produced.  A second mutating call is refused with error=in_flight instead of
# being queued: queueing would silently replay a command seconds later against a changed game.
# Same-thread re-entry is allowed because composite tools (move_near -> game_act) call the inner
# action themselves.  The lease remains the cross-process single-writer guard; this gate covers
# concurrent calls inside one MCP process.
_MUTATION_LOCK = threading.Lock()
_MUTATION_LOCAL = threading.local()
_MUTATION_OWNER: dict | None = None


def _mutation_owner_snapshot() -> dict | None:
    """Read-only view of the mutation currently in flight (None when the slot is free)."""
    owner = _MUTATION_OWNER
    if not owner:
        return None
    snapshot = dict(owner)
    started = snapshot.get('started_at')
    if isinstance(started, (int, float)):
        snapshot['age_s'] = round(max(0.0, time.time() - started), 2)
    return snapshot


def _in_flight_error(tool: str) -> dict:
    return {'ok': False, 'sent': False, 'error': 'in_flight', 'tool': tool,
            'in_flight': _mutation_owner_snapshot(),
            'hint': ('another mutating MCP call is still running; wait for it to finish, then '
                     're-observe (game_observe/game_brief) before issuing the next action '
                     '(D-014: mutating calls must never be sent concurrently)')}


@contextmanager
def _mutation_slot(tool: str):
    """Own the single mutation slot for `tool`; yields an in_flight error dict when it is taken."""
    global _MUTATION_OWNER
    depth = int(getattr(_MUTATION_LOCAL, 'depth', 0) or 0)
    if depth:
        _MUTATION_LOCAL.depth = depth + 1
        try:
            yield None
        finally:
            _MUTATION_LOCAL.depth -= 1
        return
    if not _MUTATION_LOCK.acquire(blocking=False):
        yield _in_flight_error(tool)
        return
    _MUTATION_LOCAL.depth = 1
    _MUTATION_OWNER = {'tool': tool, 'pid': os.getpid(),
                       'thread': threading.current_thread().name,
                       'started_at': time.time()}
    try:
        yield None
    finally:
        _MUTATION_OWNER = None
        _MUTATION_LOCAL.depth = 0
        _MUTATION_LOCK.release()


def _serialized_mutation(tool: str):
    """Decorator: run a mutating MCP tool only when no other mutation is in flight (D-014)."""
    def decorate(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with _mutation_slot(tool) as busy:
                if busy is not None:
                    # The refused concurrent call is evidence too: record it, then never run it.
                    try:
                        _ledger({'action': tool, 'status': 'rejected', 'accepted': False,
                                 'error': 'in_flight',
                                 'evidence': 'in_flight: another mutating call is still running',
                                 'detail': (busy.get('in_flight') or {}).get('tool')})
                    except Exception:  # noqa: BLE001 - the gate must never raise
                        pass
                    return busy
                return func(*args, **kwargs)
        return wrapper
    return decorate


# D-014 ①b: while the engine has a pending query (town/dialog/level-up/battle prompt), a new
# mutating command is refused instead of racing the open dialog.  Only the two query-resolution
# actions pass; everything else must wait until the player has answered/closed it and re-observed.
_QUERY_EXEMPT_ACTIONS = frozenset({'answer', 'close_window'})


def _ledger(entry: dict) -> None:
    """Append-only action ledger (audit + idempotency for S4).

    R6: every entry carries the logical run/branch/epoch it belongs to, so a ledger read months
    later can tell a fresh game from a load in the same session.
    """
    import datetime
    entry = dict(entry)
    entry["ts"] = datetime.datetime.now().isoformat(timespec="seconds")
    entry.setdefault("source", "llm")
    try:
        run = _run_identity()
        entry.setdefault("run_id", run.get("run_id"))
        entry.setdefault("branch_id", run.get("branch_id"))
        entry.setdefault("epoch", run.get("epoch"))
    except Exception:  # noqa: BLE001 - the ledger must never break an action
        pass
    try:
        with open(_dir() / "actions.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _overlay(kind: str, text: str, ts: str | None = None) -> None:
    """Append one line to the recorder feed (<dir>/overlay.jsonl).

    The standalone overlay recorder renders these lines into the widened 16:9 panel, so a
    video of a run shows the model's reasoning and each operation next to the game frame.

    Trimming (N5) keeps the last _OVERLAY_THOUGHT_KEEP `thought` lines plus the newest other
    lines up to _OVERLAY_MAX total; the complete feed also goes to overlay_history.jsonl so the
    reasoning history can be recovered even after the live panel moved on.
    """
    import datetime
    if not text:
        return
    entry = {'ts': ts or datetime.datetime.now().strftime('%H:%M:%S'),
             'kind': kind, 'text': str(text)[:500]}
    line = json.dumps(entry, ensure_ascii=False)
    path = _dir() / 'overlay.jsonl'
    history = _dir() / 'overlay_history.jsonl'
    try:
        if path.exists() and path.stat().st_size > _OVERLAY_TRIM_BYTES:
            entries = []
            for raw in path.read_text(encoding='utf-8').splitlines():
                try:
                    item = json.loads(raw)
                    if isinstance(item, dict):
                        entries.append(item)
                except ValueError:
                    continue
            thoughts = [item for item in entries if item.get('kind') == 'thought']
            others = [item for item in entries if item.get('kind') != 'thought']
            keep_thoughts = thoughts[-_OVERLAY_THOUGHT_KEEP:]
            # Reserve one slot for the line that is appended right after the trim, so the panel
            # ends up at _OVERLAY_MAX lines, not _OVERLAY_MAX + 1.
            keep_others = others[-(max(0, _OVERLAY_MAX - len(keep_thoughts) - 1)):]
            # Preserve original order: a thought and an action on the same line should still
            # render in the order they happened.
            keep_ids = {id(item) for item in keep_thoughts + keep_others}
            kept = [item for item in entries if id(item) in keep_ids]
            path.write_text('\n'.join(json.dumps(item, ensure_ascii=False)
                                     for item in kept) + '\n', encoding='utf-8')
        with path.open('a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass
    try:
        if history.exists() and history.stat().st_size > _OVERLAY_HISTORY_MAX_BYTES:
            rotated = _dir() / 'overlay_history.1.jsonl'
            try:
                if rotated.exists():
                    rotated.unlink()
                history.replace(rotated)
            except OSError:
                pass
        with history.open('a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


# N2/A + T33/P-B: these fields already exist in the bridge state.json (heroJson/battleJson); the
# digest and the brief pass them through instead of dropping them.  Only present fields are added,
# so an older/partial state fixture keeps its previous shape (no KeyError).
# T33/P-B added the hero-screen details the bridge now publishes (mana_max/spellbook/spells/
# skills/artifacts/luck/morale/specialty and creature names) - all player-visible in the UI.
_HERO_OBS_FIELDS = ('id', 'level', 'exp', 'mana', 'mana_max', 'spellbook', 'primary',
                    'luck', 'morale', 'specialty')
_STACK_OBS_FIELDS = ('shots', 'counterattacks', 'defending', 'waited', 'double_wide',
                     'creature_name')
_BATTLE_OBS_FIELDS = ('round', 'hero_mana', 'hero_mana_max', 'hero_spellbook', 'cast_spells')
# Caps keep the routine digest readable even for a late-game hero with many artifacts/spells.
_HERO_SPELLS_MAX = 40
_HERO_SKILLS_MAX = 8
_HERO_ARTIFACTS_MAX = 30
_BRIEF_SPELLS_MAX = 12
_BRIEF_ARTIFACTS_MAX = 12


def _optional_fields(source: dict, names: tuple) -> dict:
    return {name: source[name] for name in names if source.get(name) is not None}


# T31/P-A: the bridge already writes the Info-window log into state.json ("info", see
# AgentBridge.cpp infoJson(): week events, artifact/treasure messages, building visits, ...).
# The normal UI shows these dialogs to the player, so projecting them is an observation fix,
# not extra hidden information.  The deque is capped at 20 entries by the bridge; here we keep
# the newest few, collapse only *consecutive* repeats (repeated weekly events are real events,
# not duplicates) and truncate overly long texts so a routine brief stays compact.
_RECENT_INFO_MAX = 8
_RECENT_INFO_TEXT_MAX = 300
_ACTION_TAIL_MAX = 5
_ACTION_EVIDENCE_MAX = 200
_ACTION_ARGS_MAX = 200


def _truncate(text: str, limit: int) -> str:
    """One-line truncation marker so callers can tell a cut value from a complete one."""
    return text if len(text) <= limit else text[:limit] + '…'


def _recent_info(st: dict) -> list:
    """Newest player-visible Info-window lines from the current state (oldest->newest)."""
    raw = (st or {}).get('info')
    if not isinstance(raw, list):
        return []
    cleaned: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        if cleaned and cleaned[-1] == text:
            continue
        cleaned.append(text)
    return [_truncate(text, _RECENT_INFO_TEXT_MAX) for text in cleaned[-_RECENT_INFO_MAX:]]


def _hero_digest(hero: dict) -> dict:
    entry = {'i': hero['index'], 'name': hero['name'],
             'pos': hero.get('pos') or hero.get('visitable'),
             'visitable': hero.get('visitable'),
             'movement': hero['movement'], 'army': hero['army']}
    entry.update(_optional_fields(hero, _HERO_OBS_FIELDS))
    # T40/C11: pass the bridge's canonical interaction tile (`tile`) through.  Old/partial
    # fixtures (and pre-rebuild clients) do not carry it, so their digest shape is unchanged.
    tile = hero.get('tile')
    if isinstance(tile, list) and tile:
        entry['tile'] = list(tile)[:3]
    # T33/P-B: hero-screen lists.  Old/partial fixtures simply do not have them.
    spells = hero.get('spells')
    if isinstance(spells, list):
        entry['spells'] = spells[:_HERO_SPELLS_MAX]
    skills = hero.get('skills')
    if isinstance(skills, list):
        entry['skills'] = skills[:_HERO_SKILLS_MAX]
    artifacts = hero.get('artifacts')
    if isinstance(artifacts, dict):
        worn = artifacts.get('worn')
        backpack = artifacts.get('backpack')
        projected = {}
        if isinstance(worn, list):
            projected['worn'] = worn[:_HERO_ARTIFACTS_MAX]
        if isinstance(backpack, list):
            projected['backpack'] = backpack[:_HERO_ARTIFACTS_MAX]
        if projected:
            entry['artifacts'] = projected
    return entry


def _hero_brief(h: dict) -> dict:
    """Compact per-hero view for game_brief (hero-screen lists reduced to readable names)."""
    entry = {'i': h['i'], 'name': h['name'],
             'pos': list(h['pos'] or [0, 0, 0])[:3],
             'visitable': list(h.get('visitable') or [])[:3] or None,
             'mov': h['movement'],
             'army': sum(a['count'] for a in h['army']),
             'stacks': len(h['army']),
             **_optional_fields(h, _HERO_OBS_FIELDS)}
    # T40/C11: canonical interaction tile (`tile`) from the bridge, compact form.
    tile = h.get('tile')
    if isinstance(tile, list) and tile:
        entry['tile'] = list(tile)[:3]
    skills = h.get('skills')
    if isinstance(skills, list):
        entry['skills'] = skills[:_HERO_SKILLS_MAX]
    spells = h.get('spells')
    if isinstance(spells, list):
        entry['spell_count'] = len(spells)
        # T35: keep the spellbook compact but usable: id + name + cost + can-cast-now.
        projected_spells = []
        for spell in spells[:_BRIEF_SPELLS_MAX]:
            if not isinstance(spell, dict):
                continue
            row = {'id': spell.get('id'), 'name': spell.get('name')}
            for key in ('cost', 'can_cast', 'adventure'):
                if spell.get(key) is not None:
                    row[key] = spell[key]
            projected_spells.append(row)
        entry['spells'] = projected_spells
    artifacts = h.get('artifacts')
    if isinstance(artifacts, dict):
        names = [item.get('name') or item.get('id')
                 for key in ('worn', 'backpack')
                 for item in (artifacts.get(key) or []) if isinstance(item, dict)]
        entry['artifact_count'] = len(names)
        entry['artifacts'] = names[:_BRIEF_ARTIFACTS_MAX]
    return entry


def _stack_digest(stack: dict) -> dict:
    entry = {'id': stack.get('id'), 'side': stack.get('side'),
             'creature': stack.get('creature'), 'count': stack.get('count'),
             'hex': stack.get('hex')}
    entry.update(_optional_fields(stack, _STACK_OBS_FIELDS))
    return entry


def _town_digest(t: dict) -> dict:
    entry = {'i': t['index'], 'name': t['name'], 'pos': t.get('pos'),
             'buildables': [o['id'] for o in t.get('build_options', [])],
             'built': len(t.get('built') or []),
             'dwellings': t.get('dwellings'), 'garrison': t.get('garrison'),
             'available_heroes': t.get('available_heroes')}
    # T40/C11: canonical interaction tile (`tile`) when the client provides it.
    tile = t.get('tile')
    if isinstance(tile, list) and tile:
        entry['tile'] = list(tile)[:3]
    return entry


def _town_brief(t: dict) -> dict:
    """Compact per-town view for game_brief (same projection as before + optional tile)."""
    entry = {'i': t['i'], 'name': t['name'], 'pos': t.get('pos'),
             'buildables': t['buildables'],
             'built': t.get('built'),
             'dwellings': [(d.get('level'), d.get('available'))
                           for d in t.get('dwellings') or []],
             'garrison': sum(g['count'] for g in t.get('garrison') or []),
             'hire': [h.get('name') for h in t.get('available_heroes') or []]}
    tile = t.get('tile')
    if isinstance(tile, list) and tile:
        entry['tile'] = list(tile)[:3]
    return entry


def _digest(st: dict) -> dict:
    out = {
        'day': (st.get('date') or {}).get('day'), 'status': st.get('status'),
        'resources': st.get('resources'), 'query': st.get('query'),
        'battle_active': (st.get('battle') or {}).get('active'),
        'heroes': [_hero_digest(hero) for hero in st.get('heroes') or []],
        'towns': [_town_digest(t) for t in st.get('towns') or []],
        'objects': [{k: o.get(k) for k in ('id', 'kind', 'type', 'name', 'pos', 'visitable',
                                           'anchor', 'tile', 'owner', 'hover')
                     if o.get(k) is not None}
                    for o in st.get('objects') or []],
        # Player-visible Info-window log (already in state.json; was previously dropped here).
        'recent_info': _recent_info(st),
    }
    battle = st.get('battle') or {}
    if battle.get('active'):
        # A battle needs per-stack detail to choose orders; without it the player would have to
        # call a second tool and still not see counts/hexes.  Fixed projection, no hidden data.
        stacks = [s for s in battle.get('stacks') or [] if isinstance(s, dict)]
        out['battle'] = {
            'active': battle.get('active_stack'), 'my_side': battle.get('my_side'),
            'stacks': [_stack_digest(stack) for stack in stacks],
        }
        # T33/P-B: round / our hero's battle mana+spellbook / casts this battle (all UI-visible).
        out['battle'].update(_optional_fields(battle, _BATTLE_OBS_FIELDS))
    return out


# --------------------------------------------------------- decision events (persisted)
# Decision points are persisted to <dir>/events.jsonl with monotonic ids, so they survive context
# compaction and server restarts.  <dir>/events_cursor.json remembers which ids the client has
# already read; game_events() returns the last 20 events plus everything unread.  There is no
# in-memory battle edge any more: transitions are diffed against <dir>/events_state.json.
_EVENTS_LOCK = threading.Lock()
_EVENT_MAX_ENTRIES = 500


def _json_load(path: Path, default):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def _atomic_write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + '.tmp')
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding='utf-8')
        os.replace(tmp, path)
    except OSError:
        pass


@contextmanager
def _file_lock(name: str, timeout: float = 10.0):
    """Cross-process writer lock (O_EXCL lock file) for read-modify-replace sections.

    The atomic file replace only prevents half-written files; without a lock two processes can
    still lose an update (read old revision -> both write).  Stale locks older than 10 s are
    reclaimed so a crashed writer cannot block the game forever.
    """
    lock_path = _dir() / f'.lock_{name}'
    fd = None
    deadline = time.time() + timeout
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode('ascii'))
            except OSError:
                pass
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > 10:
                    lock_path.unlink()
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                raise RuntimeError(f'file lock {name} is busy')
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            lock_path.unlink()
        except OSError:
            pass


def _events_state_path() -> Path:
    return _dir() / 'events_state.json'


def _events_path() -> Path:
    return _dir() / 'events.jsonl'


def _events_cursor_path() -> Path:
    return _dir() / 'events_cursor.json'


def _event_state_default() -> dict:
    return {'next_id': 1, 'battle_active': False, 'query_sig': None,
            'game_status': None, 'levels': {}}


def _query_sig(query) -> str | None:
    if not query:
        return None
    try:
        return json.dumps(query, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(query)


def _append_events(events: list) -> None:
    if not events:
        return
    path = _events_path()
    try:
        with path.open('a', encoding='utf-8') as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False) + '\n')
        if path.stat().st_size > 512 * 1024:
            lines = path.read_text(encoding='utf-8').splitlines()[-_EVENT_MAX_ENTRIES:]
            path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    except OSError:
        pass


def _record_events(st: dict) -> list:
    """Diff *st* against the persisted decision state and append the new transitions."""
    if not st:
        return []
    stamp = time.strftime('%Y-%m-%d %H:%M:%S')
    with _EVENTS_LOCK, _file_lock('events_state'):
        state = _json_load(_events_state_path(), _event_state_default())
        try:
            state['next_id'] = int(state.get('next_id') or 1)
        except (TypeError, ValueError):
            state['next_id'] = 1
        new: list = []

        def add(kind: str, detail: dict) -> None:
            event = {'id': int(state['next_id']), 'ts': stamp, 'type': kind,
                     'day': (st.get('date') or {}).get('day')}
            event.update(detail)
            state['next_id'] = int(state['next_id']) + 1
            new.append(event)

        battle = st.get('battle') or {}
        battle_now = bool(battle.get('active'))
        if battle_now and not state.get('battle_active'):
            add('battle_started', {'my_side': battle.get('my_side'),
                                   'active_stack': battle.get('active_stack'),
                                   'stacks': len(battle.get('stacks') or [])})
        if state.get('battle_active') and not battle_now:
            add('battle_ended', {'status': st.get('status')})
        state['battle_active'] = battle_now
        sig = _query_sig(st.get('query'))
        if sig is not None and sig != state.get('query_sig'):
            add('query_opened', {'query': st.get('query')})
        state['query_sig'] = sig
        game_status = st.get('status')
        if game_status in ('WINNER', 'LOSER') and state.get('game_status') != game_status:
            add('game_over', {'status': game_status})
        state['game_status'] = game_status
        levels = state.get('levels') if isinstance(state.get('levels'), dict) else {}
        for hero in st.get('heroes') or []:
            level = hero.get('level') if isinstance(hero, dict) else None
            idx = hero.get('index') if isinstance(hero, dict) else None
            if isinstance(level, (int, float)) and not isinstance(level, bool) and idx is not None:
                old = levels.get(str(idx))
                if isinstance(old, (int, float)) and level > old:
                    add('level_up', {'hero': idx, 'level': level})
                levels[str(idx)] = level
        state['levels'] = levels
        _atomic_write_json(_events_state_path(), state)
        _append_events(new)
        return new


def _read_events() -> list:
    try:
        lines = _events_path().read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and 'id' in event and 'type' in event:
            events.append(event)
    return events


def _read_event_cursor() -> int:
    data = _json_load(_events_cursor_path(), {'last_id': 0})
    try:
        return int(data.get('last_id') or 0)
    except (TypeError, ValueError):
        return 0


def _write_event_cursor(last_id: int) -> None:
    _atomic_write_json(_events_cursor_path(),
                       {'last_id': int(last_id),
                        'updated_at': time.strftime('%Y-%m-%d %H:%M:%S')})


def _current_decisions(st: dict) -> list:
    """Stateless decision points visible in the current observation (same shape as before)."""
    points: list = []
    if not st:
        return points
    if st.get('query'):
        points.append({'type': 'query_opened', 'query': st.get('query')})
    battle = st.get('battle') or {}
    if battle.get('active'):
        points.append({'type': 'battle_started', 'my_side': battle.get('my_side'),
                       'active_stack': battle.get('active_stack'),
                       'stacks': battle.get('stacks')})
    if battle.get('active_stack') == -1:
        points.append({'type': 'deployment', 'active_stack': -1})
    if st.get('status') in ('WINNER', 'LOSER'):
        points.append({'type': 'game_over', 'status': st.get('status')})
    return points


def _decision_bundle(st: dict) -> list:
    """Persist transitions and merge them with the stateless points (deduplicated by type).

    Current stateless points win over the persisted transition events because they carry the
    full battle detail (stacks/active_stack) that the battle tools need; transitions add the
    events that are only visible as a history (battle_ended, level_up).
    """
    transitions = _record_events(st)
    points: list = []
    seen: set = set()
    for point in _current_decisions(st):
        if point.get('type') not in seen:
            points.append(point)
            seen.add(point.get('type'))
    for event in transitions:
        if event.get('type') not in seen:
            points.append(event)
            seen.add(event.get('type'))
    return points


@mcp.tool()
def lease_acquire(force: bool = False, reason: str | None = None) -> dict:
    """Take the single-writer lease (LLM owns the game while it holds this).

    A valid lease held by another writer is NOT overwritten: pass force=True (and optionally a
    short reason) for an explicit, ledger-recorded takeover after the holder is known to be gone.
    Expired leases are re-acquirable without force.
    """
    global _lease_token
    path = _dir() / 'lease.json'
    try:
        existing = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        existing = None
    if isinstance(existing, dict) and existing.get('owner') == 'llm':
        try:
            expires = float(existing.get('expires_at') or 0)
        except (TypeError, ValueError):
            expires = 0.0
        other_writer_holds = expires > time.time() and existing.get('token') != _lease_token
        if other_writer_holds and not force:
            return {'ok': False, 'error': 'lease_held',
                    'lease': {key: existing.get(key) for key in ('owner', 'expires_at')},
                    'hint': 'a valid lease is held by another writer; wait for expiry or call '
                            'lease_acquire(force=True, reason=...) explicitly (ledger-recorded)'}
    new_token = uuid.uuid4().hex
    _lease_token = new_token
    lease = _lease_record(new_token)
    _atomic_write_json(path, lease)
    if force and isinstance(existing, dict) and existing.get('token') != new_token:
        _ledger({'action': 'lease_acquire', 'source': 'llm-takeover',
                 'detail': {'reason': (reason or '')[:200],
                            'previous_expires_at': existing.get('expires_at')}})
    return {'ok': True, 'lease': lease,
            'took_over': bool(force and isinstance(existing, dict))}


@mcp.tool()
def lease_release() -> dict:
    """Release the lease so a maintenance process may take over (no auto-play is started).

    Only the writer that currently holds the lease may release it: a stale owner cannot delete
    a newer writer's lease file.
    """
    global _lease_token
    path = _dir() / 'lease.json'
    try:
        existing = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        existing = None
    if isinstance(existing, dict) and existing.get('owner') == 'llm':
        if not _lease_token or existing.get('token') != _lease_token:
            return {'ok': False, 'error': 'lease_not_held',
                    'detail': 'the lease file belongs to another writer; it cannot be '
                              'released by this session'}
    _lease_token = None
    try:
        path.unlink()
    except OSError:
        pass
    return {'ok': True}


# Evidence-chain files: the per-run linkage between engine segments, timeline epochs, restore
# receipts and recording clips.  A brand-new game must NOT inherit them (ISSUE-01: the WIN run's
# first recording segment pointed parent/restore_ref into the previous run because these files
# survived _rotate_run).  _rotate_run archives them per run and resets them; a resume/replay
# within one run never rotates, so its parent chain and restore_ref stay intact.
_RECORDING_SEGMENTS_FILE = 'recording_segments.jsonl'
_CHAIN_FILES = ('segments.jsonl', 'epochs.jsonl', 'restores.jsonl', 'restore.json',
                _RECORDING_SEGMENTS_FILE)
_RUN_EVIDENCE_FILES = ('actions.jsonl', 'results.log', 'events.jsonl', 'events_cursor.json',
                       'events_state.json', 'commands.txt', 'state.json', 'state_epoch.json',
                       'start_options.txt', 'start_options_applied.txt', 'start_config.json',
                       'overlay.jsonl', 'overlay_history.jsonl', 'overlay.jsonl.prev',
                       'overlay_history.1.jsonl', 'town_info.json', 'army_info.json',
                       'battle_actions.json', 'saves.json', 'unresolved.json',
                       'last_battle_round.json', 'recording.json', 'recording.lock',
                       'action_index.json', 'run.json', 'epochs.jsonl', 'segments.jsonl',
                       'restore.json', 'restores.jsonl', 'lease.json', 'ui_tree.json',
                       'prep_summary.json', 'cheat.txt', _RECORDING_SEGMENTS_FILE)


def _archive_run(old_run: dict) -> dict | None:
    """R6: copy the previous run's raw evidence under <dir>/archive/<run_id>/ before rotating.

    session_start used to reset run.json / actions.jsonl / events in place, so the old raw
    evidence of an earlier game in the same state dir was lost.  This only copies (never moves)
    so the active directory is untouched for the caller that rotates it.
    """
    import shutil
    run_id = str((old_run or {}).get('run_id') or '')
    if not run_id or run_id == 'new':
        return None
    try:
        target = _dir() / 'archive' / run_id
        target.mkdir(parents=True, exist_ok=True)
        copied = []
        for name in _RUN_EVIDENCE_FILES:
            src = _dir() / name
            if not src.exists():
                continue
            try:
                shutil.copy2(src, target / name)
                copied.append(name)
            except OSError:
                continue
        entry = {'run_id': run_id, 'branch_id': (old_run or {}).get('branch_id'),
                 'epoch': (old_run or {}).get('epoch'), 'archived_at':
                 time.strftime('%Y-%m-%d %H:%M:%S'), 'files': copied}
        with (target.parent / 'index.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        return entry
    except OSError:
        return None


def _next_chain_archive_dir(run_id: str) -> Path:
    """Unique `archive/<run_id>/chain-<n>/` dir (never reuses an earlier chain archive)."""
    base = _dir() / 'archive' / (run_id or 'unidentified')
    number = 1
    while (base / f'chain-{number}').exists():
        number += 1
    return base / f'chain-{number}'


def _archive_chain_files(old_run: dict) -> dict | None:
    """Archive the previous run's identity-chain files before a new game resets them (ISSUE-01).

    Copies (never moves) segments.jsonl / epochs.jsonl / restores.jsonl / restore.json /
    recording_segments.jsonl to `archive/<old_run>/chain-<n>/` with a per-file sha256 manifest and
    an `archive/index.jsonl` entry (`kind=chain_reset`).  Returns None when no chain file exists.
    The caller deletes the active copies only after this returns, so old evidence is never lost.
    """
    import shutil
    run_id = str((old_run or {}).get('run_id') or '') or 'unidentified'
    present = [name for name in _CHAIN_FILES if (_dir() / name).is_file()]
    if not present:
        return None
    try:
        target = _next_chain_archive_dir(run_id)
        target.mkdir(parents=True, exist_ok=False)
    except OSError:
        return None
    archived_at = time.strftime('%Y-%m-%d %H:%M:%S')
    manifest: dict = {'kind': 'chain_reset', 'reason': 'rotate:new_game',
                      'run_id': run_id, 'branch_id': (old_run or {}).get('branch_id'),
                      'epoch': (old_run or {}).get('epoch'), 'archived_at': archived_at,
                      'dir': str(target), 'files': []}
    for name in present:
        source = _dir() / name
        try:
            info = source.stat()
            shutil.copy2(source, target / name)
            manifest['files'].append({'name': name, 'bytes': info.st_size,
                                      'mtime_iso': time.strftime(
                                          '%Y-%m-%d %H:%M:%S',
                                          time.localtime(info.st_mtime)),
                                      'sha256': _file_sha256(source),
                                      'run_id': run_id})
        except OSError:
            continue
    if not manifest['files']:
        return None
    try:
        (target / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                              encoding='utf-8')
        index_entry = {'kind': 'chain_reset', 'run_id': run_id,
                       'branch_id': manifest.get('branch_id'), 'epoch': manifest.get('epoch'),
                       'archived_at': archived_at, 'dir': str(target),
                       'files': [entry['name'] for entry in manifest['files']]}
        with (target.parent.parent / 'index.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(index_entry, ensure_ascii=False) + '\n')
    except OSError:
        pass
    return manifest


def _chain_archive_summary(manifest: dict | None) -> dict | None:
    """Compact chain-reset record for run.json / session_start (no per-file duplication)."""
    if not manifest:
        return None
    return {'kind': manifest.get('kind'), 'reason': manifest.get('reason'),
            'dir': manifest.get('dir'), 'archived_at': manifest.get('archived_at'),
            'run_id': manifest.get('run_id'), 'branch_id': manifest.get('branch_id'),
            'epoch': manifest.get('epoch'),
            'files': [{'name': entry.get('name'), 'bytes': entry.get('bytes'),
                       'sha256': entry.get('sha256')}
                      for entry in manifest.get('files') or []]}


def _bump_epoch(source: str, detail: dict | None = None,
                parent: dict | None = None) -> dict:
    """R6: a load (or another timeline change) starts a new logical epoch + branch.

    Every record carries its parent (branch/epoch/watermark) so the identity chain can be walked
    from epochs.jsonl even after the active run.json moves on.
    """
    data = _run_identity()
    watermark = _ledger_watermark().get('actions_count')
    parent = dict(parent or {})
    parent.setdefault('parent_run_id', data.get('run_id'))
    parent.setdefault('parent_branch_id', data.get('branch_id'))
    parent.setdefault('parent_epoch', data.get('epoch'))
    parent.setdefault('parent_watermark', watermark)
    epoch = int(data.get('epoch') or 0) + 1
    started = time.time()
    data.update({'epoch': epoch, 'epoch_started_at': started,
                 'branch_id': f'{source}-{epoch}', 'epoch_source': source,
                 'epoch_updated_at': time.strftime('%Y-%m-%d %H:%M:%S')})
    data.update(parent)
    try:
        (_dir() / 'run.json').write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                         encoding='utf-8')
        record = {'epoch': epoch, 'source': source, 'at': time.strftime('%Y-%m-%d %H:%M:%S'),
                  'started_at': started, 'run_id': data.get('run_id'),
                  'branch_id': data.get('branch_id')}
        record.update(parent)
        if detail:
            record['detail'] = detail
        with (_dir() / 'epochs.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except OSError:
        pass
    return data


def _reset_chain_files(archived: dict | None) -> dict:
    """Remove the active chain files, but NEVER delete data that was not archived.

    `archived` is the manifest returned by _archive_chain_files (None when there was nothing to
    archive or the copy failed).  When an archive exists the active copies are deleted; when the
    copy failed an existing file is renamed to `<name>.stale-<stamp>` so the new run still starts
    with a clean chain while the old evidence stays on disk for manual recovery.
    """
    removed: list[str] = []
    kept: list[str] = []
    stamp = time.strftime('%Y%m%d-%H%M%S')
    for name in _CHAIN_FILES:
        path = _dir() / name
        if not path.is_file():
            continue
        if archived is not None:
            try:
                path.unlink()
                removed.append(name)
            except OSError:
                kept.append(name)
        else:
            try:
                path.replace(path.with_name(f'{name}.stale-{stamp}'))
                kept.append(f'{name}.stale-{stamp}')
            except OSError:
                kept.append(name)
    return {'removed': removed, 'kept': kept}


def _rotate_run(archive: bool = True) -> dict:
    """New logical game: archive the old evidence, new run_id + reset request index/events.

    Internal helper (not an MCP tool): only session_start() rotates a run.  `archive=False` is
    used by session_start after it already captured a `archive/<run>/segment-<n>/` manifest
    BEFORE the launcher cleared the active files (N1 ordering); calling with the default keeps
    the legacy behaviour used by direct callers/tests.

    R8/ISSUE-01: the identity-chain files (segments.jsonl, epochs.jsonl, restores.jsonl,
    restore.json, recording_segments.jsonl) are per-run evidence, not append-only global history.
    They are archived under `archive/<old_run>/chain-<n>/` (copy + sha256 manifest, never lost)
    and then reset, so the new run's first recording segment has parent=None / restore_ref=None
    instead of pointing into a previous run.  A resume/replay within the same run never rotates,
    so its parent chain and restore_ref remain intact.
    """
    try:
        old = _run_identity()
    except Exception:  # noqa: BLE001 - a corrupt run.json must not block a new game
        old = {}
    if archive:
        _archive_run(old)
    chain_archive = _archive_chain_files(old)
    data = {'schema_version': 1, 'run_id': time.strftime('%Y%m%d-%H%M%S'),
            'branch_id': 'main', 'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'epoch': 0, 'epoch_started_at': time.time(), 'segments': 0}
    summary = _chain_archive_summary(chain_archive)
    try:
        # Do not delete the old overlay feed: recording_start rotates it per clip and the
        # archiver copied it already.  Reset only what the new run must not inherit.
        for name in ('actions.jsonl', 'events.jsonl', 'events_cursor.json', 'events_state.json',
                     'unresolved.json', 'saves.json', 'last_battle_round.json'):
            try:
                (_dir() / name).unlink()
            except OSError:
                pass
        # The chain files were copied by _archive_chain_files above (or by the pre-start segment
        # archive in the session_start path): remove the active copies so the new run starts with
        # a clean parent/restore chain.  Unarchived data is renamed, never deleted.
        chain_reset = _reset_chain_files(chain_archive)
        if summary:
            summary['reset'] = chain_reset
            data['chain_reset'] = summary
        _atomic_write_json(_action_index_path(), {})
        (_dir() / 'run.json').write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                         encoding='utf-8')
    except OSError:
        pass
    return data


# ---------------------------------------------------------------- H3 advanced start options
# The bridge reads <dir>/start_options.txt moments after --agent-start pushes the map into the
# local lobby, then applies difficulty / human player / starting town / hero / bonus / timers
# through the same lobby API the H3 "advanced options" screen uses.  Names (factions, heroes,
# difficulty levels) are resolved here against the mod config that ships with the client.
_CONFIG_DIR = ROOT / 'vendor' / 'vcmi-src' / 'build' / 'bin' / 'config'
_DIFFICULTY_NAMES = {'easy': 0, 'pawn': 0, 'normal': 1, 'knight': 1, 'hard': 2, 'rook': 2,
                     'expert': 3, 'queen': 3, 'impossible': 4, 'king': 4}
_PLAYER_COLORS = ['red', 'blue', 'tan', 'green', 'orange', 'purple', 'teal', 'pink']
_START_BONUSES = {'random': -1, 'artifact': 0, 'gold': 1, 'resource': 2}
_OPTION_KEYS = ('difficulty', 'player', 'town', 'hero', 'bonus', 'turn_time', 'battle_time', 'unit_time')


def _as_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text and (text.lstrip('-').isdigit()):
            return int(text)
    return None


def _load_json_relaxed(path) -> dict:
    """Parse a VCMI mod-config JSON file: it may contain // comments and trailing commas."""
    text = Path(path).read_text(encoding='utf-8-sig')
    text = re.sub(r'//[^\n]*', '', text)
    text = re.sub(r',(\s*[}\]])', r'\1', text)
    return json.loads(text)


def _config_choices(subdir: str, extra_files: tuple = ()) -> dict:
    """identifier -> numeric id, read from the client's mod config (heroes/factions JSON)."""
    out: dict = {}
    if not _CONFIG_DIR.exists():
        return out
    for path in sorted((_CONFIG_DIR / subdir).glob('*.json')) + [Path(p) for p in extra_files]:
        if not path.exists():
            continue
        try:
            data = _load_json_relaxed(path)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        # hero/faction files either map identifier->{index:...} or hold the entries directly
        candidates = list(data.items())
        for key, value in candidates:
            if isinstance(value, dict) and isinstance(value.get('index'), int):
                out.setdefault(str(key).lower(), int(value['index']))
    return out


def _faction_name_id(value):
    if value is None:
        return None
    direct = _as_int(value)
    if direct is not None:
        return direct
    name = str(value).strip().lower()
    if name == 'random':
        return -1
    table = _config_choices('factions')
    if name in table:
        return table[name]
    aliases = {'academy': 'tower', 'wizard': 'tower', 'dungeon': 'dungeon', 'castel': 'castle'}
    name = aliases.get(name, name)
    return table.get(name)


def _hero_name_id(value):
    if value is None:
        return None
    direct = _as_int(value)
    if direct is not None:
        return direct
    name = str(value).strip().lower()
    if name == 'random':
        return -1
    table = _config_choices('heroes')
    return table.get(name)


def _difficulty_id(value):
    if value is None:
        return None
    direct = _as_int(value)
    if direct is not None:
        return direct if 0 <= direct <= 4 else None
    return _DIFFICULTY_NAMES.get(str(value).strip().lower())


def _player_color_id(value):
    if value is None:
        return None
    direct = _as_int(value)
    if direct is not None:
        return direct if 0 <= direct <= 7 else None
    name = str(value).strip().lower()
    if name in _PLAYER_COLORS:
        return _PLAYER_COLORS.index(name)
    return None


def _bonus_id(value):
    if value is None:
        return None
    direct = _as_int(value)
    if direct is not None:
        return direct if -1 <= direct <= 2 else None
    return _START_BONUSES.get(str(value).strip().lower())


def _build_start_options(options: dict) -> tuple[dict, dict[str, str], list[str]]:
    """Resolve the user-facing advanced options into the numeric lines read by the bridge."""
    resolved: dict = {}
    lines: dict[str, str] = {}
    errors: list[str] = []
    resolvers = {
        'difficulty': ('difficulty', _difficulty_id),
        'player': ('player', _player_color_id),
        'town': ('town', _faction_name_id),
        'hero': ('hero', _hero_name_id),
        'bonus': ('bonus', _bonus_id),
        'turn_time': ('turn_time', _as_int),
        'battle_time': ('battle_time', _as_int),
        'unit_time': ('unit_time', _as_int),
    }
    source = {'difficulty': options.get('difficulty'), 'player': options.get('player_color'),
              'town': options.get('starting_town'), 'hero': options.get('starting_hero'),
              'bonus': options.get('starting_bonus'), 'turn_time': options.get('turn_time_seconds'),
              'battle_time': options.get('battle_time_seconds'), 'unit_time': options.get('unit_time_seconds')}
    for user_key, raw in source.items():
        if raw is None:
            continue
        line_key, resolver = resolvers[user_key]
        value = resolver(raw)
        if value is None:
            errors.append(f'{line_key}={raw!r} is not a valid value')
            continue
        if value < 0 and line_key in ('turn_time', 'battle_time', 'unit_time'):
            errors.append(f'{line_key} must be >= 0 seconds (0 = unlimited)')
            continue
        lines[line_key] = str(value)
        resolved[line_key] = value
    if 'turn_time' in lines and int(lines['turn_time']) == 0:
        # 0 explicitly means unlimited; the bridge maps -1/0 to "not limited"
        lines['turn_time'] = '0'
    return resolved, lines, errors


def _write_start_options(lines: dict[str, str]) -> Path:
    path = _dir() / 'start_options.txt'
    body = ''.join(f'{key}={lines[key]}\n' for key in _OPTION_KEYS if key in lines)
    path.write_text(body, encoding='utf-8')
    return path


def _clear_start_options() -> None:
    for name in ('start_options.txt', 'start_options_applied.txt'):
        try:
            (_dir() / name).unlink()
        except OSError:
            pass


def _read_start_option_results() -> list[str]:
    """Return the bridge's start_option lines (loaded / applied / effective) from results.log."""
    out: list[str] = []
    try:
        lines = (_dir() / 'results.log').read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return out
    for line in lines[-200:]:
        text = line.strip()
        if 'start_option' in text:
            out.append(text)
    return out[-12:]


# ---------------------------------------------------------------- N1 restore / archive / W0
# A segment is one engine execution.  The session/restore transaction order is:
#   resolve target -> check sidecar+file identity -> archive the outgoing segment BEFORE the
#   launcher clears anything -> start -> prove the loaded target (day+fingerprint) -> only then
#   commit a new run/epoch/branch + restore receipt.  A failed proof never commits identity.
CLIENT_LOG_PATH = ROOT / 'var' / 'logs' / 'vcmi' / 'VCMI_Client_log.txt'
MAP_FILE_DIRS = [Path.home() / 'Documents' / 'My Games' / 'vcmi' / 'Maps',
                 Path.home() / 'Documents' / 'My Games' / 'vcmi' / 'maps',
                 ROOT / 'vendor' / 'vcmi-src' / 'build' / 'bin' / 'Maps']
_RESTORE_WAIT_SECONDS = 60.0
_RESTORE_POLL_SECONDS = 1.0

# Every active file that must be captured before a new engine segment may overwrite it.
_ARCHIVE_FILES = ('actions.jsonl', 'results.log', 'events.jsonl', 'events_cursor.json',
                  'events_state.json', 'commands.txt', 'state.json', 'state_epoch.json',
                  'start_options.txt', 'start_options_applied.txt', 'start_config.json',
                  'overlay.jsonl', 'overlay_history.jsonl', 'overlay.jsonl.prev',
                  'overlay_history.1.jsonl', 'saves.json', 'run.json', 'epochs.jsonl',
                  'recording.json', 'recording.lock', 'unresolved.json', 'action_index.json',
                  'last_battle_round.json', 'battle_actions.json', 'town_info.json',
                  'army_info.json', 'ui_tree.json', 'segments.jsonl', 'lease.json',
                  'prep_summary.json', 'restore.json', 'restores.jsonl', 'cheat.txt',
                  _RECORDING_SEGMENTS_FILE)


def _file_sha256(path: Path) -> str | None:
    """Full-file sha256 (streamed so a large save cannot be read into memory); None on error."""
    digest = hashlib.sha256()
    try:
        with open(path, 'rb') as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b''):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _write_run_identity(data: dict) -> None:
    try:
        (_dir() / 'run.json').write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                         encoding='utf-8')
    except OSError:
        pass


def _next_segment_archive_dir(run_id: str) -> Path:
    base = _dir() / 'archive' / (run_id or 'unidentified')
    number = 1
    while (base / f'segment-{number}').exists():
        number += 1
    return base / f'segment-{number}'


def _archive_segment_before_start(reason: str) -> dict | None:
    """Copy the ACTIVE run's raw evidence to a unique `archive/<run>/segment-<n>/` dir.

    Must be called BEFORE VcmiAgent.start() (it truncates results.log/commands.txt and rewrites
    state_epoch.json) and before _write_start_options() overwrites start_options.txt, otherwise
    the archive would only contain the cleared files.  Never overwrites an older segment.
    """
    run = _run_identity()
    present = [name for name in _ARCHIVE_FILES if (_dir() / name).is_file()]
    if not present:
        return None
    try:
        target = _next_segment_archive_dir(str(run.get('run_id') or ''))
        target.mkdir(parents=True, exist_ok=False)
    except OSError:
        return None
    archived_at = time.strftime('%Y-%m-%d %H:%M:%S')
    manifest: dict = {'segment_id': f"{run.get('run_id')}/{target.name}",
                      'run_id': run.get('run_id'), 'branch_id': run.get('branch_id'),
                      'epoch': run.get('epoch'), 'archived_at': archived_at,
                      'reason': reason, 'captured_before_start': True, 'files': []}
    for name in present:
        source = _dir() / name
        try:
            info = source.stat()
            shutil.copy2(source, target / name)
            manifest['files'].append({
                'name': name, 'source': str(source), 'bytes': info.st_size,
                'mtime': info.st_mtime,
                'mtime_iso': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(info.st_mtime)),
                'sha256': _file_sha256(source), 'archived_at': archived_at,
                'reason': reason, 'run_id': run.get('run_id'),
                'branch_id': run.get('branch_id'), 'epoch': run.get('epoch')})
        except OSError:
            continue
    try:
        (target / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                              encoding='utf-8')
        index_entry = {'segment_id': manifest['segment_id'], 'run_id': run.get('run_id'),
                       'branch_id': run.get('branch_id'), 'epoch': run.get('epoch'),
                       'reason': reason, 'captured_before_start': True,
                       'archived_at': archived_at, 'dir': str(target),
                       'files': [entry['name'] for entry in manifest['files']]}
        with (target.parent.parent / 'index.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(index_entry, ensure_ascii=False) + '\n')
    except OSError:
        pass
    return manifest


def _archive_summary(archive: dict | None) -> dict | None:
    if not archive:
        return None
    return {'segment_id': archive.get('segment_id'), 'run_id': archive.get('run_id'),
            'branch_id': archive.get('branch_id'), 'epoch': archive.get('epoch'),
            'captured_before_start': archive.get('captured_before_start'),
            'files': [entry.get('name') for entry in archive.get('files') or []]}


def _ledger_watermark() -> dict:
    """Stable replay watermark: number of ledger lines + the last action id in this run."""
    count = 0
    last_action_id = None
    try:
        with open(_dir() / 'actions.jsonl', encoding='utf-8') as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                count += 1
                try:
                    entry = json.loads(text)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get('action_id'):
                    last_action_id = entry['action_id']
    except OSError:
        pass
    run = _run_identity()
    return {'actions_count': count, 'last_action_id': last_action_id,
            'run_id': run.get('run_id'), 'branch_id': run.get('branch_id'),
            'epoch': run.get('epoch')}


def _current_revision() -> int | None:
    """CURRENT.md front-matter revision for the active run/branch (None when absent)."""
    try:
        meta, _body = _parse_front_matter(_memory_path('game', 'current').read_text(
            encoding='utf-8'))
        value = _as_int(meta.get('revision'))
        return value
    except (OSError, ValueError, TypeError):
        return None


def _state_file_snapshot() -> dict:
    """Raw state.json fields BEFORE a start (the file is stale evidence, not a live read)."""
    try:
        data = json.loads((_dir() / 'state.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {'session_id': data.get('session_id'), 'ts_wall': data.get('ts_wall'),
            'day': (data.get('date') or {}).get('day'), 'in_game': data.get('in_game')}


def _resolve_save_target(target: str) -> dict | None:
    """'Saves/<subdir>/<name>' (or an absolute *.vsgm1 path) -> {slot, subdir, safe, path}."""
    text = (target or '').strip()
    if not text:
        return None
    if text.lower().endswith('.vsgm1'):
        without_suffix = text[:-len('.vsgm1')]
    else:
        without_suffix = text
    if text.startswith('Saves/'):
        parts = [part for part in without_suffix.split('/') if part][1:]
        if not parts:
            return None
        name = parts[-1]
        subdir = parts[0] if len(parts) > 1 else SAVE_SUBDIR
        safe = _safe_save_name(name)
        if not safe or not re.fullmatch(r'[A-Za-z0-9_-]+', subdir):
            return None
        return {'slot': f'Saves/{subdir}/{safe}', 'subdir': subdir, 'name': safe,
                'safe': safe, 'path': SAVES_DIR / subdir / f'{safe}.vsgm1'}
    if text.lower().endswith('.vsgm1'):
        path = Path(text)
        safe = _safe_save_name(path.stem)
        if not safe:
            return None
        return {'slot': str(path), 'subdir': path.parent.name, 'name': safe,
                'safe': safe, 'path': path}
    return None


def _find_save_sidecar(safe: str, target_path: Path) -> dict | None:
    """Save metadata + the sidecar file it came from: current saves.json first, then archives."""
    current = _load_saves_meta().get(safe)
    if isinstance(current, dict):
        return {'meta': current, 'path': _saves_meta_path()}
    candidates: list[Path] = []
    try:
        for path in (_dir() / 'archive').rglob('saves.json'):
            if path.is_file():
                candidates.append(path)
    except OSError:
        candidates = []
    candidates.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0.0, reverse=True)
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        meta = data.get(safe)
        if isinstance(meta, dict):
            return {'meta': meta, 'path': path}
    return None


def _engine_identity(agent, st: dict) -> dict:
    """Engine/build identity for restore receipts (load_game and session_start resume).

    The live agent instance normally carries the pinned contract, but the load_game path uses the
    lightweight `_agent()` helper whose instance may have no contract loaded: fall back to the
    owned session notes (client_sha256/contract_match) and, as a last read-only step, to
    contract_status().  Missing values are null with an explicit reason, never an exception.
    """
    session = getattr(agent, 'session', None)
    contract = dict(getattr(agent, 'contract', None) or {})
    session_file: dict = {}
    try:
        data = json.loads((_dir() / 'session.json').read_text(encoding='utf-8'))
        session_file = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        session_file = {}
    exe_sha256 = contract.get('actual_sha256')
    session_match: bool | None = None
    for note in session_file.get('notes') or []:
        if not isinstance(note, str):
            continue
        # vcmi_agent writes ONE note string with several space-separated key=value tokens.
        for token in note.split():
            if '=' not in token:
                continue
            key, _sep, value = token.partition('=')
            key, value = key.strip(), value.strip()
            if key == 'client_sha256' and not exe_sha256:
                exe_sha256 = value
            elif key == 'contract_match':
                session_match = value == 'True'
    if not exe_sha256:
        status_fn = getattr(agent, 'contract_status', None)
        if callable(status_fn):
            try:
                fallback = status_fn() or {}
                exe_sha256 = fallback.get('actual_sha256') or exe_sha256
                if 'match' not in contract:
                    contract['match'] = fallback.get('match')
            except Exception:  # noqa: BLE001 - identity must never block a restore
                pass
    protocol = st.get('protocol')
    if protocol is None:
        try:
            protocol = json.loads((_dir() / 'state.json').read_text(
                encoding='utf-8')).get('protocol')
        except (OSError, ValueError):
            protocol = None
    client_pid = getattr(session, 'client_pid', None)
    if client_pid is None:
        client_pid = session_file.get('client_pid')
    session_id = getattr(session, 'session_id', None)
    if session_id is None:
        session_id = session_file.get('session_id')
    engine = {'session_id': session_id, 'client_pid': client_pid,
              'protocol': protocol, 'exe_sha256': exe_sha256,
              'exe_sha256_match': bool(contract.get('match')) if 'match' in contract
              else bool(session_match),
              'engine_started_at': getattr(session, 'created_at', None)}
    if session_id is None or client_pid is None:
        engine['reason'] = 'no owned client session observable'
    if exe_sha256 is None:
        engine['exe_sha256_reason'] = ('client exe hash not observable '
                                       '(contract and session note missing)')
    return engine


def _prove_restore_target(agent, st: dict, meta: dict) -> tuple[dict, dict | None]:
    """Wait for the post-load state and compare it to the TARGET sidecar (not 'it changed')."""
    expected_day = _as_int(meta.get('day'))
    target_fp = meta.get('fingerprint') if isinstance(meta.get('fingerprint'), dict) else None
    deadline = time.time() + _RESTORE_WAIT_SECONDS
    observed = st if isinstance(st, dict) else {}
    while time.time() < deadline:
        day = (observed.get('date') or {}).get('day')
        if observed.get('in_game') and (expected_day is None or day == expected_day):
            break
        time.sleep(_RESTORE_POLL_SECONDS)
        try:
            observed = agent.state(timeout=10)
        except Exception:
            observed = {}
    day = (observed.get('date') or {}).get('day')
    after_fp = _state_fingerprint(observed)
    problems: list[str] = []
    if not observed.get('in_game'):
        problems.append('state is not in_game after the restore')
    if expected_day is not None and day != expected_day:
        problems.append(f'observed day {day} != sidecar day {expected_day}')
    if target_fp is not None and after_fp != target_fp:
        different = sorted(key for key in set(target_fp) | set(after_fp or {})
                           if target_fp.get(key) != (after_fp or {}).get(key))
        problems.append(f'observed fingerprint != sidecar fingerprint (fields: {different})')
    precision = ('full_sha256' if meta.get('sha256')
                 else 'head_only' if meta.get('sha256_head') else 'unknown')
    if not meta:
        target_proof = 'sidecar_missing'
    elif target_fp is not None:
        target_proof = 'fingerprint_match' if not any(
            'fingerprint' in problem for problem in problems) else 'fingerprint_mismatch'
    else:
        target_proof = 'day_only'
    proof = {'target_proof': target_proof, 'expected_day': expected_day, 'observed_day': day,
             'expected_fingerprint': target_fp, 'observed_fingerprint': after_fp,
             'in_game': bool(observed.get('in_game')), 'identity_precision': precision,
             'problems': problems}
    conflict = None
    if problems:
        conflict = {'kind': 'restore_conflict', 'continuity': 'target_proof_failed',
                    'problems': problems, 'proof': proof}
    return proof, conflict


def _write_restore_receipt(receipt: dict) -> dict:
    path, history = _dir() / 'restore.json', _dir() / 'restores.jsonl'
    receipt = dict(receipt)
    receipt.setdefault('at', time.strftime('%Y-%m-%d %H:%M:%S'))
    try:
        _atomic_write_json(path, receipt)
        with history.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(receipt, ensure_ascii=False) + '\n')
    except OSError:
        pass
    return receipt


def _next_segment_id() -> str:
    run = _run_identity()
    return f"{run.get('run_id')}/segment-{int(run.get('segments') or 0) + 1}"


def _record_segment(segment: dict) -> dict:
    """Append one engine-execution segment and point run.json at it."""
    segment = dict(segment)
    segment.setdefault('segment_id', _next_segment_id())
    segment.setdefault('recorded_at', time.strftime('%Y-%m-%d %H:%M:%S'))
    # Identity is stamped on every segment line too, so a mixed legacy file can still be audited
    # per run (5B/ISSUE-01: a new game resets segments.jsonl to the current run only).
    run = _run_identity()
    segment.setdefault('run_id', run.get('run_id'))
    segment.setdefault('branch_id', run.get('branch_id'))
    segment.setdefault('epoch', run.get('epoch'))
    try:
        with (_dir() / 'segments.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(segment, ensure_ascii=False) + '\n')
    except OSError:
        pass
    run['segments'] = int(run.get('segments') or 0) + 1
    run['current_segment'] = segment['segment_id']
    _write_run_identity(run)
    return segment


def _append_epoch_record(record: dict) -> None:
    try:
        with (_dir() / 'epochs.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + '\n')
    except OSError:
        pass


def _commit_restore_identity(meta: dict, proof: dict, sidecar: dict | None,
                             save_target: dict, actual_sha: str | None, engine: dict,
                             before_state: dict, stale_run: dict) -> dict:
    """B3/B4/B5: classify continuity from the sidecar/watermark and commit a new identity.

    Only called after _prove_restore_target() found no conflict, so the previous identity is
    replaced exactly once, after the loaded target has been proven.
    """
    current_wm = _ledger_watermark()
    target_run = meta.get('run_id')
    target_branch = meta.get('branch_id')
    target_epoch = _as_int(meta.get('epoch'))
    target_wm = _as_int(meta.get('actions_count'))
    current_count = int(current_wm.get('actions_count') or 0)
    rollback = False
    reason = None
    if sidecar is None or not target_run:
        continuity = 'unverified'
        reason = 'sidecar_missing' if sidecar is None else 'sidecar_without_run_identity'
    elif target_run != stale_run.get('run_id'):
        continuity = 'cross_run_adopted'
    elif target_wm is not None and target_wm < current_count:
        continuity = 'replay'
        rollback = True
    else:
        continuity = 'resumed'
    if continuity in ('unverified', 'cross_run_adopted'):
        data = {'schema_version': 1, 'run_id': time.strftime('%Y%m%d-%H%M%S'),
                'branch_id': 'main', 'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                'epoch': 0, 'epoch_started_at': time.time(), 'segments': 0,
                'restore_slot': save_target['slot']}
        if continuity == 'cross_run_adopted':
            data.update({'parent_run_id': target_run, 'parent_branch_id': target_branch,
                         'parent_epoch': target_epoch, 'parent_watermark': target_wm,
                         'previous_active_run_id': stale_run.get('run_id')})
        else:
            data.update({'previous_active_run_id': stale_run.get('run_id'),
                         'restore_reason': reason})
        _write_run_identity(data)
        epoch_record = {'epoch': data.get('epoch'), 'source': f'restore-{continuity}',
                        'at': time.strftime('%Y-%m-%d %H:%M:%S'),
                        'started_at': data.get('epoch_started_at'),
                        'run_id': data.get('run_id'), 'branch_id': data.get('branch_id'),
                        'parent_run_id': data.get('parent_run_id'),
                        'parent_branch_id': data.get('parent_branch_id'),
                        'parent_epoch': data.get('parent_epoch'),
                        'parent_watermark': data.get('parent_watermark'),
                        'restore_slot': save_target['slot'], 'restore_reason': reason}
        parent_record = {key: data.get(key) for key in
                         ('parent_run_id', 'parent_branch_id', 'parent_epoch', 'parent_watermark')}
    else:
        epoch = int(stale_run.get('epoch') or 0) + 1
        branch_prefix = 'resume' if continuity == 'resumed' else 'replay'
        branch = f'{branch_prefix}-{epoch}'
        data = dict(stale_run)
        data.update({'epoch': epoch, 'epoch_started_at': time.time(), 'branch_id': branch,
                     'epoch_source': 'restore',
                     'epoch_updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                     'parent_branch_id': stale_run.get('branch_id'),
                     'parent_epoch': stale_run.get('epoch'),
                     'parent_watermark': current_wm.get('actions_count'),
                     'parent_last_action_id': current_wm.get('last_action_id'),
                     'restore_slot': save_target['slot'], 'rollback': rollback})
        _write_run_identity(data)
        parent_record = {'parent_run_id': stale_run.get('run_id'),
                         'parent_branch_id': stale_run.get('branch_id'),
                         'parent_epoch': stale_run.get('epoch'),
                         'parent_watermark': current_wm.get('actions_count'),
                         'parent_last_action_id': current_wm.get('last_action_id')}
        epoch_record = {'epoch': epoch, 'source': 'restore',
                        'at': time.strftime('%Y-%m-%d %H:%M:%S'),
                        'started_at': data.get('epoch_started_at'),
                        'run_id': data.get('run_id'), 'branch_id': branch,
                        'parent_run_id': parent_record['parent_run_id'],
                        'parent_branch_id': parent_record['parent_branch_id'],
                        'parent_epoch': parent_record['parent_epoch'],
                        'parent_watermark': parent_record['parent_watermark'],
                        'restore_slot': save_target['slot'], 'rollback': rollback}
    _append_epoch_record(epoch_record)
    segment = _record_segment({
        'started_at': time.time(), 'client_pid': engine.get('client_pid'),
        'session_id': engine.get('session_id'), 'load_target': save_target['slot'],
        'parent': parent_record, 'actions_watermark_before': current_count,
        'memory_branch': data.get('branch_id'), 'kind': 'restore',
        'continuity': continuity, 'rollback': rollback})
    file_path = save_target['path']
    try:
        file_size = file_path.stat().st_size
    except OSError:
        file_size = None
    receipt = _write_restore_receipt({
        'kind': 'session_start_resume', 'slot': save_target['slot'],
        'file': str(file_path), 'file_bytes': file_size, 'file_sha256': actual_sha,
        'sidecar_path': str(sidecar['path']) if sidecar else None,
        'sidecar_sha256': _file_sha256(sidecar['path']) if sidecar else None,
        'target': {'run_id': target_run, 'branch_id': target_branch, 'epoch': target_epoch,
                   'actions_count': target_wm,
                   'last_action_id': meta.get('last_action_id'),
                   'current_revision': meta.get('current_revision'),
                   'day': meta.get('day'), 'fingerprint': meta.get('fingerprint'),
                   'map': (meta.get('start_config') or {}).get('map'),
                   'config_match': (meta.get('start_config') or {}).get('config_match')},
        'engine': engine, 'before_state': before_state, 'observed': proof,
        'committed': {'run_id': data.get('run_id'), 'branch_id': data.get('branch_id'),
                      'epoch': data.get('epoch')},
        'parent': parent_record, 'continuity': continuity, 'rollback': rollback,
        'target_proof': proof.get('target_proof'),
        'identity_precision': proof.get('identity_precision'),
        'reason': reason, 'segment_id': segment.get('segment_id')})
    return {'identity': data, 'receipt': receipt, 'segment': segment,
            'continuity': continuity, 'rollback': rollback}


# ---------------------------------------------------------------- W0 start-config identity
def _parse_effective_lines(lines: list[str]) -> dict:
    """Parse the bridge's 'OK start_options_effective k=v ...' line into a numeric dict."""
    effective: dict = {}
    for line in lines:
        if 'start_options_effective' not in line:
            continue
        for token in line.split():
            if '=' not in token:
                continue
            key, _sep, value = token.partition('=')
            key = key.strip()
            if key not in _OPTION_KEYS:
                continue
            number = _as_int(value)
            effective[key] = number if number is not None else value
    return effective


def _read_story_lines() -> list[str]:
    """Visible scenario/INFO lines from results.log near the start (no hidden content)."""
    out: list[str] = []
    try:
        lines = (_dir() / 'results.log').read_text(
            encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return out
    for line in lines:
        text = line.strip()
        if 'start_option' in text:
            continue
        low = text.lower()
        if ' info ' not in f' {low} ':
            continue
        if any(word in low for word in ('scenario', 'map', 'story', 'hero', 'town')):
            out.append(text)
    return out[:20]


def _read_client_log_head() -> dict:
    """First VCMI build/mod lines from the client log (only recorded, nothing parsed)."""
    try:
        lines = CLIENT_LOG_PATH.read_text(
            encoding='utf-8', errors='replace').splitlines()[:400]
    except OSError:
        return {'path': str(CLIENT_LOG_PATH), 'found': False, 'lines': [],
                'reason': 'client log not readable'}
    picked: list[str] = []
    for line in lines:
        text = line.strip()
        low = text.lower()
        if not text:
            continue
        if 'vcmi' in low and ('version' in low or 'v0.' in low or 'client' in low):
            picked.append(text)
        elif 'loading mod' in low or 'mods loaded' in low or 'loaded mod' in low:
            picked.append(text)
    return {'path': str(CLIENT_LOG_PATH), 'found': True, 'lines': picked[:30]}


def _map_file_identity(map_name: str) -> dict:
    """Resolve Maps/<name> to a local .h3m and hash the whole file (never settings.lastMap)."""
    name = (map_name or '').strip()
    if name.startswith('Maps/'):
        name = name[len('Maps/'):]
    name = name.strip()
    if not name:
        return {'path': None, 'sha256': None, 'bytes': None, 'mtime': None,
                'reason': 'empty map name'}
    candidates: list[Path] = [directory / f'{name}.h3m' for directory in MAP_FILE_DIRS]
    try:
        mods = ROOT / 'vendor' / 'vcmi-src' / 'build' / 'bin' / 'Mods'
        candidates.extend(sorted(mods.glob(f'*/maps/{name}.h3m')))
    except OSError:
        pass
    for path in candidates:
        if not path.is_file():
            continue
        info = path.stat()
        return {'path': str(path), 'bytes': info.st_size, 'mtime': info.st_mtime,
                'mtime_iso': time.strftime('%Y-%m-%d %H:%M:%S',
                                           time.localtime(info.st_mtime)),
                'sha256': _file_sha256(path), 'source': 'local_map_file'}
    return {'path': None, 'sha256': None, 'bytes': None, 'mtime': None,
            'reason': 'no local .h3m file found for this map name (not using settings.lastMap)',
            'attempted': [str(path) for path in candidates[:8]]}


# W0 semantic equivalences between the requested option and the engine's effective line.
# The bridge applies client-side "random" ids (hero/town = -1) and the effective lobby line reports
# the engine-chosen value -2 (engine random/keep-default id).  That is the SAME request, not a
# mismatch; it is recorded as a semantic_equivalence note so config_match stays true.
_RANDOM_REQUEST_IDS = {'hero': (-1,), 'bonus': (-1,), 'town': (-1,)}
_RANDOM_EFFECTIVE_IDS = {'hero': (-2,), 'bonus': (-2,), 'town': (-2,)}
_TIME_EFFECTIVE_KEYS = {'turn_time': 'turn_time_ms', 'battle_time': 'battle_time_ms',
                        'unit_time': 'unit_time_ms'}


def _effective_value(effective: dict, effective_key: str):
    """Value of one effective option; the bridge logs timers as <key>_ms."""
    if effective_key in effective:
        return effective.get(effective_key)
    ms_key = _TIME_EFFECTIVE_KEYS.get(effective_key)
    if ms_key and ms_key in effective:
        try:
            return int(effective.get(ms_key)) // 1000
        except (TypeError, ValueError):
            return effective.get(ms_key)
    return None


def _compare_start_config(requested: dict, resolved: dict, effective: dict,
                          map_identity: dict) -> tuple[bool | None, list[str]]:
    """Compare explicit requested values with the bridge's effective line (W0 gate)."""
    notes: list[str] = []
    mismatches: list[str] = []
    compared = 0
    mapping = {'difficulty': 'difficulty', 'player_color': 'player', 'starting_town': 'town',
               'starting_hero': 'hero', 'starting_bonus': 'bonus',
               'turn_time_seconds': 'turn_time', 'battle_time_seconds': 'battle_time',
               'unit_time_seconds': 'unit_time'}
    for requested_key, effective_key in mapping.items():
        if requested.get(requested_key) is None:
            continue
        actual = _effective_value(effective, effective_key)
        if actual is None:
            notes.append(f'{effective_key}: not reported by the bridge (cannot confirm)')
            continue
        compared += 1
        expected = resolved.get(effective_key)
        if expected is None or actual == expected:
            continue
        # W0 semantic equivalence: requested random(-1) vs engine effective -2 (random id).
        if (expected in _RANDOM_REQUEST_IDS.get(effective_key, ())
                and actual in _RANDOM_EFFECTIVE_IDS.get(effective_key, ())):
            notes.append(f'{effective_key}: requested random({expected}) -> effective {actual} '
                         f'(engine random id) [semantic_equivalence]')
            continue
        mismatch = f'{effective_key}: requested {expected} but effective {actual}'
        mismatches.append(mismatch)
        notes.append(mismatch)
    if map_identity.get('path') is None:
        notes.append('map: local map file not found (identity unverified)')
    if compared == 0:
        return None, notes or ['no explicit option could be compared with the bridge effective line']
    return not mismatches, notes


def _collect_start_config(explicit: dict, resolved: dict, lines: dict, agent,
                          st: dict) -> dict:
    """D: write <state>/start_config.json with requested/resolved/effective/map identity."""
    applied = _read_start_option_results()
    effective = _parse_effective_lines(applied)
    map_identity = _map_file_identity(explicit.get('map_name') or '')
    requested = {key: value for key, value in explicit.items()
                 if key == 'map_name' or value is not None}
    config_match, notes = _compare_start_config(requested, resolved, effective, map_identity)
    config = {'requested': requested,
              'requested_recorded_at': time.strftime('%Y-%m-%d %H:%M:%S'),
              'resolved': resolved or None,
              'start_option_lines': applied or None,
              'effective': effective or None,
              'story_lines': _read_story_lines(),
              'client': _read_client_log_head(),
              'map': map_identity,
              'config_match': config_match, 'match_notes': notes,
              'engine': _engine_identity(agent, st)}
    try:
        _atomic_write_json(_dir() / 'start_config.json', config)
    except OSError:
        pass
    return config


@mcp.tool()
@_serialized_mutation('session_start')
def session_start(map_name: str = 'Maps/Arrogance',
                  difficulty: str | int | None = None,
                  player_color: str | int | None = None,
                  starting_town: str | int | None = None,
                  starting_hero: str | int | None = None,
                  starting_bonus: str | int | None = None,
                  turn_time_seconds: int | None = None,
                  battle_time_seconds: int | None = None,
                  unit_time_seconds: int | None = None) -> dict:
    """Start the owned VCMI client (explicit business action; tool discovery never does this).

    map_name: a scenario ("Maps/Arrogance", bare names get the Maps/ prefix) or a saved slot
    ("Saves/agent/day3" / a *.vsgm1 path), in which case the client resumes that save.

    Optional H3 "advanced options" for a NEW scenario (ignored when resuming a save):
      difficulty: 0..4 or easy/pawn, normal/knight, hard/rook, expert/queen, impossible/king
      player_color: which slot is the human player (red/blue/tan/green/orange/purple/teal/pink or 0..7)
      starting_town: faction of the starting town (castle/tower/rampart/..., numer id, or "random")
      starting_hero: hero identifier from config/heroes (e.g. "sylvia"), numeric id, or "random"
      starting_bonus: random | artifact | gold | resource
      turn_time_seconds / battle_time_seconds / unit_time_seconds: H3 turn timers (0 = unlimited)
    Only options the map allows are applied; the client logs what actually became effective.
    A NEW scenario also rotates the logical run: the outgoing run's identity-chain files
    (segments/epochs/restores/recording_segments) are archived under archive/<old_run>/chain-<n>/
    and reset, so this run's first recording segment starts with parent=None/restore_ref=None
    (RESUMING a save keeps the chain - the restore receipt is the parent evidence).
    """
    target = (map_name or '').strip()
    if target.startswith('Saves/') or target.lower().endswith('.vsgm1'):
        pass  # reopen an existing save
    elif not target.startswith('Maps/'):
        target = 'Maps/' + target
    resumed = target.startswith('Saves/') or target.lower().endswith('.vsgm1')
    explicit = {'map_name': target, 'difficulty': difficulty, 'player_color': player_color,
                'starting_town': starting_town, 'starting_hero': starting_hero,
                'starting_bonus': starting_bonus, 'turn_time_seconds': turn_time_seconds,
                'battle_time_seconds': battle_time_seconds,
                'unit_time_seconds': unit_time_seconds}
    save_target: dict | None = None
    save_meta: dict | None = None
    sidecar: dict | None = None
    actual_sha: str | None = None
    if resumed:
        # B1/B2: resolve the target BEFORE any side effect and check file-vs-sidecar identity.
        save_target = _resolve_save_target(target)
        if save_target is None:
            return {'ok': False, 'sent': False, 'status': 'rejected',
                    'error': 'invalid save target; expected Saves/<subdir>/<name>'}
        target_path = save_target['path']
        if not target_path.exists():
            return {'ok': False, 'sent': False, 'status': 'rejected',
                    'error': f'save file not found: {target_path}',
                    'slot': save_target['slot'], 'file': str(target_path)}
        actual_sha = _file_sha256(target_path)
        sidecar = _find_save_sidecar(save_target['safe'], target_path)
        save_meta = dict(sidecar['meta']) if sidecar else None
        if save_meta and save_meta.get('sha256') and actual_sha != save_meta.get('sha256'):
            return {'ok': False, 'sent': False, 'status': 'restore_conflict',
                    'continuity': 'file_hash_mismatch', 'slot': save_target['slot'],
                    'file': str(target_path), 'file_sha256': actual_sha,
                    'sidecar_sha256': save_meta.get('sha256'),
                    'error': 'save file sha256 does not match its sidecar; the client was NOT '
                             'started and no identity was committed'}
    # A: archive the outgoing segment BEFORE start() truncates evidence and before
    # _write_start_options() overwrites the old option file.
    before_state = _state_file_snapshot()
    outgoing_run = _run_identity()
    archive = _archive_segment_before_start(
        'session_start:resume' if resumed else 'session_start:new_game')
    _clear_start_options()
    resolved: dict = {}
    errors: list[str] = []
    option_lines: dict = {}
    if not resumed:
        resolved, option_lines, errors = _build_start_options({
            'difficulty': difficulty, 'player_color': player_color,
            'starting_town': starting_town, 'starting_hero': starting_hero,
            'starting_bonus': starting_bonus, 'turn_time_seconds': turn_time_seconds,
            'battle_time_seconds': battle_time_seconds, 'unit_time_seconds': unit_time_seconds,
        })
        if errors:
            # W0: an invalid advanced option must never fall back to defaults silently: the
            # whole point of the option file is that the requested setup is the one that runs.
            return {'ok': False, 'sent': False,
                    'error': 'invalid start options; the client was not started',
                    'option_errors': errors, 'start_options': resolved or None,
                    'archive': _archive_summary(archive)}
        if option_lines:
            _write_start_options(option_lines)
    a = VcmiAgent(str(_dir()), target, log=False)
    try:
        if not a.start():
            return {'ok': False, 'error': 'client did not start',
                    'option_errors': errors or None, 'archive': _archive_summary(archive)}
        st = a.wait_in_game(240)
        contract = dict(a.contract or {})
        engine = _engine_identity(a, st)
        if resumed and save_target is not None:
            # B4: prove the restored state equals the TARGET (day + fingerprint), then commit.
            proof, conflict = _prove_restore_target(a, st, save_meta or {})
            if conflict is not None:
                try:
                    a.stop()
                except Exception:  # pragma: no cover - best effort ownership cleanup
                    pass
                receipt = _write_restore_receipt({
                    'kind': 'session_start_resume', 'slot': save_target['slot'],
                    'file': str(save_target['path']), 'file_sha256': actual_sha,
                    'sidecar_path': str(sidecar['path']) if sidecar else None,
                    'sidecar_sha256': _file_sha256(sidecar['path']) if sidecar else None,
                    'target': {'run_id': (save_meta or {}).get('run_id'),
                               'branch_id': (save_meta or {}).get('branch_id'),
                               'epoch': (save_meta or {}).get('epoch'),
                               'day': (save_meta or {}).get('day'),
                               'fingerprint': (save_meta or {}).get('fingerprint')},
                    'engine': engine, 'before_state': before_state, 'observed': proof,
                    'committed': None, 'continuity': 'target_proof_failed',
                    'target_proof': proof.get('target_proof'),
                    'identity_precision': proof.get('identity_precision'),
                    'client_stopped': True, 'problems': conflict['problems']})
                return {'ok': False, 'status': 'restore_conflict',
                        'continuity': 'target_proof_failed', 'restore': receipt,
                        'problems': conflict['problems'], 'client_stopped': True,
                        'error': 'restored state does not match the save sidecar; identity NOT '
                                 'committed and the owned client was stopped'}
            commit = _commit_restore_identity(save_meta or {}, proof, sidecar, save_target,
                                              actual_sha, engine, before_state, outgoing_run)
            run = commit['identity']
            return {'ok': True,
                    'session': a.session.session_id if a.session else None,
                    'client_pid': a.session.client_pid if a.session else None,
                    'day': (st.get('date') or {}).get('day'),
                    'run_id': run.get('run_id'), 'branch_id': run.get('branch_id'),
                    'epoch': run.get('epoch'), 'resumed': True,
                    'restore': commit['receipt'], 'segment': commit['segment'],
                    'archive': _archive_summary(archive),
                    'offscreen': a.window_is_offscreen(),
                    'contract': {
                        'exe_sha256_match': bool(contract.get('match')),
                        'protocol_ok': bool(contract.get('protocol_ok')),
                        'built_from_commit': contract.get('built_from_commit'),
                        'repo_head': contract.get('repo_head'),
                        'client_source_matches_head': contract.get('source_matches_head'),
                    }}
        # A3: a brand-new scenario rotates the run AFTER the pre-start archive; the rotation must
        # not archive a second time (archive=False).  E8/ISSUE-01: the rotation also archives and
        # resets the identity-chain files, so this run's first clip has parent=None/restore_ref=None.
        run = _rotate_run(archive=False)
        chain_reset = run.get('chain_reset')
        segment = _record_segment({
            'started_at': time.time(), 'client_pid': engine.get('client_pid'),
            'session_id': engine.get('session_id'), 'load_target': None, 'parent': None,
            'actions_watermark_before': _ledger_watermark().get('actions_count'),
            'memory_branch': run.get('branch_id'), 'kind': 'new_game'})
        config = _collect_start_config(explicit, resolved, option_lines, a, st)
        applied = config.get('start_option_lines') or []
        run = _run_identity()
        return {'ok': True, 'session': a.session.session_id if a.session else None,
                'client_pid': a.session.client_pid if a.session else None,
                'day': (st.get('date') or {}).get('day'),
                'run_id': run.get('run_id'), 'branch_id': run.get('branch_id'),
                'epoch': run.get('epoch'), 'resumed': False,
                'offscreen': a.window_is_offscreen(),
                'start_options': resolved or None,
                'start_options_applied': applied or None,
                'option_errors': errors or None,
                'config': config, 'segment': segment,
                'archive': _archive_summary(archive),
                'chain_reset': chain_reset,
                'contract': {
                    'exe_sha256_match': bool(contract.get('match')),
                    'protocol_ok': bool(contract.get('protocol_ok')),
                    'built_from_commit': contract.get('built_from_commit'),
                    'repo_head': contract.get('repo_head'),
                    'client_source_matches_head': contract.get('source_matches_head'),
                }}
    except Exception as exc:
        return {'ok': False, 'error': f'{type(exc).__name__}: {exc}',
                'option_errors': errors or None, 'archive': _archive_summary(archive)}


@mcp.tool()
@_serialized_mutation('session_stop')
def session_stop() -> dict:
    """Stop only the client owned by this session and report leftovers."""
    return {'ok': True, 'result': _agent().stop()}


def _state_or_error() -> tuple[dict | None, str | None]:
    """Read the current state; return (state, None) or (None, error) instead of raising.

    Read-only tools must answer even when no game is running (the MCP client then gets a
    structured error instead of a protocol-level tool failure).
    """
    try:
        return _agent().state(timeout=10), None
    except Exception as exc:                 # no session / stale / bridge gone
        return None, f'{type(exc).__name__}: {exc}'


@mcp.tool()
def game_status() -> dict:
    a = _agent()
    st, error = _state_or_error()
    if error:
        return {'ok': False, 'error': error}
    contract = a.contract or a.contract_status()
    return {'ok': True, 'day': (st.get('date') or {}).get('day'), 'status': st.get('status'),
            'session': a.session.session_id if a.session else None,
            'offscreen': a.window_is_offscreen(), 'rect': a._window_rect(),
            'contract': {'match': contract.get('match'),
                         'client_sha256': contract.get('actual_sha256'),
                         'pinned_at': contract.get('built_from_commit'),
                         'protocol': st.get('protocol')}}


@mcp.tool()
def game_observe() -> dict:
    """Player-visible digest only; reading state never changes the game.

    `state.recent_info` carries the newest Info-window lines the bridge recorded in
    state.json ("info": week events, artifact/treasure messages, building visits, ...).  The
    normal UI shows those dialogs to the player, so they are player-visible observations, not
    hidden information; they are capped/deduped so the digest stays readable.
    """
    st, error = _state_or_error()
    if error:
        return {'ok': False, 'error': error}
    return {'ok': True, 'state': _digest(st), 'decisions': _decision_bundle(st)}


@mcp.tool()
def game_events() -> dict:
    """Pending decision points, persisted with monotonic ids.

    Returns the last 20 events from <dir>/events.jsonl plus everything unread since
    <dir>/events_cursor.json (the cursor is advanced by this call).  `decisions` keeps the old
    shape (list of {type: ...}) and includes the current stateless points (query_opened /
    battle_started / deployment / game_over) so a caller that polls it still sees pending
    decisions while unread stays empty.
    """
    st, error = _state_or_error()
    if error:
        return {'ok': False, 'error': error}
    transitions = _record_events(st)
    events = _read_events()
    cursor = _read_event_cursor()
    unread = [event for event in events if int(event.get('id') or 0) > cursor]
    if events:
        try:
            _write_event_cursor(int(events[-1].get('id') or cursor))
        except (TypeError, ValueError):
            pass
    decisions: list = []
    seen: set = set()
    for point in _current_decisions(st):
        if point.get('type') not in seen:
            decisions.append(point)
            seen.add(point.get('type'))
    for event in transitions:
        if event.get('type') not in seen:
            decisions.append(event)
            seen.add(event.get('type'))
    return {'ok': True, 'events': events[-20:], 'unread': unread,
            'decisions': decisions, 'cursor': cursor}


# T31/P-A recovery surface.  `pending_action` below only surfaces a *recent unresolved*
# mutation; after a context compaction the player may have lost track of the last *completed*
# request as well, and re-sending it would duplicate a build/recruit/move.  These helpers expose
# the tail of the append-only ledger (newest first) so the brief carries "last request + current
# state" without injecting the whole ledger.
def _ledger_age(entry: dict) -> float | None:
    import datetime
    try:
        stamp = datetime.datetime.strptime(str(entry.get('ts')), '%Y-%m-%dT%H:%M:%S')
    except (TypeError, ValueError):
        return None
    return max(0.0, time.time() - stamp.timestamp())


def _action_summary(entry: dict) -> dict:
    """One compact, JSON-safe recovery row for a ledger entry (never raises)."""
    item: dict = {'action': entry.get('action'),
                  'action_id': entry.get('action_id'),
                  'status': entry.get('status'),
                  'seq': entry.get('seq'),
                  'ts': entry.get('ts')}
    if entry.get('request_id'):
        item['request_id'] = entry['request_id']
    evidence = entry.get('evidence')
    if evidence:
        item['evidence'] = _truncate(str(evidence), _ACTION_EVIDENCE_MAX)
    args = entry.get('args')
    if args:
        try:
            text = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            text = str(args)
        item['args'] = _truncate(text, _ACTION_ARGS_MAX)
    age = _ledger_age(entry)
    if age is not None:
        item['age_s'] = round(age, 1)
    return item


def _recent_actions(limit: int = _ACTION_TAIL_MAX) -> list:
    """Newest-first summaries of the last real actions in this run (game_note skipped)."""
    try:
        lines = (_dir() / 'actions.jsonl').read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    items: list = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get('action') == 'game_note':
            continue
        items.append(_action_summary(entry))
        if len(items) >= limit:
            break
    return items


@mcp.tool()
def game_brief(hero: int | None = None, radius: int | None = None, limit: int = 16,
               kinds: str | None = None) -> dict:
    """Compact digest for routine turns (much fewer tokens than game_observe).

    Optional W4 filters:
      hero:   hero index used as the distance origin (default 0)
      radius: only objects whose map distance from that hero is <= radius (0..64)
      limit:  max objects (1..64)
      kinds:  comma list matched against an object's kind or type ("monster,hero,town", ...)
    Positions keep all three coordinates (x,y,z). Visible enemy heroes/towns are listed in
    `enemies`; `observed` carries the state timestamp/age so the caller knows how fresh the
    observation is. This tool only reads state.json - it never sends a bridge command.

    Recovery surface (after a context compaction, read this before sending anything):
      recent_info:    newest player-visible Info-window lines (week events, treasure/artifact
                      messages, building visits ...), capped at 8, consecutive repeats collapsed
      making_turn:    state.json making_turn flag (None when the bridge omits it)
      last_action:    newest ledger entry summary (action/action_id/request_id/status/seq/ts/
                      evidence/args, age_s) - completed actions included, game_note excluded
      recent_actions: up to 5 such summaries, newest first (last_action == recent_actions[0])
      pending_action: unchanged narrow view - only a *recent* submitted/unknown mutation
    Do not re-send an action that already appears here with status completed/submitted; retry
    only with its original request_id, and use a new request_id for new work.

    Hero-screen observations (T33/P-B; every listed fact is visible in the normal hero window):
      mana_max / spellbook / luck / morale / specialty (scalars)
      skills:        [{'id','level','name'}] secondary skills
      spells:        known spells [{'id','name','cost','can_cast','adventure'}] (T35 cost/
                     usability; spell_count carries the true total)
      artifacts:     worn+backpack artifact names (artifact_count carries the true total)
    When a battle is active, battle_round and hero_mana are included as well.
    Battle spells are cast with battle_cast_spell (the battle_actions tool lists the legal ones);
    adventure spells with cast_spell.
    """
    if hero is not None and (isinstance(hero, bool) or not isinstance(hero, int)
                             or not 0 <= hero <= 15):
        return {'ok': False, 'error': 'hero must be an integer in 0..15'}
    if radius is not None and (isinstance(radius, bool) or not isinstance(radius, int)
                               or not 0 <= radius <= 64):
        return {'ok': False, 'error': 'radius must be an integer in 0..64'}
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 64:
        return {'ok': False, 'error': 'limit must be an integer in 1..64'}
    kind_filter: set = set()
    if kinds is not None:
        if not isinstance(kinds, str) or not kinds.strip():
            return {'ok': False, 'error': 'kinds must be a comma-separated string'}
        kind_filter = {part.strip().lower() for part in kinds.split(',') if part.strip()}
        if not kind_filter:
            return {'ok': False, 'error': 'kinds must not be empty'}
    st, error = _state_or_error()
    if error:
        return {'ok': False, 'error': error}
    s = _digest(st)
    res = s.get('resources') or {}
    heroes = s.get('heroes') or []
    origin_index = hero if hero is not None else 0
    origin = next((h for h in heroes if h.get('i') == origin_index), heroes[0] if heroes else {})
    origin_pos = list(origin.get('pos') or [0, 0, 0])
    player = st.get('player')
    out = {
        'ok': True,
        'day': s.get('day'),
        'status': s.get('status'),
        'gold': res.get('gold'),
        'wood': res.get('wood'),
        'ore': res.get('ore'),
        'observed': {'ts_wall': st.get('ts_wall'), 'age_s': None},
        'heroes': [_hero_brief(h) for h in heroes],
        'towns': [_town_brief(t) for t in s.get('towns') or []],
        'battle': s.get('battle_active'),
        'decisions': [d.get('type') for d in _decision_bundle(st)],
        'players': player,
        'making_turn': st.get('making_turn'),
        'recent_info': s.get('recent_info') or [],
        'enemies': [],
        'objects': [],
    }
    battle_detail = s.get('battle') or {}
    if s.get('battle_active'):
        # T33/P-B: round counter + our hero's battle mana (UI-visible in the battle screen).
        out['battle_round'] = battle_detail.get('round')
        out['hero_mana'] = battle_detail.get('hero_mana')
    try:
        ts = float(st.get('ts_wall') or 0)
        if ts > 0:
            out['observed']['age_s'] = round(max(0.0, time.time() - ts / 1000.0), 1)
    except (TypeError, ValueError):
        pass
    heroes_pos_list = [list(h.get('pos') or [0, 0, 0])[:3] for h in heroes]
    for o in s.get('objects') or []:
        v = list(o.get('pos') or o.get('visitable') or [0, 0, 0])
        while len(v) < 3:
            v.append(0)
        kind = o.get('kind')
        otype = o.get('type')
        dist = abs(int(v[0]) - int(origin_pos[0])) + abs(int(v[1]) - int(origin_pos[1]))
        if (kind in ('hero', 'town') and player is not None
                and o.get('owner') is not None and o.get('owner') != player):
            out['enemies'].append({'kind': kind, 'name': o.get('name'),
                                   'owner': o.get('owner'), 'pos': v[:3], 'dist': dist})
        if kind_filter and kind not in kind_filter and otype not in kind_filter:
            continue
        if radius is not None and dist > radius:
            continue
        entry = {'id': o['id'], 'kind': kind, 'type': otype, 'name': o.get('name'),
                 'pos': v[:3], 'dist': dist,
                 'dz': int(v[2]) - int(origin_pos[2])}
        # T40/C11: canonical interaction tile (compact), when the client provides it.
        if isinstance(o.get('tile'), list) and o.get('tile'):
            entry['tile'] = list(o['tile'])[:3]
        if o.get('owner') is not None:
            entry['owner'] = o['owner']
        if o.get('hover'):
            entry['hover'] = o['hover']
        out['objects'].append(entry)
    out['enemies'].sort(key=lambda e: e['dist'])
    out['enemies'] = out['enemies'][:8]
    out['objects'].sort(key=lambda e: e['dist'])
    out['object_count'] = len(out['objects'])
    out['objects'] = out['objects'][:limit]
    if (st.get('query') or {}):
        out['query'] = st['query']
    # Session identity + memory index: the player can resume after context compaction by
    # reading CURRENT.md; the brief only advertises revision/size, never injects the full text.
    out['identity'] = _run_identity()
    out['memory'] = _memory_index()
    # pending_action: only surface a *recent* unresolved mutation; old submitted entries from
    # previous actions (e.g. a deployment step that was resolved by battle_start) are noise.
    try:
        import datetime
        lines = (_dir() / 'actions.jsonl').read_text(encoding='utf-8').splitlines()
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get('action') == 'game_note':
                continue
            age = None
            try:
                stamp = datetime.datetime.strptime(str(entry.get('ts')), '%Y-%m-%dT%H:%M:%S')
                age = time.time() - stamp.timestamp()
            except (TypeError, ValueError):
                age = None
            if (entry.get('status') in ('submitted', 'unknown')
                    and age is not None and age <= 600):
                out['pending_action'] = {'action_id': entry.get('action_id'),
                                         'action': entry.get('action'),
                                         'status': entry.get('status'),
                                         'age_s': round(age, 1),
                                         'evidence': entry.get('evidence')}
            break
    except (OSError, ValueError, IndexError):
        pass
    # Broader recovery tail (completed actions included).  pending_action above stays the narrow
    # unresolved-only signal; this lets the player reconstruct "last request / current state" and
    # prove that a re-send would be a duplicate.
    tail = _recent_actions()
    out['recent_actions'] = tail
    out['last_action'] = tail[0] if tail else None
    return out



# ------------------------------------------------------------------ session / memory
# Two memory tiers (see doc/SESSION_MEMORY_PLAN_20261024.md):
#   player     -> doc/player/PLAYER.md               (how to play, read/write discipline)
#   long_term  -> doc/player/long_term/<topic>.md    (transferable experience, keyed by topic)
#   game       -> doc/player/games/<run>/branches/<branch>/CURRENT.md (this run's working notes)
# The program only validates/stores/indexes; the player writes the content and sources.
# AUTO_HEROS3_MEMORY_ROOT is a test-isolation override (formal runs do not set it); it must be
# resolved before _ensure_memory_defaults() runs at the bottom of this import.
MEMORY_ROOT = _env_path('AUTO_HEROS3_MEMORY_ROOT', ROOT / 'doc' / 'player')
SAFE_KEY = re.compile(r'^[a-z0-9][a-z0-9_\-]{0,47}$')


def _run_identity() -> dict:
    """run_id / branch_id / epoch: one logical game (not one process).  Created lazily, persisted.

    R6: epoch increments on a load (or another timeline break) and the branch name follows it, so
    memory written before a load is not silently reused as if it described the loaded timeline.
    """
    path = _dir() / 'run.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, dict) and data.get('run_id'):
            if not isinstance(data.get('epoch'), int):
                data['epoch'] = 0
            if not isinstance(data.get('epoch_started_at'), (int, float)):
                data['epoch_started_at'] = time.time()
            return data
    except (OSError, ValueError):
        pass
    data = {'schema_version': 1, 'run_id': time.strftime('%Y%m%d-%H%M%S'),
            'branch_id': 'main', 'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'epoch': 0, 'epoch_started_at': time.time()}
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError:
        pass
    return data


def _memory_path(scope: str, key: str | None) -> Path:
    run = _run_identity()
    if scope == 'player':
        return MEMORY_ROOT / 'PLAYER.md'
    if scope == 'long_term':
        name = key or 'index'
        if not SAFE_KEY.match(name):
            raise ValueError('invalid long_term key (use [a-z0-9_-])')
        return MEMORY_ROOT / 'long_term' / f'{name}.md'
    if scope == 'game':
        base = MEMORY_ROOT / 'games' / run['run_id'] / 'branches' / run['branch_id']
        if key in (None, '', 'current'):
            return base / 'CURRENT.md'
        if key.startswith('battle:'):
            name = key.split(':', 1)[1]
            if not SAFE_KEY.match(name):
                raise ValueError('invalid battle key (use [a-z0-9_-])')
            return base / 'battles' / f'{name}.md'
        raise ValueError('invalid game key (use current or battle:<name>)')
    raise ValueError('invalid scope (player|game|long_term)')


def _parse_front_matter(text: str) -> tuple[dict, str]:
    meta: dict = {}
    body = text
    if text.startswith('---\n'):
        end = text.find('\n---\n', 4)
        if end > 0:
            body = text[end + 5:]
            for line in text[4:end].splitlines():
                if ':' in line:
                    name, value = line.split(':', 1)
                    meta[name.strip()] = value.strip()
    return meta, body


def _render_doc(meta: dict, body: str) -> str:
    header = '\n'.join(f'{name}: {value}' for name, value in meta.items())
    return f'---\n{header}\n---\n\n{body.rstrip()}\n'


def _ensure_memory_defaults() -> None:
    """Create missing PLAYER.md / long_term INDEX.md from the versioned templates.

    The real notes live in the git-ignored doc/player tree; the templates are committed under
    platform/memory_templates/ so a fresh checkout still gets the read/write discipline text.
    """
    templates = ROOT / 'platform' / 'memory_templates'
    pairs = [(templates / 'PLAYER.md', MEMORY_ROOT / 'PLAYER.md'),
             (templates / 'long_term_INDEX.md', MEMORY_ROOT / 'long_term' / 'INDEX.md')]
    for source, target in pairs:
        if target.exists() or not source.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        except OSError:
            pass


_ensure_memory_defaults()


def _memory_index() -> dict:
    """Lightweight index for game_brief: CURRENT revision + long-term topics."""
    run = _run_identity()
    index: dict = {'run_id': run['run_id'], 'branch_id': run['branch_id']}
    try:
        current = _memory_path('game', 'current')
        if current.exists():
            meta, _ = _parse_front_matter(current.read_text(encoding='utf-8'))
            index['current'] = {'revision': meta.get('revision'),
                                'updated_at': meta.get('updated_at'),
                                'observed_day': meta.get('observed_day')}
    except (OSError, ValueError):
        pass
    topics = []
    try:
        for path in sorted((MEMORY_ROOT / 'long_term').glob('*.md')):
            if path.stem != 'INDEX':
                topics.append(path.stem)
    except OSError:
        pass
    index['long_term_topics'] = topics
    return index


@mcp.tool()
def memory_read(scope: str, key: str | None = None) -> dict:
    """Read one memory document with revision metadata.

    scope: player (PLAYER.md) | game (CURRENT.md or battle:<name>) | long_term (<topic>).
    Returns body + revision; a missing document is reported as exists=false, not an error.
    """
    try:
        path = _memory_path(scope, key)
    except ValueError as exc:
        return {'ok': False, 'error': str(exc)}
    rel = str(path.relative_to(ROOT))
    if not path.exists():
        return {'ok': True, 'exists': False, 'scope': scope, 'key': key, 'path': rel,
                'revision': 0, 'body': ''}
    text = path.read_text(encoding='utf-8')
    meta, body = _parse_front_matter(text)
    return {'ok': True, 'exists': True, 'scope': scope, 'key': key, 'path': rel,
            'revision': int(meta.get('revision') or 0),
            'updated_at': meta.get('updated_at'), 'observed_day': meta.get('observed_day'),
            'sources': meta.get('source_refs'), 'body': body,
            'bytes': len(text.encode('utf-8'))}


@mcp.tool()
def memory_write(scope: str, key: str | None, body: str,
                 expected_revision: int | None = None,
                 sources: list[str] | None = None,
                 observed_day: int | None = None) -> dict:
    """Write a memory document atomically (front-matter revision managed by the program).

    expected_revision returns revision_conflict instead of silently overwriting.  The previous
    version is kept as <file>.bak<timestamp>; the write is temp-file + atomic replace.
    """
    try:
        path = _memory_path(scope, key)
    except ValueError as exc:
        return {'ok': False, 'error': str(exc)}
    if not isinstance(body, str) or not body.strip():
        return {'ok': False, 'error': 'empty body'}
    if len(body) > 20000:
        return {'ok': False, 'error': 'body too large (max 20000 chars)'}
    run = _run_identity()
    lock_name = 'memory_' + hashlib.md5(str(path).encode('utf-8')).hexdigest()[:12]
    with _file_lock(lock_name):
        # check-and-replace is one transaction for cooperating processes; the atomic file
        # replace below only guarantees that readers never see a half-written document.
        path.parent.mkdir(parents=True, exist_ok=True)
        current = 0
        if path.exists():
            meta_old, _ = _parse_front_matter(path.read_text(encoding='utf-8'))
            current = int(meta_old.get('revision') or 0)
            if expected_revision is not None and int(expected_revision) != current:
                return {'ok': False, 'error': 'revision_conflict', 'revision': current,
                        'path': str(path.relative_to(ROOT))}
            try:
                shutil.copy2(path, path.with_suffix(
                    path.suffix + '.bak' + time.strftime('%Y%m%d-%H%M%S')))
            except OSError:
                pass
        elif expected_revision is not None and int(expected_revision) != 0:
            # A new document is revision 1: a caller that expected an existing revision is
            # looking at a different document than the one on disk.
            return {'ok': False, 'error': 'revision_conflict', 'revision': 0,
                    'path': str(path.relative_to(ROOT))}
        meta = {
            'schema_version': 1, 'scope': scope, 'run_id': run['run_id'],
            'branch_id': run['branch_id'], 'revision': current + 1,
            'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'observed_day': observed_day if observed_day is not None else '',
            'source_refs': '[' + ', '.join(sources or []) + ']',
        }
        tmp = path.with_suffix(path.suffix + '.tmp')
        try:
            tmp.write_text(_render_doc(meta, body), encoding='utf-8')
            os.replace(tmp, path)
        except OSError as exc:
            return {'ok': False, 'error': f'write failed: {exc}'}
    _overlay('event', f'memory {scope}/{key or "current"} rev={current + 1}')
    return {'ok': True, 'path': str(path.relative_to(ROOT)), 'revision': current + 1,
            'bytes': path.stat().st_size}


# ---------------------------------------------------------------- action validation
# Args are typed/range-checked BEFORE any lease/state/bridge access.  Anything spliced into the
# bridge command line (which is space-delimited) must not contain whitespace or control chars,
# otherwise it could inject extra tokens/commands.
_CONTROL_CHARS = frozenset(chr(code) for code in list(range(32)) + [127])
_STR_ENUMS = {'src': ('town', 'hero'), 'dst': ('town', 'hero'),
              'mode': ('auto', 'melee', 'shot')}
_INT_ARGS = {
    'hero': (0, 15), 'town': (0, 15), 'id': (0, 999), 'level': (1, 7),
    'amount': (1, 9999), 'index': (0, 15), 'stack': (0, 999),
    'target': (0, 999), 'creature': (0, 9999), 'spell': (0, 9999),
    'mode': (0, 10), 'trade_mode': (0, 10), 'sell': (0, 6), 'buy': (0, 6),
    'src_index': (0, 15), 'dst_index': (0, 15), 'src_slot': (0, 6), 'dst_slot': (0, 6),
    'slot': (0, 6), 'slot_a': (0, 6), 'slot_b': (0, 6),
    'x': (0, 4095), 'y': (0, 4095), 'z': (0, 8),
}
_INT_OVERRIDES = {('battle_move', 'x'): (0, 32), ('battle_move', 'y'): (0, 32),
                  ('battle_cast_spell', 'x'): (0, 32), ('battle_cast_spell', 'y'): (0, 32)}
# Optional extra tokens appended to the bridge command line after the required arguments.
_OPTIONAL_ARGS = {'recruit': ('creature',), 'battle_attack': ('mode',),
                  'cast_spell': ('x', 'y', 'z'),
                  'battle_cast_spell': ('stack', 'x', 'y')}
_STR_ENUMS = {'src': ('town', 'hero'), 'dst': ('town', 'hero'),
              'owner': ('town', 'hero'),
              'mode': ('auto', 'melee', 'shot')}
_RESOURCE_NAMES = ('wood', 'mercury', 'ore', 'sulfur', 'crystal', 'gems', 'gold')
_SAFE_TOKEN = re.compile(r'^[A-Za-z0-9_.\-]{1,32}$')


def _clean_text(value: str) -> bool:
    return all(char not in _CONTROL_CHARS and not char.isspace() for char in value)


def _int_arg(action: str, name: str, value) -> tuple[int | None, str | None]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f'argument {name!r} must be an integer, got {type(value).__name__}'
    if isinstance(value, float):
        if not value.is_integer():
            return None, f'argument {name!r} must be an integer, got {value!r}'
        value = int(value)
    low, high = _INT_OVERRIDES.get((action, name), _INT_ARGS.get(name, (0, 2 ** 31 - 1)))
    if not low <= value <= high:
        return None, f'argument {name!r} out of range [{low}, {high}]: {value}'
    return int(value), None


def _validate_action(action: str, args: dict) -> str | None:
    """Validate + normalise args in place.  Returns an error string or None."""
    if not isinstance(args, dict):
        return 'args must be a JSON object'
    _, names = ACTION_SPECS[action]
    optional = _OPTIONAL_ARGS.get(action, ())
    allowed = set(names) | set(optional)
    if len(args) > 16:
        return 'too many args (max 16)'
    missing = [name for name in names if name not in args]
    if missing:
        return f'missing args for {action}: {missing}'
    for name, value in args.items():
        if isinstance(value, str):
            if not _clean_text(value):
                return (f'argument {name!r} contains whitespace/control characters; '
                        f'the bridge command line is space-delimited')
            if len(value) > 64:
                return f'argument {name!r} is too long (max 64 chars)'
        elif isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
            if isinstance(value, float) and not (float('-inf') < value < float('inf')):
                return f'argument {name!r} is not a finite number'
        else:
            return f'argument {name!r} has unsupported type {type(value).__name__}'
    unexpected = [name for name in args if name not in allowed]
    if unexpected:
        return f'unexpected args for {action}: {unexpected} (allowed: {sorted(allowed)})'
    # T35: spell target shapes.  A target is all-or-nothing and its kind is unambiguous.
    if action == 'cast_spell':
        present = [name for name in ('x', 'y', 'z') if name in args]
        if present and len(present) != 3:
            return ('cast_spell target needs all of x,y,z (or omit all three for a no-target '
                    'spell)')
    if action == 'battle_cast_spell':
        has_unit = 'stack' in args
        has_hex = 'x' in args or 'y' in args
        if has_unit and has_hex:
            return ('battle_cast_spell: give either stack (unit target) or x/y (hex target), '
                    'not both')
        if has_hex and not ('x' in args and 'y' in args):
            return 'battle_cast_spell: a hex target needs both x and y'
    ordered = list(names) + [name for name in optional if name in args]
    for name in ordered:
        value = args[name]
        if name in _STR_ENUMS:
            if value not in _STR_ENUMS[name]:
                return f"argument {name!r} must be one of {list(_STR_ENUMS[name])}"
            continue
        if action == 'answer' and name == 'value' and isinstance(value, str):
            if not _SAFE_TOKEN.match(value):
                return "argument 'value' must be a small integer or a safe token"
            continue
        parsed, error = _int_arg(action, name, value)
        if error:
            return error
        args[name] = parsed
    return None


def _bridge_line(action: str, args: dict) -> str:
    """Build the single-line bridge command: required args first, optional args last."""
    if action == 'battle_cast_spell':
        # battle_cast_spell <spellId> [unit <stackId> | hex <x> <y>]
        tokens = [str(args['spell'])]
        if 'stack' in args:
            tokens += ['unit', str(args['stack'])]
        elif 'x' in args:
            tokens += ['hex', str(args['x']), str(args['y'])]
        return 'battle_cast_spell ' + ' '.join(tokens)
    bridge_cmd, names = ACTION_SPECS[action]
    tokens = [str(args[name]) for name in names]
    tokens.extend(str(args[name]) for name in _OPTIONAL_ARGS.get(action, ()) if name in args)
    return bridge_cmd if not tokens else bridge_cmd + ' ' + ' '.join(tokens)


# ---------------------------------------------------------------- post-condition verifiers
# Diff-based (before/after), object-level: "the bridge said OK" is NOT evidence.  Every verifier
# returns one of completed / submitted / rejected / unknown.
_VERIFIED_ACTIONS = {'build', 'recruit', 'recruit_hero', 'move', 'move_hero', 'transfer',
                     'merge',
                     'end_turn', 'answer', 'battle_attack', 'battle_approach', 'battle_defend',
                     'battle_wait', 'battle_move', 'confirm_deployment',
                     'split', 'upgrade', 'market_trade',
                     'cast_spell', 'battle_cast_spell'}
_BATTLE_ACTIONS = {'battle_attack', 'battle_approach', 'battle_defend', 'battle_wait',
                   'battle_move', 'confirm_deployment'}


def _indexed(items, index):
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            if int(item.get('index', -10 ** 9)) == int(index):
                return item
        except (TypeError, ValueError):
            continue
    if isinstance(index, int) and 0 <= index < len(items or []):
        return items[index]
    return None


def _hero_entry(st: dict, index):
    return _indexed(st.get('heroes'), index)


def _town_entry(st: dict, index):
    return _indexed(st.get('towns'), index)


def _container(st: dict, kind: str, index):
    if kind == 'hero':
        return _hero_entry(st, index)
    if kind == 'town':
        return _town_entry(st, index)
    return None


def _army_entries(container) -> list:
    if not isinstance(container, dict):
        return []
    for key in ('army', 'garrison'):
        value = container.get(key)
        if isinstance(value, list):
            return [entry for entry in value if isinstance(entry, dict)]
    return []


def _army_slot(container, slot) -> dict | None:
    """One army slot entry (slot/creature/count) by slot number."""
    try:
        wanted = int(slot)
    except (TypeError, ValueError):
        return None
    for entry in _army_entries(container):
        try:
            if int(entry.get('slot', -1)) == wanted:
                return entry
        except (TypeError, ValueError):
            continue
    return None


def _count_total(entries) -> int:
    total = 0
    for entry in entries or []:
        try:
            total += int(entry.get('count') or 0)
        except (TypeError, ValueError):
            continue
    return total


def _creature_count(state: dict, town: dict | None, creature) -> int:
    """How many of one creature the town garrison + all visible hero armies hold."""
    try:
        wanted = int(creature)
    except (TypeError, ValueError):
        return 0
    containers = [town] + list((state or {}).get('heroes') or [])
    total = 0
    for container in containers:
        for entry in _army_entries(container):
            try:
                if int(entry.get('creature')) == wanted:
                    total += int(entry.get('count') or 0)
            except (TypeError, ValueError):
                continue
    return total


def _dwelling_available(town, level):
    if not isinstance(town, dict):
        return None
    for dwelling in town.get('dwellings') or []:
        if not isinstance(dwelling, dict):
            continue
        try:
            if int(dwelling.get('level')) != int(level):
                continue
        except (TypeError, ValueError):
            continue
        available = dwelling.get('available')
        if isinstance(available, bool) or not isinstance(available, (int, float)):
            return available
        return int(available)
    return None


def _battle_signature(battle: dict):
    """Active stack + per-stack (id, side, count, hex); None when no battle is active."""
    if not battle or not battle.get('active'):
        return None
    stacks = []
    for stack in battle.get('stacks') or []:
        if not isinstance(stack, dict):
            continue
        hex_pos = stack.get('hex')
        if isinstance(hex_pos, list):
            hex_pos = tuple(hex_pos)
        stacks.append((stack.get('id'), stack.get('side'), stack.get('count'), hex_pos))
    return (battle.get('active_stack'),
            tuple(sorted(stacks, key=lambda stack: str(stack))))


def _battle_stack(battle: dict, stack_id) -> dict | None:
    """Find one stack in a battle view by its unit id."""
    try:
        wanted = int(stack_id)
    except (TypeError, ValueError):
        return None
    for stack in (battle or {}).get('stacks') or []:
        if not isinstance(stack, dict):
            continue
        try:
            if int(stack.get('id', -1)) == wanted:
                return stack
        except (TypeError, ValueError):
            continue
    return None


def _state_fingerprint(st: dict):
    """Small identity fingerprint used to prove that load_game() changed the observed state."""
    if not st:
        return None
    return {
        'day': (st.get('date') or {}).get('day'),
        'gold': (st.get('resources') or {}).get('gold'),
        'heroes': sorted(str(hero.get('name')) for hero in st.get('heroes') or []
                         if isinstance(hero, dict)),
        'towns_built': [len(town.get('built') or []) for town in st.get('towns') or []
                        if isinstance(town, dict)],
        'in_game': bool(st.get('in_game')),
    }


def _state_ts(st: dict) -> float | None:
    """state.json ts_wall (milliseconds) as epoch seconds, or None when absent."""
    value = (st or {}).get('ts_wall')
    if isinstance(value, (int, float)):
        value = float(value)
        return value / 1000.0 if value > 10_000_000_000 else value
    return None


def _verify_end_turn(before: dict, after: dict) -> tuple[str, str]:
    date_before = before.get('date') or {}
    date_after = after.get('date') or {}
    keys = [key for key in ('month', 'week', 'day')
            if date_before.get(key) is not None and date_after.get(key) is not None]
    if keys:
        old = tuple(date_before.get(key) for key in keys)
        new = tuple(date_after.get(key) for key in keys)
        if new > old:
            return 'completed', f'date advanced {old}->{new}'
        if new != old:
            return 'submitted', f'date changed but not forward: {old}->{new}'
        return 'submitted', 'date unchanged (turn may still be settling)'
    day_before = date_before.get('day')
    day_after = date_after.get('day')
    if day_before is not None and day_after is not None and day_after > day_before:
        return 'completed', f'day {day_before}->{day_after}'
    return 'submitted', f'day {day_before}->{day_after}'


def _tile3(value) -> tuple:
    """Normalise a position/visitable list to a 3D tile tuple (missing z defaults to 0)."""
    items = list(value or [])
    while len(items) < 3:
        items.append(0)
    return tuple(items[:3])


def _town_position(state: dict, town_index: int):
    """Town interaction tile: `tile` (T40 canonical), else `visitable`, else pos."""
    towns = state.get('towns') or []
    if not isinstance(town_index, int) or town_index < 0 or town_index >= len(towns):
        return None
    town = towns[town_index]
    town_id = town.get('id')
    town_name = town.get('name')
    for obj in state.get('objects') or []:
        if not isinstance(obj, dict) or obj.get('kind') != 'town':
            continue
        if (town_id is not None and obj.get('id') == town_id) or \
                (town_id is None and town_name is not None and obj.get('name') == town_name):
            return _tile3(obj.get('tile') or obj.get('visitable') or obj.get('pos'))
    return None


def _hero_position(state: dict, hero_index: int):
    """Hero interaction tile: the tile the visit/path system uses.

    T40 adds the canonical names: `tile` is the interaction tile (== visitable) and `anchor` is
    the sprite anchor (== pos).  Old clients only send pos/visitable, so both are accepted.
    The hero's `pos` is the sprite anchor and can differ from the interaction tile by the hero
    visitable offset (live short game: hero anchor (34,33) with visitable (32,33), town visitable
    (32,33)).  Visiting/town comparisons therefore use the interaction tile on both sides.
    """
    heroes = state.get('heroes') or []
    if not isinstance(hero_index, int) or hero_index < 0 or hero_index >= len(heroes):
        return None
    hero = heroes[hero_index]
    return _tile3(hero.get('tile') or hero.get('visitable') or hero.get('pos'))


def _precondition_error(cmd: str, args: dict, before: dict) -> str | None:
    """Reject actions whose preconditions are visibly unmet, BEFORE sending anything.

    Prevents pointless bridge commands (and 6 s of verification) when the after-state could
    never show the effect, e.g. transfer town garrison -> hero while the hero is not in town.
    """
    if cmd == 'cast_spell':
        hero = _hero_entry(before, args.get('hero'))
        if hero is None:
            return f'hero {args.get("hero")} is not observable in the current state'
        spells = hero.get('spells')
        spell_id = int(args['spell'])
        if isinstance(spells, list):
            known = {int(sp.get('id')) for sp in spells
                     if isinstance(sp, dict) and sp.get('id') is not None}
            if known and spell_id not in known:
                return (f'spell {spell_id} is not in hero {args["hero"]}\'s spellbook '
                        f'(known: {sorted(known)})')
            for spell in spells:
                if not isinstance(spell, dict) or int(spell.get('id') or -1) != spell_id:
                    continue
                cost = spell.get('cost')
                mana = hero.get('mana')
                if isinstance(cost, int) and isinstance(mana, int) and mana < cost:
                    return (f'not enough mana for spell {spell_id}: costs {cost}, '
                            f'hero {args["hero"]} has {mana}')
        return None
    if cmd == 'battle_cast_spell':
        battle = before.get('battle') or {}
        if not battle.get('active'):
            return 'no active battle: battle_cast_spell only works inside a battle'
        if battle.get('active_stack') == -1:
            return 'deployment phase: call battle_start() before casting battle spells'
        if 'stack' in args and _battle_stack(battle, args['stack']) is None:
            return f'target stack {args["stack"]} is not visible in the battle state'
        return None
    if cmd == 'recruit_hero':
        # 5D-4 root cause (live WIN run day1/day14, short game day1): the bridge answers OK for
        # recruit_hero because it only submits the async HireHero request, while the server's
        # HeroPoolProcessor::hireHero refuses a town hire when the town has a visiting hero
        # ("There is visiting hero - no place!") - resulting in OK + no gold spent + no new hero.
        # The state exposes both facts, so reject before sending instead of accepting a no-op.
        # (Older/partial fixtures without a town entry keep the legacy path: no precondition.)
        town = _town_entry(before, args.get('town'))
        if town is not None:
            available = town.get('available_heroes')
            if isinstance(available, list) and not available:
                return (f'town {args.get("town")} offers no hero to hire '
                        f'(tavern not built or the hero pool is empty); nothing to send')
            visiting = town.get('hero_visiting')
            if not isinstance(visiting, bool):
                town_pos = _town_position(before, args.get('town'))
                visiting = bool(town_pos) and any(
                    _hero_position(before, hero.get('index')) == town_pos
                    for hero in before.get('heroes') or [] if isinstance(hero, dict))
            if visiting:
                return (f'town {args.get("town")} has a visiting hero: the server refuses a '
                        f'tavern hire while the town is occupied ("There is visiting hero - '
                        f'no place!"). Move the hero off the town tile first, then retry '
                        f'recruit_hero.')
        return None
    if cmd not in ('transfer', 'merge'):
        return None
    src, dst = args.get('src'), args.get('dst')
    if not (isinstance(src, str) and isinstance(dst, str)):
        return None
    town_idx = hero_idx = None
    if src == 'town' and dst == 'hero':
        town_idx, hero_idx = args.get('src_index'), args.get('dst_index')
    elif src == 'hero' and dst == 'town':
        town_idx, hero_idx = args.get('dst_index'), args.get('src_index')
    else:
        return None
    town_pos = _town_position(before, town_idx)
    hero_pos = _hero_position(before, hero_idx)
    if town_pos is None or hero_pos is None:
        return None
    if town_pos != hero_pos:
        return (f'hero {hero_idx} is not visiting town {town_idx} '
                f'(hero at {hero_pos}, town at {town_pos}): move the hero onto the town tile '
                f'first, then transfer')
    return None


def _slot_snapshot(container, slot: int) -> dict:
    """One army slot as {slot, creature, count, empty} (an absent stack = empty)."""
    entry = _army_slot(container, slot)
    if entry is None:
        return {'slot': slot, 'creature': None, 'count': 0, 'empty': True}
    try:
        count = int(entry.get('count') or 0)
    except (TypeError, ValueError):
        count = 0
    return {'slot': slot, 'creature': entry.get('creature'), 'count': count,
            'empty': False}


def _slot_text(snapshot: dict) -> str:
    if snapshot.get('empty'):
        return 'empty'
    return f"creature={snapshot.get('creature')} count={snapshot.get('count')}"


def _slots_match(first: dict, second: dict) -> bool:
    """Exact slot equality, including the empty-slot case (creature/count compared)."""
    return (first.get('creature') == second.get('creature')
            and int(first.get('count') or 0) == int(second.get('count') or 0))


def _transferred_count(src_before: dict, src_after: dict,
                       dst_before: dict, dst_after: dict) -> int | None:
    """Move count for a single-stack transfer between the requested slots, or None.

    Accepted shapes (bridge `swapCreatures` on the requested slots):
      * dst was empty  -> after.dst == before.src (whole stack, source slot now empty/absent)
      * dst same type  -> after.dst == before.dst + moved and after.src == before.src - moved
    Different creature types can only be proven by the exact swap shape.
    """
    src_creature = src_before.get('creature')
    if src_creature is None:
        return None
    moved = int(src_before.get('count') or 0) - int(src_after.get('count') or 0)
    if moved <= 0:
        return None
    if src_after.get('creature') is not None and src_after.get('creature') != src_creature:
        return None
    dst_creature = dst_before.get('creature')
    if dst_creature is None:
        if dst_after.get('creature') != src_creature:
            return None
        if int(dst_after.get('count') or 0) != moved:
            return None
        return moved
    if dst_creature != src_creature or dst_after.get('creature') != src_creature:
        return None
    if int(dst_after.get('count') or 0) != int(dst_before.get('count') or 0) + moved:
        return None
    return moved


def _verify_effect(action: str, args: dict, before: dict, after: dict) -> tuple[str, str]:
    """Object-level before/after DIFF verifier (S0 / D-007: submitted != completed).

    completed - the target post-condition is visible in the after-state
    submitted - bridge accepted, no observable change (yet); never fabricated as completed
    rejected  - the after-state clearly lacks the requested effect
    unknown   - the observation needed for verification is missing
    """
    if action not in _VERIFIED_ACTIONS:
        return 'submitted', 'bridge accepted; no post-condition verifier for this action'
    if not before:
        return 'unknown', 'before-state observation missing'
    if not after:
        return 'unknown', 'after-state observation missing'
    try:
        if action == 'build':
            town_before = _town_entry(before, args['town'])
            town_after = _town_entry(after, args['town'])
            if town_before is None or town_after is None:
                return 'unknown', f"town {args['town']} not observable in both observations"
            building = int(args['id'])
            built_before = {int(item) for item in town_before.get('built') or []}
            built_after = {int(item) for item in town_after.get('built') or []}
            if building in built_after and building not in built_before:
                return 'completed', f'town {args["town"]}: building {building} appeared'
            if building in built_after:
                return ('submitted',
                        f'town {args["town"]}: building {building} was already present before '
                        f'the action (cannot attribute it to this command)')
            return 'rejected', f'town {args["town"]}: building {building} absent after the action'
        if action == 'recruit_hero':
            # A hero hire must show either a new hero in the list or the hire payment.  Merely
            # accepting the command is not completion (live: OK reply while nothing happened).
            def _hero_names(snapshot: dict) -> set:
                return {str(hero.get('name')) for hero in snapshot.get('heroes') or []
                        if isinstance(hero, dict) and hero.get('name')}
            hired = sorted(_hero_names(after) - _hero_names(before))
            if hired:
                return 'completed', f'new hero observed: {", ".join(hired)}'
            gold_before = (before.get('resources') or {}).get('gold')
            gold_after = (after.get('resources') or {}).get('gold')
            spent = (gold_before - gold_after
                     if isinstance(gold_before, (int, float))
                     and isinstance(gold_after, (int, float)) else 0)
            if spent > 0:
                return ('submitted',
                        f'gold spent {gold_before}->{gold_after} but the new hero is not '
                        f'observable yet (do not retry: a hire may already be paid)')
            return ('submitted',
                    f'bridge accepted; no new hero and no gold change observed '
                    f'(town {args.get("town")} index {args.get("index")})')
        if action == 'recruit':
            town_before = _town_entry(before, args['town'])
            town_after = _town_entry(after, args['town'])
            if town_before is None or town_after is None:
                return 'unknown', f"town {args['town']} not observable in both observations"
            level = int(args['level'])
            requested = args.get('creature')
            if requested is not None:
                count_before = _creature_count(before, town_before, requested)
                count_after = _creature_count(after, town_after, requested)
                if count_after > count_before:
                    return ('completed',
                            f'creature {requested}: {count_before}->{count_after} '
                            f'(+{count_after - count_before})')
                available_before = _dwelling_available(town_before, level)
                available_after = _dwelling_available(town_after, level)
                if (available_before is not None and available_after is not None
                        and available_after < available_before):
                    return ('completed',
                            f'creature {requested}: dwelling level {level} available '
                            f'{available_before}->{available_after}')
                return ('submitted',
                        f'creature {requested}: {count_before}->{count_after} yet')
            available_before = _dwelling_available(town_before, level)
            available_after = _dwelling_available(town_after, level)
            if (available_before is not None and available_after is not None
                    and available_after < available_before):
                return ('completed',
                        f'dwelling level {level} available {available_before}->{available_after}')
            garrison_before = _count_total(_army_entries(town_before))
            garrison_after = _count_total(_army_entries(town_after))
            if garrison_after > garrison_before:
                return 'completed', f'garrison count {garrison_before}->{garrison_after}'
            heroes_before = sum(_count_total(_army_entries(hero))
                                for hero in before.get('heroes') or [])
            heroes_after = sum(_count_total(_army_entries(hero))
                               for hero in after.get('heroes') or [])
            if heroes_after > heroes_before:
                return 'completed', f'hero army count {heroes_before}->{heroes_after}'
            return 'submitted', f'dwelling level {level} / army counts unchanged yet'
        if action in ('move', 'move_hero'):
            hero_index = args['hero']
            before_hero = _hero_entry(before, hero_index)
            hero = _hero_entry(after, hero_index)
            if hero is None:
                return 'unknown', f'hero {hero_index} missing from the after-state'
            target = [int(args['x']), int(args['y']), int(args.get('z') or 0)]
            # The bridge/UI move target is the *interaction tile* (hero::visitablePos, exposed as
            # `visitable` by old clients and as the canonical `tile` by T40 clients); the sprite
            # anchor (`pos`, canonical `anchor`) is offset by the hero's visitable offset, so
            # arrive checks must use the tile and report the anchor as extra evidence.
            position = list((hero.get('tile') or hero.get('visitable')
                             or hero.get('pos') or [0, 0, 0]))
            while len(position) < 3:
                position.append(0)
            previous = list(((before_hero or {}).get('tile')
                             or (before_hero or {}).get('visitable')
                             or (before_hero or {}).get('pos') or [0, 0, 0]))
            while len(previous) < 3:
                previous.append(0)
            anchor = list(hero.get('anchor') or hero.get('pos') or [])[:3] or None
            if position[:3] == target[:3]:
                return ('completed',
                        f'hero {hero_index} tile {previous[:3]}->{position[:3]}'
                        f'{f" (anchor {anchor})" if anchor else ""}')
            return ('submitted',
                    f'hero {hero_index} tile {position[:3]}, target {target[:3]} '
                    f'(before {previous[:3]})')
        if action in ('transfer', 'merge'):
            source_before = _container(before, args['src'], args['src_index'])
            source_after = _container(after, args['src'], args['src_index'])
            dest_before = _container(before, args['dst'], args['dst_index'])
            dest_after = _container(after, args['dst'], args['dst_index'])
            if None in (source_before, source_after, dest_before, dest_after):
                return 'unknown', 'transfer endpoints not observable in both observations'
            try:
                src_slot = int(args['src_slot'])
                dst_slot = int(args['dst_slot'])
            except (TypeError, ValueError):
                return 'unknown', 'transfer slot arguments are unreadable'
            src_b = _slot_snapshot(source_before, src_slot)
            src_a = _slot_snapshot(source_after, src_slot)
            dst_b = _slot_snapshot(dest_before, dst_slot)
            dst_a = _slot_snapshot(dest_after, dst_slot)
            src_old_total = _count_total(_army_entries(source_before))
            src_new_total = _count_total(_army_entries(source_after))
            dst_old_total = _count_total(_army_entries(dest_before))
            dst_new_total = _count_total(_army_entries(dest_after))
            conserved = (src_old_total + dst_old_total) == (src_new_total + dst_new_total)
            detail = (f'{args["src"]}#{args["src_index"]}[{src_slot}] '
                      f'{_slot_text(src_b)}->{_slot_text(src_a)}; '
                      f'{args["dst"]}#{args["dst_index"]}[{dst_slot}] '
                      f'{_slot_text(dst_b)}->{_slot_text(dst_a)}; '
                      f'totals {src_old_total}+{dst_old_total}->'
                      f'{src_new_total}+{dst_new_total}')
            if action == 'transfer':
                # The bridge uses CCallback::swapCreatures on the requested slots: completed means
                # the two slots exactly exchanged, or the source stack moved into the requested dst
                # slot (dst empty) / merged into the same creature there.  Only counts were checked
                # before (a wrong creature/slot or an unrelated change could pass).
                swapped = (_slots_match(src_a, dst_b) and _slots_match(dst_a, src_b)
                           and _slots_match(src_b, src_b))
                if swapped and conserved:
                    return 'completed', f'{detail}; proof=slot_swap'
                moved = _transferred_count(src_b, src_a, dst_b, dst_a)
                if moved is not None and conserved:
                    return 'completed', f'{detail}; proof=single_slot_move moved={moved}'
                if (conserved and src_new_total < src_old_total
                        and dst_new_total > dst_old_total
                        and int(src_a.get('count') or 0) == 0
                        and int(dst_a.get('count') or 0) == 0):
                    # Legacy totals-only fixtures: the requested destination slot is not observable
                    # after the transfer (both requested slots read as empty), so only container
                    # totals can be checked.  Clearly labelled; never claims slot-level proof.
                    return ('completed', f'{detail}; proof=container_totals_legacy '
                            '(requested slots not observable after the transfer)')
                return 'submitted', detail
            # merge (same creature only; the source stack becomes empty/absent)
            try:
                src_creature_before = int(src_b['creature'])
                src_creature_after = (int(src_a['creature'])
                                      if src_a.get('creature') is not None else None)
                dst_creature_before = int(dst_b['creature'])
                dst_creature_after = (int(dst_a['creature'])
                                      if dst_a.get('creature') is not None else None)
                src_count_before = int(src_b.get('count') or 0)
                dst_count_before = int(dst_b.get('count') or 0)
                dst_count_after = int(dst_a.get('count') or 0)
            except (TypeError, ValueError, KeyError):
                return 'unknown', f'merge slot contents unreadable ({detail})'
            if (conserved and src_creature_before == dst_creature_before
                    and dst_creature_after == src_creature_before
                    and src_creature_after is None
                    and dst_count_after == src_count_before + dst_count_before):
                return 'completed', f'{detail}; proof=same_creature_merge'
            if not conserved:
                return 'submitted', f'{detail}; army totals not conserved'
            return 'submitted', detail
        if action == 'split':
            container_before = _container(before, args['owner'], args['index'])
            container_after = _container(after, args['owner'], args['index'])
            if container_before is None or container_after is None:
                return 'unknown', 'army not observable in both observations'
            slot = args['slot']
            source_before = _army_slot(container_before, slot)
            if source_before is None:
                return 'unknown', f'slot {slot} not observable before the split'
            source_after = _army_slot(container_after, slot)
            try:
                wanted = int(args['amount'])
                before_count = int(source_before.get('count') or 0)
                creature = int(source_before.get('creature') or -1)
            except (TypeError, ValueError):
                return 'unknown', 'split source stack is unreadable'
            if source_after is None:
                return 'rejected', f'slot {slot} disappeared after the split'
            try:
                after_count = int(source_after.get('count') or 0)
            except (TypeError, ValueError):
                return 'unknown', 'split source count is unreadable'
            before_slots = set()
            for old in _army_entries(container_before):
                try:
                    before_slots.add(int(old.get('slot', -1)))
                except (TypeError, ValueError):
                    continue
            new_slot = None
            for entry in _army_entries(container_after):
                try:
                    slot_no = int(entry.get('slot', -1))
                    entry_creature = int(entry.get('creature', -1))
                    entry_count = int(entry.get('count') or 0)
                except (TypeError, ValueError):
                    continue
                if slot_no not in before_slots and entry_creature == creature \
                        and entry_count == wanted:
                    new_slot = entry
                    break
            total_before = _count_total(_army_entries(container_before))
            total_after = _count_total(_army_entries(container_after))
            try:
                source_creature = int(source_after.get('creature') or -1)
            except (TypeError, ValueError):
                source_creature = -1
            if (source_creature == creature and before_count - after_count == wanted
                    and new_slot is not None and total_before == total_after):
                return ('completed',
                        f'slot {slot}: {before_count}->{after_count}, new slot '
                        f'{new_slot.get("slot")} has {wanted} (total {total_after})')
            return ('submitted',
                    f'slot {slot}: {before_count}->{after_count}, new slot={new_slot}')
        if action == 'upgrade':
            container_before = _container(before, args['owner'], args['index'])
            container_after = _container(after, args['owner'], args['index'])
            if container_before is None or container_after is None:
                return 'unknown', 'army not observable in both observations'
            slot = args['slot']
            source_before = _army_slot(container_before, slot)
            if source_before is None:
                return 'unknown', f'slot {slot} not observable before the upgrade'
            source_after = _army_slot(container_after, slot)
            if source_after is None:
                return ('rejected', f'slot {slot} disappeared after the upgrade')
            try:
                wanted = int(args['creature'])
                old_creature = int(source_before.get('creature') or -1)
                old_count = int(source_before.get('count') or 0)
                new_creature = int(source_after.get('creature') or -1)
                new_count = int(source_after.get('count') or 0)
            except (TypeError, ValueError):
                return 'unknown', 'upgrade stack is unreadable'
            if new_creature == wanted and new_count == old_count:
                return ('completed',
                        f'slot {slot}: creature {old_creature}->{new_creature}, '
                        f'count kept at {old_count}')
            return ('submitted',
                    f'slot {slot} still creature {new_creature} count {new_count} '
                    f'(wanted {wanted}, before {old_creature}x{old_count})')
        if action == 'market_trade':
            before_res = before.get('resources') or {}
            after_res = after.get('resources') or {}
            try:
                sell = _RESOURCE_NAMES[int(args['sell'])]
                buy = _RESOURCE_NAMES[int(args['buy'])]
                amount = int(args['amount'])
                sell_before = int(before_res.get(sell) or 0)
                sell_after = int(after_res.get(sell) or 0)
                buy_before = int(before_res.get(buy) or 0)
                buy_after = int(after_res.get(buy) or 0)
            except (KeyError, IndexError, TypeError, ValueError):
                return 'unknown', 'trade resources are unreadable'
            if sell_after <= sell_before - amount and buy_after > buy_before:
                return ('completed',
                        f'{sell} {sell_before}->{sell_after}, {buy} {buy_before}->{buy_after}')
            if sell_after == sell_before and buy_after == buy_before:
                return 'submitted', 'resource counts unchanged yet'
            return ('submitted',
                    f'partial change: {sell} {sell_before}->{sell_after}, '
                    f'{buy} {buy_before}->{buy_after}')
        if action == 'end_turn':
            return _verify_end_turn(before, after)
        if action == 'answer':
            query_before = before.get('query')
            query_after = after.get('query')
            if not query_after:
                return 'completed', 'query cleared'
            if _query_sig(query_after) != _query_sig(query_before):
                return 'completed', 'query changed'
            return 'submitted', 'query still pending'
        if action == 'battle_attack':
            battle_before = before.get('battle') or {}
            battle_after = after.get('battle') or {}
            if not battle_before.get('active'):
                return 'unknown', 'no battle observed before the attack'
            target_id = args['stack']
            target_before = _battle_stack(battle_before, target_id)
            if target_before is None:
                return 'unknown', f'target stack {target_id} not observed before the attack'
            if not battle_after.get('active'):
                return 'completed', 'battle ended after the attack'
            target_after = _battle_stack(battle_after, target_id)
            if target_after is None:
                return 'completed', f'target stack {target_id} is gone'
            try:
                before_count = int(target_before.get('count') or 0)
                after_count = int(target_after.get('count') or 0)
            except (TypeError, ValueError):
                return 'unknown', f'target stack {target_id} count is unreadable'
            if after_count < before_count:
                return ('completed',
                        f'target {target_id} count {before_count}->{after_count}')
            if battle_before.get('active_stack') != battle_after.get('active_stack'):
                # The action resolved (turn advanced) but the target lost no visible unit.  This
                # can be a non-killing hit, so it is honest "submitted" - never "completed".
                return ('submitted',
                        f'target {target_id} count unchanged ({after_count}); attack resolved '
                        f'but no kill was observed (damage may not have killed a unit)')
            return 'submitted', f'no observable effect on target {target_id} yet'
        if action == 'battle_approach':
            battle_before = before.get('battle') or {}
            battle_after = after.get('battle') or {}
            if not battle_before.get('active'):
                return 'unknown', 'no battle observed before the approach'
            unit_id = battle_before.get('active_stack')
            unit_before = _battle_stack(battle_before, unit_id)
            if unit_before is None:
                return 'unknown', f'active stack {unit_id} not observed before the approach'
            if not battle_after.get('active'):
                return 'submitted', 'battle is no longer active after the approach'
            unit_after = _battle_stack(battle_after, unit_id)
            if unit_after is None:
                return 'unknown', f'active stack {unit_id} missing from the after-state'
            hex_before = list(unit_before.get('hex') or [])
            hex_after = list(unit_after.get('hex') or [])
            if hex_before != hex_after:
                return ('completed', f'stack {unit_id} moved {hex_before}->{hex_after}')
            return 'submitted', f'stack {unit_id} has not moved yet'
        if action == 'cast_spell':
            hero_index = args['hero']
            hero_before = _hero_entry(before, hero_index)
            hero_after = _hero_entry(after, hero_index)
            if hero_before is None or hero_after is None:
                return 'unknown', f'hero {hero_index} not observable in both observations'
            mana_before = hero_before.get('mana')
            mana_after = hero_after.get('mana')
            if (isinstance(mana_before, int) and isinstance(mana_after, int)
                    and mana_after < mana_before):
                return ('completed',
                        f'hero {hero_index} mana {mana_before}->{mana_after} '
                        f'(spell {args["spell"]} consumed)')
            if _query_sig(after.get('query')) != _query_sig(before.get('query')):
                return ('completed',
                        f'spell {args["spell"]} opened a query (answer it with game_events/'
                        f'answer)')
            if ((after.get('battle') or {}).get('active')
                    and not (before.get('battle') or {}).get('active')):
                return 'completed', 'a battle started after the spell'
            return ('submitted',
                    f'spell {args["spell"]} accepted; hero mana unchanged yet '
                    f'({mana_before}->{mana_after})')
        if action == 'battle_cast_spell':
            battle_before = before.get('battle') or {}
            battle_after = after.get('battle') or {}
            if not battle_before.get('active'):
                return 'unknown', 'no battle observed before the spell'
            if not battle_after.get('active'):
                return 'completed', 'battle ended after the spell'
            mana_before = battle_before.get('hero_mana')
            mana_after = battle_after.get('hero_mana')
            if (isinstance(mana_before, int) and isinstance(mana_after, int)
                    and mana_after < mana_before):
                return ('completed',
                        f'hero mana {mana_before}->{mana_after} after spell '
                        f'{args["spell"]}')
            casts_before = battle_before.get('cast_spells')
            casts_after = battle_after.get('cast_spells')
            if (isinstance(casts_before, int) and isinstance(casts_after, int)
                    and casts_after > casts_before):
                return ('completed',
                        f'cast_spells {casts_before}->{casts_after} after spell '
                        f'{args["spell"]}')
            if 'stack' in args:
                target_before = _battle_stack(battle_before, args['stack'])
                target_after = _battle_stack(battle_after, args['stack'])
                if target_before is not None and target_after is None:
                    return ('completed',
                            f'target stack {args["stack"]} is gone after the spell')
                if target_before is not None and target_after is not None:
                    try:
                        count_before = int(target_before.get('count') or 0)
                        count_after = int(target_after.get('count') or 0)
                    except (TypeError, ValueError):
                        count_before = count_after = None
                    if (count_before is not None and count_after is not None
                            and count_after < count_before):
                        return ('completed',
                                f'target {args["stack"]} count {count_before}->{count_after}')
            signature_before = _battle_signature(battle_before)
            signature_after = _battle_signature(battle_after)
            if (signature_before is not None and signature_after is not None
                    and signature_before != signature_after):
                return 'completed', 'battle state changed after the spell'
            return 'submitted', 'spell accepted; no observable effect yet'
        if action in _BATTLE_ACTIONS:
            battle_before = before.get('battle') or {}
            battle_after = after.get('battle') or {}
            if battle_before.get('active') and not battle_after.get('active'):
                return 'completed', 'battle ended'
            signature_before = _battle_signature(battle_before)
            signature_after = _battle_signature(battle_after)
            if (signature_before is not None and signature_after is not None
                    and signature_before != signature_after):
                return ('completed',
                        'battle state changed (active stack / stack count / hex)')
            if battle_after.get('active'):
                return 'submitted', 'battle active but no observable change yet'
            return 'submitted', 'no battle observed after the action'
        return 'submitted', 'no post-condition verifier for this action'
    except (KeyError, TypeError, ValueError) as exc:
        return 'unknown', f'verifier error: {exc}'


def _action_index_path() -> Path:
    return _dir() / 'action_index.json'


def _load_action_index() -> dict:
    try:
        data = json.loads(_action_index_path().read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


_INDEX_LOCK = threading.Lock()


def _bridge_fields(res) -> tuple[bool, bool]:
    """(accepted, unknown_or_timeout) from a bridge CommandResult-like object."""
    status = str(getattr(res, 'status', '') or '').upper()
    line = str(getattr(res, 'line', '') or '')
    rejected = status == 'REJECTED' or ' ERR ' in f' {line} '
    unknown = status in ('UNKNOWN', 'NOT_SENT')
    return (status == 'COMPLETED' and not rejected), unknown


def _bridge_evidence(res) -> str:
    line = str(getattr(res, 'line', '') or '')
    detail = str(getattr(res, 'detail', '') or '')
    return (line or detail or 'no bridge reply')[:200]


def _bridge_rejected(res) -> bool:
    status = str(getattr(res, 'status', '') or '').upper()
    line = str(getattr(res, 'line', '') or '')
    return status == 'REJECTED' or ' ERR ' in f' {line} '


_RECONCILE_WAIT_S = 5.0        # grace time for a late bridge reply after a crash
_RECONCILE_POLL_S = 0.5
_STALE_PENDING_S = 600.0       # other-process reservation older than this is a recovery candidate


def _pending_age(previous: dict) -> float | None:
    ts = previous.get('ts')
    return (time.time() - float(ts)) if isinstance(ts, (int, float)) else None


def _parse_result_line(line: str) -> tuple[str, str]:
    body = line
    if line.startswith('seq='):
        parts = line.split(' ', 1)
        body = parts[1] if len(parts) > 1 else ''
    for kind in ('OK', 'ERR', 'QUERY', 'INFO', 'AUTO'):
        if body.startswith(kind):
            return kind, body
    return 'OTHER', body


def _reconcile_pending(previous: dict, request: str,
                       wait_s: float | None = None) -> dict:
    """Try to resolve a pending reservation WITHOUT sending any command.

    Crash-window semantics: we can never *prove* that a command was not executed, so this
    only reports what the bridge evidence actually shows:
      rejected  - the recorded seq has an ERR reply after the recorded results offset
      submitted - the recorded seq has an OK/QUERY reply (accepted, effect not verified)
      needs_reconciliation - no trustworthy reply: the command may or may not have executed
    Session/epoch changes invalidate the seq space, so such reservations are never resolved
    from late replies.
    """
    wait = _RECONCILE_WAIT_S if wait_s is None else wait_s
    expected_seq = previous.get('expected_seq')
    offset = previous.get('results_offset')
    try:
        agent = _agent()
        identity = agent.bridge_identity()
    except Exception as exc:
        return {'status': 'needs_reconciliation', 'reason': f'bridge unavailable: {exc}'}
    recorded_session = previous.get('session_id')
    current_session = identity.get('session_id')
    if recorded_session and current_session and recorded_session != current_session:
        return {'status': 'needs_reconciliation',
                'reason': 'bridge session changed since the reservation (restart/new game)'}
    recorded_epoch = previous.get('epoch_started_at')
    current_epoch = identity.get('epoch_started_at')
    if (isinstance(recorded_epoch, (int, float)) and isinstance(current_epoch, (int, float))
            and float(recorded_epoch) < float(current_epoch) - 1.0):
        return {'status': 'needs_reconciliation',
                'reason': 'engine generation changed since the reservation'}
    if not isinstance(expected_seq, int) or not isinstance(offset, int):
        return {'status': 'needs_reconciliation',
                'reason': 'reservation has no recorded bridge seq/results offset'}
    prefix = f'seq={expected_seq} '
    deadline = time.time() + wait
    while True:
        try:
            lines = agent.results()[max(0, offset):]
        except Exception:
            lines = []
        for line in lines:
            if not line.startswith(prefix):
                continue
            kind, _body = _parse_result_line(line)
            if kind == 'ERR':
                return {'status': 'rejected', 'seq': expected_seq,
                        'evidence': f'recovered bridge reply: {line[:160]}'}
            return {'status': 'submitted', 'seq': expected_seq,
                    'evidence': f'recovered bridge reply, effect not verified: {line[:160]}'}
        if time.time() >= deadline:
            break
        time.sleep(_RECONCILE_POLL_S)
    return {'status': 'needs_reconciliation', 'seq': expected_seq,
            'reason': 'no reply for the recorded seq after the recorded offset: the command may '
                      'not have been read, or its reply was lost; not resending'}


def _recovered_result(previous: dict, request: str, verdict: dict) -> dict:
    return {'ok': False, 'accepted': verdict.get('status') == 'submitted',
            'status': verdict.get('status'), 'evidence': verdict.get('evidence'),
            'request_id': request, 'action_id': previous.get('action_id'),
            'seq': verdict.get('seq'), 'recovered': True,
            'detail': 'recovered from a pending reservation; no new command was sent'}


def _pending_decision(previous: dict, request: str) -> tuple[dict | None, dict | None]:
    """(resolved result, error) for an existing pending reservation; sends nothing."""
    age = _pending_age(previous)
    pid = previous.get('pid')
    same_process = pid is None or pid == os.getpid()
    if same_process and (age is None or age < _STALE_PENDING_S):
        return None, {'ok': False, 'error': 'request_pending', 'request_id': request,
                      'detail': 'a command with this request_id is still in flight'}
    verdict = _reconcile_pending(previous, request)
    if verdict.get('status') in ('submitted', 'rejected'):
        return _recovered_result(previous, request, verdict), None
    return None, {'ok': False, 'error': 'needs_reconciliation', 'request_id': request,
                  'verdict': verdict,
                  'detail': 'crash window: the previous command may have executed. No command '
                            'was resent. Inspect the game state, then use a NEW request_id.'}


def _request_epoch_problem(previous, request: str | None) -> dict | None:
    """C4: a request id belongs to one engine epoch/timeline; never reuse it across a load.

    Legacy entries written before epochs were recorded (no 'epoch' key) keep the old dedupe
    semantics so old evidence stays queryable; entries written from now on are epoch-bound.
    """
    if not isinstance(previous, dict):
        return None
    stored_epoch = previous.get('epoch')
    if stored_epoch is None:
        return None
    run = _run_identity()
    stored_run = previous.get('run_id')
    if stored_epoch == run.get('epoch') and (not stored_run or stored_run == run.get('run_id')):
        return None
    return {'ok': False, 'error': 'request_epoch_mismatch', 'request_id': request,
            'request_epoch': stored_epoch, 'current_epoch': run.get('epoch'),
            'request_run_id': stored_run, 'current_run_id': run.get('run_id'),
            'hint': 'this request_id belongs to an older epoch/timeline; its completed result '
                    'is not reused and the action is not replayed. Query it with '
                    'game_action_status(action_id) and send new work with a new request_id.'}


def _index_check(request: str | None, arg_key: str):
    """(deduplicated result, error dict, needs_reservation) for a request_id."""
    if not request:
        return None, None, False
    with _INDEX_LOCK, _file_lock('action_index'):
        previous = _load_action_index().get(request)
    if not previous:
        return None, None, True
    epoch_problem = _request_epoch_problem(previous, request)
    if epoch_problem is not None:
        return None, epoch_problem, False
    if not isinstance(previous, dict) or previous.get('key') != arg_key:
        return None, {'ok': False, 'error': 'request_id reused with different args',
                      'request_id': request}, False
    if previous.get('state') != 'pending':
        return dict(previous.get('result') or {}, deduplicated=True), None, False
    resolved, error = _pending_decision(previous, request)
    if resolved is not None:
        _index_resolve(request, arg_key, resolved)
    return resolved, error, False


def _index_reserve(request: str | None, arg_key: str,
                   meta: dict | None = None) -> dict | None:
    """Reserve the request id as pending BEFORE submitting; re-check under the lock."""
    if not request:
        return None
    with _INDEX_LOCK, _file_lock('action_index'):
        index = _load_action_index()
        previous = index.get(request)
        if isinstance(previous, dict):
            epoch_problem = _request_epoch_problem(previous, request)
            if epoch_problem is not None:
                return epoch_problem
            if previous.get('key') != arg_key:
                return {'ok': False, 'error': 'request_id reused with different args',
                        'request_id': request}
            if previous.get('state') != 'pending':
                return dict(previous.get('result') or {}, deduplicated=True)
    if isinstance(previous, dict):
        # Crash recovery may poll the bridge for a few seconds: keep it outside the lock.
        resolved, error = _pending_decision(previous, request)
        if resolved is not None:
            _index_resolve(request, arg_key, resolved)
            return resolved
        if error is not None:
            return error
    run = _run_identity()
    entry: dict = {'key': arg_key, 'state': 'pending', 'ts': time.time(),
                   'pid': os.getpid(), 'epoch': run.get('epoch'),
                   'run_id': run.get('run_id'), 'branch_id': run.get('branch_id')}
    if isinstance(meta, dict):
        entry.update(meta)
    with _INDEX_LOCK, _file_lock('action_index'):
        index = _load_action_index()
        current = index.get(request)
        if isinstance(current, dict) and current.get('state') != 'pending':
            return dict(current.get('result') or {}, deduplicated=True)
        index[request] = entry
        _atomic_write_json(_action_index_path(), index)
    return None


def _index_resolve(request: str | None, arg_key: str, result: dict) -> None:
    if not request:
        return
    with _INDEX_LOCK, _file_lock('action_index'):
        index = _load_action_index()
        entry = index.get(request) if isinstance(index.get(request), dict) else {}
        entry.update({'key': arg_key, 'state': 'done', 'result': result,
                      'ts': time.time(), 'pid': os.getpid()})
        index[request] = entry
        _atomic_write_json(_action_index_path(), index)


def _index_finish(request: str | None, arg_key: str, result: dict) -> None:
    _index_resolve(request, arg_key, result)


@mcp.tool()
def game_reconcile(request_id: str) -> dict:
    """Resolve a pending request from a crashed MCP process WITHOUT sending any command.

    Returns resolved=true with the recovered bridge evidence when a recorded seq has a reply;
    otherwise returns error=needs_reconciliation — the command may have executed, so it is
    never retried automatically and never reported as success or failure.
    """
    request = (request_id or '').strip()
    if not request:
        return {'ok': False, 'error': 'request_id required'}
    with _INDEX_LOCK, _file_lock('action_index'):
        entry = _load_action_index().get(request)
    if not isinstance(entry, dict):
        return {'ok': False, 'error': 'unknown_request_id', 'request_id': request}
    if entry.get('state') != 'pending':
        return {'ok': True, 'resolved': True, 'deduplicated': True,
                'result': entry.get('result'), 'request_id': request}
    age = _pending_age(entry)
    if (entry.get('pid') in (None, os.getpid())) and (age is None or age < _STALE_PENDING_S):
        return {'ok': False, 'error': 'request_pending', 'request_id': request,
                'detail': 'this process is still executing the request'}
    verdict = _reconcile_pending(entry, request)
    if verdict.get('status') in ('submitted', 'rejected'):
        result = _recovered_result(entry, request, verdict)
        _index_resolve(request, entry.get('key'), result)
        return {'ok': True, 'resolved': True, 'verdict': verdict, 'result': result,
                'request_id': request}
    return {'ok': False, 'resolved': False, 'error': 'needs_reconciliation',
            'verdict': verdict, 'request_id': request}


@mcp.tool()
def game_action_status(action_id: str) -> dict:
    """Look up one action by action_id (from game_act) in the append-only ledger."""
    target = (action_id or '').strip()
    if not target:
        return {'ok': False, 'error': 'action_id required'}
    try:
        lines = (_dir() / 'actions.jsonl').read_text(encoding='utf-8').splitlines()
    except OSError:
        return {'ok': False, 'error': 'no ledger'}
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get('action_id') == target:
            return {'ok': True, 'action': entry}
    return {'ok': False, 'error': 'action not found', 'action_id': target}


@mcp.tool()
def resolve_unresolved(action_id: str, observed: str) -> dict:
    """Close an UNKNOWN action after inspecting the game state (R2).

    A mutating action whose outcome could not be observed blocks every further action.  This
    tool does NOT turn unknown into success: it requires a fresh readable game state, records
    the caller's observation in the ledger and the overlay, and only then removes the block.
    Inspect the game first (game_observe / game_action_status) and describe what you actually
    saw, e.g. "hero still at [12,34]; day unchanged; army count 18".
    """
    unresolved = _load_unresolved()
    if unresolved is None:
        return {'ok': False, 'error': 'no_unresolved_action'}
    target = (action_id or '').strip()
    expected = str(unresolved.get('action_id') or '')
    if not target or target != expected:
        return {'ok': False, 'error': 'action_id_mismatch',
                'unresolved_action_id': expected,
                'unresolved_action': unresolved.get('action')}
    text = observed.strip() if isinstance(observed, str) else ''
    if (not text or len(text) > 300 or any(char in text for char in '\r\n')
            or any(char in text for char in _CONTROL_CHARS)):
        return {'ok': False,
                'error': 'observed must be a short single-line description of the inspected state'}
    try:
        _require_lease()
    except RuntimeError as exc:
        return {'ok': False, 'error': str(exc)}
    try:
        st = _agent().state(timeout=10)
    except Exception as exc:
        return {'ok': False, 'error': f'cannot observe the game state: {exc}'}
    run = _run_identity()
    previous = _load_unresolved()
    try:
        _unresolved_path().unlink()
    except OSError:
        pass
    _ledger({'action': 'resolve_unresolved', 'args': {'action_id': target},
             'observed': text, 'resolved_action': previous.get('action'),
             'resolved_evidence': previous.get('evidence'), 'status': 'unknown',
             'source': 'llm', 'run_id': run['run_id'], 'branch_id': run['branch_id'],
             'day': (st.get('date') or {}).get('day'), 'in_game': st.get('in_game')})
    _overlay('event', f'未决动作已收尾 {target}: {text}')
    return {'ok': True, 'resolved': True, 'action_id': target,
            'resolved_action': previous.get('action'), 'observed': text,
            'day': (st.get('date') or {}).get('day')}


_ACTION_RETRIES = 4
_ACTION_RETRY_SLEEP = 0.7
# Town/building data reaches state.json through a 2 s "heavy" refresh after the server has
# already applied the command and the client has applied the package: give build-like actions
# and multi-tile hero movement (the client animates tile by tile, so the final position can land
# seconds later) a longer verification window so a slow refresh is not reported as "rejected".
_SLOW_VERIFY_ACTIONS = {'build', 'recruit', 'recruit_hero', 'transfer', 'merge', 'end_turn',
                        'move', 'move_hero', 'cast_spell', 'battle_cast_spell'}
_SLOW_RETRIES = 10
_SLOW_RETRY_SLEEP = 0.6
# recruit_hero goes through the tavern UI: the hire packet can be lost while the town screen is
# still closing (live 2026-10-25: OK reply, no gold spent, no hero).  After a failed verification
# the action is retried once, but only when neither a payment nor a new hero was observed - a paid
# hire must never be duplicated.
_RECRUIT_HERO_RETRY_WAIT = 8.0
_RECRUIT_HERO_RETRY_SLEEP = 0.5


# ---------------------------------------------------------------- UI post-conditions (T40/C11)
# `open_hero` has no state.json diff to verify: its effect is a *client window*.  The bridge
# ui_tree dump is the player-visible widget tree, so it is a valid observation for the honest
# status contract.  The helper only reads: it sends ui_tree (bridge read-only op) and parses
# <state-dir>/ui_tree.json.  It never closes the window - the caller decides (the 1080p demo needs
# the hero screen to stay on screen; `close_window` pops it via the bridge safe list).
#
# T42/F1 (audit 2026-10-27): a stale dump must never be accepted as a fresh observation.  The
# old code ignored the ui_tree reply and returned whatever ui_tree.json happened to be on disk,
# so an UNKNOWN/REJECTED query (or a query that never refreshed the file) could still report
# `open_hero -> completed` from a CHeroWindow left in an older dump.  The observation is now
# bound to THIS query: reply status is checked first, and the file signature (mtime_ns/size/
# sha256) must change after the query before it is trusted.
_UI_TREE_REFRESH_WAIT = 2.0
_UI_TREE_REFRESH_SLEEP = 0.1


def _file_signature(path: Path):
    """(mtime_ns, size, sha256) for freshness binding, or None when the file is absent."""
    try:
        stat = path.stat()
    except OSError:
        return None
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        digest = ''
    return (stat.st_mtime_ns, stat.st_size, digest)


def _ui_tree_window_classes() -> tuple[list[str], str]:
    """Window root classes from a *fresh* bridge ui_tree dump, or ([], error text).

    T42/F1: the observation is only trusted when (a) the bridge query itself succeeded and
    (b) the ui_tree.json file was refreshed by that query.  Otherwise the caller keeps the
    action at `unknown` - an old file can never upgrade the result to `completed`.
    """
    path = _dir() / 'ui_tree.json'
    before = _file_signature(path)
    try:
        res = _agent().execute('ui_tree', verify=None, result_timeout=10)
    except Exception as exc:  # noqa: BLE001 - report, never crash the action
        return [], f'ui_tree failed: {type(exc).__name__}: {exc}'
    status = str(getattr(res, 'status', '') or 'UNKNOWN').upper()
    line = str(getattr(res, 'line', '') or '')[:200]
    if status in ('UNKNOWN', 'NOT_SENT'):
        return [], f'ui_tree outcome {status} (no fresh observation): {line or "no reply"}'
    if status == 'REJECTED':
        return [], f'ui_tree rejected by the bridge: {line or "no bridge line"}'
    after = _file_signature(path)
    deadline = time.time() + _UI_TREE_REFRESH_WAIT
    while after == before and time.time() < deadline:
        time.sleep(_UI_TREE_REFRESH_SLEEP)
        after = _file_signature(path)
    if after is None:
        return [], f'ui_tree file missing after a {status} query ({path.name})'
    if after == before:
        return [], ('ui_tree file was not refreshed by this query (stale observation); '
                    'refusing to accept it')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        return [], f'ui_tree unreadable: {exc}'
    classes: list[str] = []
    for window in data.get('windows') or []:
        root = (window or {}).get('root') or {}
        cls = str(root.get('cls') or '')
        if cls:
            classes.append(cls)
    return classes, ''


def _verify_open_hero() -> tuple[str, str]:
    """Verify CHeroWindow in the widget tree; leave it open (demo-friendly)."""
    classes, error = _ui_tree_window_classes()
    if error:
        return 'unknown', f'open_hero accepted but the window observation failed: {error}'
    hero_windows = [cls for cls in classes if 'CHeroWindow' in cls]
    if hero_windows:
        return ('completed',
                f'hero screen opened (window={hero_windows[0]}); window left open - close it '
                f'with game_act("close_window") before sending other actions')
    return ('rejected',
            f'bridge accepted but the ui_tree shows no CHeroWindow (windows={classes or "[]"})')


@mcp.tool()
@_serialized_mutation('game_act')
def game_act(action: str, args: dict | None = None, request_id: str | None = None,
             thought: str | None = None) -> dict:
    """Execute exactly ONE whitelisted action.

    Honest status contract (S0 / D-007) - a bridge OK reply only proves *submission*:
      completed - the post-condition was observed in the after-state; ok=True
      submitted - bridge accepted, effect not observable (yet); ok=False
      rejected  - bridge rejected it OR the after-state clearly lacks the effect; ok=False
      unknown   - no reply/timeout, missing observations or verifier error; ok=False
    `accepted` is the bridge reply flag and is deliberately separate from `ok`.
    A bridge REJECTED/ERR reply and a bridge UNKNOWN/timeout are terminal - the verifier can
    never upgrade them to completed.
    request_id is idempotent: an in-flight id returns error=request_pending, a finished id
    returns the stored result with deduplicated=true (different args are rejected).
    Invalid/typed args are rejected before any lease/state/bridge access (`sent: false`).

    D-014 gates (mutating calls must be serial):
      error='in_flight'    - another mutating MCP call is still running; wait, then re-observe.
                             Never send mutating actions concurrently (a live batch crashed the
                             client this way).
      error='query_pending' - a UI query is open; only `answer`/`close_window` are accepted.
                             Answer it, re-observe (query must be null), then send the action.

    thought: one short line of reasoning to put on the recording's "思考" panel *before* the
    action runs. Pass it on every non-trivial action - the panel only ever shows lines that were
    written to <state-dir>/overlay.jsonl, so reasoning that is not passed here is not recorded.

    recruit-specific: args are town/level/amount plus an optional `creature` (CreatureID; the
    default is that level's base creature). The bridge reports the creature, the requested and
    the actually bought amount (clamped to available/affordable) and dest=hero|town. A command
    without a fresh before-state is NOT sent; status is 'unknown' with sent=False.

    recruit_hero: hires a hero from a town's tavern (args town/index).  The bridge now pre-checks
    the server's own hire conditions and refuses deterministically with
    ERR recruit_hero_refused <reason> (visiting_hero / no_tavern / enemy_town / not_enough_gold /
    hero_cap_on_map / hero_cap_total) instead of the old misleading OK; the hire itself is still
    asynchronous, so verification is a state diff (new hero and/or gold spent).

    open_hero: opens the real hero screen for hero index `hero` (CHeroWindow).  The verifier reads
    the bridge ui_tree and requires a CHeroWindow; the window is left open on purpose so it can be
    photographed/recorded.  Close it with game_act("close_window") before sending other actions.

    battle-specific: battle_attack takes a stack id plus an optional `mode` (auto|melee|shot);
    it performs a walk-and-attack or a legal shot and NEVER silently becomes a move - use
    battle_approach(target) for an explicit move towards the target. battle_actions() lists the
    legal targets/attack-from hexes/shots before you choose.

    magic (T35):
      cast_spell(hero, spell[, x,y,z]) - adventure-map spell.  Omit x/y/z for no-target spells
        (View Air/Earth, ...); pass all three for a ranged target (Dimension Door, ...).  The
        bridge validates spellbook/mana/target with the engine and reports the problem name;
        success requires a visible mana drop (or a query it opened).
      battle_cast_spell(spell[, stack=<unitId> | x,y]) - battle spell for our fighting hero.
        battle_actions() lists each known battle spell with cost/target_kind/can_cast; unit
        targets use `stack`, hex targets x/y, no-target spells omit both.  Success requires a
        visible drop of hero_mana / rise of cast_spells / a battle effect.
      The bridge never exposes cheat spells: learning spells is a test-only env-gated channel.
    """
    args = args if isinstance(args, dict) else ({} if args is None else args)
    cmd = (action or '').strip() if isinstance(action, str) else str(action or '')
    if cmd not in ACTIONS:
        return {'ok': False, 'error': f'unsupported action {cmd!r}', 'allowed': sorted(ACTIONS)}
    validation_error = _validate_action(cmd, args)
    if validation_error:
        return {'ok': False, 'error': validation_error, 'action': cmd, 'sent': False,
                'expected_args': list(ACTION_SPECS[cmd][1])}
    thought_text = (thought or '').strip() if isinstance(thought, str) else ''
    if len(thought_text) > 500:
        thought_text = thought_text[:500]
    if thought_text:
        _overlay('thought', thought_text)
    bridge_cmd, arg_names = ACTION_SPECS[cmd]
    if request_id is not None and not isinstance(request_id, str):
        return {'ok': False, 'error': 'request_id must be a string', 'sent': False}
    request = (request_id or '').strip() or None
    if request and (len(request) > 128 or not _clean_text(request)):
        return {'ok': False, 'error': 'invalid request_id', 'sent': False}
    arg_key = json.dumps({'action': cmd, 'args': args}, sort_keys=True, ensure_ascii=False,
                         default=str)
    deduplicated, request_error, _ = _index_check(request, arg_key)
    if deduplicated is not None:
        return deduplicated
    if request_error is not None:
        return request_error
    try:
        _require_lease()
    except RuntimeError as exc:
        return {'ok': False, 'error': str(exc), 'sent': False}
    unresolved_error = _unresolved_error(request)
    if unresolved_error is not None:
        return unresolved_error
    action_id = uuid.uuid4().hex[:12]
    run = _run_identity()
    # Reservation metadata for crash recovery: the seq this command will take, the results.log
    # offset before submission, and the bridge generation. A later reconcile can then match
    # *this* reply only (never a stale one from an older session) or report uncertainty.
    recovery_meta: dict = {'action': cmd, 'args': args, 'action_id': action_id}
    try:
        early = _agent()
        identity = early.bridge_identity()
        recovery_meta.update({'expected_seq': early.peek_next_seq(),
                              'results_offset': len(early.results()),
                              'session_id': identity.get('session_id'),
                              'epoch_started_at': identity.get('epoch_started_at')})
    except Exception:
        pass
    reservation = _index_reserve(request, arg_key, recovery_meta)
    if reservation is not None:
        return reservation
    seq = None
    bridge_status = 'NOT_SENT'
    detail = 'not sent'
    line_text = ''
    accepted = False
    status = 'unknown'
    evidence = 'not sent'
    before: dict = {}
    st: dict = {}
    retry_seq = None
    try:
        agent = _agent()
        try:
            before = agent.state(timeout=10)
        except Exception as exc:
            before = {}
            evidence = f'no before-state observation ({type(exc).__name__})'
        if not before:
            # D-007 / W1: a command whose precondition cannot be observed must not be sent at
            # all.  Sending it could change the game while the caller cannot even verify it.
            result = {'ok': False, 'accepted': False, 'action_id': action_id,
                      'request_id': request, 'seq': None, 'bridge_status': 'NOT_SENT',
                      'status': 'unknown', 'evidence': evidence,
                      'detail': 'not sent: no fresh before-state observation',
                      'decisions': [], 'battle': None, 'sent': False}
            _ledger({'action': cmd, 'args': args, 'seq': None, 'bridge_status': 'NOT_SENT',
                     'status': 'unknown', 'accepted': False, 'evidence': evidence,
                     'action_id': action_id, 'request_id': request,
                     'run_id': run['run_id'], 'branch_id': run['branch_id'],
                     'detail': result['detail']})
            if request:
                _index_finish(request, arg_key, result)
            _overlay('action', f'{cmd} -> not_sent (no before-state)')
            return result
        # D-014 ①b: an open engine query owns the UI.  Sending a new mutating command now races
        # the dialog (the third-batch crash) and would be rejected/queued server-side anyway, so
        # refuse it before it reaches the bridge.  answer/close_window are the resolution paths.
        query = before.get('query')
        if query and cmd not in _QUERY_EXEMPT_ACTIONS:
            query_kind = (query.get('type') if isinstance(query, dict) else None) or 'unknown'
            blocked = (f'a UI query is open (type={query_kind}); resolve it first with '
                       f'game_act("answer", ...) or close_window, then re-observe before {cmd}')
            result = {'ok': False, 'accepted': False, 'action_id': action_id,
                      'request_id': request, 'seq': None, 'bridge_status': 'NOT_SENT',
                      'status': 'rejected', 'evidence': blocked, 'detail': blocked,
                      'error': 'query_pending', 'query': query, 'sent': False,
                      'decisions': ['query_opened'], 'battle': None}
            _ledger({'action': cmd, 'args': args, 'seq': None, 'bridge_status': 'NOT_SENT',
                     'status': 'rejected', 'accepted': False, 'evidence': blocked,
                     'action_id': action_id, 'request_id': request,
                     'run_id': run['run_id'], 'branch_id': run['branch_id'],
                     'error': 'query_pending', 'detail': blocked})
            if request:
                _index_finish(request, arg_key, result)
            _overlay('action', f'{cmd} -> rejected (query_pending)')
            return result
        # Preconditions: do not send a command whose visible precondition is unmet (it could
        # not produce the target effect and would only burn the verification window).
        precondition = _precondition_error(cmd, args, before) if before else None
        if precondition:
            result = {'ok': False, 'accepted': False, 'action_id': action_id,
                      'request_id': request, 'seq': None, 'bridge_status': 'NOT_SENT',
                      'status': 'rejected', 'evidence': precondition, 'detail': precondition,
                      'decisions': [], 'battle': None, 'sent': False}
            _ledger({'action': cmd, 'args': args, 'seq': None, 'bridge_status': 'NOT_SENT',
                     'status': 'rejected', 'accepted': False, 'evidence': precondition,
                     'action_id': action_id, 'request_id': request,
                     'run_id': run['run_id'], 'branch_id': run['branch_id'],
                     'detail': precondition})
            if request:
                _index_finish(request, arg_key, result)
            _overlay('action', f"{cmd} -> rejected (precondition)")
            return result
        line = _bridge_line(cmd, args)
        res = agent.execute(line, verify=None, result_timeout=15)
        seq = getattr(res, 'seq', None)
        bridge_status = str(getattr(res, 'status', '') or 'UNKNOWN')
        detail = str(getattr(res, 'detail', '') or getattr(res, 'line', '') or '')[:300]
        line_text = str(getattr(res, 'line', '') or '')
        accepted, bridge_unknown = _bridge_fields(res)
        if _bridge_rejected(res):
            status, evidence = 'rejected', f'bridge rejected: {line_text or bridge_status}'
        elif bridge_unknown:
            status, evidence = 'unknown', f'bridge outcome {bridge_status}: {detail or line_text}'
        else:
            try:
                st = agent.state(timeout=10)
            except Exception as exc:
                st = {}
                evidence = evidence or f'no after-state observation ({type(exc).__name__})'
            if not before:
                status = 'unknown'
                evidence = evidence or 'before-state observation missing'
            elif not st:
                status = 'unknown'
                evidence = evidence or 'after-state observation missing'
            else:
                status, evidence = _verify_effect(cmd, args, before, st)
                # The bridge refreshes town/objects JSON on a 2 s "heavy" tick, so a verdict
                # based on those views can be stale in *both* directions for a moment:
                #   submitted -> completed once the effect lands;
                #   rejected  -> completed/rejected once the view is fresh (observation-based
                #                rejection only; a bridge REJECTED/ERR stays terminal above).
                # Never retry a bridge rejection and never turn rejection into success without
                # an explicit fresh observation.
                # This branch only runs when the bridge did NOT reject/unknown the command
                # (those two handled above are terminal), so observation-based rejected may be
                # a stale 2 s heavy view and deserves the retry window.
                bridge_rejected = False
                slow = cmd in _SLOW_VERIFY_ACTIONS
                max_attempts = _SLOW_RETRIES if slow else _ACTION_RETRIES
                retry_sleep = _SLOW_RETRY_SLEEP if slow else _ACTION_RETRY_SLEEP
                attempts = 0
                observations = 1          # the after-state read above is one observation
                while status in ('submitted', 'rejected') and attempts < max_attempts:
                    attempts += 1
                    if retry_sleep:
                        time.sleep(retry_sleep)
                    try:
                        st = agent.state(timeout=10)
                    except Exception:
                        # The client can still be settling right after a start/turn change;
                        # a missing observation must not freeze an earlier "rejected" verdict.
                        time.sleep(0.3)
                        continue
                    observations += 1
                    next_status, next_evidence = _verify_effect(cmd, args, before, st)
                    if next_status == 'completed':
                        status, evidence = next_status, next_evidence
                        break
                    if next_status == 'unknown':
                        status, evidence = next_status, next_evidence
                        break
                    if next_status == 'rejected' and bridge_rejected:
                        break
                    evidence = next_evidence
                    status = next_status
                if status == 'rejected' and observations == 0:
                    status = 'unknown'
                    evidence = 'verification observation unavailable after the action'
                    st = {}
    except Exception as exc:
        status = 'unknown'
        evidence = f'{type(exc).__name__}: {exc}'
    if cmd == 'open_hero' and status == 'submitted':
        # T40/C11: the post-condition of open_hero is a client window, not a state.json diff.
        # Verify CHeroWindow through a read-only ui_tree dump; the window is deliberately left
        # open (the 1080p demo records it), so closing is the caller's job (`close_window`).
        # A bridge REJECTED/ERR reply is terminal and stays untouched above.
        status, evidence = _verify_open_hero()
    if cmd == 'recruit_hero' and status == 'submitted':
        # Tavern UI automation is timing sensitive: the hire packet may be lost while the town
        # screen is still closing.  Retry once, but only when neither payment nor a new hero was
        # observed (never duplicate a hire that may already have been paid).
        def _hero_names(snapshot: dict) -> set:
            return {str(hero.get('name')) for hero in (snapshot or {}).get('heroes') or []
                    if isinstance(hero, dict) and hero.get('name')}

        def _gold_of(snapshot: dict):
            value = ((snapshot or {}).get('resources') or {}).get('gold')
            return int(value) if isinstance(value, (int, float)) else None

        gold_before = _gold_of(before)
        gold_now = _gold_of(st)
        hired = bool(_hero_names(st) - _hero_names(before))
        paid = (gold_before is not None and gold_now is not None and gold_now < gold_before)
        if not hired and not paid:
            town_idx = args.get('town', 0)
            hero_idx = args.get('index', 0)
            try:
                retry = agent.execute(f'recruit_hero {town_idx} {hero_idx}', verify=None,
                                      result_timeout=15)
                retry_seq = getattr(retry, 'seq', None)
                deadline = time.time() + _RECRUIT_HERO_RETRY_WAIT
                st = {}
                while time.time() < deadline:
                    try:
                        st = agent.state(timeout=10)
                    except Exception:
                        st = {}
                    hired = bool(_hero_names(st) - _hero_names(before))
                    gold_now = _gold_of(st)
                    paid = (gold_before is not None and gold_now is not None
                            and gold_now < gold_before)
                    if hired or paid:
                        break
                    time.sleep(_RECRUIT_HERO_RETRY_SLEEP)
                if hired:
                    status = 'completed'
                    evidence = f'new hero observed after retry (seq={retry_seq})'
                elif paid:
                    status = 'submitted'
                    evidence = (f'gold spent after retry ({gold_before}->{gold_now}) but the new '
                                f'hero is not observable yet (seq={retry_seq})')
                else:
                    status = 'submitted'
                    evidence = ('recruit_hero accepted twice but neither a new hero nor a gold '
                                'change was observed (tavern UI may need a manual confirmation)')
            except Exception as exc:  # noqa: BLE001 - report, never crash the action
                evidence = f'{evidence} | retry failed: {type(exc).__name__}: {exc}'
    if cmd == 'confirm_deployment' and status != 'completed':
        battle_now = ((st or {}).get('battle') or {})
        if battle_now.get('active') and battle_now.get('active_stack') == -1:
            evidence = (f'{evidence} | deployment still waiting: use battle_start() '
                        f'(clicks the battle-window start button)')
    decisions: list[str] = []
    try:
        if st:
            decisions = list(dict.fromkeys(
                str(point.get('type')) for point in _decision_bundle(st) if point.get('type')))
    except Exception:
        decisions = []
    battle = (st.get('battle') or {}) if st else {}
    summary = None
    if battle.get('active'):
        stacks = [stack for stack in battle.get('stacks') or [] if isinstance(stack, dict)]
        mine = battle.get('my_side')

        def _stack_count(stack: dict) -> int:
            try:
                return int(stack.get('count') or 0)
            except (TypeError, ValueError):
                return 0

        summary = {'active': battle.get('active_stack'), 'my_side': mine,
                   'ours': sum(_stack_count(stack) for stack in stacks
                               if stack.get('side') == mine),
                   'enemies': [{'id': stack.get('id'), 'count': stack.get('count'),
                                'hex': stack.get('hex')}
                               for stack in stacks if stack.get('side') != mine]}
    if status == 'unknown' and (seq is not None or bridge_status not in ('NOT_SENT', '', None)):
        # The command may have reached the engine but its outcome is not observable.  Block
        # further mutations until the caller inspects the game and closes this explicitly.
        _set_unresolved({'action': cmd, 'args': args, 'action_id': action_id,
                         'request_id': request, 'seq': seq, 'status': status,
                         'evidence': evidence, 'bridge_status': bridge_status,
                         'ts': time.time(), 'run_id': run['run_id'],
                         'branch_id': run['branch_id']})
        _overlay('event', f'未决动作 {cmd}（unknown）：后续动作已阻塞，需 resolve_unresolved')
    result = {'ok': status == 'completed', 'accepted': bool(accepted),
              'action_id': action_id, 'request_id': request, 'seq': seq,
              'retry_seq': retry_seq,
              'bridge_status': bridge_status, 'status': status, 'evidence': evidence,
              'detail': detail, 'bridge_line': line_text[:300],
              'decisions': decisions, 'battle': summary}
    _ledger({'action': cmd, 'args': args, 'seq': seq, 'retry_seq': retry_seq,
             'bridge_status': bridge_status,
             'status': status, 'accepted': bool(accepted), 'evidence': evidence,
             'action_id': action_id, 'request_id': request, 'run_id': run['run_id'],
             'branch_id': run['branch_id'], 'detail': detail})
    if request:
        _index_finish(request, arg_key, result)
    _overlay('action', f"{cmd} {json.dumps(args, ensure_ascii=False)} -> {status}")
    for kind in decisions:
        if kind in ('battle_started', 'battle_ended', 'query_opened', 'game_over',
                    'level_up', 'town_screen_opened'):
            _overlay('event', kind)
    if battle.get('active') and summary:
        enemy = ', '.join(f"{item['count']}@{item['hex']}" for item in summary['enemies'])
        _overlay('result', f"战斗 seq={seq} 我方 {summary['ours']} vs {enemy}")
    return result


def _battle_summary(st: dict) -> dict | None:
    battle = (st.get('battle') or {}) if st else {}
    if not battle.get('active'):
        return None
    stacks = [stack for stack in battle.get('stacks') or [] if isinstance(stack, dict)]
    mine = battle.get('my_side')

    def _count(stack: dict) -> int:
        try:
            return int(stack.get('count') or 0)
        except (TypeError, ValueError):
            return 0

    return {'active': battle.get('active_stack'), 'my_side': mine,
            'ours': sum(_count(stack) for stack in stacks if stack.get('side') == mine),
            'enemies': [{'id': stack.get('id'), 'count': stack.get('count'),
                         'hex': stack.get('hex')}
                        for stack in stacks if stack.get('side') != mine]}


def _order_status(res) -> tuple[str, str]:
    """Map one bridge order reply to (completed/safe status, evidence)."""
    if _bridge_rejected(res):
        line = str(getattr(res, 'line', '') or '')
        if 'not_my_turn' in line:
            return 'rejected', 'not_my_turn'
        return 'rejected', f'bridge rejected: {line or getattr(res, "status", "")}'[:200]
    if str(getattr(res, 'status', '') or '').upper() in ('UNKNOWN', 'NOT_SENT'):
        return 'unknown', f'bridge outcome {getattr(res, "status", "UNKNOWN")}'
    return 'submitted', 'bridge accepted'


_BATTLE_ROUND_DEADLINE = 22.0


def _battle_round_record(round_id: str, target, orders: list, status: str, unknown: int,
                         interrupted, run: dict) -> None:
    """R7: one durable record per battle_round call plus a per-order ledger trail."""
    record = {'round_id': round_id, 'target': target, 'status': status,
              'unknown': unknown, 'orders': orders, 'interrupted': interrupted,
              'run_id': run.get('run_id'), 'branch_id': run.get('branch_id'),
              'epoch': run.get('epoch'), 'ts': time.strftime('%Y-%m-%d %H:%M:%S')}
    try:
        _atomic_write_json(_dir() / 'last_battle_round.json', record)
    except Exception:  # noqa: BLE001 - evidence must not break the battle loop
        pass
    _ledger({'action': 'battle_round', 'args': {'target': target}, 'round_id': round_id,
             'status': status, 'unknown': unknown, 'orders': orders,
             'interrupted': interrupted, 'evidence': f'{len(orders)} order(s), {unknown} unknown',
             'run_id': run.get('run_id'), 'branch_id': run.get('branch_id'),
             'epoch': run.get('epoch')})


@mcp.tool()
@_serialized_mutation('battle_round')
def battle_round(target: int | None = None, allow_auto_answer: bool = False,
                 allow_auto_enter: bool = False, allow_retarget: bool = False) -> dict:
    """Let the model lead exactly one battle round: it picks `target`, the tool issues that
    order for every friendly stack that still has to act this round.

    The LLM stays the decision maker (which enemy to focus, when to switch); the tool only
    removes the per-stack bookkeeping so a full battle fits into a sane number of calls.
    Safety defaults: it does NOT answer a query, does NOT auto-enter deployment and does NOT
    silently retarget; when such a decision point is reached the round stops with
    status='interrupted' and sends nothing further.  Pass the allow_* flags to opt in.
    ok is True only when at least one order was VERIFIED and no order ended up unknown.
    """
    if isinstance(target, bool) or (target is not None and not isinstance(target, int)):
        return {'ok': False, 'error': 'target must be an integer stack id', 'sent': False}
    try:
        _require_lease()
    except RuntimeError as exc:
        return {'ok': False, 'error': str(exc)}
    unresolved_error = _unresolved_error()
    if unresolved_error is not None:
        return {'ok': False, 'error': unresolved_error['error'], 'sent': False,
                'unresolved': unresolved_error.get('unresolved'),
                'hint': unresolved_error.get('hint')}
    agent = _agent()
    run = _run_identity()
    round_guard = uuid.uuid4().hex[:12]
    acted: set = set()
    orders: list = []
    completed = 0
    unknown = 0
    landed_count = 0
    need = None
    interrupted = None
    deadline = time.time() + _BATTLE_ROUND_DEADLINE
    while time.time() < deadline:
        try:
            st = agent.state(timeout=10)
        except Exception as exc:
            interrupted = {'reason': 'state_error',
                           'error': f'{type(exc).__name__}: {exc}'}
            break
        battle = st.get('battle') or {}
        if not battle.get('active'):
            break
        if st.get('query'):
            if not allow_auto_answer:
                interrupted = {'reason': 'query_opened', 'query': st.get('query')}
                break
            agent.execute('answer 0', verify=None, result_timeout=10)
            time.sleep(0.4)
            continue
        if battle.get('active_stack') == -1:
            if not allow_auto_enter:
                interrupted = {'reason': 'deployment'}
                break
            agent.execute('key return', verify=None, result_timeout=10)
            time.sleep(0.6)
            continue
        mine = battle.get('my_side')
        active_id = battle.get('active_stack')
        stacks = [stack for stack in battle.get('stacks') or [] if isinstance(stack, dict)]
        if need is None:
            need = len([stack for stack in stacks if stack.get('side') == mine])
        if landed_count >= (need or 0):
            break
        if active_id in acted:
            time.sleep(0.15)
            continue
        active = [stack for stack in stacks if stack.get('id') == active_id]
        if not active or active[0].get('side') != mine:
            time.sleep(0.2)
            continue
        enemies = [stack for stack in stacks if stack.get('side') != mine]
        if not enemies:
            break
        usable = [enemy.get('id') for enemy in enemies]
        if target in usable:
            chosen = target
        elif allow_retarget:
            chosen = max(enemies, key=lambda stack: int(stack.get('count') or 0)).get('id')
        else:
            interrupted = {'reason': 'retarget_required', 'target': target,
                           'enemy_ids': usable}
            break
        before_signature = _battle_signature(battle)
        try:
            result = agent.execute(f'battle_attack {chosen}', verify=None, result_timeout=10)
        except Exception as exc:
            interrupted = {'reason': 'bridge_error',
                           'error': f'{type(exc).__name__}: {exc}'}
            break
        order_status, order_evidence = _order_status(result)
        landed = order_status in ('submitted', 'completed')
        if not landed and allow_retarget:
            for enemy in sorted(enemies, key=lambda stack: -int(stack.get('count') or 0)):
                if enemy.get('id') == chosen:
                    continue
                try:
                    retry = agent.execute(f"battle_attack {enemy.get('id')}", verify=None,
                                          result_timeout=10)
                except Exception as exc:
                    interrupted = {'reason': 'bridge_error',
                                   'error': f'{type(exc).__name__}: {exc}'}
                    break
                retry_status, retry_evidence = _order_status(retry)
                if retry_status in ('submitted', 'completed'):
                    result, order_status, order_evidence = retry, retry_status, retry_evidence
                    chosen = enemy.get('id')
                    landed = True
                    break
            if interrupted is not None:
                break
        if landed:
            after_lost = False
            try:
                after_state = agent.state(timeout=10)
            except Exception:
                after_state = {}
                after_lost = True
            if not after_state:
                after_lost = True
            after_battle = (after_state.get('battle') or {}) if after_state else {}
            after_signature = _battle_signature(after_battle)
            if after_lost:
                # Reading the after-state failed: the order may or may not have landed.  Never
                # report a completion (or retry the order) that was not observed.
                order_status = 'unknown'
                order_evidence = 'after-state observation failed; battle outcome unknown'
            elif before_signature is not None and after_signature != before_signature:
                order_status = 'completed'
                order_evidence = 'battle state changed (active stack / stack count / hex)'
            else:
                order_status = 'submitted'
                order_evidence = 'bridge accepted; no observable battle change yet'
        rejected_interrupt = None
        if not landed:
            # The bridge now refuses an illegal attack instead of silently walking.  Do not spin
            # on the same stack: stop and let the caller pick an explicit action.
            rejected_interrupt = {
                'reason': ('attack_not_legal' if 'cannot_' in order_evidence
                           else 'order_rejected'),
                'stack': active_id, 'target': chosen, 'detail': order_evidence,
                'hint': ('call battle_actions() for legal targets/moves, then use battle_approach/'
                         'battle_move/battle_wait/battle_defend explicitly'),
            }
        orders.append({'stack': active_id, 'target': chosen, 'status': order_status,
                       'seq': getattr(result, 'seq', None),
                       'evidence': order_evidence,
                       'line': str(getattr(result, 'line', '') or '')[:120]})
        _ledger({'action': 'battle_attack',
                 'args': {'stack': active_id, 'target': chosen, 'round_id': round_guard},
                 'seq': getattr(result, 'seq', None), 'accepted': bool(landed),
                 'status': order_status, 'evidence': order_evidence,
                 'run_id': run['run_id'], 'branch_id': run['branch_id'],
                 'epoch': run.get('epoch')})
        acted.add(active_id)
        if order_status == 'completed':
            completed += 1
            landed_count += 1
        elif order_status == 'submitted':
            landed_count += 1
        elif order_status == 'unknown':
            unknown += 1
            # An unobserved order must not be retried blindly; stop the round and let the caller
            # re-observe the battle state before deciding anything else.
            interrupted = {'reason': 'order_unknown', 'stack': active_id,
                           'target': chosen, 'seq': getattr(result, 'seq', None),
                           'round_id': round_guard}
            _set_unresolved({'action': 'battle_round', 'args': {'target': target},
                             'action_id': round_guard, 'request_id': None,
                             'seq': getattr(result, 'seq', None), 'status': 'unknown',
                             'evidence': order_evidence, 'ts': time.time(),
                             'run_id': run['run_id'], 'branch_id': run['branch_id'],
                             'stack': active_id, 'order_target': chosen})
            _overlay('event', '未决战斗命令（unknown）：后续动作已阻塞，需 resolve_unresolved')
        _overlay('action', f'battle_attack {chosen} -> {order_status}')
        if interrupted is not None:
            break
        if rejected_interrupt is not None:
            interrupted = rejected_interrupt
            break
        time.sleep(0.2)
    try:
        st = agent.state(timeout=10)
        if st:
            _record_events(st)
    except Exception:
        st = {}
    result = {'ok': completed > 0 and unknown == 0, 'orders': orders,
              'accepted_orders': landed_count,
              'completed': completed, 'unknown': unknown,
              'battle': _battle_summary(st)}
    if interrupted is not None:
        result.update({'status': 'interrupted', 'reason': interrupted.get('reason'),
                       'interrupt': interrupted, 'ok': False})
    else:
        result['status'] = ('completed' if result['ok'] else
                            ('unknown' if unknown else
                             ('submitted' if landed_count else 'idle')))
    _battle_round_record(round_guard, target, orders, result.get('status'), unknown,
                         interrupted, run)
    return result


@mcp.tool()
def battle_actions() -> dict:
    """Report the currently legal battle options for the active stack (read-only).

    Returns the active stack (id/creature/count/hex/shots/counterattacks), legal move
    destinations, every enemy target with `can_melee` / `walk` / `attack_from` / `can_shoot`,
    and the visible turn order (current + next round).  Read-only: it never flushes a pending
    UI transaction.  Use it to choose between battle_attack (attack), battle_approach
    (explicit move towards), battle_move, battle_wait and battle_defend.

    T35 hero magic: `hero` gives our fighting hero's mana/mana_max/spellbook/can_cast/reason/
    casts_used and `spells` lists every known battle spell with engine-provided cost, can_cast
    and target_kind ('none' | 'unit' | 'hex'); cast one with battle_cast_spell(spell=...,
    stack=<unit> for target_kind 'unit', x/y for target_kind 'hex', omitted for 'none').
    """
    st, error = _state_or_error()
    if error:
        return {'ok': False, 'error': error}
    if not (st.get('battle') or {}).get('active'):
        return {'ok': False, 'error': 'no active battle'}
    agent = _agent()
    try:
        agent.execute('battle_actions', verify=None, result_timeout=10)
    except Exception as exc:
        return {'ok': False, 'error': f'battle_actions failed: {exc}'}
    path = _dir() / 'battle_actions.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        return {'ok': False, 'error': f'battle_actions.json unreadable: {exc}'}
    if not isinstance(data, dict):
        return {'ok': False, 'error': 'battle_actions.json has unexpected shape'}
    data['ok'] = True
    return data


@mcp.tool()
def path_preview(hero: int, x: int, y: int, z: int = 0) -> dict:
    """Read-only path preview for one map tile (bridge `probe`; no movement is sent).

    Returns reachable / turns / movement cost / path length and whether the engine path uses a
    teleport (subterranean gate etc.) or a boat embark.  The pathfinder only knows tiles the
    player has discovered, so this is a player-view preview.  This tool never sends a mutating
    command: it does not require the lease and never creates a pending action.
    """
    if not all(isinstance(value, int) and not isinstance(value, bool)
               for value in (hero, x, y, z)):
        return {'ok': False, 'error': 'hero/x/y/z must be integers'}
    if not 0 <= hero <= 15 or not 0 <= x <= 4095 or not 0 <= y <= 4095 or not 0 <= z <= 8:
        return {'ok': False, 'error': 'argument out of range (hero 0..15, x/y 0..4095, z 0..8)'}
    st, error = _state_or_error()
    if error:
        return {'ok': False, 'error': error}
    agent = _agent()
    try:
        res = agent.execute(f'probe {hero} {x} {y} {z}', verify=None, result_timeout=15)
    except Exception as exc:
        return {'ok': False, 'error': f'probe failed: {exc}'}
    line = str(getattr(res, 'line', '') or '')
    if _bridge_rejected(res):
        return {'ok': False, 'error': f'bridge rejected: {line[:200]}'}
    if str(getattr(res, 'status', '') or '').upper() in ('UNKNOWN', 'NOT_SENT'):
        return {'ok': False, 'error': f'probe outcome {getattr(res, "status", "UNKNOWN")}'}
    point = next((tok for tok in line.split() if tok.startswith(f'{x},{y},{z}=')), None)
    if point is None:
        return {'ok': False, 'error': 'probe reply does not contain the target tile',
                'bridge_line': line[:300]}
    reachable = point.split('=', 1)[1].startswith('1')
    fields = {key: int(value) for key, value in re.findall(r'(turns|cost|len|teleport|embark)=(-?\d+)', line)}
    return {'ok': True, 'hero': hero, 'target': [x, y, z], 'reachable': reachable,
            'turns': fields.get('turns') if reachable else None,
            'cost': fields.get('cost') if reachable else None,
            'path_len': fields.get('len') if reachable else None,
            'teleport': bool(fields.get('teleport', 0)),
            'embark': bool(fields.get('embark', 0)),
            'bridge_line': line[:300]}


@mcp.tool()
@_serialized_mutation('move_near')
def move_near(hero: int, x: int, y: int, z: int = 0, radius: int = 1) -> dict:
    """Move the hero to the target tile, trying nearby tiles when the exact one is unreachable.

    The pinned bridge sometimes answers `ERR no_path` for a perfectly reasonable adjacent tile
    (terrain/town footprint), so this convenience tool issues ONE bridge command per candidate
    (exact target first, then the surrounding ring) and stops at the first accepted move.
    It is an explicit composite: every attempt goes through the normal single-action path and
    is visible in the ledger; the budget is radius 0..2.
    """
    if not isinstance(radius, int) or not 0 <= radius <= 2:
        return {'ok': False, 'error': 'radius must be an integer in 0..2', 'sent': False}
    if not all(isinstance(value, int) for value in (hero, x, y, z)):
        return {'ok': False, 'error': 'hero/x/y/z must be integers', 'sent': False}
    unresolved_error = _unresolved_error()
    if unresolved_error is not None:
        return {'ok': False, 'status': 'blocked', 'error': unresolved_error['error'],
                'unresolved': unresolved_error.get('unresolved'),
                'hint': unresolved_error.get('hint'), 'attempts': []}
    candidates = [(x, y)]
    for ring in range(1, radius + 1):
        ring_offsets = []
        for dx in range(-ring, ring + 1):
            for dy in range(-ring, ring + 1):
                if max(abs(dx), abs(dy)) != ring:
                    continue
                ring_offsets.append((dx, dy))
        ring_offsets.sort(key=lambda d: (abs(d[0]) + abs(d[1]), abs(d[0]), d[1]))
        candidates.extend((x + dx, y + dy) for dx, dy in ring_offsets)
    attempts: list[dict] = []
    for cx, cy in candidates:
        result = game_act('move_hero', {'hero': hero, 'x': cx, 'y': cy, 'z': z})
        status = result.get('status')
        attempts.append({'target': [cx, cy], 'status': status,
                         'evidence': str(result.get('evidence'))[:120],
                         'action_id': result.get('action_id')})
        if status in ('completed', 'submitted'):
            return {'ok': True, 'status': status, 'target': [cx, cy],
                    'action_id': result.get('action_id'), 'attempts': attempts}
        if status == 'unknown':
            # An unobserved move must not fall through to the next candidate.
            return {'ok': False, 'status': 'unknown',
                    'error': 'move outcome unknown; no further candidates were tried',
                    'action_id': result.get('action_id'), 'attempts': attempts}
        if result.get('error') in ('request_pending', 'needs_reconciliation',
                                   'unresolved_prior_action'):
            return {'ok': False, 'status': 'blocked', 'error': result.get('error'),
                    'attempts': attempts}
    return {'ok': False, 'status': 'rejected', 'error': 'no reachable candidate tile',
            'attempts': attempts}


@mcp.tool()
@_serialized_mutation('explore_toward')
def explore_toward(hero: int, x: int, y: int, z: int = 0, max_steps: int = 8,
                   max_turns: int = 0, map_w: int | None = None,
                   map_h: int | None = None) -> dict:
    """Fallback shroud-border BFS explorer: walk the black shroud, step by step, toward a target.

    Why it exists: the client pathfinder (the very same call that draws the hover arrows) treats
    every tile that was never revealed as BLOCKED - `PathfinderUtil::evaluateAccessibility`
    returns BLOCKED when `fogOfWarMap[pos] == 0`.  Therefore `reachable=false` from probe /
    path_preview does NOT prove that a far target is unreachable: it can simply be behind the
    black shroud.  The only way to open the shroud is to put the hero next to it: each step lets
    the hero's sight (`getSightRadius`) reveal the ring around it, the border moves, repeat.

    Brute force by design - use it only when normal progress is stuck (PLAYER.md):
      * probe the WHOLE level in one read-only command (1296 tiles ~0.1 s) -> complete
        reachable set;
      * if the target became reachable: issue ONE normal move_hero and return status=direct;
      * else pick the UNVISITED border tile (reachable tile touching a non-reachable one)
        closest to (x,y,z), step on it, re-probe (this is the "step on the shroud edge" loop);
      * the visited border set survives end-of-turn, so a new day does not replay the tour.

    Every movement goes through the normal `move_hero` action (own ledger row, own request_id);
    probes are read-only.  A battle or an open query stops the search (status=battle / refused).

    Returns status: direct (target reachable, one move issued) / reached / battle / stuck
    (border exhausted: walls/void or another walkable component) / need_turn (movement spent;
    pass max_turns>0 to end turns automatically) / max_steps.

    map_w/map_h: optional scenario size (e.g. 36 for Key to Victory) - the explorer only ever
    probes tiles inside the map box, because the engine indexes the fog map without bounds
    checks.  Without them the box is inferred from the observed coordinates (smaller, still safe).
    """
    if not all(isinstance(value, int) and not isinstance(value, bool)
               for value in (hero, x, y, z)):
        return {'ok': False, 'error': 'hero/x/y/z must be integers'}
    if not 0 <= hero <= 15 or not 0 <= x <= 4095 or not 0 <= y <= 4095 or not 0 <= z <= 8:
        return {'ok': False, 'error': 'argument out of range (hero 0..15, x/y 0..4095, z 0..8)'}
    if not isinstance(max_steps, int) or isinstance(max_steps, bool) or not 1 <= max_steps <= 40:
        return {'ok': False, 'error': 'max_steps must be an integer in 1..40'}
    if not isinstance(max_turns, int) or isinstance(max_turns, bool) or not 0 <= max_turns <= 10:
        return {'ok': False, 'error': 'max_turns must be an integer in 0..10'}
    if (map_w is None) != (map_h is None):
        return {'ok': False, 'error': 'pass map_w and map_h together (or neither)'}
    if map_w is not None and (not isinstance(map_w, int) or not isinstance(map_h, int)
                              or map_w < 1 or map_h < 1):
        return {'ok': False, 'error': 'map_w/map_h must be positive integers'}
    unresolved_error = _unresolved_error()
    if unresolved_error is not None:
        return {'ok': False, 'status': 'blocked', 'error': unresolved_error['error'],
                'unresolved': unresolved_error.get('unresolved'),
                'hint': unresolved_error.get('hint')}
    state, error = _state_or_error()
    if error:
        return {'ok': False, 'error': error}
    if (state.get('battle') or {}).get('active'):
        return {'ok': False, 'status': 'battle', 'error': 'a battle is active'}
    if state.get('query'):
        return {'ok': False, 'status': 'query_pending',
                'error': 'a UI query is open; answer it before exploring'}
    import fog_path as fp
    try:
        bounds = (fp.bounds_from_map(map_w, map_h) if map_w is not None
                  else fp.observed_bounds(state))
    except ValueError as exc:
        return {'ok': False, 'error': f'bad map size: {exc}'}
    agent = _agent()

    def probe(tiles):
        result = agent.execute(fp.probe_command(hero, tiles), verify=None,
                               result_timeout=60)
        body = str(getattr(result, 'line', '') or '')
        if 'OK probe' not in body:
            raise RuntimeError(f'probe failed: {body[:200]}')
        return fp.parse_probe_reply(body)

    def move(tile):
        outcome = game_act('move_hero', {'hero': hero, 'x': int(tile[0]),
                                        'y': int(tile[1]), 'z': int(tile[2])},
                           request_id=f'explore-move-{uuid.uuid4().hex[:12]}',
                           thought=f'探索黑幕边缘：踩上 ({tile[0]},{tile[1]}) 打开视野')
        status = str(outcome.get('status') or '')
        return {'ok': status in ('completed', 'submitted'), 'status': status,
                'action_id': outcome.get('action_id'), 'error': outcome.get('error'),
                'evidence': outcome.get('evidence')}

    def observe():
        fresh, observe_error = _state_or_error()
        return fresh if (fresh and not observe_error) else {}

    def end_turn():
        outcome = game_act('end_turn', {},
                           request_id=f'explore-turn-{uuid.uuid4().hex[:12]}',
                           thought='探索黑幕边缘：移动力用尽，结束回合后继续沿黑幕走')
        status = str(outcome.get('status') or '')
        return {'ok': status in ('completed', 'submitted'), 'status': status,
                'action_id': outcome.get('action_id'), 'error': outcome.get('error')}

    explorer = fp.FogExplorer(probe=probe, move=move, observe=observe, hero=hero,
                              target=(x, y, z), bounds=bounds, max_steps=max_steps,
                              end_turn=end_turn if max_turns else None,
                              max_turns=max_turns)
    try:
        result = explorer.run(max_moves=max_steps, max_turns=max_turns)
    except Exception as exc:  # noqa: BLE001 - bridge/probe errors must be a typed result
        return {'ok': False, 'status': 'error', 'error': f'explore_toward failed: {exc}'}
    trace = result.get('trace') or []
    stats = result.get('stats') or {}
    moves = len([entry for entry in trace if entry.get('kind') in ('frontier', 'direct')])
    _ledger({'action': 'explore_toward',
             'status': 'completed' if result.get('ok') else 'submitted',
             'accepted': bool(result.get('ok')),
             'hero': hero, 'target': [x, y, z], 'result_status': result.get('status'),
             'moves': moves, 'turns': result.get('turns'), 'bounds': list(bounds),
             'evidence': (f"hero {result.get('hero_tile')} status={result.get('status')} "
                          f"region={stats.get('region_reachable')} "
                          f"revealed={stats.get('revealed_tiles')} moves={moves}")})
    return {'ok': bool(result.get('ok')), 'status': result.get('status'),
            'hero': hero, 'hero_tile': result.get('hero_tile'), 'target': [x, y, z],
            'map_bounds': list(bounds), 'moves': moves, 'turns': result.get('turns'),
            'stats': stats, 'trace': trace[-8:], 'query': result.get('query'),
            'hint': result.get('hint'), 'reason': result.get('reason')}


_BATTLE_ASSESS_DISCLAIMER = (
    '仅可见数量比例，不含英雄/法术/地形/兵种能力，不得作为接战许可或安全证明'
    '（only visible count ratio; no hero stats/spells/terrain/abilities - '
    'NOT a permission to engage and NOT a safety proof）')


def _battle_assessment(st: dict) -> dict:
    """Post-contact counts summary from player-visible battle state.

    This only works once a battle has started (there is no adventure-map pre-battle evaluation):
    it projects the stacks the battle screen shows.  It deliberately does NOT try to score hidden
    information; the risk label is a coarse count heuristic and must not be read as permission to
    engage.
    """
    battle = (st or {}).get('battle') or {}
    stacks = [stack for stack in battle.get('stacks') or [] if isinstance(stack, dict)]
    mine = battle.get('my_side')

    def _count(stack: dict) -> int:
        try:
            return int(stack.get('count') or 0)
        except (TypeError, ValueError):
            return 0

    def _entry(stack: dict) -> dict:
        return {'id': stack.get('id'), 'creature': stack.get('creature'),
                'count': stack.get('count'), 'hex': stack.get('hex')}

    ours = [stack for stack in stacks if stack.get('side') == mine]
    enemies = [stack for stack in stacks if stack.get('side') != mine]
    our_units = sum(_count(stack) for stack in ours)
    enemy_units = sum(_count(stack) for stack in enemies)
    ratio = round(enemy_units / our_units, 2) if our_units else None
    if ratio is None:
        risk = 'unknown'
    elif ratio <= 0.8:
        risk = 'advantage'
    elif ratio <= 1.1:
        risk = 'even'
    elif ratio <= 1.5:
        risk = 'risky'
    else:
        risk = 'bad'
    return {'ours': {'stacks': len(ours), 'units': our_units, 'detail': [_entry(s) for s in ours]},
            'enemies': {'stacks': len(enemies), 'units': enemy_units,
                        'detail': [_entry(s) for s in enemies]},
            'ratio_enemy_to_ours': ratio, 'risk': risk,
            'disclaimer': _BATTLE_ASSESS_DISCLAIMER,
            'note': ('post-contact visible counts only; hero stats, spells, terrain and creature '
                     'abilities are not scored - never a permission to engage')}


def _overlay_battle_assessment(assessment: dict) -> None:
    ours, enemies = assessment.get('ours') or {}, assessment.get('enemies') or {}
    # The old label was '战前评估'; keep the token recognizable for archive readers while the
    # explicit semantics is now post-contact counts (not a pre-battle assessment/permission).
    _overlay('event',
             f"入战后数量摘要（非战力评估，不再称战前评估）: 我方 "
             f"{ours.get('stacks')}队/{ours.get('units')}只 vs "
             f"敌方 {enemies.get('stacks')}队/{enemies.get('units')}只 -> "
             f"{assessment.get('risk')}（仅可见数量，不含英雄/法术/地形，不作为接战许可）")


@mcp.tool()
def battle_assess() -> dict:
    """Post-contact visible-stack counts summary (read-only; no lease needed).

    ONLY valid after a battle has started: state.json exposes stacks only while the battle
    screen is active.  This is NOT a pre-battle risk assessment.  The returned `risk` label is a
    coarse count ratio kept for compatibility; `disclaimer` states explicitly that it is not a
    permission to engage and not a safety proof.
    """
    st, error = _state_or_error()
    if error:
        return {'ok': False, 'error': error}
    battle = st.get('battle') or {}
    if not battle.get('active'):
        return {'ok': False,
                'error': 'battle_assess only works after a battle has started; it is not a '
                         'pre-battle risk assessment',
                'status': 'no_active_battle'}
    assessment = _battle_assessment(st)
    _overlay_battle_assessment(assessment)
    return {'ok': True, **assessment,
            'active_stack': battle.get('active_stack'),
            'deployment_waiting': battle.get('active_stack') == -1,
            'reading_hint': '这是入战后数量摘要，不是战前评估；数量占优也不构成接战许可。'
                            '英雄属性/法术/地形/远程单位仍需自行判断。'}


@mcp.tool()
@_serialized_mutation('battle_start')
def battle_start() -> dict:
    """Start a battle that is waiting in the deployment phase (clicks the on-screen button).

    The pinned client ignores synthetic 'key return' in this phase, so the deployment overlay's
    wide bottom button is clicked instead (position read from the bridge ui_tree, with the fixed
    client layout as a fallback).  Never used while the battle is already running.
    """
    st, error = _state_or_error()
    if error:
        return {'ok': False, 'error': error}
    try:
        _require_lease()
    except RuntimeError as exc:
        return {'ok': False, 'error': str(exc), 'sent': False}
    unresolved_error = _unresolved_error()
    if unresolved_error is not None:
        return {'ok': False, 'error': unresolved_error['error'], 'sent': False,
                'unresolved': unresolved_error.get('unresolved'),
                'hint': unresolved_error.get('hint')}
    battle = st.get('battle') or {}
    if not battle.get('active'):
        return {'ok': False, 'status': 'no_battle', 'error': 'no active battle'}
    assessment = _battle_assessment(st)
    _overlay_battle_assessment(assessment)
    if battle.get('active_stack') != -1:
        return {'ok': True, 'status': 'running',
                'active_stack': battle.get('active_stack'),
                'assessment': assessment,
                'evidence': 'battle already past the deployment phase'}
    agent = _agent()
    attempts: list[dict] = []
    try:
        agent.execute('ui_tree', verify=None, result_timeout=10)
    except Exception:
        pass
    button = _battle_start_button()
    positions = ([button] if button else []) + [(601, 683), (395, 683)]
    for pos in positions:
        res = agent.execute(f'click {pos[0]} {pos[1]}', verify=None, result_timeout=10)
        attempts.append({'pos': list(pos), 'line': str(getattr(res, 'line', ''))[:80]})
        time.sleep(0.4)
        try:
            st2 = agent.state(timeout=10)
        except Exception:
            st2 = {}
        battle2 = st2.get('battle') or {}
        if battle2.get('active') and battle2.get('active_stack') != -1:
            return {'ok': True, 'status': 'completed',
                    'active_stack': battle2.get('active_stack'),
                    'assessment': assessment,
                    'evidence': f'deployment started via click {pos}', 'attempts': attempts}
    for _ in range(2):                     # fallback for clients where Return works
        agent.execute('key return', verify=None, result_timeout=10)
        time.sleep(0.3)
    try:
        st3 = agent.state(timeout=10)
    except Exception:
        st3 = {}
    battle3 = st3.get('battle') or {}
    if battle3.get('active') and battle3.get('active_stack') != -1:
        return {'ok': True, 'status': 'completed',
                'active_stack': battle3.get('active_stack'),
                'assessment': assessment,
                'evidence': 'deployment started via key return', 'attempts': attempts}
    return {'ok': False, 'status': 'needs_reconciliation',
            'error': 'battle still in the deployment phase after click/key attempts',
            'attempts': attempts}


@mcp.tool()
def town_info(town: int = 0) -> dict:
    """Read-only town decision dump for building/build-order choices (town_info.json).

    Returns, for every building of the town type: id/name, `built`, engine `state` code+name
    (allowed / already_present / no_resources / prerequires / missing_base / cant_build_today /
    town_not_owned / ...), cost, cost_total, affordable, missing_resources, requirements_satisfied,
    the requirement tree (all/any/none of building ids), missing_requirement_candidates, mode and
    upgrade_of. Also returns the market: available modes and sell->buy `give`/`receive` quotes.
    Read-only: it never flushes a pending UI transaction.
    """
    if isinstance(town, bool) or not isinstance(town, int) or not 0 <= town <= 15:
        return {'ok': False, 'error': 'town must be an integer in 0..15', 'sent': False}
    st, error = _state_or_error()
    if error:
        return {'ok': False, 'error': error}
    agent = _agent()
    try:
        agent.execute(f'town_info {town}', verify=None, result_timeout=10)
    except Exception as exc:
        return {'ok': False, 'error': f'town_info failed: {exc}'}
    path = _dir() / 'town_info.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        return {'ok': False, 'error': f'town_info.json unreadable: {exc}'}
    if not isinstance(data, dict):
        return {'ok': False, 'error': 'town_info.json has unexpected shape'}
    data['ok'] = True
    return data


@mcp.tool()
def army_info(owner: str = 'hero', index: int = 0) -> dict:
    """Read-only army dump with upgrade options (army_info.json).

    For every slot of the hero/town army: slot, creature, count, `upgradable`, and the
    engine-provided upgrade list with per-unit costs (from CGameInfoCallback::fillUpgradeInfo).
    Read-only: it never flushes a pending UI transaction.
    """
    if owner not in ('hero', 'town'):
        return {'ok': False, 'error': "owner must be 'hero' or 'town'", 'sent': False}
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 15:
        return {'ok': False, 'error': 'index must be an integer in 0..15', 'sent': False}
    st, error = _state_or_error()
    if error:
        return {'ok': False, 'error': error}
    agent = _agent()
    try:
        agent.execute(f'army_info {owner} {index}', verify=None, result_timeout=10)
    except Exception as exc:
        return {'ok': False, 'error': f'army_info failed: {exc}'}
    path = _dir() / 'army_info.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        return {'ok': False, 'error': f'army_info.json unreadable: {exc}'}
    if not isinstance(data, dict):
        return {'ok': False, 'error': 'army_info.json has unexpected shape'}
    data['ok'] = True
    return data


@mcp.tool()
def ui_tree() -> dict:
    """Dump the client widget tree (windows + buttons) for client-side dialogs.

    Some interactions (shipyard, quests, battle dialogs) are client windows, not server
    queries, so they never appear in state['query'].  This reads the bridge ui_tree dump and
    returns each window with its buttons; combine with the `click` action to answer them.

    Read-only: it never flushes a pending recruitment BUY and never closes town/puzzle windows.
    """
    st, error = _state_or_error()
    if error:
        return {'ok': False, 'error': error}
    agent = _agent()
    try:
        agent.execute('ui_tree', verify=None, result_timeout=10)
    except Exception as exc:
        return {'ok': False, 'error': f'ui_tree failed: {exc}'}
    path = _dir() / 'ui_tree.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        return {'ok': False, 'error': f'ui_tree unreadable: {exc}'}

    def collect(node, buttons: list) -> None:
        cls = str(node.get('cls') or '')
        if cls.endswith('CButton'):
            buttons.append({'cls': cls, 'x': node.get('x'), 'y': node.get('y'),
                            'w': node.get('w'), 'h': node.get('h')})
        for child in node.get('children') or []:
            if isinstance(child, dict):
                collect(child, buttons)

    windows = []
    for window in data.get('windows') or []:
        root = window.get('root') or {}
        buttons: list = []
        collect(root, buttons)
        windows.append({'cls': root.get('cls'), 'x': root.get('x'), 'y': root.get('y'),
                        'w': root.get('w'), 'h': root.get('h'), 'buttons': buttons})
    return {'ok': True, 'windows': windows}


def _battle_start_button() -> tuple[int, int] | None:
    """Read the bridge ui_tree dump and pick the battle window's wide bottom button."""
    path = _dir() / 'ui_tree.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    best: tuple[int, int] | None = None

    def walk(node) -> None:
        nonlocal best
        if not isinstance(node, dict):
            return
        cls = str(node.get('cls') or '')
        x, y, w, h = (node.get(key) for key in ('x', 'y', 'w', 'h'))
        if (cls.endswith('CButton') and isinstance(x, int) and isinstance(y, int)
                and isinstance(w, int) and w >= 100 and y >= 600):
            height = h if isinstance(h, int) and h > 0 else 36
            centre = (x + w // 2, y + height // 2)
            if best is None or centre[0] > best[0]:
                best = centre
        for child in node.get('children') or []:
            walk(child)

    for window in data.get('windows') or []:
        root = window.get('root') or {}
        if 'Battle' in str(root.get('cls') or ''):
            walk(root)
    return best


@mcp.tool()
def game_screenshot(path: str | None = None) -> object:
    """Screenshot of the off-screen game window (image content when supported by the SDK).

    path: explicit output path (recommended for evidence: use var/runs/<run>/shots/...).
    Default: var/shots/current.png - deliberately an untracked path.  The old default
    var/shots/mcp_v2.png is a git-tracked file: a routine capture used to overwrite it and dirty
    the worktree (D-014 ④), so it must never be the implicit target again.
    """
    target = Path(path) if path else (ROOT / 'var' / 'shots' / 'current.png')
    saved = _agent().shot(target)
    if Image is not None:
        try:
            return Image(path=str(saved))
        except Exception:
            pass
    return {'ok': True, 'path': str(saved), 'bytes': saved.stat().st_size}


# ------------------------------------------------------------------ recording sidecars (§5.2 / T8)
# The recorder process (tools/vcmi_overlay_recorder.py) writes video.mp4 + meta.json + health.jsonl
# + health_summary.json.  The MCP side binds the logical evidence: <out>/identity.json at start and
# <out>/segment.json at stop, appending one line to <state>/recording_segments.jsonl per clip.
# _RECORDING_SEGMENTS_FILE is declared with the other evidence-chain constants near the top.
_HEALTH_SUMMARY_FILE = 'health_summary.json'


def _read_json_file(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_health_summary(out: Path) -> dict:
    """T8 health_summary.json, or an explicit unavailable record (never raises)."""
    path = Path(out) / _HEALTH_SUMMARY_FILE
    summary = _read_json_file(path)
    if summary is None:
        return {'available': False, 'path': str(path),
                'reason': 'health_summary.json missing or unreadable'}
    return {'available': True, 'path': str(path), **summary}


def _video_record(out: Path, meta: dict) -> dict:
    """video.mp4 identity for the segment: path/bytes/full sha256 + the recorder meta fields."""
    video_path = Path(str(meta.get('video') or (Path(out) / 'video.mp4')))
    exists = video_path.exists()
    signature = _file_signature(video_path) if exists else None
    record = {'path': str(video_path), 'exists': exists,
              'bytes': signature.get('size') if signature else None,
              'sha256': _file_sha256(video_path) if exists else None,
              'meta': {key: meta.get(key) for key in
                       ('ok', 'frames', 'fps_target', 'fps_actual', 'height', 'canvas', 'crf',
                        'elapsed', 'missed_slots', 'duplicated_frames', 'cache_removed')
                       if meta.get(key) is not None}}
    return record


def _actions_window(started_at, stopped_at) -> dict:
    """Ledger entries inside this recording window (count + first/last ts + action names)."""
    import datetime
    low = float(started_at) - 5.0 if isinstance(started_at, (int, float)) else None
    high = float(stopped_at) + 5.0 if isinstance(stopped_at, (int, float)) else None
    entries: list[dict] = []
    try:
        lines = (_dir() / 'actions.jsonl').read_text(
            encoding='utf-8', errors='replace').splitlines()
    except OSError:
        lines = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            entry = json.loads(text)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        try:
            stamp = datetime.datetime.strptime(str(entry.get('ts')), '%Y-%m-%dT%H:%M:%S')
            when = stamp.timestamp()
        except (TypeError, ValueError):
            continue
        if low is not None and when < low:
            continue
        if high is not None and when > high:
            continue
        entries.append(entry)
    return {'count': len(entries),
            'first_ts': entries[0].get('ts') if entries else None,
            'last_ts': entries[-1].get('ts') if entries else None,
            'actions': [entry.get('action') for entry in entries][:200],
            'window_start': low, 'window_stop': high}


def _latest_restore_ref() -> dict | None:
    """The newest restore.json receipt (N1) as the segment's restore reference, if any.

    ISSUE-01: a restore receipt written by a previous run must never be attached to the current
    run's clip.  The active file is reset by _rotate_run(), and this second guard also ignores a
    receipt whose committed run_id differs from the current run (legacy dirs / crash leftover).
    """
    data = _read_json_file(_dir() / 'restore.json')
    if not data:
        return None
    committed = data.get('committed') if isinstance(data.get('committed'), dict) else {}
    owner_run = committed.get('run_id') or data.get('run_id')
    current_run = _run_identity().get('run_id')
    if owner_run and current_run and str(owner_run) != str(current_run):
        return None
    return {key: data.get(key) for key in
            ('kind', 'slot', 'save_name', 'file', 'file_sha256', 'continuity', 'target_proof',
             'rollback', 'committed', 'at') if data.get(key) is not None}


def _last_recording_segment() -> dict | None:
    """Newest appended recording segment belonging to the ACTIVE run (ISSUE-01).

    A clip from another run must not become the parent of this run's first clip, so lines whose
    run_id differs from run.json are skipped even if an older state dir still mixes them.
    """
    path = _dir() / _RECORDING_SEGMENTS_FILE
    try:
        lines = [line for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    except OSError:
        return None
    current_run = _run_identity().get('run_id')
    for line in reversed(lines):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        owner = data.get('run_id')
        if owner and current_run and str(owner) != str(current_run):
            continue
        return data
    return None


def _recording_identity_extra(identity: dict) -> dict:
    """Client/build/contract facts for identity.json (read-only files, never a game command)."""
    extra: dict = {'protocol': None, 'exe_sha256': identity.get('client_sha256'),
                   'contract_built_from_commit': None,
                   'contract_exe_sha256_match': None, 'offscreen': None}
    state_raw = _read_json_file(_dir() / 'state.json') or {}
    extra['protocol'] = state_raw.get('protocol')
    session = _read_json_file(_dir() / 'session.json') or {}
    for note in session.get('notes') or []:
        if not isinstance(note, str):
            continue
        # window_rect=(l, t, r, b) contains spaces, so parse it as one expression first.
        rect = re.search(r'window_rect=\(([^)]*)\)', note)
        if rect and extra.get('window_rect') is None:
            extra['window_rect'] = rect.group(1).strip()
        for token in note.split():
            if '=' not in token:
                continue
            key, _sep, value = token.partition('=')
            key, value = key.strip(), value.strip()
            if key == 'contract_match':
                extra['contract_exe_sha256_match'] = value == 'True'
            elif key == 'pinned_at':
                extra['contract_built_from_commit'] = value
            elif key == 'offscreen':
                extra['offscreen'] = value
    if extra.get('offscreen') is None:
        try:
            extra['offscreen'] = bool(_agent().window_is_offscreen())
        except Exception:  # noqa: BLE001 - identity must never block a recording
            pass
    return extra


def finalize_recording_segment(out, identity: dict | None = None, started_at=None,
                               meta: dict | None = None, recovered: bool = False) -> dict:
    """Write <out>/segment.json and append <state>/recording_segments.jsonl (never raises).

    `out` is the recorder output directory.  Missing health/video/restore files are represented as
    null + reason; the segment still records the identity/run/branch/epoch and the parent chain.
    """
    import datetime
    out = Path(out)
    meta = meta if isinstance(meta, dict) else {}
    identity = dict(identity or {})
    stopped_at = time.time()
    run = _run_identity()
    health = _read_health_summary(out)
    parent = _last_recording_segment()
    segment = {
        'kind': 'recording_segment',
        'segment_id': identity.get('segment_id') or out.name,
        'identity': identity,
        'started_at': started_at,
        'stopped_at': stopped_at,
        'stopped_at_iso': datetime.datetime.now().isoformat(timespec='seconds'),
        'recovered': bool(recovered),
        'video': _video_record(out, meta),
        'health': health,
        'actions_window': _actions_window(started_at, stopped_at),
        'restore_ref': _latest_restore_ref(),
        'parent_segment': (parent or {}).get('segment_id'),
        'parent': ({key: parent.get(key) for key in
                    ('segment_id', 'run_id', 'branch_id', 'epoch')} if parent else None),
        'run_id': run.get('run_id'), 'branch_id': run.get('branch_id'),
        'epoch': run.get('epoch'),
        'identity_file': str(out / 'identity.json'),
    }
    try:
        _atomic_write_json(out / 'segment.json', segment)
    except OSError:
        pass
    try:
        with (_dir() / _RECORDING_SEGMENTS_FILE).open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(segment, ensure_ascii=False) + '\n')
    except OSError:
        pass
    return segment


def _recording_state_path() -> Path:
    return _dir() / 'recording.json'


def _recording_lock_path() -> Path:
    return _dir() / 'recording.lock'


def _evidence_identity() -> dict:
    """R9: identity stamped on evidence: logical run/branch/epoch, owned client and this MCP."""
    identity: dict = {'mcp_pid': os.getpid()}
    try:
        sess = json.loads((_dir() / 'session.json').read_text(encoding='utf-8'))
        if isinstance(sess, dict):
            identity['client_pid'] = sess.get('client_pid')
            identity['session_id'] = sess.get('session_id')
            identity['window_rect'] = sess.get('window_rect')
            identity['client_sha256'] = None
            for note in sess.get('notes') or []:
                if isinstance(note, str) and note.startswith('client_sha256='):
                    identity['client_sha256'] = note.split('=', 1)[1].split()[0]
    except (OSError, ValueError):
        pass
    run = _run_identity()
    identity.update({'run_id': run.get('run_id'), 'branch_id': run.get('branch_id'),
                     'epoch': run.get('epoch'), 'epoch_started_at': run.get('epoch_started_at')})
    return identity


def _acquire_recording_lock(identity: dict) -> dict:
    """R9: atomic singleton lock so two MCP processes can never start two recorders."""
    path = _recording_lock_path()
    for _ in range(3):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, json.dumps(identity, ensure_ascii=False).encode('utf-8'))
            finally:
                os.close(fd)
            return {'ok': True, 'path': str(path)}
        except FileExistsError:
            try:
                current = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                current = {}
            owner_pid = (current or {}).get('pid') or (current or {}).get('mcp_pid')
            if _pid_alive(owner_pid) and int(owner_pid) != os.getpid():
                return {'ok': False, 'error': 'recording_active', 'lock_owner': current}
            try:
                path.unlink()
            except OSError:
                pass
        except OSError as exc:
            return {'ok': False, 'error': f'recording_lock_failed: {exc}'}
    return {'ok': False, 'error': 'recording_lock_busy'}


def _release_recording_lock() -> None:
    path = _recording_lock_path()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if int((data or {}).get('mcp_pid') or 0) == os.getpid():
            path.unlink()
    except (OSError, ValueError, TypeError):
        pass


def _read_recording_state() -> dict:
    try:
        data = json.loads(_recording_state_path().read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _pid_alive(pid) -> bool:
    try:
        import psutil
        return bool(pid) and psutil.pid_exists(int(pid))
    except Exception:
        return False


@mcp.tool()
def recording_start(fps: int = 60, height: int = 768, crf: int = 10) -> dict:
    """Passive overlay recording: 16:9 canvas (game 4:3 + reasoning/action panel).

    Runs in a separate process, so reloading this MCP server never interrupts a recording.
    It only captures frames and never issues game commands.

    fps: target frame rate (default 60).  height: canvas height, 768 (1366x768) or
    1080 (1920x1080).  crf: x264 quality; 10 is visually near-lossless, 0 = lossless.
    Frames are streamed straight into the encoder - no intermediate files to clean up.
    """
    import subprocess
    # Singleton: the recorder writes frames into one encoder, so a second instance corrupts the
    # clip (seen in practice: two recorders -> 0-byte video).  The state file survives an MCP
    # restart, so a recorder started by a previous process is recognised too.
    proc = _recorder.get('proc')
    record = _read_recording_state()
    active_dir = None
    if proc is not None and proc.poll() is None:
        active_dir = _recorder.get('dir')
    elif record.get('dir') and _pid_alive(record.get('pid')):
        active_dir = record.get('dir')
    if active_dir is not None:
        return {'ok': False, 'error': 'recording_active', 'dir': str(active_dir),
                'hint': 'call recording_stop to finalize the existing clip first'}
    if record:
        try:
            _recording_state_path().unlink()   # stale record of a dead recorder
        except OSError:
            pass
    identity = _evidence_identity()
    lock = _acquire_recording_lock(identity)
    if not lock.get('ok'):
        return {'ok': False, 'error': lock.get('error'),
                'owner': lock.get('lock_owner')}
    stamp = time.strftime('%Y%m%d-%H%M%S')
    out = _recordings_root() / f'overlay-{stamp}'
    out.mkdir(parents=True, exist_ok=True)
    stop_file = out / 'STOP'
    if stop_file.exists():
        stop_file.unlink()
    overlay_path = _dir() / 'overlay.jsonl'
    if overlay_path.exists() and overlay_path.stat().st_size > 0:
        # each clip gets its own panel feed: stale lines from an earlier run must not show up
        try:
            overlay_path.replace(_dir() / 'overlay.jsonl.prev')
        except OSError:
            pass
    if not overlay_path.exists():
        overlay_path.write_text('', encoding='utf-8')
    _overlay('event', f'录制开始 {fps}fps/{height}p')
    log = (out / 'recorder.log').open('w', encoding='utf-8')
    cmd = [sys.executable, '-X', 'utf8', str(ROOT / 'tools' / 'vcmi_overlay_recorder.py'),
           '--dir', str(_dir()), '--out', str(out), '--fps', str(int(fps)),
           '--height', str(int(height)), '--crf', str(int(crf))]
    proc = subprocess.Popen(cmd, cwd=str(ROOT),
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                            stdout=log, stderr=subprocess.STDOUT)
    started_at = time.time()
    _recorder.update({'proc': proc, 'dir': out, 'started': started_at})
    canvas_w = round(int(height) * 16 / 9)
    if canvas_w % 2:
        canvas_w += 1
    canvas = f'{canvas_w}x{int(height)}'
    # N1/F §5.2: identity.json binds this clip to the logical run/segment and the engine identity;
    # the recorder process writes health.jsonl/health_summary.json into the same <out> dir.
    identity.update({'recording_started_at': started_at,
                     'segment_id': f"{identity.get('run_id')}/rec-{stamp}",
                     'canvas': canvas, 'fps': int(fps), 'height': int(height),
                     'crf': int(crf), 'recorder_pid': proc.pid,
                     'output_dir': str(out)})
    identity.update(_recording_identity_extra(identity))
    for key in ('client_pid', 'window_rect', 'exe_sha256', 'protocol',
                'contract_built_from_commit', 'contract_exe_sha256_match', 'offscreen'):
        identity.setdefault(key, None)
    _atomic_write_json(out / 'identity.json', identity)
    _atomic_write_json(_recording_state_path(),
                       {'pid': proc.pid, 'dir': str(out), 'started': started_at,
                        'fps': int(fps), 'height': int(height), 'crf': int(crf),
                        'identity': identity, 'run_id': identity.get('run_id'),
                        'branch_id': identity.get('branch_id'),
                        'epoch': identity.get('epoch'),
                        'segment_id': identity.get('segment_id'),
                        'client_pid': identity.get('client_pid'),
                        'mcp_pid': identity.get('mcp_pid'),
                        'identity_file': str(out / 'identity.json')})
    return {'ok': True, 'dir': str(out), 'pid': proc.pid, 'fps': int(fps),
            'canvas': canvas, 'crf': int(crf),
            'identity': identity, 'run_id': identity.get('run_id'),
            'branch_id': identity.get('branch_id'), 'epoch': identity.get('epoch'),
            'segment_id': identity.get('segment_id'),
            'identity_file': str(out / 'identity.json')}


@mcp.tool()
def recording_stop() -> dict:
    """Stop the overlay recorder, wait for the mp4 encode, and report the clip.

    Recovery: if this MCP process lost its in-memory handle (server restart) but a recorder
    state file exists, the recorder is stopped through its STOP file / process identity instead
    of failing, so a long recording is never orphaned.
    """
    proc = _recorder.get('proc')
    record = _read_recording_state()
    recovered = proc is None
    out: Path | None = None
    if proc is not None and _recorder.get('dir'):
        out = Path(_recorder['dir'])
    elif record.get('dir'):
        out = Path(record['dir'])
    if out is None:
        _release_recording_lock()
        return {'ok': False, 'error': 'not recording'}
    if not out.exists():
        _recorder.update({'proc': None, 'dir': None})
        try:
            _recording_state_path().unlink()
        except OSError:
            pass
        _release_recording_lock()
        return {'ok': False, 'error': 'recording_dir_missing', 'dir': str(out)}
    _overlay('event', '录制结束')
    (out / 'STOP').write_text('stop', encoding='utf-8')
    if proc is not None:
        try:
            proc.wait(timeout=300)
        except Exception:
            proc.kill()
    else:
        deadline = time.time() + 180
        while time.time() < deadline and not (out / 'meta.json').exists():
            time.sleep(0.5)
        if not (out / 'meta.json').exists() and _pid_alive(record.get('pid')):
            pid = int(record['pid'])
            # Never kill a process we cannot prove is our recorder (a stale/wrong pid must not
            # take down an unrelated process, and certainly not this MCP server itself).
            try:
                import psutil
                if pid != os.getpid():
                    process = psutil.Process(pid)
                    cmdline = ' '.join(process.cmdline() or [])
                    if 'vcmi_overlay_recorder' in cmdline:
                        for child in process.children(recursive=True):
                            child.kill()
                        process.kill()
            except Exception:
                pass
    meta = {}
    try:
        meta = json.loads((out / 'meta.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        pass
    started_at = record.get('started') or _recorder.get('started')
    _recorder.update({'proc': None, 'dir': None})
    try:
        _recording_state_path().unlink()
    except OSError:
        pass
    _release_recording_lock()
    # F §5.2: bind identity + video sha256 + health summary + actions window + restore/parent chain.
    segment = finalize_recording_segment(out, identity=record.get('identity') or {},
                                         started_at=started_at, meta=meta,
                                         recovered=recovered)
    return {'ok': bool(meta.get('ok')), 'frames': meta.get('frames'),
            'fps_target': meta.get('fps_target'), 'fps_actual': meta.get('fps_actual'),
            'canvas': meta.get('canvas'), 'bytes': meta.get('bytes'),
            'cache_removed': meta.get('cache_removed'), 'video': meta.get('video'),
            'dir': str(out), 'elapsed': meta.get('elapsed'), 'recovered': recovered,
            'identity': record.get('identity') or {}, 'run_id': record.get('run_id'),
            'branch_id': record.get('branch_id'), 'epoch': record.get('epoch'),
            'segment': segment, 'health': segment.get('health'),
            'segment_file': str(out / 'segment.json'),
            'recording_segments_file': str(_dir() / _RECORDING_SEGMENTS_FILE)}


@mcp.tool()
def recording_status() -> dict:
    """Report the recorder state (works after an MCP restart via <dir>/recording.json)."""
    proc = _recorder.get('proc')
    in_memory = proc is not None and proc.poll() is None
    record = _read_recording_state()
    alive = in_memory or _pid_alive(record.get('pid'))
    out = _recorder.get('dir') if in_memory else record.get('dir')
    current = _evidence_identity()
    bound = record.get('identity') or {}
    lock = {}
    try:
        lock = json.loads(_recording_lock_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        lock = {}
    last_segment = _last_recording_segment()
    return {'ok': True, 'recording': bool(alive), 'dir': str(out) if out else None,
            'pid': record.get('pid') if not in_memory else getattr(proc, 'pid', None),
            'started': record.get('started'), 'fps': record.get('fps'),
            'height': record.get('height'), 'state_file': str(_recording_state_path()),
            'run_id': bound.get('run_id'), 'branch_id': bound.get('branch_id'),
            'epoch': bound.get('epoch'), 'client_pid': bound.get('client_pid'),
            'mcp_pid': bound.get('mcp_pid'), 'identity': bound,
            'segment_id': bound.get('segment_id'),
            'identity_file': str(Path(out) / 'identity.json') if out else None,
            'has_identity_file': bool(out and (Path(out) / 'identity.json').exists()),
            'segment_file': str(Path(out) / 'segment.json') if out else None,
            'health_available': bool(out and (Path(out) / _HEALTH_SUMMARY_FILE).exists()),
            'health': (_read_health_summary(Path(out))
                       if out and (Path(out) / _HEALTH_SUMMARY_FILE).exists() else None),
            'last_segment': ({key: last_segment.get(key) for key in
                              ('segment_id', 'run_id', 'branch_id', 'epoch', 'stopped_at_iso',
                               'parent_segment', 'health')} if last_segment else None),
            'recording_segments_file': str(_dir() / _RECORDING_SEGMENTS_FILE),
            'matches_current_run': bool(bound) and bound.get('run_id') == current.get('run_id')
            and bound.get('branch_id') == current.get('branch_id'),
            'lock_owner': lock or None,
            'lock_alive': _pid_alive((lock or {}).get('mcp_pid')) if lock else False}


@mcp.tool()
def game_note(text: str, kind: str = 'thought') -> dict:
    """Add a line to the on-video panel: the model's reasoning ('thought') or a note.

    kind: thought | action | result | event.  Recorded runs use this to show *why* each
    operation was chosen next to the game frame.
    """
    kind = kind if kind in ('thought', 'action', 'result', 'event') else 'thought'
    _overlay(kind, text)
    _ledger({'action': 'game_note', 'args': {'kind': kind, 'text': text}, 'seq': None,
             'status': 'OK', 'detail': kind})
    return {'ok': True, 'kind': kind, 'recording': bool(_recorder['proc'])}


@mcp.tool()
@_serialized_mutation('debug_resources')
def debug_resources(amounts: dict) -> dict:
    """DEBUG ONLY: set resource amounts through the engine-side debug cheat hook.

    Requires the client to have been started by this agent (the launcher exports
    AUTO_HEROS3_CHEAT_FILE).  Every use is recorded in the action ledger with
    source=debug-cheat, and such runs must not be presented as fair-play evidence.
    """
    allowed = ('wood', 'mercury', 'ore', 'sulfur', 'crystal', 'gems', 'gold')
    target = {}
    for name, value in (amounts or {}).items():
        if name not in allowed:
            return {'ok': False, 'error': f'unknown resource {name!r}', 'allowed': allowed}
        target[name] = int(value)
    if not target:
        return {'ok': False, 'error': 'no resources requested'}
    try:
        _require_lease()
    except RuntimeError as exc:
        return {'ok': False, 'error': str(exc)}
    path = _dir() / 'cheat.txt'
    path.write_text('\n'.join(f'{k} {v}' for k, v in target.items()) + '\n', encoding='utf-8')
    deadline = time.time() + 5.0
    last = {}
    while time.time() < deadline:
        try:
            last = _agent().state(timeout=10).get('resources') or {}
        except Exception:
            last = {}
        if all(last.get(k) == v for k, v in target.items()):
            _ledger({'action': 'debug_resources', 'args': target, 'seq': None,
                     'status': 'COMPLETED', 'detail': 'applied via engine debug hook',
                     'source': 'debug-cheat'})
            return {'ok': True, 'debug': True, 'resources': last}
        time.sleep(0.2)
    return {'ok': False, 'error': 'debug hook not applied (is the client started by this agent?)',
            'last': last}


# ---------------------------------------------------------------- save / load verification
# save_game: the file must be observably NEW/CHANGED after the command, and saving must not move
# the game day.  load_game: the day must match the stored metadata and the observed state must
# change (or the slot was written moments ago, in which case no visible diff is expected).
_SAVE_HASH_BYTES = 64 * 1024
_SAVE_WAIT_SECONDS = 20.0
_SAVE_POLL_SECONDS = 0.2
# D-014 ③: the engine writes the save in place, so right after the bridge reply the file can be
# briefly 0 bytes (live WIN run: formal-d11 first save reported "changed (None -> 0 bytes)" and was
# accepted as completed; the re-save produced the real 186,055-byte file).  A completed save
# therefore requires size > 0 AND the signature (size/mtime_ns/head hash) to stay unchanged for
# this short window, so a half-written file can never be reported as completed.
_SAVE_STABLE_SECONDS = 0.6
_LOAD_WAIT_SECONDS = 40.0
_LOAD_POLL_SECONDS = 0.5
_LOAD_FRESH_SECONDS = 120.0


def _save_path(safe: str) -> Path:
    return SAVES_DIR / SAVE_SUBDIR / f'{safe}.vsgm1'


def _file_signature(path: Path) -> dict | None:
    """(exists, size, mtime_ns, sha256 of the first 64 KiB) or None for a missing file."""
    try:
        info = path.stat()
    except OSError:
        return None
    signature = {'exists': True, 'size': info.st_size, 'mtime_ns': info.st_mtime_ns}
    try:
        with open(path, 'rb') as handle:
            head = handle.read(_SAVE_HASH_BYTES)
        signature['sha256_head'] = hashlib.sha256(head).hexdigest()
        signature['hashed_bytes'] = len(head)
    except OSError:
        signature['sha256_head'] = None
        signature['hashed_bytes'] = 0
    return signature


def _signature_changed(before: dict | None, after: dict | None) -> bool:
    if not after:
        return False
    if before is None:
        return True
    return (before.get('size') != after.get('size')
            or before.get('sha256_head') != after.get('sha256_head')
            or before.get('mtime_ns') != after.get('mtime_ns'))


def _same_save_signature(first: dict | None, second: dict | None) -> bool:
    """Two observations of the same complete file (size + mtime + head hash all equal)."""
    if not first or not second:
        return False
    return (first.get('exists') == second.get('exists')
            and first.get('size') == second.get('size')
            and first.get('mtime_ns') == second.get('mtime_ns')
            and first.get('sha256_head') == second.get('sha256_head'))


def _await_save_file(path: Path, before: dict | None,
                     wait: float | None = None) -> dict:
    """Poll a save file until it is non-empty AND its signature stayed stable (D-014 ③).

    Returns {ready, signature, changed, waited_s, empty_observed, reason}.  `ready` is only True
    for a file with size > 0 whose size/mtime/head-hash were identical for _SAVE_STABLE_SECONDS;
    a 0-byte or still-growing file therefore stays `ready=False` and the caller must not report
    `completed` (it reports submitted/unknown + this reason instead).
    """
    timeout = _SAVE_WAIT_SECONDS if wait is None else float(wait)
    started = time.time()
    deadline = started + timeout
    stable_seconds = max(0.0, float(_SAVE_STABLE_SECONDS))
    poll = max(0.01, float(_SAVE_POLL_SECONDS))
    signature = _file_signature(path)
    previous: dict | None = None
    stable_since: float | None = None
    empty_observed = bool(signature and int(signature.get('size') or 0) <= 0)
    while True:
        now = time.time()
        if signature and int(signature.get('size') or 0) > 0:
            if _same_save_signature(previous, signature):
                if stable_since is None:
                    stable_since = now
                if now - stable_since >= stable_seconds:
                    return {'ready': True, 'signature': signature,
                            'changed': _signature_changed(before, signature),
                            'waited_s': round(now - started, 2),
                            'stable_s': round(now - stable_since, 2),
                            'empty_observed': empty_observed, 'reason': None}
            else:
                stable_since = now
        else:
            stable_since = None
        previous = signature
        if now >= deadline:
            break
        time.sleep(min(poll, max(0.0, deadline - now)))
        signature = _file_signature(path)
        if signature and int(signature.get('size') or 0) <= 0:
            empty_observed = True
    waited = round(time.time() - started, 2)
    if not signature or not signature.get('exists'):
        reason = f'save file missing after {waited}s'
    elif int(signature.get('size') or 0) <= 0:
        reason = (f'save file is still 0 bytes after {waited}s (the engine may still be writing); '
                  f're-observe before saving again')
    else:
        reason = (f'save file did not become stable within {waited}s '
                  f'(size/mtime/hash kept changing)')
    return {'ready': False, 'signature': signature,
            'changed': _signature_changed(before, signature),
            'waited_s': waited, 'stable_s': 0.0,
            'empty_observed': empty_observed, 'reason': reason}


def _start_config_summary() -> dict | None:
    """Map/config identity captured for the active run (None when no start_config.json)."""
    try:
        data = json.loads((_dir() / 'start_config.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {'requested': data.get('requested'), 'effective': data.get('effective'),
            'map': data.get('map'), 'config_match': data.get('config_match')}


def _saved_recently(saved_at, window: float | None = None) -> bool:
    if not saved_at:
        return False
    try:
        stamp = time.mktime(time.strptime(str(saved_at), '%Y-%m-%d %H:%M:%S'))
    except (TypeError, ValueError, OverflowError):
        return False
    limit = _LOAD_FRESH_SECONDS if window is None else window
    return abs(time.time() - stamp) <= limit


@mcp.tool()
@_serialized_mutation('save_game')
def save_game(name: str) -> dict:
    """Save the running game to Saves/agent/<name> and verify the save file really changed.

    ok=True only when the bridge accepted the command AND a non-empty save file was observed with
    a signature (size/mtime/head hash) that stayed stable for a short window AND the observed day
    is unchanged by saving.  Otherwise status is submitted/rejected/unknown with an explicit
    `reason` (a seq/bridge OK alone is never reported as completed).  A transient 0-byte file -
    the engine writes the save in place - can therefore never be accepted (D-014 ③).
    Metadata (day, heroes, sizes) is kept in <state-dir>/saves.json so load_game can verify that
    the restored state matches.
    """
    safe = _safe_save_name(name)
    if not safe:
        return {'ok': False, 'status': 'rejected', 'error': 'invalid save name'}
    try:
        _require_lease()
    except RuntimeError as exc:
        return {'ok': False, 'status': 'unknown', 'error': str(exc)}
    unresolved_error = _unresolved_error()
    if unresolved_error is not None:
        return {'ok': False, 'status': 'unknown', 'error': unresolved_error['error'],
                'unresolved': unresolved_error.get('unresolved'),
                'hint': unresolved_error.get('hint')}
    slot = f'Saves/{SAVE_SUBDIR}/{safe}'
    path = _save_path(safe)
    status = 'unknown'
    evidence = 'not sent'
    seq = None
    bridge_status = 'NOT_SENT'
    accepted = False
    day_after = None
    after_file: dict | None = None
    save_reason: str | None = None
    save_settle: dict | None = None
    st: dict = {}
    try:
        before_file = _file_signature(path)
        agent = _agent()
        try:
            before_state = agent.state(timeout=10)
        except Exception:
            before_state = {}
        res = agent.execute(f'save_game {slot}', verify=None, result_timeout=30)
        seq = getattr(res, 'seq', None)
        bridge_status = str(getattr(res, 'status', '') or 'UNKNOWN')
        accepted, bridge_unknown = _bridge_fields(res)
        day_before = (before_state.get('date') or {}).get('day')
        if _bridge_rejected(res):
            status, evidence = 'rejected', f'bridge rejected: {_bridge_evidence(res)}'
        elif bridge_unknown:
            status, evidence = 'unknown', f'bridge outcome {bridge_status}: {_bridge_evidence(res)}'
        else:
            # D-014 ③: do not accept a 0-byte / still-growing file.  Poll until non-empty AND
            # stable, then read the after-state.  A file that never settles stays submitted.
            save_settle = _await_save_file(path, before_file)
            after_file = save_settle.get('signature')
            try:
                st = agent.state(timeout=10)
            except Exception:
                st = {}
            day_after = (st.get('date') or {}).get('day')
            file_ok = bool(after_file and int(after_file.get('size') or 0) > 0)
            changed = bool(save_settle.get('changed'))
            day_ok = (day_before is not None and day_after is not None
                      and day_before == day_after)
            if save_settle.get('ready') and file_ok and changed and day_ok:
                status = 'completed'
                evidence = (f'save file verified non-empty and stable '
                            f'({(before_file or {}).get("size") if before_file else None} -> '
                            f'{after_file.get("size")} bytes, settled '
                            f'{save_settle.get("waited_s")}s), day {day_after} unchanged')
            else:
                status = 'submitted'
                reasons = []
                if not save_settle.get('ready'):
                    save_reason = str(save_settle.get('reason') or 'save file not verified')
                    reasons.append(save_reason)
                elif not file_ok:
                    save_reason = 'save file is 0 bytes; not a usable checkpoint'
                    reasons.append(save_reason)
                elif not changed:
                    save_reason = 'save file not changed'
                    reasons.append(save_reason)
                if not day_ok:
                    reasons.append(f'day mismatch before={day_before} after={day_after}')
                evidence = ('bridge accepted but '
                            + ', '.join(reasons or ['verification incomplete']))
    except Exception as exc:
        status, evidence = 'unknown', f'{type(exc).__name__}: {exc}'
        save_reason = save_reason or f'{type(exc).__name__}: {exc}'
    if status == 'completed' and after_file:
        run = _run_identity()
        watermark = _ledger_watermark()
        session = getattr(agent, 'session', None)
        meta = {'slot': slot, 'file': str(path), 'bytes': after_file.get('size'),
                'sha256': _file_sha256(path),
                'sha256_head': after_file.get('sha256_head'), 'day': day_after,
                'heroes': [hero.get('name') for hero in st.get('heroes') or []],
                'resources': st.get('resources'),
                'fingerprint': _state_fingerprint(st),
                # N1 checkpoint watermark: the load target is bound to the reviewed state, not
                # just to 'some file with the same name'.
                'actions_count': watermark.get('actions_count'),
                'last_action_id': watermark.get('last_action_id'),
                'current_revision': _current_revision(),
                'segment_id': run.get('current_segment'),
                'session_id': getattr(session, 'session_id', None),
                'start_config': _start_config_summary(),
                'run_id': run.get('run_id'), 'branch_id': run.get('branch_id'),
                'epoch': run.get('epoch'), 'epoch_started_at': run.get('epoch_started_at'),
                'saved_at': time.strftime('%Y-%m-%d %H:%M:%S')}
        saves = _load_saves_meta()
        saves[safe] = meta
        try:
            _saves_meta_path().write_text(json.dumps(saves, ensure_ascii=False, indent=2),
                                          encoding='utf-8')
        except OSError:
            pass
        _overlay('event', f'已存档 {safe}（第 {day_after} 天，run {run.get("run_id")}'
                          f'/{run.get("branch_id")} epoch {run.get("epoch")}）')
    run = _run_identity()
    _ledger({'action': 'save_game', 'args': {'name': safe}, 'seq': seq,
             'bridge_status': bridge_status, 'accepted': bool(accepted), 'status': status,
             'evidence': evidence, 'detail': slot, 'run_id': run.get('run_id'),
             'branch_id': run.get('branch_id'), 'epoch': run.get('epoch'),
             **({'reason': save_reason} if save_reason else {})})
    result = {'ok': status == 'completed', 'accepted': bool(accepted), 'status': status,
              'name': safe, 'slot': slot, 'file': str(path), 'day': day_after,
              'bytes': after_file.get('size') if after_file else None,
              'run_id': run.get('run_id'), 'branch_id': run.get('branch_id'),
              'epoch': run.get('epoch'),
              'evidence': evidence, 'seq': seq, 'bridge_status': bridge_status}
    if save_reason:
        result['reason'] = save_reason
    if save_settle is not None:
        result['file_verified'] = bool(save_settle.get('ready'))
        result['settle'] = {'waited_s': save_settle.get('waited_s'),
                            'stable_s': save_settle.get('stable_s'),
                            'empty_observed': save_settle.get('empty_observed'),
                            'changed': bool(save_settle.get('changed'))}
    return result


@mcp.tool()
@_serialized_mutation('load_game')
def load_game(name: str) -> dict:
    """Quick-load a saved slot into the running client and verify the restored state.

    ok=True only when the bridge accepted AND the observed day matches the stored metadata (when
    present) AND the before/after state fingerprint actually changed.  A same-day reload that
    produces no observable change cannot be proven and stays 'unknown' - it is never reported as
    completed just because the slot was saved recently (R5).
    A missing save file is rejected without sending anything to the bridge.
    Use session_start('Saves/agent/<name>') instead when the client is not running yet.
    """
    safe = _safe_save_name(name)
    if not safe:
        return {'ok': False, 'status': 'rejected', 'error': 'invalid save name'}
    meta = _load_saves_meta().get(safe) or {}
    slot = meta.get('slot') or f'Saves/{SAVE_SUBDIR}/{safe}'
    path = _save_path(safe)
    if not path.exists():
        return {'ok': False, 'accepted': False, 'status': 'rejected', 'name': safe,
                'slot': slot, 'file': str(path), 'error': 'save file not found'}
    try:
        _require_lease()
    except RuntimeError as exc:
        return {'ok': False, 'status': 'unknown', 'error': str(exc)}
    unresolved_error = _unresolved_error()
    if unresolved_error is not None:
        return {'ok': False, 'status': 'unknown', 'error': unresolved_error['error'],
                'unresolved': unresolved_error.get('unresolved'),
                'hint': unresolved_error.get('hint')}
    expected_day = meta.get('day')
    target_fp = meta.get('fingerprint') if isinstance(meta.get('fingerprint'), dict) else None
    actual_sha = _file_sha256(path)
    if meta.get('sha256') and actual_sha != meta.get('sha256'):
        return {'ok': False, 'accepted': False, 'status': 'rejected', 'name': safe,
                'slot': slot, 'file': str(path), 'sent': False,
                'file_sha256': actual_sha, 'expected_sha256': meta.get('sha256'),
                'error': 'save file sha256 changed since the checkpoint was recorded; load was '
                         'not sent and the run identity is unchanged'}
    stale_run = _run_identity()
    sent_at = time.time()
    status = 'unknown'
    evidence = 'not sent'
    seq = None
    bridge_status = 'NOT_SENT'
    accepted = False
    day = None
    st: dict = {}
    before_fp = None
    before_ts = None
    after_fp = None
    fresh_ok = False
    bumped: dict | None = None
    receipt: dict | None = None
    try:
        agent = _agent()
        try:
            before_state = agent.state(timeout=10)
            before_fp = _state_fingerprint(before_state)
            before_ts = _state_ts(before_state)
        except Exception:
            before_fp = None
        sent_at = time.time()
        res = agent.execute(f'load_game {slot}', verify=None, result_timeout=30)
        seq = getattr(res, 'seq', None)
        bridge_status = str(getattr(res, 'status', '') or 'UNKNOWN')
        accepted, bridge_unknown = _bridge_fields(res)
        if _bridge_rejected(res):
            status, evidence = 'rejected', f'bridge rejected: {_bridge_evidence(res)}'
        elif bridge_unknown:
            status, evidence = 'unknown', f'bridge outcome {bridge_status}: {_bridge_evidence(res)}'
        else:
            # R6 load barrier: a state written BEFORE the load command cannot be used as proof.
            deadline = time.time() + _LOAD_WAIT_SECONDS
            while time.time() < deadline:
                try:
                    st = agent.state(timeout=10)
                except Exception:
                    st = {}
                day = (st.get('date') or {}).get('day')
                ts = _state_ts(st)
                # Missing ts_wall (old state.json / offline fixtures) cannot prove the barrier,
                # but the target comparison below still applies; real bridge states have ts_wall.
                fresh_ok = ts is None or ts >= sent_at - 0.5
                if st.get('in_game') and (expected_day is None or day == expected_day) and fresh_ok:
                    break
                time.sleep(_LOAD_POLL_SECONDS)
            after_fp = _state_fingerprint(st)
            day_ok = expected_day is None or day == expected_day
            fingerprint_changed = (before_fp is not None and after_fp is not None
                                   and before_fp != after_fp)
            if target_fp is not None:
                # C2: the loaded state must EQUAL the checkpoint target, not merely differ from
                # whatever was on screen before.  A same-day unrelated change stays unknown.
                fingerprint_ok = after_fp == target_fp
                target_proof = 'fingerprint_match' if fingerprint_ok else 'fingerprint_mismatch'
            else:
                # Legacy meta (written before full target fingerprints existed): cannot prove the
                # target.  Keep the historical completion rule but label the weaker proof.
                fingerprint_ok = fingerprint_changed
                target_proof = 'legacy_change_only'
            fresh_save = _saved_recently(meta.get('saved_at'))
            if day_ok and fingerprint_ok and fresh_ok:
                status = 'completed'
                watermark = _ledger_watermark().get('actions_count')
                parent = {'parent_run_id': stale_run.get('run_id'),
                          'parent_branch_id': stale_run.get('branch_id'),
                          'parent_epoch': stale_run.get('epoch'),
                          'parent_watermark': watermark,
                          'parent_last_action_id': _ledger_watermark().get('last_action_id')}
                bumped = _bump_epoch('load', {'save': safe, 'day': day,
                                              'saved_run_id': meta.get('run_id'),
                                              'saved_branch_id': meta.get('branch_id'),
                                              'saved_epoch': meta.get('epoch'),
                                              'target_proof': target_proof},
                                     parent=parent)
                engine = _engine_identity(agent, st)
                try:
                    file_size = path.stat().st_size
                except OSError:
                    file_size = None
                receipt = _write_restore_receipt({
                    'kind': 'load_game', 'slot': slot, 'save_name': safe,
                    'file': str(path), 'file_bytes': file_size, 'file_sha256': actual_sha,
                    'sidecar_path': str(_saves_meta_path()),
                    'sidecar_sha256': _file_sha256(_saves_meta_path()),
                    'target': {'run_id': meta.get('run_id'),
                               'branch_id': meta.get('branch_id'),
                               'epoch': meta.get('epoch'),
                               'actions_count': meta.get('actions_count'),
                               'last_action_id': meta.get('last_action_id'),
                               'current_revision': meta.get('current_revision'),
                               'day': expected_day, 'fingerprint': target_fp},
                    'engine': engine,
                    'before_state': {'fingerprint': before_fp, 'ts_wall': before_ts},
                    'observed': {'day': day, 'fingerprint': after_fp,
                                 'in_game': st.get('in_game'), 'ts_wall': st.get('ts_wall')},
                    'committed': {'run_id': bumped.get('run_id'),
                                  'branch_id': bumped.get('branch_id'),
                                  'epoch': bumped.get('epoch')},
                    'parent': parent, 'target_proof': target_proof,
                    'identity_precision': ('full_sha256' if meta.get('sha256')
                                           else 'head_only' if meta.get('sha256_head')
                                           else 'unknown')})
                fresh_note = ('state snapshot newer than the load command'
                              if _state_ts(st) is not None
                              else 'state snapshot carries no timestamp (barrier not provable)')
                evidence = (f'day {day} matches; target proof {target_proof}; '
                            f'{fresh_note}; new epoch {bumped.get("epoch")} '
                            f'branch {bumped.get("branch_id")}')
            else:
                reasons = []
                if not day_ok:
                    reasons.append(f'day {day} != expected {expected_day}')
                if target_fp is not None and not fingerprint_ok:
                    different = sorted(key for key in set(target_fp) | set(after_fp or {})
                                       if target_fp.get(key) != (after_fp or {}).get(key))
                    reasons.append(f'post-load fingerprint != target fingerprint '
                                   f'(fields: {different})')
                elif target_fp is None and not fingerprint_ok:
                    reason = 'no observable state change after load'
                    if fresh_save:
                        reason += (' (the slot was saved recently, but that does not prove '
                                   'a load)')
                    reasons.append(reason)
                if not fresh_ok:
                    reasons.append('no state snapshot newer than the load command (barrier)')
                status = 'unknown'
                evidence = 'bridge accepted but ' + ', '.join(reasons)
    except Exception as exc:
        status, evidence = 'unknown', f'{type(exc).__name__}: {exc}'
    run = bumped or _run_identity()
    _ledger({'action': 'load_game', 'args': {'name': safe}, 'seq': seq,
             'bridge_status': bridge_status, 'accepted': bool(accepted), 'status': status,
             'evidence': evidence, 'detail': slot, 'run_id': run.get('run_id'),
             'branch_id': run.get('branch_id'), 'epoch': run.get('epoch')})
    _overlay('event', f'已读档 {safe}（第 {day} 天，新 epoch {run.get("epoch")}）'
             if status == 'completed' else f'读档 {safe} 结果 {status}')
    return {'ok': status == 'completed', 'accepted': bool(accepted), 'status': status,
            'name': safe, 'slot': slot, 'day': day, 'expected_day': expected_day,
            'heroes': [hero.get('name') for hero in st.get('heroes') or []],
            'run_id': run.get('run_id'), 'branch_id': run.get('branch_id'),
            'epoch': run.get('epoch'),
            'saved_run_id': meta.get('run_id'), 'saved_branch_id': meta.get('branch_id'),
            'saved_epoch': meta.get('epoch'), 'restore': receipt,
            'evidence': evidence, 'seq': seq, 'bridge_status': bridge_status}


@mcp.tool()
def list_saves() -> dict:
    """List save slots written by save_game (plus the raw files on disk)."""
    meta = _load_saves_meta()
    disk = sorted(str(p.relative_to(SAVES_DIR)) for p in SAVES_DIR.glob(f'{SAVE_SUBDIR}/*.vsgm1')) \
        if SAVES_DIR.exists() else []
    return {'ok': True, 'saves': meta, 'files': disk}


def selftest() -> int:
    print('tools:', sorted(ACTIONS | {'session_start', 'session_stop', 'game_status',
                                      'game_observe', 'game_events', 'game_act',
                                      'game_action_status', 'battle_round', 'battle_actions',
                                      'battle_assess', 'battle_start',
                                      'town_info', 'army_info', 'path_preview',
                                      'game_screenshot', 'recording_start', 'recording_stop',
                                      'debug_resources', 'save_game', 'load_game', 'list_saves',
                                      'lease_acquire', 'lease_release', 'memory_read',
                                      'memory_write', 'resolve_unresolved'}))
    print('lease:', lease_acquire())
    print('lease_release:', lease_release())
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    mcp.run()
