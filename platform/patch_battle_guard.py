from pathlib import Path

p = Path('platform/vcmi-patch/client/AgentBridge.cpp')
s = p.read_text(encoding='utf-8')
if 'ERR not_my_turn' not in s:
    s = s.replace(
        '\tconst CStack * active = nullptr;\n',
        '\tconst CStack * active = nullptr;\n\tbool mine = false; // true when the active stack belongs to our side\n',
        1)
    s = s.replace(
        '\t\tconst int activeId = battle->getBattle()->getActiveStackID();\n'
        '\t\tactive = battle->battleGetStackByID(activeId, true);\n'
        '\t\treturn active != nullptr;\n',
        '\t\tconst int activeId = battle->getBattle()->getActiveStackID();\n'
        '\t\tactive = battle->battleGetStackByID(activeId, true);\n'
        '\t\tif (!active)\n'
        '\t\t\treturn false;\n'
        '\t\t// Never send battle actions while the other side is active: doing so corrupts the\n'
        '\t\t// server-side battle state machine (the battle then waits forever for the AI move).\n'
        '\t\tif (auto cb = localCallback())\n'
        '\t\t{\n'
        '\t\t\tif (active->owner)\n'
        '\t\t\t{\n'
        '\t\t\t\tfor (const auto * hero : cb->getHeroesInfo())\n'
        '\t\t\t\t\tif (hero == active->owner)\n'
        '\t\t\t\t\t\tmine = true;\n'
        '\t\t\t\tif (!mine)\n'
        '\t\t\t\t\tfor (const auto * town : cb->getTownsInfo(true))\n'
        '\t\t\t\t\t\tif (town == active->owner)\n'
        '\t\t\t\t\t\t\tmine = true;\n'
        '\t\t\t}\n'
        '\t\t}\n'
        '\t\treturn true;\n',
        1)
    guard = ('\tif (!ctx.acquire())\n'
             '\t{\n'
             '\t\tappendResult("ERR no_active_stack");\n'
             '\t\treturn;\n'
             '\t}\n')
    guarded = (guard +
               '\tif (!ctx.mine)\n'
               '\t{\n'
               '\t\tappendResult("ERR not_my_turn");\n'
               '\t\treturn;\n'
               '\t}\n')
    count = s.count(guard)
    s = s.replace(guard, guarded)
    p.write_text(s, encoding='utf-8')
    print('patched battle guard on', count, 'call sites')
else:
    print('already patched')
