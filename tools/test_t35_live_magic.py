# -*- coding: utf-8 -*-
r"""T35 LIVE smoke: hero magic (battle + adventure casting) on the rebuilt client.

LIVE/DEBUG - starts ONE real VCMI client in an isolated tree; refused unless every writable path
is redirected.  Not part of the offline regression suite.

Flow (one isolated new game):
  1  explicit config start + pinned exe match; lease acquired
  2  debug cheat channel (test-only, env-gated): allspells all + mana all <n>
  3  game_brief shows the learned spells with cost/adventure/can_cast
  4  ADVENTURE magic: cast one known adventure spell and check the hero mana dropped
  5  BATTLE magic: move to the weakest visible neutral stack, start the battle,
     battle_actions lists castable spells (id/name/cost/can_cast/target_kind/hero_mana),
     cast >=1 damage spell and >=1 beneficial/no-target spell, verify mana/effects, finish battle
  6  recording identity binds the new exe sha; clean stop; formal dirs untouched

Safety gate (executed BEFORE importing mcp_server_v2): AUTO_HEROS3_LIVE_TEST=1 + the five
isolated paths (see test_n1_live_restore.py docstring); DRYRUN=1 prints the paths and exits.

Manual run (example):
    set AUTO_HEROS3_LIVE_TEST=1
    set AUTO_HEROS3_STATE_DIR=%TEMP%\t35\state
    set AUTO_HEROS3_SAVES_DIR=%USERPROFILE%\Documents\My Games\vcmi\Saves
    set AUTO_HEROS3_SAVE_SUBDIR=agent_n1_t35
    set AUTO_HEROS3_MEMORY_ROOT=%TEMP%\t35\memory
    set AUTO_HEROS3_RECORDINGS_DIR=%TEMP%\t35\recordings
    .venv\Scripts\python.exe -X utf8 tools\test_t35_live_magic.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    results.append((name, bool(ok), detail))
    print(('PASS' if ok else 'FAIL'), name, detail)


def _canon(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _guard() -> dict:
    resolved = {
        'STATE_DIR': os.environ.get('AUTO_HEROS3_STATE_DIR'),
        'SAVES_DIR': os.environ.get('AUTO_HEROS3_SAVES_DIR'),
        'MEMORY_ROOT': os.environ.get('AUTO_HEROS3_MEMORY_ROOT'),
        'RECORDINGS_DIR': os.environ.get('AUTO_HEROS3_RECORDINGS_DIR'),
    }
    allowed = [_canon(Path(tempfile.gettempdir())), _canon(ROOT / 'var' / 'test_n1')]
    default_saves = _canon(Path.home() / 'Documents' / 'My Games' / 'vcmi' / 'Saves')
    formal = {
        'STATE_DIR': _canon(ROOT / 'var' / 'agent_mcp_v2'),
        'SAVES_DIR': _canon(default_saves / 'agent'),
        'MEMORY_ROOT': _canon(ROOT / 'doc' / 'player'),
        'RECORDINGS_DIR': _canon(ROOT / 'var' / 'recordings'),
    }
    problems: list[str] = []
    for var, raw in resolved.items():
        if not raw:
            problems.append(f'{var} is not set')
            continue
        path = _canon(Path(raw))
        if var != 'SAVES_DIR' and (path == formal[var] or formal[var].is_relative_to(path)
                                   or path.is_relative_to(formal[var])):
            problems.append(f'{var} points at the formal directory ({formal[var]})')
        if var == 'SAVES_DIR':
            if path != default_saves and not any(path == r or path.is_relative_to(r)
                                                for r in allowed):
                problems.append(f'{var}={path} is not the default saves root')
        elif not any(path == r or path.is_relative_to(r) for r in allowed):
            problems.append(f'{var}={path} is outside the allowed test roots')
    subdir = os.environ.get('AUTO_HEROS3_SAVE_SUBDIR')
    if not subdir:
        problems.append('AUTO_HEROS3_SAVE_SUBDIR is not set (must start with agent_n1)')
    elif not re.fullmatch(r'[A-Za-z0-9_-]+', subdir) or not subdir.startswith('agent_n1'):
        problems.append(f'AUTO_HEROS3_SAVE_SUBDIR={subdir!r} must be a plain name starting '
                        'with agent_n1')
    dryrun = os.environ.get('AUTO_HEROS3_LIVE_TEST_DRYRUN') == '1'
    if not dryrun and os.environ.get('AUTO_HEROS3_LIVE_TEST') != '1':
        problems.append('AUTO_HEROS3_LIVE_TEST=1 is required to run the live smoke')
    if problems:
        print('LIVE SMOKE REFUSED: this test starts a real VCMI client.')
        for problem in problems:
            print(f'  - {problem}')
        raise SystemExit(2)
    if dryrun:
        for var, raw in resolved.items():
            print(f'DRYRUN {var}={raw}')
        print(f'DRYRUN SAVE_SUBDIR={subdir}')
        raise SystemExit(0)
    return resolved


_guard()

sys.path.insert(0, str(ROOT / 'tools'))
import mcp_server_v2 as M  # noqa: E402

DAMAGE_NAMES = ('魔法箭', '魔法神箭', '闪电', '冰', '火球', '雷鸣', '死亡之指', '毒', '流星')
BENEFIT_NAMES = ('加速', '祝福', '护盾', '石肤', '疗伤', '嗜血', '幸运', '祈祷', '驱魔', '圣杯',
                 '抵御', '防护', '魔法护盾')
ADVENTURE_PREFERRED = ('观气', '观地', '观天', '伪装')


def state_now() -> dict:
    try:
        return M._agent().state(timeout=10) or {}
    except Exception:
        return {}


def wait_ready(timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    st: dict = {}
    while time.time() < deadline:
        st = state_now()
        if st.get('in_game') and (st.get('date') or {}).get('day') is not None:
            return st
        time.sleep(1.0)
    return st


def wait_for(predicate, timeout: float = 20.0, interval: float = 0.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def write_cheat(lines: list[str]) -> None:
    """Test-only debug channel: the launcher points AUTO_HEROS3_CHEAT_FILE at <state>/cheat.txt."""
    path = M._dir() / 'cheat.txt'
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def hero_of(st: dict) -> dict:
    heroes = st.get('heroes') or []
    return heroes[0] if heroes else {}


def spells_of(st: dict) -> list:
    return [sp for sp in (hero_of(st).get('spells') or []) if isinstance(sp, dict)]


def pick_adventure_spell(spells: list):
    candidates = [sp for sp in spells if sp.get('adventure')]
    for keyword in ADVENTURE_PREFERRED:
        for sp in candidates:
            if keyword in str(sp.get('name') or ''):
                return sp
    return candidates[0] if candidates else None


def pick_battle_spell(rows: list, names: tuple, kinds: tuple):
    """Keyword-priority pick: try each name (in order) against every castable row."""
    usable = [sp for sp in rows
              if sp.get('can_cast') and sp.get('target_kind') in kinds]
    for keyword in names:
        for sp in usable:
            if keyword and keyword in str(sp.get('name') or ''):
                return sp
    return usable[0] if usable else None


def advance_round(rounds: int = 1, deadline: float = 45.0) -> dict:
    """Let the battle move to the next round without attacking (wait/defend only).

    VCMI allows one hero spell per combat turn; the second smoke cast needs a new round, and
    the enemy stack must survive, so our stacks only wait/defend during the transition.
    """
    st = state_now()
    start_round = int((st.get('battle') or {}).get('round') or 0)
    target = start_round + max(1, rounds)
    end = time.time() + deadline
    waited: set = set()
    while time.time() < end:
        st = state_now()
        battle = st.get('battle') or {}
        if not battle.get('active'):
            return st
        if int(battle.get('round') or 0) >= target:
            return st
        if battle.get('active_stack') == -1:
            M.battle_start()
            time.sleep(0.7)
            continue
        stacks = [s for s in battle.get('stacks') or [] if isinstance(s, dict)]
        mine = battle.get('my_side')
        active = battle.get('active_stack')
        active_row = next((s for s in stacks if s.get('id') == active), None)
        if active_row is not None and active_row.get('side') == mine:
            if active not in waited:
                res = M.game_act('battle_wait', {},
                                 thought='T35 live: wait for the next combat round (spell limit)')
                if res.get('status') in ('completed', 'submitted'):
                    waited.add(active)
                    continue
            M.game_act('battle_defend', {},
                       thought='T35 live: defend to reach the next combat round (spell limit)')
            time.sleep(0.4)
        else:
            time.sleep(0.5)
    return state_now()


def visible_creatures(st: dict, hero: dict) -> list:
    hero_pos = list(hero.get('pos') or hero.get('visitable') or [0, 0, 0])
    out = []
    for obj in st.get('objects') or []:
        if not isinstance(obj, dict) or obj.get('kind') != 'creature':
            continue
        pos = list(obj.get('pos') or [0, 0, 0])
        dist = abs(pos[0] - hero_pos[0]) + abs(pos[1] - hero_pos[1])
        out.append((dist, obj))
    out.sort(key=lambda item: item[0])
    return out


def hover_rank(obj: dict) -> int:
    text = str(obj.get('hover') or '')
    weak = ('少数', '几个', '小群', '一群')
    strong = ('成群', '蜂群', '军团', '大量', '众多')
    if any(k in text for word in weak for k in (word,)):
        return 0
    if any(word in text for word in strong):
        return 2
    return 1


def wait_for_my_turn(deadline: float = 30.0) -> dict:
    """Wait until one of our stacks is active again (hero spells need our combat turn)."""
    end = time.time() + deadline
    while time.time() < end:
        st = state_now()
        battle = st.get('battle') or {}
        if not battle.get('active'):
            return st
        if battle.get('active_stack') == -1:
            M.battle_start()
            time.sleep(0.7)
            continue
        stacks = [s for s in battle.get('stacks') or [] if isinstance(s, dict)]
        mine = battle.get('my_side')
        active = next((s for s in stacks if s.get('id') == battle.get('active_stack')), None)
        if active is not None and active.get('side') == mine:
            return st
        time.sleep(0.5)
    return state_now()


def finish_battle(deadline: float = 75.0) -> None:
    """Cast phase is over: let the battle finish.

    War-machine stacks (first-aid tent) cannot attack; they wait/defend instead, otherwise a
    battle would never advance past their turn.
    """
    end = time.time() + deadline
    while time.time() < end:
        st = state_now()
        battle = st.get('battle') or {}
        if not battle.get('active'):
            return
        if battle.get('active_stack') == -1:
            M.battle_start()
            time.sleep(0.8)
            continue
        stacks = [s for s in battle.get('stacks') or [] if isinstance(s, dict)]
        mine = battle.get('my_side')
        enemies = [s.get('id') for s in stacks if s.get('side') != mine]
        active_row = next((s for s in stacks
                           if s.get('id') == battle.get('active_stack')), None)
        if active_row is None or not enemies:
            time.sleep(0.5)
            continue
        if active_row.get('side') != mine:
            time.sleep(0.4)          # enemy turn: the AI acts on its own
            continue
        actions = M.battle_actions()
        usable = [t for t in (actions.get('targets') or [])
                  if isinstance(t, dict) and (t.get('can_melee') or t.get('can_shoot'))]
        if not usable:
            # non-attacking stack (war machine): consume its turn with wait/defend
            res = M.game_act('battle_wait', {},
                             thought='T35 live: war machine cannot attack - wait')
            if res.get('status') not in ('completed', 'submitted'):
                M.game_act('battle_defend', {},
                           thought='T35 live: war machine cannot attack - defend')
            time.sleep(0.4)
            continue
        target_id = usable[0].get('id')
        M.battle_round(target=target_id, allow_retarget=True, allow_auto_enter=True,
                       allow_auto_answer=True)
        time.sleep(0.5)


def main() -> int:
    started = False
    health: dict = {}
    try:
        start = M.session_start('Maps/Key to Victory', difficulty='easy', player_color='red',
                                starting_town='castle', starting_hero='random',
                                starting_bonus='random')
        started = bool(start.get('ok'))
        check('T35L-1 explicit config start', started and (start.get('config') or {}).get('config_match') is True,
              str(start.get('error') or (start.get('config') or {}).get('config_match')))
        M.lease_acquire()
        st = wait_ready()
        day = (st.get('date') or {}).get('day')
        hero = hero_of(st)
        check('T35L-1b in_game with a day and one hero',
              bool(day) and bool(hero), f'day={day} heroes={len(st.get("heroes") or [])}')
        pinned = json.loads((ROOT / 'tools' / 'bridge_contract.json')
                            .read_text(encoding='utf-8'))['client']
        status = M.game_status()
        contract = status.get('contract') or {}
        check('T35L-1c live client matches the new pinned exe',
              contract.get('match') is True
              and contract.get('client_sha256') == pinned.get('sha256'), str(contract)[:220])

        # ---- 2) debug cheat (test-only): learn spells + set mana ------------------------------
        initial_spells = len(spells_of(st))
        write_cheat(['allspells all', 'mana all 80'])
        learned = wait_for(lambda: len(spells_of(state_now())) > initial_spells, timeout=20.0)
        st = state_now()
        hero = hero_of(st)
        spells = spells_of(st)
        check('T35L-2 debug channel learned spells (test-only, env-gated)',
              learned and len(spells) > initial_spells,
              f'before={initial_spells} after={len(spells)}')
        check('T35L-2b debug channel set hero mana', int(hero.get('mana') or 0) >= 40,
              f'mana={hero.get("mana")}/{hero.get("mana_max")}')

        # ---- 3) observation: brief shows cost/adventure/can_cast -------------------------------
        brief = M.game_brief()
        brief_hero = (brief.get('heroes') or [{}])[0]
        brief_spells = [sp for sp in (brief_hero.get('spells') or []) if isinstance(sp, dict)]
        check('T35L-3 game_brief shows spell id/name/cost/can_cast/adventure',
              brief_spells and all('cost' in sp and 'can_cast' in sp for sp in brief_spells),
              str(brief_spells[:3]))
        adventure_spell = pick_adventure_spell(spells)
        check('T35L-3b an adventure spell is known', adventure_spell is not None,
              str([sp.get('name') for sp in spells if sp.get('adventure')]))

        # ---- 4) adventure magic ---------------------------------------------------------------
        ad_mana_before = int(hero.get('mana') or 0)
        ad = None
        if adventure_spell:
            ad = M.game_act('cast_spell', {'hero': 0, 'spell': adventure_spell['id']},
                            thought='T35 live: adventure spell smoke test')
            ok = ad.get('status') in ('completed', 'submitted')
            if not ok and 'target_required' in str(ad.get('evidence')):
                hero_pos = list(hero.get('visitable') or hero.get('pos') or [0, 0, 0])
                ad = M.game_act('cast_spell',
                                {'hero': 0, 'spell': adventure_spell['id'],
                                 'x': hero_pos[0], 'y': hero_pos[1], 'z': hero_pos[2]},
                                thought='T35 live: ranged adventure spell retry on the hero tile')
                ok = ad.get('status') in ('completed', 'submitted')
            st = state_now()
            hero = hero_of(st)
            check('T35L-4 adventure cast accepted (status completed/submitted)',
                  ok and ad.get('status') != 'unknown',
                  f"spell={adventure_spell.get('name')} status={ad.get('status')} "
                  f"evidence={str(ad.get('evidence'))[:160]}")
            check('T35L-4b adventure cast consumed mana',
                  int(hero.get('mana') or 0) < ad_mana_before,
                  f'mana {ad_mana_before}->{hero.get("mana")} '
                  f'cost={adventure_spell.get("cost")}')
        else:
            check('T35L-4 adventure cast', False, 'no adventure spell known')

        # ---- 5) battle magic: find + engage the weakest visible neutral stack ------------------
        st = state_now()
        hero = hero_of(st)
        if int(hero.get('movement') or 0) < 200:
            # Adventure casting commonly consumes the hero's remaining movement: take a fresh
            # day before walking to the test battle (the isolated run has no other purpose).
            before_day = (st.get('date') or {}).get('day')
            M.game_act('end_turn', {}, thought='T35 live: refresh movement after the adventure cast')
            wait_for(lambda: (state_now().get('date') or {}).get('day') != before_day,
                     timeout=60.0)
            st = state_now()
            hero = hero_of(st)
        creatures = visible_creatures(st, hero)
        check('T35L-5 at least one visible neutral stack to fight',
              bool(creatures), f'visible={len(creatures)}')
        battle_started = False
        recent_queries = 0
        if creatures:
            creatures.sort(key=lambda item: (hover_rank(item[1]), item[0]))
            target_obj = creatures[0][1]
            target_pos = list(target_obj.get('visitable') or target_obj.get('pos') or [0, 0, 0])
            move = M.move_near(0, target_pos[0], target_pos[1], target_pos[2], radius=1)
            # wait for either a battle, an attack query, or a failed move
            deadline = time.time() + 25.0
            while time.time() < deadline:
                st = state_now()
                battle = st.get('battle') or {}
                query = st.get('query')
                if battle.get('active') or (query and query.get('type') in ('yes_no',
                                                                           'select')):
                    break
                if query:
                    break
                if move.get('status') in ('rejected',) and 'no_path' in str(move.get('evidence')):
                    break
                time.sleep(0.6)
            st = state_now()
            query = st.get('query')
            if query:
                # neutral-guard "attack?" / creature-join prompt: answer 1 (fight)
                recent_queries += 1
                M.game_act('answer', {'value': 1},
                           thought='T35 live: answer the guard/join prompt to fight')
                time.sleep(1.0)
            st = state_now()
            battle = st.get('battle') or {}
            if battle.get('active'):
                if battle.get('active_stack') == -1:
                    M.battle_start()
                    time.sleep(1.0)
                st = state_now()
                battle = st.get('battle') or {}
                battle_started = bool(battle.get('active'))
            check('T35L-5b reached a battle (move/guard answered)',
                  battle_started, f'move={move.get("status")} query={recent_queries} '
                                  f'battle={(st.get("battle") or {}).get("active")}')
            if not battle_started:
                check('T35L-5c battle magic', False,
                      f'could not reach a battle this turn: {str(move.get("evidence"))[:200]}')

        if battle_started:
            actions = M.battle_actions()
            hero_block = actions.get('hero') or {}
            spell_rows = [sp for sp in (actions.get('spells') or []) if isinstance(sp, dict)]
            check('T35L-6 battle_actions lists our hero mana/spellbook',
                  isinstance(hero_block.get('mana'), int)
                  and isinstance(hero_block.get('mana_max'), int)
                  and hero_block.get('can_cast') is True,
                  str(hero_block))
            check('T35L-6b battle_actions lists castable spells with target kinds',
                  bool(spell_rows) and all(
                      {'id', 'name', 'cost', 'can_cast', 'target_kind'} <= set(sp)
                      for sp in spell_rows),
                  str(spell_rows[:3]))
            st = state_now()
            battle = st.get('battle') or {}
            mine = battle.get('my_side')
            stacks = [s for s in battle.get('stacks') or [] if isinstance(s, dict)]
            enemies = [s.get('id') for s in stacks if s.get('side') != mine]
            ours = [s.get('id') for s in stacks if s.get('side') == mine]
            damage_spell = pick_battle_spell(spell_rows, DAMAGE_NAMES, ('unit', 'hex', 'none'))
            benefit_spell = pick_battle_spell(spell_rows, BENEFIT_NAMES, ('unit', 'none'))
            mana_before = int((M.game_brief().get('heroes') or [{}])[0].get('mana') or 0)
            round_before = int(battle.get('round') or 0)
            results_by_kind: dict = {}

            def cast(spell_obj: dict, target_kind: str) -> dict:
                cast_args = {'spell': spell_obj['id']}
                if spell_obj.get('target_kind') == 'unit':
                    cast_args['stack'] = ours[0] if target_kind == 'benefit' and ours else (
                        enemies[0] if enemies else None)
                    if cast_args['stack'] is None:
                        cast_args.pop('stack')
                elif spell_obj.get('target_kind') == 'hex' and enemies:
                    st_now = state_now()
                    battle_now = st_now.get('battle') or {}
                    rows_now = [s for s in battle_now.get('stacks') or [] if isinstance(s, dict)]
                    enemy_row = next((s for s in rows_now if s.get('id') == enemies[0]), None)
                    if enemy_row is not None:
                        hexpos = list(enemy_row.get('hex') or [0, 0])
                        cast_args['x'], cast_args['y'] = hexpos[0], hexpos[1]
                return M.game_act('battle_cast_spell', cast_args,
                                  thought=f'T35 live: {target_kind} battle spell smoke test')

            # First cast (one hero spell per combat turn): prefer the beneficial buff, fall back
            # to the damage spell.
            first_kind = 'benefit' if benefit_spell is not None else 'damage'
            first_spell = benefit_spell if benefit_spell is not None else damage_spell
            if first_spell is not None:
                results_by_kind[first_kind] = cast(first_spell, first_kind)
                check(f'T35L-7 {first_kind} battle spell accepted',
                      results_by_kind[first_kind].get('status') in ('completed', 'submitted'),
                      f"spell={first_spell.get('name')} "
                      f"status={results_by_kind[first_kind].get('status')} "
                      f"evidence={str(results_by_kind[first_kind].get('evidence'))[:180]}")
            else:
                check('T35L-7 first battle spell accepted', False,
                      f'no usable battle spell among {[sp.get("name") for sp in spell_rows]}')

            # Second cast needs the next combat round (engine: one hero spell per combat turn).
            other_kind = 'damage' if first_kind == 'benefit' else 'benefit'
            other_spell = damage_spell if other_kind == 'damage' else benefit_spell
            if other_spell is not None:
                st_round = advance_round(rounds=1, deadline=45.0)
                battle_round2 = st_round.get('battle') or {}
                check('T35L-8a battle advanced to a new round for the second cast',
                      int(battle_round2.get('round') or 0) > round_before
                      or not battle_round2.get('active'),
                      f'round {round_before}->{battle_round2.get("round")} '
                      f'active={battle_round2.get("active")}')
                st_turn = wait_for_my_turn(deadline=30.0)
                battle_turn = st_turn.get('battle') or {}
                check('T35L-8a2 our combat turn is active for the second cast',
                      not battle_turn.get('active') or battle_turn.get('active_stack') != -1,
                      f'active_stack={battle_turn.get("active_stack")} '
                      f'my_side={battle_turn.get("my_side")}')
                if battle_turn.get('active'):
                    results_by_kind[other_kind] = cast(other_spell, other_kind)
                    check(f'T35L-8b {other_kind} battle spell accepted in the next round',
                          results_by_kind[other_kind].get('status') in ('completed',
                                                                        'submitted'),
                          f"spell={other_spell.get('name')} "
                          f"status={results_by_kind[other_kind].get('status')} "
                          f"evidence={str(results_by_kind[other_kind].get('evidence'))[:180]}")
                else:
                    check('T35L-8b second battle spell', False,
                          'battle ended before the second round cast')
            else:
                check('T35L-8b second battle spell', False,
                      f'no second spell among {[sp.get("name") for sp in spell_rows]}')

            st = state_now()
            hero_after = hero_of(st)
            battle_after = st.get('battle') or {}
            damage_ok = results_by_kind.get('damage', {}).get('status') in ('completed',
                                                                           'submitted')
            benefit_ok = results_by_kind.get('benefit', {}).get('status') in ('completed',
                                                                             'submitted')
            if battle_after.get('active'):
                check('T35L-9 battle mana/cast counter moved after casting',
                      int(hero_after.get('mana') or 0) < mana_before
                      or int(battle_after.get('cast_spells') or 0) > 0,
                      f'mana {mana_before}->{hero_after.get("mana")} '
                      f'casts={battle_after.get("cast_spells")} '
                      f'battle_active={battle_after.get("active")}')
            else:
                check('T35L-9 battle ended after casting (effect observable via battle end)',
                      damage_ok or benefit_ok, 'battle already over')
            finish_battle()
            went_down = wait_for(lambda: not ((state_now().get('battle') or {}).get('active')),
                                 timeout=25.0)
            st = state_now()
            check('T35L-10 battle resolves and the game continues',
                  went_down and st.get('in_game') is not False,
                  f'in_game={st.get("in_game")} battle={(st.get("battle") or {}).get("active")} '
                  f'status={st.get("status")}')

        # ---- 6) recording identity ------------------------------------------------------------
        rec = M.recording_start(fps=15, height=768)
        check('T35L-11 recording started', rec.get('ok') is True, str(rec.get('error')))
        time.sleep(1.4)
        rec_stop = M.recording_stop()
        health = rec_stop.get('health') or {}
        identity = rec_stop.get('identity') or {}
        check('T35L-11b recording identity binds the new exe sha',
              identity.get('client_sha256') == pinned.get('sha256'),
              f"identity_sha={(identity.get('client_sha256') or '')[:12]}")
        check('T35L-11c recording health samples usable',
              bool(health.get('samples')) and health.get('first_frame_black') is False,
              str({k: health.get(k) for k in
                   ('samples', 'first_frame_black', 'black_ratio')}))
    except Exception as exc:  # noqa: BLE001 - live failures must be reported, not hidden
        check('T35 live smoke completed without an exception', False,
              f'{type(exc).__name__}: {exc}')
    finally:
        try:
            stop = M.session_stop()
            if started:
                check('T35L-12 clean stop (no leftover client)',
                      stop.get('ok') is True and not (stop.get('leftover') or []),
                      str(stop)[:220])
        except Exception as exc:  # noqa: BLE001
            check('T35L-12 clean stop', False, f'{type(exc).__name__}: {exc}')

    failed = [item for item in results if not item[1]]
    print(f'\n{len(results) - len(failed)}/{len(results)} checks passed')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
