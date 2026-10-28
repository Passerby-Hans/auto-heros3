# -*- coding: utf-8 -*-
"""One-shot: hide VCMI's green in-game debug console when the agent bridge is active."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
cpp = ROOT / 'vendor' / 'vcmi-src' / 'client' / 'adventureMap' / 'CInGameConsole.cpp'
text = cpp.read_text(encoding='utf-8')
if 'AgentBridge.h' not in text:
    text = text.replace('#include "CInGameConsole.h"',
                        '#include "CInGameConsole.h"\n#include "../AgentBridge.h"', 1)
    old_show = 'void CInGameConsole::show(Canvas & to)\n{\n'
    new_show = ('void CInGameConsole::show(Canvas & to)\n{\n'
                '\tif (AgentBridge::enabled())\n'
                '\t\treturn; // agent mode: hide debug log overlay\n')
    if old_show not in text:
        raise SystemExit('show() anchor not found')
    text = text.replace(old_show, new_show, 1)
    cpp.write_text(text, encoding='utf-8')
    print('patched CInGameConsole.cpp')
else:
    print('CInGameConsole.cpp already patched')

apply_py = ROOT / 'platform' / 'vcmi-patch' / 'apply.py'
body = apply_py.read_text(encoding='utf-8')
if 'patch_agent_ui' not in body:
    step = '''def patch_agent_ui() -> None:
    """Agent mode: hide the green in-game debug console from screenshots/recordings."""
    cpp = TARGET / 'client' / 'adventureMap' / 'CInGameConsole.cpp'
    patch_text(cpp,
               '#include "CInGameConsole.h"',
               '#include "CInGameConsole.h"\\n#include "../AgentBridge.h"',
               'agent bridge include')
    patch_text(cpp,
               'void CInGameConsole::show(Canvas & to)\\n{\\n',
               'void CInGameConsole::show(Canvas & to)\\n{\\n'
               '\\tif (AgentBridge::enabled())\\n\\t\\treturn; // agent mode: hide debug log overlay\\n',
               'agent console hide')


def main() -> int:'''
    body = body.replace('def main() -> int:', step, 1)
    body = body.replace('    patch_debug_cheat()\n',
                        '    patch_debug_cheat()\n    patch_agent_ui()\n', 1)
    apply_py.write_text(body, encoding='utf-8')
    print('patched apply.py')
else:
    print('apply.py already patched')

