# -*- coding: utf-8 -*-
"""Bridge action table: action verb -> (bridge command, ordered argument names).

B0.3: moved verbatim out of ``src/auto_heros3/mcp/server.py`` (pure data, no module
state).  Tool arguments arrive as a JSON object, so the bridge command line is built
from this table instead of relying on dict insertion order.
"""
from __future__ import annotations

# action -> (bridge command, ordered argument names).  Tool args arrive as a JSON object, so the
# command line must be built from this spec instead of relying on dict insertion order.
ACTION_SPECS = {
    'move': ('move_hero', ('hero', 'x', 'y', 'z')),
    'move_hero': ('move_hero', ('hero', 'x', 'y', 'z')),
    'build': ('build', ('town', 'id')),
    'recruit': ('recruit', ('town', 'level', 'amount')),
    'recruit_hero': ('recruit_hero', ('town', 'index')),
    # T40/C11: open the real hero screen (CPlayerInterface::openHeroWindow -> CHeroWindow).  The
    # window is left OPEN so a demo can show the equipment/paper-doll; close it with close_window
    # (the bridge accepts CHeroWindow in its close_window_safe list).
    'open_hero': ('open_hero', ('hero',)),
    'transfer': ('transfer', ('src', 'src_index', 'src_slot', 'dst', 'dst_index', 'dst_slot')),
    'merge': ('merge', ('src', 'src_index', 'src_slot', 'dst', 'dst_index', 'dst_slot')),
    'swap_stacks': ('swap_stacks', ('owner', 'index', 'slot_a', 'slot_b')),
    'split': ('split', ('owner', 'index', 'slot', 'amount')),
    'upgrade': ('upgrade', ('owner', 'index', 'slot', 'creature')),
    'market_trade': ('market_trade', ('town', 'trade_mode', 'sell', 'buy', 'amount')),
    'answer': ('answer', ('value',)),
    'end_turn': ('end_turn', ()),
    # T35: hero magic.  cast_spell is the adventure-map spellbook request (optional x,y,z target);
    # battle_cast_spell is the battle spellbook request (optional unit stack or hex target).
    'cast_spell': ('cast_spell', ('hero', 'spell')),
    'battle_cast_spell': ('battle_cast_spell', ('spell',)),
    'battle_attack': ('battle_attack', ('stack',)),
    # Explicit move-towards-without-attacking (the old battle_attack silently did this).
    'battle_approach': ('battle_approach', ('target',)),
    'battle_move': ('battle_move', ('x', 'y')),
    'battle_defend': ('battle_defend', ()),
    'battle_wait': ('battle_wait', ()),
    'battle_result_ok': ('battle_result_ok', ()),
    'confirm_deployment': ('key return', ()),
    'close_window': ('close_window_safe', ()),
    'center_hero': ('center_hero', ()),
    # UI clicks for client-side dialogs the bridge does not expose as server queries
    # (shipyard, quest windows, battle dialogs).  Coordinates are the same event-dispatcher
    # coordinates the ui_tree dump uses.  It is a plain single action with no verifier.
    'click': ('click', ('x', 'y')),
}
ACTIONS = set(ACTION_SPECS)
