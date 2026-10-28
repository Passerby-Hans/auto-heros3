# -*- coding: utf-8 -*-
"""Debug prep v3: push the town building chain toward tier 6/7 dwellings, recruit at week
growth, and merge everything into the main hero.  Never touches battles.

Usage: python tools/dev_prep3.py --target-day 22 [--dir var/agent_mcp_v2]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from vcmi_agent import VcmiAgent  # noqa: E402

# Support buildings that unlock higher-tier dwellings come first, then anything cheap.
SUPPORT = ('物资', '铁匠', '行会', '议会', '城堡', '大本营', '图书馆', '市场', '智慧', '了望')


def cost(option: dict) -> int:
    return sum(int(v) for v in (option.get('cost') or {}).values())


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
        options = [o for o in town.get('build_options') or []
                   if int(o.get('id') or -1) not in built]

        def rank(option: dict) -> tuple:
            name = option.get('name') or ''
            support = 0 if any(word in name for word in SUPPORT) else 1
            return (support, cost(option))

        for option in sorted(options, key=rank)[:6]:
            detail = self.send(f"build 0 {int(option['id'])}")
            if 'OK' in detail or 'COMPLETED' in detail:
                return f"{option.get('name')}#{option['id']}"
        return '-'

    def recruit_all(self) -> int:
        st = self.state()
        total = 0
        for dwelling in (st.get('towns') or [{}])[0].get('dwellings') or []:
            level = dwelling.get('level')
            available = int(dwelling.get('available') or 0)
            if level and available > 0:
                detail = self.send(f"recruit 0 {level} {available}")
                if 'OK' in detail or 'COMPLETED' in detail:
                    total += available
        return total

    def merge_all(self) -> int:
        st = self.state()
        hero_slots = {s['slot']: s['creature'] for s in (st.get('heroes') or [{}])[0].get('army') or []}
        by_creature = {creature: slot for slot, creature in hero_slots.items()}
        moved = 0
        for row in (st.get('towns') or [{}])[0].get('garrison') or []:
            slot, creature = row.get('slot'), row.get('creature')
            if slot is None:
                continue
            if creature in by_creature:
                detail = self.send(f"merge town 0 {slot} hero 0 {by_creature[creature]}")
            else:
                free = [i for i in range(7) if i not in hero_slots]
                if not free:
                    break
                detail = self.send(f"transfer town 0 {slot} hero 0 {free[0]}")
                if 'OK' in detail or 'COMPLETED' in detail:
                    hero_slots[free[0]] = creature
                    by_creature[creature] = free[0]
            if 'OK' in detail or 'COMPLETED' in detail:
                moved += 1
        return moved

    def run(self, target_day: int) -> dict:
        while self.day(self.state()) < target_day:
            st = self.state()
            if (st.get('battle') or {}).get('active') or st.get('query'):
                self.send('answer 0')
                time.sleep(1)
                st = self.state()
            day = self.day(st)
            built = self.build_best(st)
            st = self.state()
            print(f'[prep3] day={day} build={built} army={self.army(st)} '
                  f'built={len(((st.get("towns") or [{}])[0].get("built")) or [])}', flush=True)
            self.send('end_turn')
            deadline = time.time() + 30
            while time.time() < deadline and self.day(self.state()) == day:
                time.sleep(0.5)
        recruited = self.recruit_all()
        merged = self.merge_all()
        st = self.state()
        summary = {'day': self.day(st), 'army': self.army(st), 'recruited': recruited,
                   'merged': merged}
        print('[prep3] done', summary, flush=True)
        return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--target-day', type=int, default=22)
    ap.add_argument('--dir', default=str(ROOT / 'var' / 'agent_mcp_v2'))
    args = ap.parse_args()
    Prep(Path(args.dir)).run(args.target_day)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
