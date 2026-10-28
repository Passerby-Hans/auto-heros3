# -*- coding: utf-8 -*-
"""List/extract entries in HoMM3 .lod archives (tolerant parser).

Standard format: header at 0x200 = count * records of
    name ASCIIZ (12-byte field, 8.3 names) + off + size + type + csize  = 28 bytes
This Chinese-localized rebuild extends the name field to 16 bytes when a
name needs 9..12 chars, making those records 32 bytes. We walk adaptively
and validate each hop.
"""
import struct, sys, zlib, fnmatch, os
from collections import Counter


class Lod:
    def __init__(self, path):
        self.path = path
        self.blob = open(path, 'rb').read()
        if self.blob[:4] != b'LOD\x00':
            raise ValueError(f"not a LOD: {path}")
        self.version, self.count = struct.unpack_from('<II', self.blob, 4)
        self.entries = []
        pos = 0x200
        blob = self.blob
        for i in range(self.count):
            ne = pos
            while ne < pos + 16 and blob[ne] != 0:
                ne += 1
            name = blob[pos:ne].decode('ascii', 'replace')
            if not name or not name[0].isalnum():
                break  # phantom/empty tail slot: real entries end here
            field = None
            for cand in range(((ne + 3) & ~3), ((ne + 3) & ~3) + 13, 4):
                off, size, etype, csize = struct.unpack_from('<IIII', blob, cand)
                if not (0x200 <= off < len(blob) and 0 < size < 0x4000000):
                    continue
                nxt = cand + 16
                if i + 1 < self.count and not (self._name_like(nxt) or self._zero_pad(nxt)):
                    continue
                field = cand - pos
                self.entries.append({'name': name, 'offset': off, 'size': size,
                                     'type': etype, 'csize': csize})
                break
            if field is None:
                # resync: scan ahead for the next plausible record start
                resync = None
                for p in range(pos + 1, pos + 128):
                    if (self._name_like(p) and p + 16 + 16 <= len(blob)):
                        off, size, etype, csize = struct.unpack_from('<IIII', blob, p + 12)
                        if 0x200 <= off < len(blob) and 0 < size < 0x4000000:
                            resync = p
                            break
                if resync is None:
                    print(f"[warn] stop at entry {i} {name!r} {pos:#x}: no resync")
                    break
                print(f"[warn] resync at entry {i} {name!r}: {pos:#x} -> {resync:#x}")
                pos = resync
                continue
            pos += field + 16
        self.dir_end = pos

    def _name_like(self, p):
        blob = self.blob
        if not (0x20 <= blob[p] < 0x7f):
            return False
        for j in range(p, min(p + 17, len(blob))):
            if blob[j] == 0:
                return all(0x20 <= b < 0x7f for b in blob[p:j])
        return False

    def _zero_pad(self, p):
        return all(b == 0 for b in self.blob[p:p + 12])

    def raw(self, e):
        n = e['csize'] or e['size']
        return self.blob[e['offset']:e['offset'] + n]

    def read(self, name):
        for e in self.entries:
            if e['name'].lower() == name.lower():
                data = self.raw(e)
                if data[:2] == b'x\x9c':
                    return zlib.decompress(data)
                return data
        raise KeyError(name)

    def stats(self):
        t = Counter(e['type'] for e in self.entries)
        ext = Counter(os.path.splitext(e['name'])[1].lower() for e in self.entries)
        return t, ext


def main():
    lod = Lod(sys.argv[1])
    print(f"{sys.argv[1]}: version={lod.version} count={lod.count} dir_end={lod.dir_end:#x}")
    t, ext = lod.stats()
    print("type histogram:", dict(t.most_common(10)))
    print("extensions    :", dict(ext.most_common(15)))
    if len(sys.argv) > 2:
        pat = sys.argv[2].lower()
        hits = [e for e in lod.entries if fnmatch.fnmatch(e['name'].lower(), pat)]
        print(f"matches for {pat!r}: {len(hits)}")
        for e in hits[:40]:
            print(f"  type={e['type']:#04x} size={e['size']:8d} csize={e['csize']:8d}  {e['name']}")
    # verify EVERY entry's data magic + a sample decompress
    bad = 0
    sample_ok = 0
    for i, e in enumerate(lod.entries):
        head = lod.raw(e)[:2]
        if e['type'] == 0x10 and head != b'x\x9c':
            bad += 1
            if bad <= 3:
                print(f"[warn] {e['name']}: type=0x10 but head={head!r}")
        if i % max(1, lod.count // 10) == 0:
            try:
                d = lod.read(e['name'])
                assert len(d) == e['size']
                sample_ok += 1
            except Exception as ex:
                print(f"[FAIL] {e['name']}: {ex}")
    print(f"magic check: bad={bad}/{lod.count}, sampled decompress ok={sample_ok}")


if __name__ == '__main__':
    main()
