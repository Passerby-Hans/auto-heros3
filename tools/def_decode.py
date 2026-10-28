# -*- coding: utf-8 -*-
"""DEF sprite decoder for HoMM3 (H3sprite.lod / H3ab_spr.lod assets).

Format (community-documented, validated live):
  header: u32 type, u32 fullWidth, u32 fullHeight, u32 groups
  groups: for each: u32 groupId, u32 nFrames,
          frames: char name[12] (ASCIIZ), u32 offset-to-frame-block
  palette: 256 x RGBA(B,G,R,unused) right after the directory
  frame block: u32 pixelDataSize, u32 format, u32 fw, u32 fh,
               u32 w, u32 h, u32 x, u32 y, then rows of RLE
  RLE (type 0x20|0x40 sprite): per row read u32 chunkLen; inside:
      u8 code: 0x80..0xFF -> skip (code&0x7F)+1 px (transparent)
               0x00..0x7F -> copy (code)+1 literal palette indices
"""
import struct, sys
from pathlib import Path
import numpy as np


def decode(data: bytes):
    if data[:4] != b'\x00\x00\x00\x00'[:0]:  # no magic; just parse
        pass
    dtype, fw, fh, ngroups = struct.unpack_from('<IIII', data, 0)
    off = 16
    frames = []
    for _ in range(ngroups):
        gid, nf = struct.unpack_from('<II', data, off); off += 8
        for _ in range(nf):
            name = data[off:off + 12].split(b'\x00')[0].decode('ascii', 'replace')
            foff = struct.unpack_from('<I', data, off + 12)[0]
            frames.append((gid, name, foff))
            off += 16
    pal_off = off
    palette = np.frombuffer(data[pal_off:pal_off + 1024], dtype=np.uint8).reshape(256, 4)
    rgb_pal = np.zeros((256, 3), dtype=np.uint8)
    rgb_pal[:, 0] = palette[:, 2]  # R
    rgb_pal[:, 1] = palette[:, 1]  # G
    rgb_pal[:, 2] = palette[:, 0]  # B

    out = []
    for gid, name, foff in frames:
        (dsize, fmt, Fw, Fh, w, h, x, y) = struct.unpack_from('<IIIIIIII', data, foff)
        pixels = np.zeros((h, w), dtype=np.uint8)  # 255 = transparent marker
        pixels[:] = 255
        p = foff + 32
        for row in range(h):
            chunk = struct.unpack_from('<I', data, p)[0]; p += 4
            end = p + chunk
            cx = 0
            while p < end and cx < w:
                code = data[p]; p += 1
                if code >= 0x80:
                    cx += (code & 0x7F) + 1
                else:
                    n = code + 1
                    pixels[row, cx:cx + n] = data[p:p + n]
                    p += n
                    cx += n
            if cx != w:
                pass  # tolerate short rows
            p = end
        out.append(dict(group=gid, name=name, w=w, h=h, x=x, y=y,
                        fmt=fmt, pixels=pixels, palette=rgb_pal))
    meta = dict(def_type=dtype, full=(fw, fh), nframes=len(out))
    return meta, out


def frame_rgba(frame):
    px = frame['pixels']
    pal = np.vstack([frame['palette'], [[0, 0, 0]]])  # idx 255 -> black
    img = pal[px]
    alpha = np.where(px == 255, 0, 255).astype(np.uint8)
    rgba = np.dstack([img, alpha])
    return rgba


def main():
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from lod_lister import Lod
    lod_name = sys.argv[1]
    fname = sys.argv[2]
    lod = Lod(lod_name)
    data = lod.read(fname)
    meta, frames = decode(data)
    print(meta)
    outdir = Path('var/def_frames') / fname.lower()
    outdir.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    for i, f in enumerate(frames):
        rgba = frame_rgba(f)
        Image.fromarray(rgba, 'RGBA').save(outdir / f"{i:02d}_{f['name']}.png")
        print(f"  [{i}] group={f['group']} name={f['name']!r} {f['w']}x{f['h']} at ({f['x']},{f['y']}) fmt={f['fmt']:#x}")


if __name__ == '__main__':
    main()
