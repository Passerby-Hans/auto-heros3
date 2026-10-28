# -*- coding: utf-8 -*-
"""Debug build-up helper: spend cheated resources on dwellings and recruit a big army.

Preparation only - never touches battles.  Each day it builds one structure (preferring
creature dwellings, then the cheapest available building to unlock prerequisites), recruits
every available creature and moves the garrison into the main hero, then ends the turn.

Usage:
    python tools/dev_prep.py --days 21 [--dir var/agent_mcp_v2]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from vcmi_agent import VcmiAgent  # noqa: E402

DWELLING_IDS = set(range(30, 40))          # H3 dwelling slots (levels 1-7 + upgrades)
DWELLING_WORDS = ('巢', '穴', '阁', '柱', '塔', '洞', '栏', '房', '殿', '厅', '营', '场',
                  '厂', '门', '坛', '树', '屋', '窟', '室', '殿', '龙', '凤', '兽')


def total_cost(option: dict) -> int:
    return sum(int(v) for v in (option.get('cost') or {}).values())


def is_dwelling(option: dict) -> bool:
    if int(option.get('id') or 0) in DWELLING_IDS:
        return True
    name = option.get('name') or ''
    return any(word in name for word in DWELLING_WORDS)


def candidates(options: list[dict], built: set[int]) -> list[dict]:
    fresh = [o for o in options if int(o.get('id') or -1) not in built]
    dwellings = [o for o in fresh if is_dwelling(o)]
    others = [o for o in fresh if not is_dwelling(o)]
    # Cheapest dwelling first (unlocks growth now), then cheapest structure for prerequisites.
    return sorted(dwellings, key=total_cost) + sorted(others, key=total_cost)


class Prep:
    def __init__(self, state_dir: Path):
        self.agent = VcmiAgent(str(state_dir), log=False)
        self.agent.session = self.agent._load_session()

    def send(self, line: str, timeout: float = 25.0) -> str:
        return self.agent.execute(line, verify=None, result_timeout=timeout).line

    def state(self) -> dict:
        return self.agent.state(timeout=10)

    @staticmethod
    def day(st: dict) -> int:
        return int((st.get('date') or {}).get('day') or 0)

    @staticmethod
    def army(st: dict) -> int:
        return sum(int(s.get('count') or 0) for h in st.get('heroes') or []
                   for s in h.get('army') or [])

    def build_best(self, st: dict) -> str:
        town = (st.get('towns') or [{}])[0]
        built = set(town.get('built') or [])
        for option in candidates(town.get('build_options') or [], built)[:6]:
            detail = self.send(f"build 0 {int(option['id'])}")
            if ' OK ' in f' {detail} ' or 'COMPLETED' in detail:
                return f"{option.get('name')}#{option['id']}"
            if 'cannot_build' in detail and 'state=4' in detail:
                continue
            return f"{option.get('name')}#{option['id']} FAIL {detail[:30]}"
        return '-'

    def recruit_all(self, st: dict) -> int:
        total = 0
        town = (st.get('towns') or [{}])[0]
        for dwelling in town.get('dwellings') or []:
            level = dwelling.get('level')
            available = int(dwelling.get('available') or 0)
            if not level or available <= 0:
                continue
            detail = self.send(f"recruit 0 {level} {available}")
            if 'OK' in detail or 'COMPLETED' in detail:
                total += available
        return total

    def transfer_all(self, st: dict) -> int:
        moved = 0
        town = (st.get('towns') or [{}])[0]
        for slot in [g.get('slot') for g in town.get('garrison') or []]:
            if slot is None:
                continue
            for dst in range(7):
                detail = self.send(f"transfer town 0 {slot} hero 0 {dst}")
                if 'OK' in detail or 'COMPLETED' in detail:
                    moved += 1
                    break
                if 'src_slot_empty' in detail:
                    break
        return moved

    def wait_next_day(self, day: int) -> None:
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.day(self.state()) != day:
                return
            time.sleep(0.5)
        # A modal window can block turn end; close it and retry once.
        self.send('close_window')
        self.send('end_turn')
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.day(self.state()) != day:
                return
            time.sleep(0.5)

    def run(self, days: int) -> dict:
        day0 = self.day(self.state())
        print(f'[prep] start day={day0} army={self.army(self.state())}', flush=True)
        while self.day(self.state()) - day0 < days:
            st = self.state()
            if (st.get('battle') or {}).get('active') or st.get('query'):
                print('[prep] interrupted by battle/query', flush=True)
                break
            day = self.day(st)
            built = self.build_best(st)
            recruited = self.recruit_all(self.state())
            moved = self.transfer_all(self.state())
            st = self.state()
            print(f'[prep] day={day} build={built} recruit={recruited} transfer={moved} '
                  f'army={self.army(st)} stacks={len((st.get("heroes") or [{}])[0].get("army") or [])} '
                  f'gold={(st.get("resources") or {}).get("gold")}', flush=True)
            self.send('end_turn')
            self.wait_next_day(day)
        st = self.state()
        summary = {'day': self.day(st), 'army': self.army(st),
                   'stacks': len((st.get('heroes') or [{}])[0].get('army') or []),
                   'gold': (st.get('resources') or {}).get('gold'),
                   'built': len(((st.get('towns') or [{}])[0].get('built')) or [])}
        (ROOT / 'var' / 'agent_mcp_v2' / 'prep_summary.json').write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        print('[prep] done', json.dumps(summary, ensure_ascii=False), flush=True)
        return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=21)
    ap.add_argument('--dir', default=str(ROOT / 'var' / 'agent_mcp_v2'))
    args = ap.parse_args()
    Prep(Path(args.dir)).run(args.days)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
