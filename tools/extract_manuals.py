# -*- coding: utf-8 -*-
"""Extract all game manual PDFs to markdown (page-marked) under doc/knowledge/."""
import sys
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
from pypdf import PdfReader

SRC = Path('doc')
DST = Path('doc/knowledge')


def main():
    DST.mkdir(exist_ok=True)
    for pdf in sorted(SRC.glob('游戏手册*.pdf')):
        out = DST / (pdf.stem + '.md')
        if out.exists():
            print(f'skip {out.name}')
            continue
        r = PdfReader(str(pdf))
        parts = [f'# {pdf.stem}\n\n共 {len(r.pages)} 页\n']
        for i, page in enumerate(r.pages):
            try:
                txt = page.extract_text() or ''
            except Exception as e:
                txt = f'[extract error: {e}]'
            txt = txt.strip()
            if txt:
                parts.append(f'\n## 第{i+1}页\n\n{txt}\n')
        out.write_text('\n'.join(parts), encoding='utf-8')
        print(f'{out.name}: {len(r.pages)}页 -> {out.stat().st_size//1024}KB')


if __name__ == '__main__':
    main()
