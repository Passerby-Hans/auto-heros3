# -*- coding: utf-8 -*-
"""Game state extraction from Heroes3.exe memory (fair-play scope).

FAIRNESS RULE (user requirement): only player-visible data may be exposed.
Enemy info under fog of war must be filtered via the game's own visibility
flags before inclusion.

Verified anchors (SoD 3.1 CN, no ASLR):
  resources : [0x82b0bc] -> player obj, +0x9c (7 x i32, rw-verified)
  day       : [0x8322a0] (.data global; week/month derived)
  hero list : [0x818068..] stride 8, hero obj ptrs (8 slots seen)
  hero      : obj 0x327000e for Octavia:
              +0x406 packed stats bytes (atk, def, pow, kng)
              +0x445 GBK name; movement lives in map-obj region:
              [0x3143cfc]=current(1560) [0x3143d00]=base(1500)
  map header: 0x3143d20: w=36,h=36 + tile-array ptrs (0x31e74b0 etc.)
  town names: heap block 0x332c3c0 area = all map town/hero GBK names
"""
from __future__ import annotations
import struct

RES_ROOT_PTR = 0x82b0bc
RES_OFFSET = 0x9C
DAY_ADDR = 0x8322a0
RES_NAMES = ('wood', 'mercury', 'ore', 'sulfur', 'crystal', 'gems', 'gold')

HERO_LIST = 0x818068          # 8-byte stride pointer array
HERO_LIST_MAX = 16
HERO_STATS_OFF = 0x406        # 4 packed bytes at hero+0x406
HERO_NAME_OFF = 0x445         # GBK ASCIIZ
MAP_W_ADDR = 0x3143d20
MOVE_CUR = 0x3143cfc
MOVE_BASE = 0x3143d00


class GameState:
    def __init__(self, proc):
        self.p = proc

    def resources(self) -> dict:
        root = struct.unpack('<I', self.p.read(RES_ROOT_PTR, 4))[0]
        vals = struct.unpack('<7i', self.p.read(root + RES_OFFSET, 28))
        return dict(zip(RES_NAMES, vals))

    def day(self) -> int:
        return self.p.i32(DAY_ADDR)

    def date(self) -> dict:
        d = self.day()
        return {'total_day': d,
                'week': (d - 1) // 7 + 1,
                'week_day': (d - 1) % 7 + 1,
                'month': (d - 1) // 28 + 1}

    def heroes(self) -> list[dict]:
        out = []
        for i in range(HERO_LIST_MAX):
            ptr = struct.unpack('<I', self.p.try_read(HERO_LIST + i * 8, 4) or b'\0\0\0\0')[0]
            if not (0x01000000 <= ptr < 0x10000000):
                continue
            stats = self.p.try_read(ptr + HERO_STATS_OFF, 4)
            name_raw = self.p.try_read(ptr + HERO_NAME_OFF, 32)
            if not stats or not all(0 <= b <= 30 for b in stats):
                continue  # empty/template slot (only live heroes carry valid packed stats)
            hero = {'ptr': hex(ptr),
                    'stats': {'attack': stats[0], 'defense': stats[1],
                              'power': stats[2], 'knowledge': stats[3]}}
            if name_raw:
                nm = name_raw.split(b'\x00')[0]
                try:
                    hero['name'] = nm.decode('gbk')
                except UnicodeDecodeError:
                    continue
                if not hero['name']:
                    continue
            out.append(hero)
        return out

    def map_info(self) -> dict:
        w = self.p.i32(MAP_W_ADDR)
        h = self.p.i32(MAP_W_ADDR + 4)
        return {'width': w, 'height': h}

    def hero_movement(self) -> dict:
        return {'current': self.p.i32(MOVE_CUR), 'base': self.p.i32(MOVE_BASE)}

    def snapshot(self) -> dict:
        return {
            'date': self.date(),
            'resources': self.resources(),
            'map': self.map_info(),
            'hero_movement': self.hero_movement(),
            'heroes': self.heroes(),
        }


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    import json
    from agent.h3.mem import Process
    gs = GameState(Process.attach())
    print(json.dumps(gs.snapshot(), ensure_ascii=False, indent=1))
