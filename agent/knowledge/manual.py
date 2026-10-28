# -*- coding: utf-8 -*-
"""Game manual knowledge base: keyword search over the extracted official
manuals (doc/knowledge/*.md). Feeds the LLM decision loop with rules
(movement, buildings, units, spells, combat, costs).

Usage:
    from agent.knowledge.manual import search
    search("movement points") -> [(source, page, snippet), ...]
"""
from __future__ import annotations
from pathlib import Path

KNOW = Path(__file__).resolve().parents[2] / 'doc' / 'knowledge'


class Manual:
    def __init__(self):
        self.pages = []  # (source, page_no, text)
        for md in sorted(KNOW.glob('*.md')):
            src = md.stem
            cur_page, cur = 0, []
            for line in md.read_text(encoding='utf-8').splitlines():
                if line.startswith('## 第') and line.endswith('页'):
                    if cur:
                        self.pages.append((src, cur_page, '\n'.join(cur)))
                    cur_page = int(line[3:].replace('第', '').replace('页', ''))
                    cur = []
                else:
                    cur.append(line)
            if cur:
                self.pages.append((src, cur_page, '\n'.join(cur)))

    def search(self, *keywords, limit=5) -> list[dict]:
        """Pages containing ALL keywords (case-insensitive), ranked by
        total hit count."""
        kws = [k.lower() for k in keywords]
        scored = []
        for src, page, text in self.pages:
            low = text.lower()
            if not all(k in low for k in kws):
                continue
            score = sum(low.count(k) for k in kws)
            scored.append((score, src, page, text))
        scored.sort(reverse=True)
        return [{'source': s, 'page': p, 'score': sc, 'text': t.strip()[:2500]}
                for sc, s, p, t in scored[:limit]]


_m: Manual | None = None


def search(*keywords, limit=5) -> list[dict]:
    global _m
    if _m is None:
        _m = Manual()
    return _m.search(*keywords, limit=limit)


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    for q in [sys.argv[1] if len(sys.argv) > 1 else 'movement points']:
        print(f'=== 查询: {q} ===')
        for hit in search(*q.split(), limit=3):
            print(f"[{hit['source']} 第{hit['page']}页 score={hit['score']}]")
            print(hit['text'][:400])
            print('---')
