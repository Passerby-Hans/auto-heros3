# -*- coding: utf-8 -*-
"""Player-visible observation projections (state digest + brief rows).

B0.3: moved verbatim out of ``src/auto_heros3/mcp/server.py``.  Every function here is
pure with respect to the server: it only reads the state/hero/town dict passed in and
carries no module state, file I/O or transport dependencies.
"""
from __future__ import annotations

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
