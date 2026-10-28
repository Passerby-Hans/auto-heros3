# -*- coding: utf-8 -*-
"""Offline regression tests for the W0/W1 trust batch (no game, no client, no network).

Covered:
  W0-1  an invalid --agent-start advanced option must reject the session before start()
  W0-2  valid advanced options are written and the contract summary is reported
  W0-3  pinned contract: protocol v2, EXE hash match, built commit sources still match HEAD
        (this is the post-rebuild gate; run it after updating tools/bridge_contract.json)
  N4    same as W0-1 from the MCP API side
  R3    no fresh before-state -> the action is NOT sent (no unverifiable side effect)
  R1    battle_round: unreadable after-state -> order unknown / round interrupted, no retry
  N3    recruit explicit creature: optional 4th token, creature-level verifier, bad input
  N1/N2 source guards: the delayed BUY keeps the requesting command's seq, and read-only
        diagnostics (ui_tree/probe/ping) cannot flush it or close windows
Run: python -X utf8 tools/test_w0_w1_fixes.py
Exit code 0 only when every check passes.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))
import mcp_server_v2 as M  # noqa: E402
import vcmi_agent  # noqa: E402
from test_action_contract import Env, FakeAgent, bridge_ok, make_hero, state  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition, detail: str = '') -> None:
    RESULTS.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


# ------------------------------------------------------------------ W0: session start gate
class FakeSessionAgent:
    """Stand-in for VcmiAgent used by session_start(); never starts a real process."""

    started = 0

    def __init__(self, dirpath, map_name=None, log=False):
        self.dir = Path(dirpath)
        self.map_name = map_name
        self.contract = {'match': True, 'protocol_ok': True, 'built_from_commit': 'c0ffee',
                         'repo_head': 'c0ffee', 'source_matches_head': True}
        self.session = types.SimpleNamespace(session_id='fake-session', client_pid=424242)

    def start(self, timeout: float = 120.0) -> bool:
        type(self).started += 1
        return True

    def wait_in_game(self, timeout: float = 240.0) -> dict:
        return {'date': {'day': 1}}

    def window_is_offscreen(self) -> bool:
        return True


def with_fake_session_agent():
    original = M.VcmiAgent
    FakeSessionAgent.started = 0
    M.VcmiAgent = FakeSessionAgent
    return original


def test_w0_invalid_start_option() -> None:
    with Env() as env:
        original = with_fake_session_agent()
        try:
            res = M.session_start('Maps/Key to Victory', difficulty='not-a-difficulty')
        finally:
            M.VcmiAgent = original
        option_file = env.root / 'state' / 'start_options.txt'
        check('W0-1 invalid start option rejected before start',
              res.get('ok') is False and res.get('sent') is False
              and FakeSessionAgent.started == 0, str(res)[:160])
        check('W0-1b option_errors explains the bad value',
              any('difficulty' in str(e) for e in res.get('option_errors') or []),
              str(res.get('option_errors')))
        check('W0-1c no start_options.txt was left behind', not option_file.exists(),
              str(option_file))


def test_w0_valid_start_options() -> None:
    with Env() as env:
        original = with_fake_session_agent()
        try:
            res = M.session_start('Maps/Key to Victory', difficulty='easy', player_color='red',
                                  turn_time_seconds=0)
        finally:
            M.VcmiAgent = original
        option_file = env.root / 'state' / 'start_options.txt'
        body = option_file.read_text(encoding='utf-8') if option_file.exists() else ''
        check('W0-2 valid start options reach the client',
              res.get('ok') is True and FakeSessionAgent.started == 1, str(res)[:160])
        check('W0-2b option file written with resolved values',
              'difficulty=0\n' in body and 'player=0\n' in body, body.replace('\n', '|'))
        check('W0-2c contract summary reported',
              (res.get('contract') or {}).get('exe_sha256_match') is True
              and (res.get('contract') or {}).get('protocol_ok') is True,
              str(res.get('contract')))


def test_w0_contract_pinned() -> None:
    """Post-rebuild gate: the pinned contract must describe this repo state."""
    contract = vcmi_agent.load_bridge_contract()
    client = contract.get('client') or {}
    behaviour = ' '.join(str(item) for item in contract.get('frozen_behaviour') or [])
    status = vcmi_agent.VcmiAgent(str(ROOT / 'var' / 'agent_test_contract'),
                                  log=False).contract_status()
    check('W0-3 contract protocol is v2', client.get('protocol') == 2, str(client.get('protocol')))
    check('W0-3b contract frozen behaviour mentions the 30-frame BUY',
          '30 frames' in behaviour and 'recruit' in behaviour,
          behaviour[:120])
    check('W0-3c pinned EXE hash matches the built client', status.get('match') is True,
          f"actual={(status.get('actual_sha256') or '')[:12]} "
          f"expected={(status.get('expected_sha256') or '')[:12]}")
    check('W0-3d contract built_from_commit sources match current HEAD',
          status.get('source_matches_head') is True,
          f"built={status.get('built_from_commit')} head={status.get('repo_head')}")


# ------------------------------------------------------------------ W1: action trust
def test_no_before_state_does_not_send() -> None:
    with Env() as env:
        fake = env.agent(FakeAgent([RuntimeError('state read failed')]))
        res = M.game_act('end_turn', {})
        check('R3 no before-state -> command not sent',
              res.get('status') == 'unknown' and res.get('sent') is False
              and fake.sent == [], str(res)[:200])
        check('R3b state was actually attempted', fake.state_calls == 1,
              f'state_calls={fake.state_calls}')


def battle_state(active_stack: int = 1) -> dict:
    stacks = [
        {'id': 1, 'side': 0, 'creature': 10, 'count': 10, 'hex': [1, 1]},
        {'id': 2, 'side': 1, 'creature': 20, 'count': 5, 'hex': [2, 2]},
    ]
    return state(battle={'active': True, 'active_stack': active_stack, 'my_side': 0,
                         'stacks': stacks})


def test_battle_round_after_state_unknown() -> None:
    with Env() as env:
        fake = env.agent(FakeAgent([battle_state(), RuntimeError('after read failed'),
                                    RuntimeError('final read failed')],
                                   results=[bridge_ok(1, 'battle_attack 2')]))
        res = M.battle_round(target=2)
        orders = res.get('orders') or []
        check('R1 unreadable after-state -> order unknown',
              len(orders) == 1 and orders[0].get('status') == 'unknown',
              str(orders))
        check('R1b round interrupted, not reported as done',
              res.get('status') == 'interrupted'
              and (res.get('interrupt') or {}).get('reason') == 'order_unknown',
              str(res)[:220])
        check('R1c unobserved order is not retried',
              fake.sent == ['battle_attack 2'], f'sent={fake.sent}')


def town_with_garrison(creature: int, count: int) -> dict:
    return {'index': 0, 'name': 'Town0', 'built': [], 'garrison': [
        {'slot': 0, 'creature': creature, 'count': count}], 'dwellings': [],
        'build_options': [], 'available_heroes': []}


def test_recruit_explicit_creature() -> None:
    with Env() as env:
        before = state(towns=[town_with_garrison(100, 10)])
        after = state(towns=[town_with_garrison(100, 15)])
        fake = env.agent(FakeAgent([before, after]))
        res = M.game_act('recruit', {'town': 0, 'level': 1, 'amount': 5, 'creature': 100})
        check('N3a explicit creature is appended to the bridge command',
              fake.sent == ['recruit 0 1 5 100'], f'sent={fake.sent}')
        check('N3b creature-level before/after delta verifies the purchase',
              res.get('status') == 'completed' and '10->15' in str(res.get('evidence')),
              str(res.get('evidence')))

    with Env() as env:
        before = state(towns=[town_with_garrison(100, 10)])
        after = state(towns=[town_with_garrison(100, 12)])
        fake = env.agent(FakeAgent([before, after]))
        res = M.game_act('recruit', {'town': 0, 'level': 1, 'amount': 5, 'creature': 100})
        check('N3c clamped purchase reports the amount actually bought',
              res.get('status') == 'completed' and '+2' in str(res.get('evidence')),
              str(res.get('evidence')))

    with Env() as env:
        fake = env.agent(FakeAgent([state(towns=[town_with_garrison(100, 10)])]))
        res = M.game_act('recruit', {'town': 0, 'level': 1, 'amount': 5})
        check('N3d recruit without creature keeps the old 3-token command',
              fake.sent == ['recruit 0 1 5'], f'sent={fake.sent}')

    with Env() as env:
        fake = env.agent(FakeAgent([state()]))
        res = M.game_act('recruit', {'town': 0, 'level': 1, 'amount': 5, 'creature': 'x'})
        check('N3e non-integer creature rejected before send',
              res.get('ok') is False and 'integer' in str(res.get('error')) and fake.sent == [],
              str(res.get('error')))

    with Env() as env:
        fake = env.agent(FakeAgent([state()]))
        res = M.game_act('build', {'town': 0, 'id': 8, 'village': 1})
        check('N3f unknown argument rejected before send',
              res.get('ok') is False and 'unexpected args' in str(res.get('error'))
              and fake.sent == [], str(res.get('error')))


# ------------------------------------------------------------------ N1/N2 source guards
def test_bridge_source_guards() -> None:
    bridge = (ROOT / 'platform' / 'vcmi-patch' / 'client' / 'AgentBridge.cpp').read_text(
        encoding='utf-8')
    apply_py = (ROOT / 'platform' / 'vcmi-patch' / 'apply.py').read_text(encoding='utf-8')
    check('N1a pending recruit keeps the requesting command seq',
          'int64_t cmdSeq = 0;' in bridge
          and 'pendingRecruit.cmdSeq = currentCmdSeq.load();' in bridge
          and 'appendResultSeq(step.cmdSeq' in bridge,
          'cmdSeq captured + explicit-seq replies')
    check('N1b delayed BUY reports creature/amount/destination',
          'OK recruit_ui_bought town=' in bridge and '" dest=" + step.dest' in bridge,
          'buy reply carries dest')
    check('N2a read-only diagnostics cannot flush a pending purchase',
          'const bool readOnly = (op == "ui_tree"' in bridge
          and 'if (!readOnly && pendingRecruit.active)' in bridge,
          'readOnly guard before flush')
    check('N2b read-only diagnostics cannot close town/puzzle windows',
          'if (!readOnly && !keepsTownUi && townScreenCloseAt != 0)' in bridge
          and 'if (!readOnly && puzzleWindowCloseAt != 0)' in bridge,
          'readOnly guard before close')
    check('N3g recruitment helper selects the requested creature card',
          'int agentBuy(CreatureID creature, int amount)' in apply_py
          and 'card->creature->getId() == creature' in apply_py,
          'agentBuy(CreatureID, int)')
    check('N3h recruitment helper sets the slider via scrollTo, not setValue',
          'slider->scrollTo(buyAmount, true)' in apply_py,
          'CSlider::setValue ignores its argument in this engine build')


def main() -> int:
    test_w0_invalid_start_option()
    test_w0_valid_start_options()
    test_no_before_state_does_not_send()
    test_battle_round_after_state_unknown()
    test_recruit_explicit_creature()
    test_bridge_source_guards()
    test_w0_contract_pinned()
    failed = [item for item in RESULTS if not item[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
