# -*- coding: utf-8 -*-
"""Minimal H3M map header parser (RoE/AB/SoD) for reconnaissance.

Empirically verified header on the CN Complete install:
  u32 version     (0x0e RoE / 0x15 AB / 0x1c SoD)
  u8  anyHuman
  u8  mapDimension (36/72/108/144 - S/M/L/XL)
  u8  hasUnderground
  u8  unknown x2
  u32 nameLen + GBK bytes, u32 descLen + GBK bytes, u8 difficulty, ...
File itself is gzip-compressed.
"""
import struct, sys, gzip

VER = {0x0e: 'RoE', 0x15: 'AB', 0x1c: 'SoD', 0x1d: 'HotA?', 0x33: 'WoG'}


class R:
    def __init__(self, b):
        self.b = b
        self.p = 0

    def u8(self):
        v = self.b[self.p]; self.p += 1; return v

    def u32(self):
        v = struct.unpack_from('<I', self.b, self.p)[0]; self.p += 4; return v

    def raw(self, n):
        v = self.b[self.p:self.p + n]; self.p += n; return v

    def strn(self):
        n = self.u32()
        raw = self.raw(n)
        for enc in ('gbk', 'cp1251', 'utf-8', 'latin1'):
            try:
                return raw.decode(enc), enc
            except UnicodeDecodeError:
                continue
        return raw.decode('latin1'), 'fallback'


def parse(path, brief=False):
    data = open(path, 'rb').read()
    if data[:2] == b'\x1f\x8b':
        data = gzip.decompress(data)
    r = R(data)
    ver = r.u32()
    human = r.u8()
    dim = r.u8()
    underground = r.u8()
    # adaptive: locate name via plausible (len, text, descLen) triple
    name = desc = None
    enc = '?'
    for probe in range(5, 40):
        try:
            rr = R(data)
            rr.p = probe
            nlen = rr.u32()
            if not (1 <= nlen <= 512):
                continue
            raw = rr.raw(nlen)
            try:
                cand = raw.decode('gbk')
            except UnicodeDecodeError:
                continue
            dlen = rr.u32()
            if not (0 <= dlen <= 8192) or probe + 8 + nlen + dlen > len(data):
                continue
            name, enc = cand, 'gbk'
            desc = rr.raw(dlen).decode('gbk', 'replace')
            break
        except (IndexError, struct.error):
            continue
    if name is None:
        raise ValueError('header scan failed')
    if brief:
        return dict(path=path, ver=ver, dim=dim, underground=underground,
                    name=name, enc=enc)
    if brief:
        return dict(path=path, ver=ver, dim=dim, underground=underground,
                    name=name, enc=enc, diff=diff)
    print(f"{path}")
    print(f"  format={VER.get(ver, hex(ver))} size={dim}x{dim} underground={underground} enc={enc}")
    print(f"  name: {name!r}")
    print(f"  desc: {desc[:70]!r}")
    return None


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--all':
        import glob, collections
        stats = collections.Counter()
        rows = []
        for p in glob.glob(sys.argv[2] + '/**/*.h3m', recursive=True):
            try:
                info = parse(p, brief=True)
                rows.append(info)
                stats[(VER.get(info['ver'], '?'), f"{info['dim']}")] += 1
            except Exception as e:
                print(f"[FAIL] {p}: {e}")
        print("format/size distribution:", dict(stats))
        for i in sorted(rows, key=lambda x: (x['ver'], x['dim'], x['name']))[:200]:
            print(f"  {VER.get(i['ver'],i['ver']):4s} {i['dim']:3d} {'UG' if i['underground'] else '--'} {i['name']}")
    else:
        for p in sys.argv[1:]:
            try:
                parse(p)
            except Exception as e:
                print(f"{p}: FAILED: {e}")
