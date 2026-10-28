from pathlib import Path

p = Path('platform/vcmi-patch/client/AgentBridge.cpp')
s = p.read_text(encoding='utf-8')
old = '''		// Never send battle actions while the other side is active: doing so corrupts the
		// server-side battle state machine (the battle then waits forever for the AI move).
		if (auto cb = localCallback())
		{
			if (active->owner)
			{
				for (const auto * hero : cb->getHeroesInfo())
					if (hero == active->owner)
						mine = true;
				if (!mine)
					for (const auto * town : cb->getTownsInfo(true))
						if (town == active->owner)
							mine = true;
			}
		}
		return true;'''
new = '''		// Never send battle actions while the other side is active: doing so corrupted the
		// server-side battle state machine (the battle then waited forever for the AI move).
		const auto myColor = battle->getPlayerID();
		mine = myColor.has_value() && active->owner == *myColor;
		return true;'''
assert old in s, 'anchor not found'
p.write_text(s.replace(old, new, 1), encoding='utf-8')
print('battle guard uses battle->getPlayerID()')
