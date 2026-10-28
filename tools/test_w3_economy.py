# -*- coding: utf-8 -*-
"""Offline regression tests for the W3 economy batch (no game, no client, no network).

Covered:
  E1  new actions are whitelisted with the right bridge command lines
      (market_trade / split / upgrade / swap_stacks)
  E2  market_trade verifier: sell down + buy up -> completed; unchanged -> submitted
  E3  split verifier: source down exactly amount + new slot with that count + conserved total
  E4  upgrade verifier: slot creature changed to the requested id with count kept
  E5  town_info / army_info read-only tools return the bridge dumps and validate inputs
  E6  bridge source guards: building state+cost+requirement tree, market quotes, upgrade
      options, split with free-slot check, read-only registration
Run: python -X utf8 tools/test_w3_economy.py
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
from test_action_contract import Env, FakeAgent, bridge_ok, make_hero, state  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition, detail: str = '') -> None:
    RESULTS.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


def hero_with_army(entries: list) -> dict:
    return {'index': 0, 'name': 'Hero0', 'visitable': [1, 1], 'movement': 1000,
            'army': list(entries)}


def resources(**values) -> dict:
    base = {name: 0 for name in M._RESOURCE_NAMES}
    base.update(values)
    return base


# ------------------------------------------------------------------ E1: action surface
def test_action_surface() -> None:
    expected = {
        'market_trade': 'market_trade 0 0 6 0 100',
        'split': 'split hero 0 1 5',
        'upgrade': 'upgrade hero 0 1 14',
        'swap_stacks': 'swap_stacks hero 0 0 1',
    }
    args = {
        'market_trade': {'town': 0, 'trade_mode': 0, 'sell': 6, 'buy': 0, 'amount': 100},
        'split': {'owner': 'hero', 'index': 0, 'slot': 1, 'amount': 5},
        'upgrade': {'owner': 'hero', 'index': 0, 'slot': 1, 'creature': 14},
        'swap_stacks': {'owner': 'hero', 'index': 0, 'slot_a': 0, 'slot_b': 1},
    }
    for action, line in expected.items():
        with Env() as env:
            fake = env.agent(FakeAgent([state(), state()]))
            M.game_act(action, dict(args[action]))
            check(f'E1 {action} builds the expected bridge command',
                  fake.sent == [line], f'sent={fake.sent}')
    with Env() as env:
        fake = env.agent(FakeAgent([state()]))
        bad = M.game_act('split', {'owner': 'villain', 'index': 0, 'slot': 1, 'amount': 5})
        check('E1b invalid owner enum rejected before send',
              bad.get('ok') is False and 'must be one of' in str(bad.get('error'))
              and fake.sent == [], str(bad.get('error')))
    with Env() as env:
        fake = env.agent(FakeAgent([state()]))
        bad = M.game_act('market_trade', {'town': 0, 'trade_mode': 0, 'sell': 9,
                                          'buy': 0, 'amount': 10})
        check('E1c out-of-range resource index rejected',
              bad.get('ok') is False and 'range' in str(bad.get('error')) and fake.sent == [],
              str(bad.get('error')))


# ------------------------------------------------------------------ E2..E4: verifiers
def test_market_verifier() -> None:
    with Env() as env:
        before = state(gold=1000)
        before['resources'] = resources(gold=1000, wood=10)
        after = state()
        after['resources'] = resources(gold=900, wood=20)
        env.agent(FakeAgent([before, after]))
        res = M.game_act('market_trade', {'town': 0, 'trade_mode': 0, 'sell': 6,
                                          'buy': 0, 'amount': 100})
        check('E2a sell down + buy up -> completed',
              res.get('status') == 'completed' and '1000->900' in str(res.get('evidence')),
              str(res.get('evidence')))
    with Env() as env:
        before = state()
        before['resources'] = resources(gold=1000, wood=10)
        after = state()
        after['resources'] = resources(gold=1000, wood=10)
        env.agent(FakeAgent([before, after]))
        res = M.game_act('market_trade', {'town': 0, 'trade_mode': 0, 'sell': 6,
                                          'buy': 0, 'amount': 100})
        check('E2b unchanged resources -> submitted', res.get('status') == 'submitted',
              str(res.get('status')))


def test_split_verifier() -> None:
    with Env() as env:
        before = state(heroes=[hero_with_army([{'slot': 0, 'creature': 98, 'count': 18}])])
        after = state(heroes=[hero_with_army([{'slot': 0, 'creature': 98, 'count': 13},
                                              {'slot': 5, 'creature': 98, 'count': 5}])])
        env.agent(FakeAgent([before, after]))
        res = M.game_act('split', {'owner': 'hero', 'index': 0, 'slot': 0, 'amount': 5})
        check('E3a split moves exactly amount into a new slot',
              res.get('status') == 'completed' and '18->13' in str(res.get('evidence')),
              str(res.get('evidence')))
    with Env() as env:
        before = state(heroes=[hero_with_army([{'slot': 0, 'creature': 98, 'count': 18}])])
        after = state(heroes=[hero_with_army([{'slot': 0, 'creature': 98, 'count': 18}])])
        env.agent(FakeAgent([before, after]))
        res = M.game_act('split', {'owner': 'hero', 'index': 0, 'slot': 0, 'amount': 5})
        check('E3b unchanged army -> submitted', res.get('status') == 'submitted',
              str(res.get('status')))


def test_upgrade_verifier() -> None:
    with Env() as env:
        before = state(heroes=[hero_with_army([{'slot': 0, 'creature': 98, 'count': 18}])])
        after = state(heroes=[hero_with_army([{'slot': 0, 'creature': 99, 'count': 18}])])
        env.agent(FakeAgent([before, after]))
        res = M.game_act('upgrade', {'owner': 'hero', 'index': 0, 'slot': 0, 'creature': 99})
        check('E4a creature changed + count kept -> completed',
              res.get('status') == 'completed' and '98->99' in str(res.get('evidence')),
              str(res.get('evidence')))
    with Env() as env:
        before = state(heroes=[hero_with_army([{'slot': 0, 'creature': 98, 'count': 18}])])
        after = state(heroes=[hero_with_army([{'slot': 0, 'creature': 98, 'count': 18}])])
        env.agent(FakeAgent([before, after]))
        res = M.game_act('upgrade', {'owner': 'hero', 'index': 0, 'slot': 0, 'creature': 99})
        check('E4b unchanged stack -> submitted', res.get('status') == 'submitted',
              str(res.get('status')))


# ------------------------------------------------------------------ E5: read-only info tools
def test_info_tools() -> None:
    town_payload = {'index': 0, 'name': 'Town', 'buildings': [
        {'id': 8, 'name': 'Citadel', 'can_build': True, 'state': {'name': 'allowed'},
         'cost': {'gold': 2500, 'ore': 5}, 'missing_resources': {}, 'affordable': True}],
        'market': {'available': True, 'resource_quotes': []}}
    army_payload = {'owner': 'hero', 'index': 0, 'slots': [
        {'slot': 0, 'creature': 98, 'count': 18, 'upgradable': True,
         'upgrades': [{'creature': 99, 'cost': {'gold': 20}}]}]}
    with Env() as env:
        def on_execute(line: str) -> None:
            state_dir = env.root / 'state'
            state_dir.mkdir(parents=True, exist_ok=True)
            if line.startswith('town_info'):
                (state_dir / 'town_info.json').write_text(json.dumps(town_payload),
                                                          encoding='utf-8')
            else:
                (state_dir / 'army_info.json').write_text(json.dumps(army_payload),
                                                          encoding='utf-8')
        fake = env.agent(FakeAgent([state()], on_execute=on_execute))
        town = M.town_info(town=0)
        check('E5a town_info returns the bridge dump',
              town.get('ok') is True and town.get('buildings') == town_payload['buildings'],
              str(town)[:160])
        army = M.army_info(owner='hero', index=0)
        check('E5b army_info returns upgrade options',
              army.get('ok') is True and army.get('slots') == army_payload['slots'],
              str(army)[:160])
        check('E5c both info commands actually reached the bridge',
              fake.sent == ['town_info 0', 'army_info hero 0'], f'sent={fake.sent}')
    with Env() as env:
        env.agent(FakeAgent([state()]))
        bad = M.town_info(town=99)
        check('E5d town_info validates its argument', bad.get('ok') is False and '0..15' in
              str(bad.get('error')), str(bad.get('error')))
        bad = M.army_info(owner='dragon', index=0)
        check('E5e army_info validates the owner', bad.get('ok') is False,
              str(bad.get('error')))


# ------------------------------------------------------------------ E6: bridge source guards
def test_bridge_source_guards() -> None:
    text = (ROOT / 'platform' / 'vcmi-patch' / 'client' / 'AgentBridge.cpp').read_text(
        encoding='utf-8')
    check('E6a town_info exposes all building states/costs/missing resources',
          'void doTownInfo(' in text and 'cb->canBuildStructure(town, bid)' in text
          and 'town->getBuildingCost(bid)' in text and '\\"missing_resources\\":' in text
          and '\\"affordable\\":' in text, 'state/cost/missing')
    check('E6b requirement tree + unmet candidates are serialized',
          'RequirementTreeWriter' in text and 'RequirementUnmetWriter' in text
          and '\\"requirement_tree\\":' in text
          and '\\"missing_requirement_candidates\\":' in text, 'requirement tree')
    check('E6c market quotes use the engine IMarket::getOffer',
          'market->getOffer(sell, buy' in text and '\\"resource_quotes\\":' in text,
          'getOffer')
    check('E6d army_info uses fillUpgradeInfo for upgrade options',
          'cb->fillUpgradeInfo(armed, slot, info)' in text
          and 'getAvailableUpgrades()' in text, 'fillUpgradeInfo')
    check('E6e upgrade pre-validates upgrade target/cost/affordability',
          'ERR not_upgradable' in text and 'ERR bad_upgrade_creature' in text
          and 'ERR cannot_afford' in text and 'ERR upgrade_rejected' not in text,
          'upgrade validation (callback return is not an error signal)')
    check('E6f split uses getFreeSlot + splitStack with a strict fraction check',
          'armed->getFreeSlot()' in text and 'cb->splitStack(armed, armed'
          in text and 'ERR no_free_slot' in text and 'ERR split_failed' not in text,
          'split (CCallback::splitStack always returns 0, so its return is not an error signal)')
    check('E6j market availability comes from the trade mode, not the town IMarket pointer',
          'market->allowsTrade(EMarketMode::RESOURCE_RESOURCE)' in text
          and '"available\\":" + std::string(resourceTrade' in text,
          'market availability')
    check('E6g market_trade pre-validates market/kind/amount/resources',
          'cb->getMarket(towns[townIdx]->id)' in text and 'ERR no_market' in text
          and 'ERR not_enough_resource' in text and 'est_receive=' in text,
          'market validation')
    check('E6h build refusal carries the engine reason name',
          'ERR cannot_build state=' in text and 'buildingStateName(state)' in text,
          'building reason')
    check('E6i info/split commands are dispatched; info is read-only',
          'op == "town_info"' in text and 'op == "army_info"' in text
          and 'else if (op == "split") doSplit(in);' in text
          and '"town_info" || op == "army_info"' in text,
          'dispatch + read-only')


def main() -> int:
    test_action_surface()
    test_market_verifier()
    test_split_verifier()
    test_upgrade_verifier()
    test_info_tools()
    test_bridge_source_guards()
    failed = [item for item in RESULTS if not item[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
