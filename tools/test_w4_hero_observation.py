# -*- coding: utf-8 -*-
"""T33/P-B offline tests: hero-screen observation fields projected through the MCP layer.

No game, no client, no network: reuses the fake harness from test_action_contract (Env fixes
STATE_DIR/SAVES_DIR to a temp dir and replaces _agent()/_require_lease()).  The fixtures below
mirror the exact additive JSON that the rebuilt bridge writes (AgentBridge.cpp heroJson/battleJson
T33/P-B fields), so this suite covers the MCP projection contract, not the C++ side (which the
live smoke covers).

Covered:
  A  _hero_digest: mana_max / spellbook / luck / morale / specialty pass through; spells,
     skills and artifacts (worn + backpack) pass through with names; army keeps creature_name;
     per-list caps; old/partial fixtures keep their previous shape; malformed values are ignored
  B  game_brief hero view: scalars; full skills; spell names (capped) + spell_count; artifact
     names (worn+backpack, capped) + artifact_count; old fixtures unchanged
  C  battle projection: round / hero_mana / hero_mana_max / hero_spellbook / cast_spells and
     per-stack creature_name in the digest; battle_round + hero_mana in the brief only while a
     battle is active
  D  tool level: game_observe/game_brief return the new fields end-to-end; read-only (no bridge
     command is ever sent); the old aggregate keys are unchanged

Run: python -X utf8 tools/test_w4_hero_observation.py
Exit code 0 only when every check passes.
"""
from __future__ import annotations

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


def rich_hero(index: int = 0, hero_id: int = 7) -> dict:
    """Hero JSON in the exact shape the T33/P-B bridge writes (heroJson additions)."""
    return {
        'index': index, 'id': hero_id, 'name': 'Adela', 'level': 5, 'exp': 12345,
        'mana': 33, 'mana_max': 60, 'spellbook': True,
        'movement': 1800, 'pos': [3, 4, 0], 'visitable': [2, 4, 0],
        'primary': {'attack': 1, 'defence': 2, 'spellpower': 3, 'knowledge': 4},
        'army': [{'slot': 0, 'creature': 0, 'creature_name': 'Pikemen', 'count': 42},
                 {'slot': 1, 'creature': 2, 'creature_name': 'Archers', 'count': 10}],
        'spells': [{'id': 15, 'name': 'Haste'}, {'id': 27, 'name': 'Bless'}],
        'skills': [{'id': 6, 'level': 2, 'name': 'Leadership'},
                   {'id': 2, 'level': 3, 'name': 'Logistics'}],
        'artifacts': {
            # The bridge iterates std::map<ArtifactPosition, ...>, i.e. ascending slot order
            # (0 = head ... 17 = spellbook) - the fixture keeps that order on purpose.
            'worn': [{'slot': 0, 'id': 7, 'name': 'Helm of the Alabaster Unicorn',
                      'locked': False},
                     {'slot': 17, 'id': 0, 'name': 'Spellbook', 'locked': False}],
            'backpack': [{'slot': 19, 'id': 1, 'name': 'Spell Scroll'}],
        },
        'luck': 1, 'morale': 2, 'specialty': 'Archers',
    }


def rich_state() -> dict:
    st = T.state(day=3, heroes=[rich_hero()])
    st['battle'] = {'active': 4, 'my_side': 0, 'active_stack': 4, 'round': 2,
                    'hero_mana': 30, 'hero_mana_max': 60, 'hero_spellbook': True,
                    'cast_spells': 1,
                    'stacks': [{'id': 4, 'side': 0, 'creature': 0,
                                'creature_name': 'Pikemen', 'count': 42, 'hex': [1, 1]},
                               {'id': 9, 'side': 1, 'creature': 72,
                                'creature_name': 'Harpy Hags', 'count': 6, 'hex': [9, 9]}]}
    return st


# ------------------------------------------------------------------ A: hero digest
def test_hero_digest_fields() -> None:
    hero = M._hero_digest(rich_hero())
    check('A1 mana_max/spellbook/luck/morale/specialty are projected',
          hero.get('mana_max') == 60 and hero.get('spellbook') is True
          and hero.get('luck') == 1 and hero.get('morale') == 2
          and hero.get('specialty') == 'Archers', str(hero))
    check('A1b the pre-existing hero fields are unchanged',
          hero.get('i') == 0 and hero.get('id') == 7 and hero.get('level') == 5
          and hero.get('exp') == 12345 and hero.get('mana') == 33
          and hero.get('primary') == {'attack': 1, 'defence': 2, 'spellpower': 3,
                                      'knowledge': 4}, str(hero))
    check('A2 spells and skills pass through with readable names',
          hero.get('spells') == [{'id': 15, 'name': 'Haste'}, {'id': 27, 'name': 'Bless'}]
          and hero.get('skills') == [{'id': 6, 'level': 2, 'name': 'Leadership'},
                                     {'id': 2, 'level': 3, 'name': 'Logistics'}],
          f"spells={hero.get('spells')} skills={hero.get('skills')}")
    artifacts = hero.get('artifacts') or {}
    check('A3 artifacts keep worn slots (with locked flag) and backpack entries',
          [item.get('slot') for item in artifacts.get('worn') or []] == [0, 17]
          and all('locked' in item for item in artifacts.get('worn') or [])
          and [item.get('name') for item in artifacts.get('backpack') or []]
          == ['Spell Scroll'], str(artifacts))
    check('A4 army stacks keep the creature_name added by the bridge',
          [item.get('creature_name') for item in hero.get('army') or []]
          == ['Pikemen', 'Archers'], str(hero.get('army')))
    check('A4b spells are passed in engine order (not sorted)',
          [item['id'] for item in hero['spells']] == [15, 27], str(hero['spells']))


def test_hero_digest_caps() -> None:
    hero = rich_hero()
    hero['spells'] = [{'id': i, 'name': f'Spell{i}'} for i in range(50)]
    hero['skills'] = [{'id': i, 'level': 1, 'name': f'Skill{i}'} for i in range(12)]
    hero['artifacts'] = {'worn': [], 'backpack': [{'slot': 19 + i, 'id': i, 'name': f'Art{i}'}
                                                 for i in range(40)]}
    digest = M._hero_digest(hero)
    check('A5 spells are capped at 40 entries',
          len(digest.get('spells') or []) == M._HERO_SPELLS_MAX
          and digest['spells'][-1]['name'] == 'Spell39', str(len(digest.get('spells') or [])))
    check('A5b skills are capped at 8 entries',
          len(digest.get('skills') or []) == M._HERO_SKILLS_MAX,
          str(len(digest.get('skills') or [])))
    check('A5c backpack artifacts are capped at 30 entries',
          len((digest.get('artifacts') or {}).get('backpack') or []) == M._HERO_ARTIFACTS_MAX,
          str(len((digest.get('artifacts') or {}).get('backpack') or [])))


def test_hero_digest_compat() -> None:
    old = {'index': 1, 'name': 'OldHero', 'visitable': [2, 2], 'movement': 900,
           'army': [{'slot': 0, 'creature': 1, 'count': 3}]}
    digest = M._hero_digest(old)
    check('A6 an old/partial hero keeps its previous digest shape (no KeyError)',
          digest == {'i': 1, 'name': 'OldHero', 'pos': [2, 2], 'visitable': [2, 2],
                     'movement': 900,
                     'army': [{'slot': 0, 'creature': 1, 'count': 3}]}, str(digest))
    junk = M._hero_digest({'index': 0, 'name': 'J', 'movement': 1, 'army': [],
                           'spells': 'not-a-list', 'skills': {'a': 1},
                           'artifacts': ['not-a-dict'],
                           'luck': 0, 'morale': None, 'specialty': ''})
    check('A6b malformed hero lists are ignored instead of crashing',
          'spells' not in junk and 'skills' not in junk and 'artifacts' not in junk
          and junk.get('luck') == 0 and 'morale' not in junk, str(junk))


# ------------------------------------------------------------------ B: brief hero view
def test_brief_hero_view() -> None:
    with T.Env() as env:
        env.agent(T.FakeAgent([rich_state()]))
        brief = M.game_brief()
        hero = (brief.get('heroes') or [{}])[0]
        check('B1 brief heroes expose the new scalar facts',
              hero.get('mana_max') == 60 and hero.get('spellbook') is True
              and hero.get('luck') == 1 and hero.get('morale') == 2
              and hero.get('specialty') == 'Archers', str(hero))
        check('B2 brief keeps the full secondary-skill list',
              hero.get('skills') == [{'id': 6, 'level': 2, 'name': 'Leadership'},
                                     {'id': 2, 'level': 3, 'name': 'Logistics'}],
              str(hero.get('skills')))
        check('B3 brief keeps the spell list compactly (id+name) with the true count',
              [row.get('name') for row in (hero.get('spells') or [])] == ['Haste', 'Bless']
              and hero.get('spell_count') == 2, str(hero.get('spells')))
        check('B4 brief lists worn+backpack artifact names with the true count',
              hero.get('artifacts') == ['Helm of the Alabaster Unicorn', 'Spellbook',
                                        'Spell Scroll']
              and hero.get('artifact_count') == 3, str(hero.get('artifacts')))
        check('B5 the old compact hero keys are unchanged',
              hero.get('i') == 0 and hero.get('name') == 'Adela'
              and hero.get('army') == 52 and hero.get('stacks') == 2
              and hero.get('mov') == 1800 and hero.get('level') == 5,
              str({k: hero.get(k) for k in ('i', 'army', 'stacks', 'mov')}))

    many = rich_state()
    many['heroes'][0]['spells'] = [{'id': i, 'name': f'Spell{i}'} for i in range(20)]
    many['heroes'][0]['artifacts'] = {
        'worn': [{'slot': i, 'id': i, 'name': f'Worn{i}', 'locked': False} for i in range(10)],
        'backpack': [{'slot': 19 + i, 'id': i, 'name': f'Bag{i}'} for i in range(10)]}
    with T.Env() as env:
        env.agent(T.FakeAgent([many]))
        hero = (M.game_brief().get('heroes') or [{}])[0]
        check('B6 brief spell names are capped while spell_count stays complete',
              len(hero.get('spells') or []) == M._BRIEF_SPELLS_MAX
              and hero.get('spell_count') == 20, str(len(hero.get('spells') or [])))
        check('B6b brief artifact names are capped while artifact_count stays complete',
              len(hero.get('artifacts') or []) == M._BRIEF_ARTIFACTS_MAX
              and hero.get('artifact_count') == 20, str(len(hero.get('artifacts') or [])))

    with T.Env() as env:
        env.agent(T.FakeAgent([T.state(heroes=[T.make_hero(0, (1, 1))])]))
        hero = (M.game_brief().get('heroes') or [{}])[0]
        check('B7 an old/partial hero keeps the previous brief shape',
              'skills' not in hero and 'spells' not in hero and 'artifacts' not in hero
              and 'mana_max' not in hero and hero.get('i') == 0, str(hero))


# ------------------------------------------------------------------ C: battle projection
def test_battle_projection() -> None:
    digest = M._digest(rich_state())
    battle = digest.get('battle') or {}
    check('C1 battle digest carries round + our hero mana/spellbook/casts',
          battle.get('round') == 2 and battle.get('hero_mana') == 30
          and battle.get('hero_mana_max') == 60 and battle.get('hero_spellbook') is True
          and battle.get('cast_spells') == 1, str(battle))
    stacks = battle.get('stacks') or []
    check('C1b battle stacks carry creature_name next to the numeric id',
          [stack.get('creature_name') for stack in stacks] == ['Pikemen', 'Harpy Hags']
          and [stack.get('creature') for stack in stacks] == [0, 72], str(stacks))
    check('C1c the previous battle fields are unchanged',
          battle.get('active') == 4 and battle.get('my_side') == 0
          and stacks[0].get('count') == 42 and stacks[0].get('hex') == [1, 1], str(stacks))

    with T.Env() as env:
        env.agent(T.FakeAgent([rich_state()]))
        brief = M.game_brief()
        check('C2 brief exposes battle_round/hero_mana while a battle is active',
              brief.get('battle_round') == 2 and brief.get('hero_mana') == 30
              and brief.get('battle') == 4, str({k: brief.get(k) for k in
                                                ('battle', 'battle_round', 'hero_mana')}))
    with T.Env() as env:
        env.agent(T.FakeAgent([T.state()]))
        brief = M.game_brief()
        check('C2b no battle -> no battle detail keys (compact brief stays compact)',
              'battle_round' not in brief and 'hero_mana' not in brief
              and brief.get('battle') is False, str(brief.get('battle')))
    old_battle = {'active': 2, 'my_side': 0, 'active_stack': 2,
                  'stacks': [T.make_stack(2, 0, 5, (1, 1))]}
    digest = M._digest(T.state(battle=old_battle))
    battle = digest.get('battle') or {}
    check('C3 an old battle without the new fields keeps its previous shape',
          'round' not in battle and 'hero_mana' not in battle
          and 'creature_name' not in (battle.get('stacks') or [{}])[0], str(battle))


# ------------------------------------------------------------------ D: tool level
def test_tools_end_to_end() -> None:
    with T.Env() as env:
        fake = env.agent(T.FakeAgent([rich_state()]))
        observed = M.game_observe()
        hero = ((observed.get('state') or {}).get('heroes') or [{}])[0]
        check('D1 game_observe carries the full hero-screen observation',
              observed.get('ok') is True and 'Haste' in str(hero.get('spells'))
              and hero.get('mana_max') == 60
              and 'Spellbook' in str((hero.get('artifacts') or {}).get('worn')),
              str(hero)[:200])
        brief = M.game_brief()
        check('D2 game_brief carries the compact hero observation',
              brief.get('ok') is True
              and ((brief.get('heroes') or [{}])[0].get('skills') or [])[0].get('name')
              == 'Leadership', str(brief.get('heroes'))[:200])
        check('D3 observe/brief stay read-only (no bridge command was sent)',
              fake.sent == [], str(fake.sent))
        check('D4 aggregate keys are unchanged for old consumers',
              set(observed['state']) >= {'day', 'status', 'resources', 'query',
                                         'battle_active', 'heroes', 'towns', 'objects',
                                         'recent_info'}
              and {'ok', 'day', 'gold', 'heroes', 'towns', 'battle', 'decisions',
                   'players', 'enemies', 'objects', 'observed', 'identity', 'memory'}
              <= set(brief), str(sorted(set(brief))))


def main() -> int:
    test_hero_digest_fields()
    test_hero_digest_caps()
    test_hero_digest_compat()
    test_brief_hero_view()
    test_battle_projection()
    test_tools_end_to_end()
    failed = [item for item in RESULTS if not item[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
