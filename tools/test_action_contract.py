# -*- coding: utf-8 -*-
"""Offline contract tests for the MCP action surface in tools/mcp_server_v2.py.

No game, no client, no network: every test monkeypatches the module-level `_agent()` /
`_require_lease()` (or points STATE_DIR / SAVES_DIR at a temp dir) and feeds fake bridge
replies.  The real VcmiAgent / VCMI_client is never touched.

Covered trust rules:
  T1  argument validation (typed ints/ranges, control chars) BEFORE lease/state/bridge
  T2  bridge REJECTED / UNKNOWN can never be upgraded to completed by the verifier
  T3  object-level before/after diff verifiers (build town index, move_hero hero arg, ...)
  T4  battle no observable change -> submitted, never completed
  T5  ok=True only for status=='completed'; `accepted` is separate
  T6  request_id: pending duplicate -> request_pending; dedup replay; different args -> error
  T7  lease token must match module memory (server restart loses it)
  T8  battle_round refuses auto-answer / auto-enter / auto-retarget by default
  T9  save_game requires a changed file + unchanged day; load_game requires evidence
  T10 persisted events with monotonic ids + unread cursor

Run: python -X utf8 tools/test_action_contract.py
Exit code 0 only when every check passes.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))
import mcp_server_v2 as M  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition, detail: str = '') -> None:
    RESULTS.append((name, bool(condition), detail))
    print(('PASS ' if condition else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


# ------------------------------------------------------------------ fakes / fixtures
def bridge_ok(seq: int = 1, line: str = '') -> types.SimpleNamespace:
    return types.SimpleNamespace(seq=seq, status='COMPLETED',
                                 line=f'seq={seq} OK {line}'.strip(), detail='')


def bridge_err(seq: int = 1, line: str = 'rejected by engine') -> types.SimpleNamespace:
    return types.SimpleNamespace(seq=seq, status='REJECTED',
                                 line=f'seq={seq} ERR {line}', detail='')


def bridge_unknown(seq: int = 1) -> types.SimpleNamespace:
    return types.SimpleNamespace(seq=seq, status='UNKNOWN', line='',
                                 detail='no reply before timeout')


class FakeAgent:
    """Minimal VcmiAgent stand-in: scripted states + scripted bridge replies."""

    def __init__(self, states, results=None, on_execute=None):
        self.states = list(states)
        self.results = list(results or [])
        self.on_execute = on_execute
        self.sent: list[str] = []
        self.state_calls = 0

    def state(self, timeout: float = 10.0) -> dict:
        index = min(self.state_calls, len(self.states) - 1)
        self.state_calls += 1
        item = self.states[index]
        if isinstance(item, BaseException):
            raise item
        return json.loads(json.dumps(item))

    def execute(self, line: str, verify=None, result_timeout: float = 20.0):
        self.sent.append(line)
        if self.on_execute is not None:
            self.on_execute(line)
        if not self.results:
            return bridge_ok(len(self.sent), line)
        item = self.results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def make_hero(index: int, pos, army=None) -> dict:
    return {'index': index, 'name': f'Hero{index}', 'visitable': list(pos),
            'movement': 1000, 'army': list(army or [])}


def make_town(index: int, built=None, garrison=None, dwellings=None) -> dict:
    return {'index': index, 'name': f'Town{index}', 'built': list(built or []),
            'garrison': list(garrison or []), 'dwellings': list(dwellings or []),
            'build_options': [], 'available_heroes': []}


def make_stack(stack_id: int, side: int, count: int, hex_pos) -> dict:
    return {'id': stack_id, 'side': side, 'count': count, 'hex': list(hex_pos)}


def state(day=1, gold=1000, heroes=None, towns=None, query=None, battle=None,
          status=None, in_game=True) -> dict:
    return {'in_game': in_game, 'date': {'day': day}, 'resources': {'gold': gold},
            'heroes': list(heroes or []), 'towns': list(towns or []), 'objects': [],
            'query': query, 'battle': battle or {'active': False}, 'status': status}


class Env:
    """Isolated state/save dirs + fake lease/agent; restores every module global on exit."""

    NAMES = ('STATE_DIR', 'SAVES_DIR', '_require_lease', '_agent', '_lease_token',
             '_ACTION_RETRIES', '_ACTION_RETRY_SLEEP', '_SLOW_RETRIES',
             '_SLOW_RETRY_SLEEP', '_RECRUIT_HERO_RETRY_WAIT', '_RECRUIT_HERO_RETRY_SLEEP',
             '_SAVE_WAIT_SECONDS',
             '_SAVE_POLL_SECONDS', '_SAVE_STABLE_SECONDS',
             '_LOAD_WAIT_SECONDS', '_LOAD_POLL_SECONDS',
             '_MUTATION_LOCK', '_MUTATION_OWNER')

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._saved: dict = {}

    def __enter__(self) -> 'Env':
        self._saved = {name: getattr(M, name) for name in self.NAMES}
        M.STATE_DIR = self.root / 'state'
        M.SAVES_DIR = self.root / 'saves'
        M._require_lease = lambda: None
        M._agent = self._no_agent
        M._lease_token = None
        M._ACTION_RETRIES = 0
        M._ACTION_RETRY_SLEEP = 0
        M._SLOW_RETRIES = 0
        M._SLOW_RETRY_SLEEP = 0
        M._RECRUIT_HERO_RETRY_WAIT = 0.2
        M._RECRUIT_HERO_RETRY_SLEEP = 0
        M._SAVE_WAIT_SECONDS = 0.3
        M._SAVE_POLL_SECONDS = 0.05
        M._SAVE_STABLE_SECONDS = 0.1
        M._LOAD_WAIT_SECONDS = 0.2
        M._LOAD_POLL_SECONDS = 0.02
        # D-014 gate: every test starts with a free mutation slot (fresh lock + no owner)
        M._MUTATION_LOCK = threading.Lock()
        M._MUTATION_OWNER = None
        try:
            M._MUTATION_LOCAL.depth = 0
        except Exception:  # noqa: BLE001 - thread-local attribute is always settable
            pass
        return self

    @staticmethod
    def _no_agent():
        raise AssertionError('the real game agent must not be touched in offline tests')

    def agent(self, fake: FakeAgent) -> FakeAgent:
        M._agent = lambda: fake
        return fake

    def restore_real_lease(self):
        M._require_lease = self._saved['_require_lease']

    def __exit__(self, *exc_info) -> None:
        for name, value in self._saved.items():
            setattr(M, name, value)
        self.tmp.cleanup()


# ------------------------------------------------------------------ T1 argument validation
def test_validation() -> None:
    with Env() as env:
        fake = env.agent(FakeAgent([state()]))
        res = M.game_act('answer', {'value': '0\nanswer 1'})
        check('T1 newline arg rejected before submit',
              res.get('ok') is False and 'sent' in res and res.get('sent') is False
              and 'control' in str(res.get('error', '')), str(res.get('error')))
        check('T1b newline arg never reached the bridge', fake.sent == [] and fake.state_calls == 0,
              f'sent={fake.sent} state_calls={fake.state_calls}')

        res = M.game_act('move', {'hero': 0, 'x': 'abc', 'y': 2, 'z': 0})
        check('T1c non-integer coord rejected',
              res.get('ok') is False and 'integer' in str(res.get('error', '')),
              str(res.get('error')))

        res = M.game_act('move', {'hero': 0, 'x': '1\n2', 'y': 2, 'z': 0})
        check('T1c2 newline in coordinate rejected',
              res.get('ok') is False and 'control' in str(res.get('error', '')),
              str(res.get('error')))

        res = M.game_act('build', {'town': 0, 'id': 8, 'extra': 'a\tb'})
        check('T1d control char in extra arg rejected',
              res.get('ok') is False and 'control' in str(res.get('error', '')),
              str(res.get('error')))

        res = M.game_act('recruit', {'town': 0, 'level': 9, 'amount': 1})
        check('T1e out-of-range level rejected',
              res.get('ok') is False and 'range' in str(res.get('error', '')),
              str(res.get('error')))

        res = M.game_act('transfer', {'src': 'town', 'src_index': 0, 'src_slot': 0,
                                      'dst': 'hero;drop', 'dst_index': 0, 'dst_slot': 0})
        check('T1f enum arg injection rejected',
              res.get('ok') is False and 'must be one of' in str(res.get('error', '')),
              str(res.get('error')))

        res = M.game_act('nuke', {})
        check('T1g unsupported action rejected', res.get('ok') is False, '')

        res = M.game_act('build', {'town': 0})
        check('T1h missing args rejected',
              res.get('ok') is False and 'missing' in str(res.get('error', '')),
              str(res.get('error')))
        check('T1i validation never called the agent', fake.sent == [] and fake.state_calls == 0,
              f'sent={fake.sent} state_calls={fake.state_calls}')


# ------------------------------------------------------------------ T2 bridge mapping
def test_bridge_mapping() -> None:
    with Env() as env:
        before = state(towns=[make_town(0, built=[8])])
        after = state(towns=[make_town(0, built=[8])])
        fake = env.agent(FakeAgent([before, after], [bridge_err(1)]))
        res = M.game_act('build', {'town': 0, 'id': 8}, request_id='req-rejected')
        check('T2 REJECTED bridge + preexisting building -> rejected, not completed',
              res.get('ok') is False and res.get('status') == 'rejected'
              and res.get('accepted') is False and 'bridge rejected' in str(res.get('evidence')),
              f"status={res.get('status')} accepted={res.get('accepted')} "
              f"evidence={res.get('evidence')}")
        check('T2b rejected action was sent exactly once', fake.sent == ['build 0 8'],
              str(fake.sent))

    with Env() as env:
        before = state(towns=[make_town(0, built=[])])
        after = state(towns=[make_town(0, built=[8])])
        fake = env.agent(FakeAgent([before, after], [bridge_unknown(1)]))
        res = M.game_act('build', {'town': 0, 'id': 8})
        check('T2c UNKNOWN bridge never upgraded by the verifier',
              res.get('ok') is False and res.get('status') == 'unknown'
              and res.get('accepted') is False,
              f"status={res.get('status')} evidence={res.get('evidence')}")


# ------------------------------------------------------------------ T3 diff verifiers
def test_verifiers() -> None:
    with Env() as env:
        before = state(towns=[make_town(0, built=[3]), make_town(1, built=[])])
        after = state(towns=[make_town(0, built=[3]), make_town(1, built=[8])])
        env.agent(FakeAgent([before, after], [bridge_ok(1)]))
        res = M.game_act('build', {'town': 1, 'id': 8})
        check('T3 build verifier uses args["town"] (not town 0)',
              res.get('status') == 'completed' and res.get('ok') is True,
              f"status={res.get('status')} evidence={res.get('evidence')}")

    with Env() as env:
        before = state(towns=[make_town(0, built=[])])
        after = state(towns=[make_town(0, built=[])])
        env.agent(FakeAgent([before, after], [bridge_ok(1)]))
        res = M.game_act('build', {'town': 0, 'id': 8})
        check('T3b missing building after OK bridge -> rejected with accepted=True',
              res.get('ok') is False and res.get('status') == 'rejected'
              and res.get('accepted') is True,
              f"status={res.get('status')} accepted={res.get('accepted')}")

    with Env() as env:
        before = state(towns=[make_town(0, built=[8])])
        after = state(towns=[make_town(0, built=[8])])
        env.agent(FakeAgent([before, after], [bridge_ok(1)]))
        res = M.game_act('build', {'town': 0, 'id': 8})
        check('T3c building preexisting before the action cannot be attributed -> submitted',
              res.get('ok') is False and res.get('status') == 'submitted',
              f"status={res.get('status')} evidence={res.get('evidence')}")

    with Env() as env:
        before = state(heroes=[make_hero(0, (5, 5)), make_hero(1, (7, 7))])
        after = state(heroes=[make_hero(0, (5, 5)), make_hero(1, (9, 9))])
        env.agent(FakeAgent([before, after], [bridge_ok(1)]))
        res = M.game_act('move_hero', {'hero': 1, 'x': 9, 'y': 9, 'z': 0})
        check('T3d move_hero verifier checks args["hero"] (not hero 0)',
              res.get('status') == 'completed' and res.get('ok') is True,
              f"status={res.get('status')} evidence={res.get('evidence')}")

    with Env() as env:
        before = state(towns=[make_town(0, garrison=[{'slot': 0, 'creature': 1, 'count': 20}],
                                       dwellings=[{'level': 1, 'available': 10}])])
        after = state(towns=[make_town(0, garrison=[{'slot': 0, 'creature': 1, 'count': 24}],
                                      dwellings=[{'level': 1, 'available': 6}])])
        env.agent(FakeAgent([before, after], [bridge_ok(1)]))
        res = M.game_act('recruit', {'town': 0, 'level': 1, 'amount': 4})
        check('T3e recruit verifier uses dwelling availability diff',
              res.get('status') == 'completed', f"evidence={res.get('evidence')}")

    with Env() as env:
        before = state(day=4)
        after = state(day=5)
        env.agent(FakeAgent([before, after], [bridge_ok(1)]))
        res = M.game_act('end_turn')
        check('T3f end_turn completed when the day advances',
              res.get('status') == 'completed', f"evidence={res.get('evidence')}")

    with Env() as env:
        before = state(day=4)
        after = state(day=4)
        env.agent(FakeAgent([before, after], [bridge_ok(1)]))
        res = M.game_act('end_turn')
        check('T3g end_turn with unchanged day -> submitted, not completed',
              res.get('ok') is False and res.get('status') == 'submitted',
              f"status={res.get('status')}")

    with Env() as env:
        before = state(heroes=[make_hero(0, (1, 1), [{'slot': 0, 'creature': 1, 'count': 10}])],
                       towns=[make_town(0, garrison=[{'slot': 0, 'creature': 1, 'count': 30}])])
        after = state(heroes=[make_hero(0, (1, 1), [{'slot': 0, 'creature': 1, 'count': 15}])],
                      towns=[make_town(0, garrison=[{'slot': 0, 'creature': 1, 'count': 25}])])
        env.agent(FakeAgent([before, after], [bridge_ok(1)]))
        res = M.game_act('transfer', {'src': 'town', 'src_index': 0, 'src_slot': 0,
                                      'dst': 'hero', 'dst_index': 0, 'dst_slot': 0})
        check('T3h transfer completed when source drops and destination grows',
              res.get('status') == 'completed' and res.get('ok') is True,
              f"status={res.get('status')} evidence={res.get('evidence')}")

    with Env() as env:
        battle = {'active': True, 'my_side': 0, 'active_stack': 1,
                  'stacks': [make_stack(1, 0, 10, (1, 1)),
                             make_stack(2, 1, 10, (5, 5))]}
        before = state(battle=battle)
        after = state(battle=json.loads(json.dumps(battle)))
        env.agent(FakeAgent([before, after], [bridge_ok(1)]))
        res = M.game_act('battle_attack', {'stack': 2})
        check('T3i battle with no observable change -> submitted, never completed',
              res.get('ok') is False and res.get('status') == 'submitted',
              f"status={res.get('status')}")

    with Env() as env:
        before = state(towns=[make_town(0, built=[])])
        after = state(towns=[make_town(0, built=[8])])
        env.agent(FakeAgent([RuntimeError('stale state'), after], [bridge_ok(1)]))
        res = M.game_act('build', {'town': 0, 'id': 8})
        check('T3j missing before observation -> unknown (never completed/submitted)',
              res.get('ok') is False and res.get('status') == 'unknown',
              f"status={res.get('status')} evidence={res.get('evidence')}")


# ------------------------------------------------------------------ T6 request ids
def test_request_idempotency() -> None:
    with Env() as env:
        before = state(towns=[make_town(0, built=[])])
        after = state(towns=[make_town(0, built=[8])])
        fake = env.agent(FakeAgent([before, after], [bridge_ok(1)]))
        first = M.game_act('build', {'town': 0, 'id': 8}, request_id='req-1')
        calls_after_first = fake.state_calls
        replay = M.game_act('build', {'town': 0, 'id': 8}, request_id='req-1')
        check('T6 finished request_id replays the stored result without resubmitting',
              replay.get('deduplicated') is True
              and replay.get('action_id') == first.get('action_id')
              and len(fake.sent) == 1 and fake.state_calls == calls_after_first,
              f"dedup={replay.get('deduplicated')} sent={len(fake.sent)}")
        conflict = M.game_act('build', {'town': 0, 'id': 9}, request_id='req-1')
        check('T6b same request_id with different args -> error',
              conflict.get('ok') is False
              and 'different args' in str(conflict.get('error', '')),
              str(conflict.get('error')))

    with Env() as env:
        arg_key = json.dumps({'action': 'build', 'args': {'town': 0, 'id': 8}},
                             sort_keys=True, ensure_ascii=False)
        M._action_index_path().write_text(
            json.dumps({'req-pending': {'key': arg_key, 'state': 'pending'}}), encoding='utf-8')
        fake = env.agent(FakeAgent([state(towns=[make_town(0, built=[])])]))
        res = M.game_act('build', {'town': 0, 'id': 8}, request_id='req-pending')
        check('T6c duplicate request while pending -> error request_pending, nothing sent',
              res.get('ok') is False and res.get('error') == 'request_pending'
              and fake.sent == [] and fake.state_calls == 0,
              f"error={res.get('error')} sent={fake.sent}")


# ------------------------------------------------------------------ T7 lease token
def test_lease_token() -> None:
    with Env() as env:
        env.restore_real_lease()
        M._lease_token = None
        raised = False
        try:
            M._require_lease()
        except RuntimeError:
            raised = True
        check('T7 no in-memory lease token -> must re-acquire', raised)

        acquired = M.lease_acquire()
        check('T7b lease_acquire stores an owner/token/expiry record',
              acquired.get('ok') is True
              and acquired.get('lease', {}).get('owner') == 'llm'
              and acquired.get('lease', {}).get('token') == M._lease_token,
              str(acquired.get('lease')))
        M._require_lease()  # matching token + unexpired: must not raise
        check('T7c matching lease token accepted', True)

        lease_path = M._dir() / 'lease.json'
        tampered = json.loads(lease_path.read_text(encoding='utf-8'))
        tampered['token'] = 'not-this-session'
        lease_path.write_text(json.dumps(tampered), encoding='utf-8')
        mismatch = False
        try:
            M._require_lease()
        except RuntimeError as exc:
            mismatch = 'mismatch' in str(exc)
        check('T7d lease token mismatch rejected', mismatch, str(tampered))

        # The file now belongs to another writer (tampered token): a stale in-memory token must
        # NOT be able to delete that writer's lease (R4).
        release = M.lease_release()
        check('T7e stale owner cannot release another writer lease',
              release.get('ok') is False and release.get('error') == 'lease_not_held'
              and M._lease_token is not None, str(release))
        # Explicit, ledger-recorded takeover is the controlled way back.
        taken = M.lease_acquire(force=True, reason='test takeover')
        check('T7f forced takeover re-acquires the lease', taken.get('ok') is True
              and taken.get('took_over') is True, str(taken)[:160])
        M.lease_release()
        cleared = False
        try:
            M._require_lease()
        except RuntimeError:
            cleared = True
        check('T7g holder release clears the in-memory token',
              cleared and M._lease_token is None)


# ------------------------------------------------------------------ T8 battle_round
def test_battle_round_defaults() -> None:
    battle = {'active': True, 'my_side': 0, 'active_stack': 1,
              'stacks': [make_stack(1, 0, 10, (1, 1)),
                         make_stack(2, 1, 6, (5, 5))]}
    with Env() as env:
        fake = env.agent(FakeAgent([state(query={'type': 'yesno'}, battle=battle)]))
        res = M.battle_round()
        check('T8 battle_round refuses to answer a query by default',
              res.get('ok') is False and res.get('status') == 'interrupted'
              and res.get('reason') == 'query_opened' and fake.sent == [],
              f"status={res.get('status')} reason={res.get('reason')} sent={fake.sent}")

    with Env() as env:
        deployment = {'active': True, 'my_side': 0, 'active_stack': -1,
                      'stacks': [make_stack(1, 0, 10, (1, 1))]}
        fake = env.agent(FakeAgent([state(battle=deployment)]))
        res = M.battle_round()
        check('T8b battle_round refuses to auto-enter deployment by default',
              res.get('ok') is False and res.get('status') == 'interrupted'
              and res.get('reason') == 'deployment' and fake.sent == [],
              f"reason={res.get('reason')} sent={fake.sent}")

    with Env() as env:
        fake = env.agent(FakeAgent([state(battle=battle)]))
        res = M.battle_round()
        check('T8c battle_round refuses to silently retarget without a target',
              res.get('ok') is False and res.get('status') == 'interrupted'
              and res.get('reason') == 'retarget_required' and fake.sent == [],
              f"reason={res.get('reason')} sent={fake.sent}")

    with Env() as env:
        fake = env.agent(FakeAgent([state(query={'type': 'yesno'}, battle=battle),
                                    state(battle=battle)]))
        res = M.battle_round(allow_auto_answer=True)
        check('T8d allow_auto_answer=True sends the answer',
              fake.sent[:1] == ['answer 0'] and res.get('ok') is False,
              f'sent={fake.sent} status={res.get("status")}')


# ------------------------------------------------------------------ T9 save / load
def test_save_load() -> None:
    save_dir = None
    with Env() as env:
        save_dir = env.root / 'saves'
        slot_dir = save_dir / M.SAVE_SUBDIR
        slot_dir.mkdir(parents=True)
        path = slot_dir / 'old.vsgm1'
        path.write_bytes(b'old-save-content')
        env.agent(FakeAgent([state(day=3), state(day=3)], [bridge_ok(1)]))
        res = M.save_game('old')
        check('T9 save_game with an unchanged old file -> not completed',
              res.get('ok') is False and res.get('status') == 'submitted'
              and 'not changed' in str(res.get('evidence')),
              f"status={res.get('status')} evidence={res.get('evidence')}")

    with Env() as env:
        slot_dir = env.root / 'saves' / M.SAVE_SUBDIR
        slot_dir.mkdir(parents=True)
        target = slot_dir / 'fresh.vsgm1'

        def write_save(_line):
            target.write_bytes(b'brand-new-save-content')

        env.agent(FakeAgent([state(day=3), state(day=3)], [bridge_ok(1)],
                            on_execute=write_save))
        res = M.save_game('fresh')
        meta = M._load_saves_meta()
        check('T9b save_game completed when the file changed and the day did not',
              res.get('ok') is True and res.get('status') == 'completed',
              f"status={res.get('status')} evidence={res.get('evidence')}")
        check('T9c completed save writes slot metadata for load_game',
              'fresh' in meta and meta['fresh'].get('day') == 3, str(meta))

    with Env() as env:
        env.agent(FakeAgent([state(day=3)], [bridge_err(1)]))
        res = M.save_game('nope')
        check('T9d save_game bridge rejection -> rejected', res.get('ok') is False
              and res.get('status') == 'rejected', f"status={res.get('status')}")

    with Env() as env:
        fake = env.agent(FakeAgent([state()]))
        res = M.load_game('missing')
        check('T9e load_game for a missing save -> rejected without sending',
              res.get('ok') is False and res.get('status') in ('rejected', 'unknown')
              and fake.sent == [],
              f"status={res.get('status')} sent={fake.sent}")

    with Env() as env:
        slot_dir = env.root / 'saves' / M.SAVE_SUBDIR
        slot_dir.mkdir(parents=True)
        (slot_dir / 'known.vsgm1').write_bytes(b'save-of-day-3')
        M._dir().mkdir(parents=True, exist_ok=True)
        M._saves_meta_path().write_text(json.dumps(
            {'known': {'slot': 'Saves/agent/known', 'day': 3,
                       'saved_at': '2020-01-01 00:00:00'}}), encoding='utf-8')
        env.agent(FakeAgent([state(day=5, gold=100), state(day=3, gold=50)],
                            [bridge_ok(1)]))
        res = M.load_game('known')
        check('T9f load_game completed when day matches and the fingerprint changed',
              res.get('ok') is True and res.get('status') == 'completed',
              f"status={res.get('status')} evidence={res.get('evidence')}")

    with Env() as env:
        slot_dir = env.root / 'saves' / M.SAVE_SUBDIR
        slot_dir.mkdir(parents=True)
        (slot_dir / 'stale.vsgm1').write_bytes(b'save-of-day-1')
        M._dir().mkdir(parents=True, exist_ok=True)
        M._saves_meta_path().write_text(json.dumps(
            {'stale': {'slot': 'Saves/agent/stale', 'day': 1,
                       'saved_at': '2020-01-01 00:00:00'}}), encoding='utf-8')
        env.agent(FakeAgent([state(day=1), state(day=1)], [bridge_ok(1)]))
        res = M.load_game('stale')
        check('T9g load_game with no observable change and an old slot -> unknown',
              res.get('ok') is False and res.get('status') == 'unknown',
              f"status={res.get('status')} evidence={res.get('evidence')}")


# ------------------------------------------------------------------ T10 persisted events
def test_events() -> None:
    with Env() as env:
        env.agent(FakeAgent([state(query={'type': 'yesno'})]))
        first = M.game_events()
        types = [event.get('type') for event in first.get('events') or []]
        check('T10 first game_events returns unread transitions with ids',
              types == ['query_opened'] and first.get('unread')
              and first['unread'][0].get('id') == 1,
              f'events={types} unread={len(first.get("unread") or [])}')
        second = M.game_events()
        check('T10b cursor advanced: a second call has no unread events',
              (second.get('unread') or []) == []
              and [event.get('type') for event in second.get('events') or []]
              == ['query_opened'],
              f"unread={second.get('unread')}")

    with Env() as env:
        battle = {'active': True, 'my_side': 0, 'active_stack': 1,
                  'stacks': [make_stack(1, 0, 10, (1, 1)),
                             make_stack(2, 1, 6, (5, 5))]}
        env.agent(FakeAgent([state(battle=battle), state()]))
        first = M.game_events()
        second = M.game_events()
        ids = [event.get('id') for event in second.get('events') or []]
        types = [event.get('type') for event in second.get('events') or []]
        check('T10c battle_started / battle_ended persisted with monotonic ids',
              types == ['battle_started', 'battle_ended'] and ids == [1, 2],
              f'types={types} ids={ids}')
        check('T10d game_events decisions keep the {type:...} shape',
              isinstance(first.get('decisions'), list) and first['decisions']
              and 'type' in first['decisions'][0],
              str(first.get('decisions'))[:120])


# ------------------------------------------------------------------ T11 observation tools
def test_observation_tools() -> None:
    battle = {'active': True, 'my_side': 0, 'active_stack': 1,
              'stacks': [make_stack(1, 0, 10, (1, 1)),
                         make_stack(2, 1, 6, (5, 5))]}
    rich = state(day=2, gold=1234,
                 heroes=[make_hero(0, (3, 4), [{'slot': 0, 'creature': 1, 'count': 7}])],
                 towns=[make_town(0, built=[1, 2],
                                  garrison=[{'slot': 0, 'creature': 1, 'count': 5}],
                                  dwellings=[{'level': 1, 'available': 9}])],
                 query={'type': 'yesno', 'text': 'Hire?'}, battle=battle, status='PLAYING')
    with Env() as env:
        env.agent(FakeAgent([rich]))
        observed = M.game_observe()
        check('T11 game_observe still returns digest + decisions',
              observed.get('ok') is True and observed['state'].get('day') == 2
              and any(point.get('type') == 'query_opened'
                      for point in observed.get('decisions') or []),
              str([point.get('type') for point in observed.get('decisions') or []]))
        brief = M.game_brief()
        check('T11b game_brief stays compact and reports decision types',
              brief.get('ok') is True and brief.get('battle') is True
              and 'query_opened' in (brief.get('decisions') or []),
              str(brief.get('decisions')))


# ------------------------------------------------------------------ T12 ledger lookup
def test_ledger_lookup() -> None:
    with Env() as env:
        before = state(towns=[make_town(0, built=[])])
        after = state(towns=[make_town(0, built=[8])])
        env.agent(FakeAgent([before, after], [bridge_ok(1)]))
        res = M.game_act('build', {'town': 0, 'id': 8})
        lookup = M.game_action_status(res.get('action_id'))
        check('T12 ledger lookup returns the honest status + accepted flag',
              lookup.get('ok') is True
              and lookup['action'].get('status') == 'completed'
              and lookup['action'].get('accepted') is True,
              str(lookup.get('action'))[:150])


# ------------------------------------------------------------------ main
def main() -> int:
    test_validation()
    test_bridge_mapping()
    test_verifiers()
    test_request_idempotency()
    test_lease_token()
    test_battle_round_defaults()
    test_save_load()
    test_events()
    test_observation_tools()
    test_ledger_lookup()
    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    if failed:
        print('FAILED:', ', '.join(failed))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
