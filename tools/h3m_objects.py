# -*- coding: utf-8 -*-
"""Dump object templates + placed object instances from a Heroes 3 .h3m map.

Why this exists
---------------
The agent bridge/state only reveals objects the player has seen, and some maps gate progress
behind objects far away (e.g. "Key to Victory" hides the RED keymaster tent underground while the
RED border guard blocks the only surface pass). Parsing the map file directly answered that in
seconds.

Format notes (verified against VCMI lib/mapping + lib/mapObjects/ObjectTemplate.cpp, SoD 0x1c):
  * terrain section : 7 bytes per tile, [z][y][x]  -> 7*w*h*levels bytes
  * template section: u32 count, then per template
        u32 nameLen + name bytes        ("animation" file, e.g. AVXkey20.def)
        u8 blockMask[6] + u8 visitMask[6]
        u16 unused + u16 terrainMask
        u32 objectId + u32 subId
        u8 type + u8 printPriority
        16 zero bytes
      => record size = 42 + nameLen
  * instance section: u32 count, then per instance
        int3 position (x,y,z as u8) + u32 templateIndex + 5 bytes customization (usually 0)
        + object-type-specific payload (variable) -- which is why instances are found by scanning
          for (x,y,z, templateIndex, five zero bytes) instead of advancing sequentially.

Usage:
  python -X utf8 tools/h3m_objects.py "C:\\path\\map.h3m"                # all objects
  python -X utf8 tools/h3m_objects.py map.h3m --id 9 --id 10            # border guards + tents
  python -X utf8 tools/h3m_objects.py map.h3m --name-contains AVXkey    # by template def name

Object ids (VCMI Obj enum): BORDERGUARD=9, KEYMASTER=10, HERO=34, PRISON=62, TOWN=98,
SUBTERRANEAN_GATE=103, BORDER_GATE=212, QUEST_GUARD=215.
"""
from __future__ import annotations

import argparse
import gzip
import json
import struct
import sys
from pathlib import Path

TERRAIN_BYTES_PER_TILE = 7
TEMPLATE_FIXED_BYTES = 42          # name string excluded
MAX_TEMPLATES = 5000
MAX_OBJECTS = 200000


def decompress(raw: bytes) -> bytes:
    if raw[:2] == b'\x1f\x8b':
        return gzip.decompress(raw)
    return raw


def _read_template(data: bytes, off: int):
    if off + TEMPLATE_FIXED_BYTES > len(data):
        return None
    name_len = struct.unpack_from('<I', data, off)[0]
    if not 1 <= name_len <= 64 or off + 4 + name_len + TEMPLATE_FIXED_BYTES > len(data):
        return None
    name = data[off + 4:off + 4 + name_len]
    if any(c < 32 or c > 126 for c in name):
        return None
    p = off + 4 + name_len
    object_id, sub_id = struct.unpack_from('<II', data, p + 16)
    obj_type, priority = data[p + 24], data[p + 25]
    if object_id > 400 or sub_id > 600 or obj_type > 8 or priority > 32:
        return None
    if data[p + 26:p + 42] != b'\x00' * 16:
        return None
    return {
        'next': p + TEMPLATE_FIXED_BYTES,
        'name': name.decode('latin1'),
        'id': object_id,
        'sub': sub_id,
        'blockMask': list(data[p:p + 6]),
        'visitMask': list(data[p + 6:p + 12]),
    }


def find_templates(data: bytes):
    """Locate the template section: u32 count + `count` parseable templates whose end looks
    like the objects section (u32 object count + a plausible first instance record)."""
    for start in range(0, len(data) - 60):
        count = struct.unpack_from('<I', data, start)[0]
        if not 20 <= count <= MAX_TEMPLATES:
            continue
        off = start + 4
        templates = []
        for index in range(count):
            tmpl = _read_template(data, off)
            if tmpl is None:
                break
            tmpl['index'] = index
            templates.append(tmpl)
            off = tmpl['next']
        if len(templates) != count:
            continue
        # objects section must start right after the templates and look sane
        if off + 16 > len(data):
            continue
        object_count = struct.unpack_from('<I', data, off)[0]
        if not 10 <= object_count <= MAX_OBJECTS:
            continue
        x, y, z = data[off + 4], data[off + 5], data[off + 6]
        first_template = struct.unpack_from('<I', data, off + 7)[0]
        if x > 40 or y > 40 or z > 1 or first_template >= count:
            continue
        return templates, off
    raise SystemExit('template section not found (not a SoD/AB H3M?)')


def scan_instances(data: bytes, templates: list[dict], map_size: int = 144) -> list[dict]:
    """Find placed objects by the (x, y, z, templateIndex, 00000) record signature.

    Object anchors may sit up to 8 tiles outside the playable area (VCMI tolerates that), so the
    bounds check is `size + 8` and is mainly there to reject random byte patterns.
    """
    count = len(templates)
    limit = map_size + 8
    hits, seen = [], set()
    for i in range(0, len(data) - 12):
        x, y, z = data[i], data[i + 1], data[i + 2]
        if x >= limit or y >= limit or z > 1:
            continue
        index = struct.unpack_from('<I', data, i + 3)[0]
        if index >= count:
            continue
        if data[i + 7:i + 12] != b'\x00' * 5:
            continue
        tmpl = templates[index]
        key = (x, y, z, tmpl['id'], tmpl['sub'])
        if key in seen:
            continue
        seen.add(key)
        hits.append({'x': x, 'y': y, 'z': z, 'template': index,
                     'id': tmpl['id'], 'sub': tmpl['sub'], 'def': tmpl['name']})
    return hits


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Dump objects of a Heroes 3 .h3m map')
    ap.add_argument('map', help='path to .h3m')
    ap.add_argument('--id', type=int, action='append', default=[], help='filter object id (repeatable)')
    ap.add_argument('--name-contains', default=None, help='filter by template def name substring')
    ap.add_argument('--json', action='store_true', help='print JSON')
    ap.add_argument('--templates', action='store_true', help='dump the template table')
    args = ap.parse_args(argv)

    data = decompress(Path(args.map).read_bytes())
    templates, objects_offset = find_templates(data)
    map_size = data[5] if 10 <= data[5] <= 252 else 144   # header byte: map side length
    instances = scan_instances(data, templates, map_size)
    if args.id:
        instances = [o for o in instances if o['id'] in args.id]
    if args.name_contains:
        needle = args.name_contains.lower()
        instances = [o for o in instances if needle in o['def'].lower()]
    instances.sort(key=lambda o: (o['z'], o['y'], o['x']))

    if args.templates:
        payload = templates if args.json else [
            f"{t['index']:4d} id={t['id']:3d} sub={t['sub']:2d} {t['name']}" for t in templates]
        print(json.dumps(payload, ensure_ascii=False) if args.json else '\n'.join(payload))
        return 0
    if args.json:
        print(json.dumps({'templates_count': len(templates), 'objects_at': objects_offset,
                          'objects': instances}, ensure_ascii=False, indent=1))
    else:
        print(f'# templates={len(templates)}  instances={len(instances)}  '
              f'(objects section around offset {objects_offset})')
        for o in instances:
            print(f"id={o['id']:3d} sub={o['sub']:2d} z={o['z']} "
                  f"({o['x']:2d},{o['y']:2d}) tmpl={o['template']:3d} {o['def']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
