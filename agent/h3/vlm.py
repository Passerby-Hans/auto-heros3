# -*- coding: utf-8 -*-
"""Vision LLM (GLM-4V series via bigmodel API) for screenshot understanding.

API key is read from the local ZCode config (the user's own key that powers
this tooling). Usage:
    from agent.h3.vlm import vlm_ask
    answer = vlm_ask('var/shots/ingame_full.png', 'where is the end turn button?')
"""
from __future__ import annotations
import base64, json, re, sys
from pathlib import Path
import urllib.request

CONFIG = Path.home() / '.zcode' / 'cli' / 'config.json'
API_URL = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
MODELS = ['glm-4.5v', 'glm-4v-plus', 'glm-4v-flash', 'glm-4v']


def _api_key() -> str:
    cfg = json.loads(CONFIG.read_text(encoding='utf-8'))
    return cfg['mcp']['servers']['zai-mcp-server']['environment']['Z_AI_API_KEY']


def vlm_ask(image_path, prompt: str, model: str | None = None, max_tokens: int = 4096) -> str:
    raw = Path(image_path).read_bytes()
    b64 = base64.b64encode(raw).decode()
    key = _api_key()
    tried = []
    for m in ([model] if model else MODELS):
        body = json.dumps({
            'model': m,
            'messages': [{
                'role': 'user',
                'content': [
                    {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{b64}'}},
                    {'type': 'text', 'text': prompt},
                ]}],
            'max_tokens': max_tokens,
        }).encode()
        req = urllib.request.Request(API_URL, data=body, headers={
            'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.loads(r.read())
            msg = resp['choices'][0]['message']
            content = msg.get('content') or ''
            if not content.strip():
                content = msg.get('reasoning_content') or ''
            return content
        except Exception as e:
            tried.append(f'{m}: {e}')
    raise RuntimeError('all models failed: ' + '; '.join(tried))


def extract_coords(text: str) -> list[tuple[int, int]]:
    """Pull (x, y) integer pairs from VLM answer text."""
    return [(int(a), int(b)) for a, b in re.findall(r'\(?(\d{1,4})\s*[,，]\s*(\d{1,4})\)?', text)]


if __name__ == '__main__':
    img = sys.argv[1]
    q = sys.argv[2] if len(sys.argv) > 2 else 'Describe this game screen in detail.'
    print(vlm_ask(img, q))
