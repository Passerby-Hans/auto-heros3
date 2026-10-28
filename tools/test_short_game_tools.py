# -*- coding: utf-8 -*-
"""Offline tests for the short-game pre-flight tools.

Covered:
  SG-1 recruit_hero post-verification: a new hero or a paid hire is evidence; nothing observed is
       NOT completion; a failed verification is retried exactly once and never after a payment.
  SG-2 battle_assess: read-only stack counts + coarse risk label + overlay line; no lease needed.
Run: python -X utf8 tools/test_short_game_tools.py
Exit code 0 only when every check passes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))
import mcp_server_v2 as M  # noqa: E402
from test_action_contract import Env, FakeAgent, state  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition, detail: str = '') -> None:
    RESULTS.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


def hero(name: str) -> dict:
    return {'index': 0, 'name': name, 'pos': [5, 5, 0], 'visitable': [5, 5, 0],
            'movement': 1000, 'army': [{'slot': 0, 'creature': 1, 'count': 1}]}


def hero_state(heroes, gold=10000) -> dict:
    st = state(heroes=heroes)
    st['resources'] = {'gold': gold, 'wood': 10, 'ore': 10}
    return st


# ------------------------------------------------------------------ SG-1 recruit_hero
def test_recruit_hero_verification() -> None:
    # immediate new hero -> completed
    with Env() as env:
        fake = env.agent(FakeAgent([hero_state([hero('A')]),
                                    hero_state([hero('A'), hero('B')], gold=7500)]))
        res = M.game_act('recruit_hero', {'town': 0, 'index': 0})
        check('SG-1a a new hero in the after-state is completed',
              res.get('status') == 'completed' and 'B' in str(res.get('evidence')),
              f"status={res.get('status')} evidence={res.get('evidence')}")
        check('SG-1b a confirmed hire is not retried', len(fake.sent) == 1, str(fake.sent))

    # paid but hero not observable yet -> submitted, never retried
    with Env() as env:
        fake = env.agent(FakeAgent([hero_state([hero('A')]),
                                    hero_state([hero('A')], gold=7500)]))
        res = M.game_act('recruit_hero', {'town': 0, 'index': 0})
        check('SG-1c a paid hire without an observable hero stays submitted',
              res.get('status') == 'submitted' and 'gold spent' in str(res.get('evidence')),
              f"status={res.get('status')} evidence={res.get('evidence')}")
        check('SG-1d a paid hire is never retried (no double hire)',
              len(fake.sent) == 1 and res.get('retry_seq') is None, str(fake.sent))

    # nothing observed -> retry once, then the retry succeeds
    with Env() as env:
        nochange = hero_state([hero('A')])
        fake = env.agent(FakeAgent([nochange, nochange, nochange,
                                    hero_state([hero('A'), hero('B')], gold=7500)]))
        res = M.game_act('recruit_hero', {'town': 0, 'index': 0})
        check('SG-1e a lost hire is retried once and completes when the hero appears',
              res.get('status') == 'completed' and res.get('retry_seq') is not None
              and len(fake.sent) == 2, f"status={res.get('status')} sent={fake.sent}")
        entries = (env.root / 'state' / 'actions.jsonl').read_text(encoding='utf-8').splitlines()
        last = json.loads(entries[-1])
        check('SG-1f the ledger records the retry seq',
              last.get('retry_seq') == res.get('retry_seq'), str(last.get('retry_seq')))

    # nothing observed even after the retry -> submitted with an honest message
    with Env() as env:
        nochange = hero_state([hero('A')])
        fake = env.agent(FakeAgent([nochange, nochange, nochange, nochange, nochange]))
        res = M.game_act('recruit_hero', {'town': 0, 'index': 0})
        check('SG-1g two accepted-but-unobserved hires stay submitted (not completed)',
              res.get('status') == 'submitted'
              and 'accepted twice' in str(res.get('evidence')), str(res.get('evidence'))[:160])
        check('SG-1h exactly one retry is attempted', len(fake.sent) == 2, str(fake.sent))


# ------------------------------------------------------------------ SG-2 battle_assess
def battle_state(ours: int, enemy_counts) -> dict:
    st = hero_state([hero('A')])
    stacks = [{'id': 0, 'side': 0, 'creature': 1, 'count': ours, 'hex': [1, 1]}]
    for index, count in enumerate(enemy_counts, start=1):
        stacks.append({'id': index, 'side': 1, 'creature': 115, 'count': count,
                       'hex': [10, index]})
    st['battle'] = {'active': True, 'my_side': 0, 'active_stack': 0, 'stacks': stacks}
    return st


def test_battle_assess() -> None:
    with Env() as env:
        env.agent(FakeAgent([battle_state(10, (2, 2, 1))]))
        res = M.battle_assess()
        check('SG-2a assessment reports per-side stacks/units',
              res.get('ok') is True and res['ours']['units'] == 10
              and res['enemies']['units'] == 5 and res['enemies']['stacks'] == 3,
              str({k: res.get(k) for k in ('risk', 'ratio_enemy_to_ours')}))
        check('SG-2b a clear numeric advantage is labelled advantage',
              res.get('risk') == 'advantage', str(res.get('risk')))
        overlay = (env.root / 'state' / 'overlay.jsonl').read_text(encoding='utf-8')
        check('SG-2c the assessment is written to the overlay panel',
              '战前评估' in overlay and 'advantage' in overlay, overlay.splitlines()[-1][:160])
    with Env() as env:
        env.agent(FakeAgent([battle_state(10, (10,))]))
        res = M.battle_assess()
        check('SG-2d even numbers are labelled even', res.get('risk') == 'even',
              str(res.get('risk')))
    with Env() as env:
        env.agent(FakeAgent([battle_state(10, (10, 4))]))
        check('SG-2e a 1.4x enemy count is risky', M.battle_assess().get('risk') == 'risky')
    with Env() as env:
        env.agent(FakeAgent([battle_state(10, (10, 10))]))
        check('SG-2f a 2x enemy count is bad', M.battle_assess().get('risk') == 'bad')
    with Env() as env:
        env.restore_real_lease()
        M._lease_token = None
        env.agent(FakeAgent([battle_state(10, (2, 2))]))
        check('SG-2g assessment is read-only (no lease needed)',
              M.battle_assess().get('ok') is True)


def transfer_state(hero_pos, hero_visit, town_pos, town_visit) -> dict:
    st = hero_state([{'index': 0, 'name': 'Hero', 'pos': list(hero_pos),
                      'visitable': list(hero_visit), 'movement': 1000,
                      'army': [{'slot': 0, 'creature': 0, 'count': 5}]}])
    st['towns'] = [{'index': 0, 'id': 543, 'name': 'Home', 'pos': list(town_pos),
                    'built': [], 'build_options': [], 'dwellings': [],
                    'garrison': [{'slot': 0, 'creature': 0, 'count': 10}],
                    'available_heroes': []}]
    st['objects'] = [{'id': 543, 'kind': 'town', 'type': 'town', 'name': 'Home',
                      'pos': list(town_pos), 'visitable': list(town_visit), 'owner': 0}]
    return st


def test_transfer_visiting_tiles() -> None:
    # hero pos != town pos, but the interaction tiles match -> the transfer is legal
    with Env() as env:
        before = transfer_state([33, 33, 0], [32, 33, 0], [34, 33, 0], [32, 33, 0])
        after = transfer_state([33, 33, 0], [32, 33, 0], [34, 33, 0], [32, 33, 0])
        after['towns'][0]['garrison'] = [{'slot': 0, 'creature': 0, 'count': 0}]
        after['heroes'][0]['army'] = [{'slot': 0, 'creature': 0, 'count': 5},
                                      {'slot': 1, 'creature': 0, 'count': 10}]
        fake = env.agent(FakeAgent([before, after]))
        res = M.game_act('transfer', {'src': 'town', 'src_index': 0, 'src_slot': 0,
                                      'dst': 'hero', 'dst_index': 0, 'dst_slot': 1})
        check('SG-4a visiting is compared on interaction tiles, not sprite anchors',
              len(fake.sent) == 1 and res.get('status') in ('completed', 'submitted'),
              f"sent={fake.sent} status={res.get('status')} evidence={res.get('evidence')}")
    # a genuinely distant hero stays blocked before sending anything
    with Env() as env:
        before = transfer_state([10, 10, 0], [9, 10, 0], [34, 33, 0], [32, 33, 0])
        fake = env.agent(FakeAgent([before, before]))
        res = M.game_act('transfer', {'src': 'town', 'src_index': 0, 'src_slot': 0,
                                      'dst': 'hero', 'dst_index': 0, 'dst_slot': 1})
        check('SG-4b a hero away from town is still rejected without sending',
              fake.sent == [] and 'not visiting town' in str(res.get('evidence')),
              f"sent={fake.sent} evidence={res.get('evidence')}")


def test_source_guards() -> None:
    source = (ROOT / 'tools' / 'mcp_server_v2.py').read_text(encoding='utf-8')
    check('SG-3a recruit_hero has a verifier and a slow verification window',
          "'recruit_hero'" in source and 'def _hero_names' in source
          and 'new hero observed' in source, 'verifier')
    check('SG-3b a failed hire is retried at most once and never after payment',
          'a hire may already be paid' in source and 'accepted twice' in source
          and '_RECRUIT_HERO_RETRY_WAIT' in source, 'retry policy')
    check('SG-3c battle_assess is a read-only tool and battle_start embeds the assessment',
          'def battle_assess(' in source and "'assessment': assessment" in source
          and '战前评估' in source, 'battle assessment')


def main() -> int:
    test_recruit_hero_verification()
    test_battle_assess()
    test_transfer_visiting_tiles()
    test_source_guards()
    failed = [item for item in RESULTS if not item[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
