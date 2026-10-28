/*
 * AgentBridge.cpp, part of VCMI engine — agent integration patch (not upstream)
 *
 * See AgentBridge.h for the contract.  Implementation notes:
 *
 *  - All *reading* uses the visibility-checked callbacks.  For the adventure map:
 *      CGameInfoCallback::isVisible/getObj/getVisitableObjs/getTile/canBuildStructure
 *      CPlayerSpecificInfoCallback::getHeroesInfo/getTownsInfo/getResourceAmount
 *    For battles: CPlayerBattleCallback::battleGetStacks (player's own view).
 *  - All *actions* go through the exact methods the UI invokes:
 *      adventure map -> IGameActionCallback (moveHero / buildBuilding / recruitCreatures / endTurn)
 *      battle        -> BattleActionsController::onHexLeftClicked(hex)  (the mouse-left handler)
 *  - Threading: the poller thread only reads files and posts work to the GUI thread via
 *    ENGINE->dispatchMainThread(); every game access happens on the GUI thread.
 *  - Files: <dir>/state.json (atomic overwrite), <dir>/commands.txt (append-only,
 *    processed incrementally by offset), <dir>/results.log (append-only).
 */
#include "StdInc.h"
#include "AgentBridge.h"

#include "CPlayerInterface.h"
#include "PlayerLocalState.h"
#include "GameEngine.h"
#include "GameInstance.h"
#include "CServerHandler.h"
#include "Client.h"
#include "adventureMap/AdventureMapInterface.h"
#include "gui/CIntObject.h"
#include "gui/EventDispatcher.h"
#include "gui/WindowHandler.h"
#include "windows/CWindowObject.h"
#include "windows/CCastleInterface.h"
#include "windows/GUIClasses.h"
#include "windows/InfoWindows.h"
#include "windows/CPuzzleWindow.h"
#include "lobby/CLobbyScreen.h"
#include "mainmenu/CMainMenu.h"
#include "battle/BattleInterface.h"
#include "battle/BattleActionsController.h"
#include "battle/BattleResultWindow.h"

#include "../lib/callback/CCallback.h"
#include "../lib/callback/CPlayerSpecificInfoCallback.h"
#include "../lib/constants/EntityIdentifiers.h"
#include "../lib/constants/NumericConstants.h"
#include "../lib/IGameSettings.h"
#include "../lib/GameLibrary.h"
#include "../lib/battle/CPlayerBattleCallback.h"
#include "../lib/battle/IBattleState.h"
#include "../lib/battle/BattleAction.h"
#include "../lib/battle/BattleHex.h"
#include "../lib/gameState/UpgradeInfo.h"
#include "../lib/mapObjects/IMarket.h"
#include "../lib/mapObjects/CGHeroInstance.h"
#include "../lib/pathfinder/CGPathNode.h"
#include "../lib/entities/building/CBuilding.h"
#include "../lib/mapObjects/CGCreature.h"
#include "../lib/mapObjects/MiscObjects.h"
#include "../lib/mapObjects/CGTownInstance.h"
#include "../lib/CStack.h"
#include "../lib/entities/artifact/CArtifact.h"
#include "../lib/entities/hero/CHero.h"
#include "../lib/CSkillHandler.h"
#include "../lib/spells/CSpellHandler.h"
#include "../lib/battle/PossiblePlayerBattleAction.h"
#include "../lib/spells/ISpellMechanics.h"
#include "../lib/spells/Problem.h"
#include "../lib/spells/adventure/AdventureSpellEffect.h"
#include "../lib/constants/EntityIdentifiers.h"
#include "../lib/constants/Enumerations.h"
#include "../lib/StartInfo.h"
#include "../lib/filesystem/ResourcePath.h"
#include "../lib/mapping/CMapInfo.h"
#include "../lib/networkPacks/PacksForLobby.h"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <thread>
#include <deque>
#include <vector>
#include <thread>
#include <mutex>
#include <vector>
#include <cstdio>
#include <cstdlib>

namespace fs = std::filesystem;

namespace
{
constexpr int STATE_INTERVAL_MS = 400;

std::string bridgeDir;
// ---- D-007 trusted-loop support: session identity + per-command sequence ----
std::string sessionId;
std::atomic<int64_t> currentCmdSeq{0};
std::atomic<bool> inCommand{false};
std::atomic<bool> started{false};
std::atomic<bool> stopping{false};
std::thread poller;
std::mutex ioMutex;

// frame-driven "start a single-player game" state machine
enum class StartPhase { Idle, Reset, WaitLobby, PushMap, SendStart, Done, Failed };
StartPhase startPhase = StartPhase::Idle;
std::string startMapName;
std::shared_ptr<CMapInfo> startMapInfo;
int startTicks = 0;

// main-thread heartbeat: lets the external agent tell "no dispatch happened"
// apart from "the main loop is stuck"
std::atomic<uint64_t> tickFrames{0};


std::string creatureName(int id)
{
	try
	{
		const CCreature * c = dynamic_cast<const CCreature *>(LIBRARY->creatures()->getById(CreatureID(id)));
		return c ? c->getNamePluralTranslated() : std::string();
	}
	catch (...)
	{
		return std::string();
	}
}

// W5/N3a (T33/P-B): human-readable names for the player-visible hero details added below.
// Same defensive pattern as creatureName(): a missing/invalid entity yields "" instead of an
// exception, so a name lookup can never break the state.json writer.
std::string artifactName(int id)
{
	try
	{
		const CArtifact * artifact = ArtifactID(id).toArtifact();
		return artifact ? artifact->getNameTranslated() : std::string();
	}
	catch (...)
	{
		return std::string();
	}
}

std::string skillName(int id)
{
	try
	{
		if (id < 0)
			return std::string();
		const CSkill * skill = SecondarySkill(id).toSkill();
		return skill ? skill->getNameTranslated() : std::string();
	}
	catch (...)
	{
		return std::string();
	}
}

std::string spellName(int id)
{
	try
	{
		if (id < 0)
			return std::string();
		const CSpell * spell = SpellID(id).toSpell();
		return spell ? spell->getNameTranslated() : std::string();
	}
	catch (...)
	{
		return std::string();
	}
}

// The spell service throws for an out-of-range id, so every raw numeric id from a command goes
// through this guard instead of toSpell() directly.
const CSpell * spellById(int id)
{
	try
	{
		if (id < 0)
			return nullptr;
		return SpellID(id).toSpell();
	}
	catch (...)
	{
		return nullptr;
	}
}

std::string ownerJson(const PlayerColor & color)
{
	if (!color.isValidPlayer())
		return std::string();
	return ",\"owner\":" + std::to_string(color.getNum());
}

std::string jsonEscape(const std::string & in)
{
	std::string out;
	out.reserve(in.size() + 8);
	for (unsigned char c : in)
	{
		switch (c)
		{
		case '"':  out += "\\\""; break;
		case '\\': out += "\\\\"; break;
		case '\n': out += "\\n"; break;
		case '\r': out += "\\r"; break;
		case '\t': out += "\\t"; break;
		default:
			if (c < 0x20)
			{
				char buf[8];
				std::snprintf(buf, sizeof(buf), "\\u%04x", c);
				out += buf;
			}
			else
				out += static_cast<char>(c);
		}
	}
	return out;
}

std::string q(const std::string & s)
{
	return "\"" + jsonEscape(s) + "\"";
}

CPlayerInterface * localPlayerInterface()
{
	if (!GAME)
		return nullptr;
	CClient * client = GAME->server().client.get();
	if (!client)
		return nullptr;
	for (auto & entry : client->playerint)
	{
		if (auto * pi = dynamic_cast<CPlayerInterface *>(entry.second.get()))
			return pi;
	}
	return nullptr;
}

std::shared_ptr<CCallback> localCallback()
{
	auto * pi = localPlayerInterface();
	return pi ? pi->cb : nullptr;
}


void writeTextFile(const std::string & name, const std::string & content)
{
	fs::path dir(bridgeDir);
	std::error_code ec;
	fs::create_directories(dir, ec);
	fs::path tmp = dir / (name + ".tmp");
	fs::path dst = dir / name;
	{
		std::ofstream f(tmp, std::ios::binary | std::ios::trunc);
		f << content;
	}
	fs::rename(tmp, dst, ec);
	if (ec)
		fs::copy_file(tmp, dst, fs::copy_options::overwrite_existing, ec);
}

void writeStateFile(const std::string & content)
{
	fs::path dir(bridgeDir);
	std::error_code ec;
	fs::create_directories(dir, ec);
	fs::path tmp = dir / "state.json.tmp";
	fs::path dst = dir / "state.json";
	{
		std::ofstream f(tmp, std::ios::binary | std::ios::trunc);
		f << content;
	}
	fs::rename(tmp, dst, ec);
	if (ec)
		fs::copy_file(tmp, dst, fs::copy_options::overwrite_existing, ec);
}

void appendResult(const std::string & line)
{
	std::lock_guard<std::mutex> lock(ioMutex);
	std::ofstream f(fs::path(bridgeDir) / "results.log", std::ios::binary | std::ios::app);
	// Lines produced while executing a command carry the command sequence number, so the
	// caller can match a reply to *its own* request instead of reading the next OK in the file.
	if (inCommand.load())
		f << "seq=" << currentCmdSeq.load() << " ";
	f << line << "\n";
}

// Asynchronous steps (the delayed recruitment BUY) finish outside the command that requested
// them.  They must keep *that* command's seq even when the flush happens while a later command
// is being dispatched, otherwise the old purchase reply would be attributed to the new request.
void appendResultSeq(int64_t seq, const std::string & line)
{
	std::lock_guard<std::mutex> lock(ioMutex);
	std::ofstream f(fs::path(bridgeDir) / "results.log", std::ios::binary | std::ios::app);
	f << "seq=" << seq << " " << line << "\n";
}

std::string heroJson(const CGHeroInstance * hero, int index)
{
	std::string s = "{";
	s += "\"index\":" + std::to_string(index);
	s += ",\"id\":" + std::to_string(hero->id.getNum());
	s += ",\"name\":" + q(hero->getNameTranslated());
	s += ",\"level\":" + std::to_string(hero->level);
	s += ",\"exp\":" + std::to_string(hero->exp);
	s += ",\"mana\":" + std::to_string(hero->mana);
	s += ",\"movement\":" + std::to_string(hero->movementPointsRemaining());
	const int3 heroAnchor = hero->pos;
	const int3 heroVisit = hero->visitablePos();
	s += ",\"pos\":[" + std::to_string(heroAnchor.x) + "," + std::to_string(heroAnchor.y) + "," + std::to_string(heroAnchor.z) + "]";
	s += ",\"visitable\":[" + std::to_string(heroVisit.x) + "," + std::to_string(heroVisit.y) + "," + std::to_string(heroVisit.z) + "]";
	// T40/C11: canonical coordinate names.  `anchor` is the sprite anchor (hero::pos, the sprite is
	// drawn there) and `tile` is the interaction/visit tile (hero::visitablePos(), what pathfinding
	// and object visits use).  They differ by the map object's visitable offset and are NOT a bug;
	// pos/visitable stay unchanged so a consumer that predates these names keeps working.
	s += ",\"anchor\":[" + std::to_string(heroAnchor.x) + "," + std::to_string(heroAnchor.y) + "," + std::to_string(heroAnchor.z) + "]";
	s += ",\"tile\":[" + std::to_string(heroVisit.x) + "," + std::to_string(heroVisit.y) + "," + std::to_string(heroVisit.z) + "]";
	s += ",\"primary\":{";
	s += "\"attack\":" + std::to_string(hero->getPrimSkillLevel(PrimarySkill::ATTACK));
	s += ",\"defence\":" + std::to_string(hero->getPrimSkillLevel(PrimarySkill::DEFENSE));
	s += ",\"spellpower\":" + std::to_string(hero->getPrimSkillLevel(PrimarySkill::SPELL_POWER));
	s += ",\"knowledge\":" + std::to_string(hero->getPrimSkillLevel(PrimarySkill::KNOWLEDGE));
	s += "}";

	s += ",\"army\":[";
	bool first = true;
	for (const auto & slotEntry : hero->Slots())
	{
		const CStackInstance & inst = *slotEntry.second;
		if (!first)
			s += ",";
		first = false;
		const auto creatureID = inst.getCreatureID();
		s += "{\"slot\":" + std::to_string(slotEntry.first.getNum());
		s += ",\"creature\":" + std::to_string(creatureID.getNum());
		s += ",\"creature_name\":" + q(creatureName(creatureID.getNum()));
		s += ",\"count\":" + std::to_string(inst.getCount());
		s += "}";
	}
	s += "]";

	// ---- W5/N3a (T33/P-B): hero-screen facts the player can see but the MCP could not read ----
	// Purely additive fields: existing keys/order above are untouched, a protocol-2 consumer that
	// does not know them simply ignores them.
	s += ",\"mana_max\":" + std::to_string(hero->manaLimit());
	s += ",\"spellbook\":" + std::string(hero->hasSpellbook() ? "true" : "false");
	s += ",\"spells\":[";
	bool firstSpell = true;
	try
	{
		for (const SpellID & spell : hero->getSpellsInSpellbook())
		{
			if (!firstSpell) s += ",";
			firstSpell = false;
			s += "{\"id\":" + std::to_string(spell.getNum());
			s += ",\"name\":" + q(spellName(spell.getNum()));
			// T35: adventure-map usability facts the spellbook UI shows (cost / can cast now).
			const CSpell * spellObject = spellById(spell.getNum());
			if (spellObject)
			{
				const int spellCost = hero->getSpellCost(spellObject);
				s += ",\"cost\":" + std::to_string(spellCost);
				s += ",\"adventure\":"
					+ std::string(spellObject->isAdventure() ? "true" : "false");
				s += ",\"can_cast\":"
					+ std::string(spellCost >= 0 && hero->mana >= spellCost ? "true" : "false");
			}
			s += "}";
		}
	}
	catch (...)
	{
	}
	s += "]";
	s += ",\"skills\":[";
	bool firstSkill = true;
	for (const auto & skillEntry : hero->secSkills)
	{
		// placeholder entries (-1,-1) mean "default secondary skills", not a real skill
		if (skillEntry.first.getNum() < 0)
			continue;
		if (!firstSkill) s += ",";
		firstSkill = false;
		s += "{\"id\":" + std::to_string(skillEntry.first.getNum());
		s += ",\"level\":" + std::to_string(skillEntry.second);
		s += ",\"name\":" + q(skillName(skillEntry.first.getNum())) + "}";
	}
	s += "]";
	s += ",\"artifacts\":{\"worn\":[";
	bool firstWorn = true;
	for (const auto & slotEntry : hero->artifactsWorn)
	{
		const CArtifactInstance * art = slotEntry.second.getArt();
		if (!art)
			continue;
		const int typeId = art->getTypeId().getNum();
		if (!firstWorn) s += ",";
		firstWorn = false;
		s += "{\"slot\":" + std::to_string(slotEntry.first.getNum());
		s += ",\"id\":" + std::to_string(typeId);
		s += ",\"name\":" + q(artifactName(typeId));
		s += ",\"locked\":" + std::string(slotEntry.second.locked ? "true" : "false") + "}";
	}
	s += "],\"backpack\":[";
	bool firstBag = true;
	for (size_t i = 0; i < hero->artifactsInBackpack.size(); ++i)
	{
		const CArtifactInstance * art = hero->artifactsInBackpack[i].getArt();
		if (!art)
			continue;
		const int typeId = art->getTypeId().getNum();
		const int bagSlot = static_cast<int>(ArtifactPosition::BACKPACK_START) + static_cast<int>(i);
		if (!firstBag) s += ",";
		firstBag = false;
		s += "{\"slot\":" + std::to_string(bagSlot);
		s += ",\"id\":" + std::to_string(typeId);
		s += ",\"name\":" + q(artifactName(typeId)) + "}";
	}
	s += "]}";
	s += ",\"luck\":" + std::to_string(hero->luckVal());
	s += ",\"morale\":" + std::to_string(hero->moraleVal());
	{
		std::string specialty;
		try
		{
			if (const CHero * heroType = hero->getHeroType())
				specialty = heroType->getSpecialtyNameTranslated();
		}
		catch (...)
		{
		}
		s += ",\"specialty\":" + q(specialty);
	}
	s += "}";
	return s;
}

// N2/town-cache fix: the cached part of a town is this body WITHOUT the outer braces and WITHOUT
// "index" (it starts at "id").  The caller wraps it with the CURRENT list index, so a cached body
// can never expose a stale "index" when the town order changes between heavy refreshes.
std::string townJsonFields(const CGTownInstance * town, const std::shared_ptr<CCallback> & cb)
{
	std::string s = "\"id\":" + std::to_string(town->id.getNum());
	s += ",\"name\":" + q(town->getNameTranslated());
	const int3 townAnchor = town->pos;
	const int3 townVisit = town->visitablePos();
	s += ",\"pos\":[" + std::to_string(townAnchor.x) + "," + std::to_string(townAnchor.y) + "," + std::to_string(townAnchor.z) + "]";
	// T40/C11: canonical names - anchor = sprite anchor, tile = the town's interaction tile (the
	// tile a hero stands on to visit/enter the town, i.e. visitablePos).  Additive only.
	s += ",\"anchor\":[" + std::to_string(townAnchor.x) + "," + std::to_string(townAnchor.y) + "," + std::to_string(townAnchor.z) + "]";
	s += ",\"tile\":[" + std::to_string(townVisit.x) + "," + std::to_string(townVisit.y) + "," + std::to_string(townVisit.z) + "]";

	s += ",\"built\":[";
	bool first = true;
	for (const auto & b : town->getBuildings())
	{
		if (!first) s += ",";
		first = false;
		s += std::to_string(b.getNum());
	}
	s += "]";

	// Buildings the player could order right now (legality comes from the engine itself).
	// "buildable" is the v1 list of allowed ids (kept for old consumers, restored per R04);
	// "build_options" adds names and costs.  Iterate every building id: id 0 is Mage Guild 1.
	static const char * const resNames7[7] = {"wood", "mercury", "ore", "sulfur", "crystal", "gems", "gold"};
	std::string buildableIds;
	std::string buildOptions;
	bool firstBuildable = true;
	for (int i = 0; i < 128; ++i)
	{
		const BuildingID bid(i);
		if (cb->canBuildStructure(town, bid) != EBuildingState::ALLOWED)
			continue;
		// Some "buildings" are special one-offs whose cost is empty (e.g. the Grail statue,
		// building id 26).  canBuildStructure() reports them as allowed, but the server rejects
		// the build, so the bridge would emit a misleading OK.  Only offer real costed builds.
		{
			const ResourceSet candidateCost = town->getBuildingCost(bid);
			int candidateTotal = 0;
			for (int r = 0; r < 7; ++r)
				candidateTotal += candidateCost[GameResID(r)];
			if (candidateTotal <= 0)
				continue;
		}
		if (!firstBuildable) buildableIds += ",";
		firstBuildable = false;
		buildableIds += std::to_string(i);
		if (!buildOptions.empty()) buildOptions += ",";
		buildOptions += "{\"id\":" + std::to_string(i);
		// building name, so the agent knows what it is paying for
		try
		{
			const auto * townType = town->getTown();
			auto it = townType->buildings.find(bid);
			if (it != townType->buildings.end() && it->second)
				buildOptions += ",\"name\":" + q(it->second->getNameTranslated());
		}
		catch (...)
		{
		}
		const ResourceSet cost = town->getBuildingCost(bid);
		buildOptions += ",\"cost\":{";
		bool fc = true;
		for (int r = 0; r < 7; ++r)
		{
			const int amount = cost[GameResID(r)];
			if (!amount) continue;
			if (!fc) buildOptions += ",";
			fc = false;
			buildOptions += "\"" + std::string(resNames7[r]) + "\":" + std::to_string(amount);
		}
		buildOptions += "}}";
	}
	s += ",\"buildable\":[" + buildableIds + "]";
	s += ",\"build_options\":[" + buildOptions + "]";

	// recruitable creatures per dwelling level
	s += ",\"dwellings\":[";
	first = true;
	for (size_t level = 0; level < town->creatures.size(); ++level)
	{
		const auto & entry = town->creatures[level];
		if (entry.second.empty())
			continue;
		if (!first) s += ",";
		first = false;
		s += "{\"level\":" + std::to_string(level + 1);
		s += ",\"available\":" + std::to_string(entry.first);
		s += ",\"creatures\":[";
		bool f2 = true;
		for (const auto & cid : entry.second)
		{
			if (!f2) s += ",";
			f2 = false;
			s += std::to_string(cid.getNum());
		}
		s += "]}";
	}
	s += "]";

	// garrison army
	s += ",\"garrison\":[";
	first = true;
	for (const auto & slotEntry : town->Slots())
	{
		const CStackInstance & inst = *slotEntry.second;
		if (!first) s += ",";
		first = false;
		s += "{\"slot\":" + std::to_string(slotEntry.first.getNum());
		s += ",\"creature\":" + std::to_string(inst.getCreatureID().getNum());
		s += ",\"count\":" + std::to_string(inst.getCount());
		s += "}";
	}
	s += "]";

	// heroes the tavern offers right now (player-visible info)
	s += ",\"available_heroes\":[";
	first = true;
	{
		auto available = cb->getAvailableHeroes(town);
		for (size_t i = 0; i < available.size(); ++i)
		{
			if (!first) s += ",";
			first = false;
			s += "{\"index\":" + std::to_string(i);
			s += ",\"name\":" + q(available[i]->getNameTranslated());
			s += ",\"hero_type\":" + std::to_string(available[i]->getHeroTypeID().getNum());
			s += "}";
		}
	}
	s += "]";
	return s;
}



// ---------------------------------------------------------------- economy / army info (W3)
// Read-only dumps used to make build / market / upgrade / split decisions with the same
// information the player UI shows: every building's state + cost + missing resources +
// requirement tree, market exchange quotes, and per-stack upgrade options with costs.
static const char * const gResNames[7] = {"wood", "mercury", "ore", "sulfur", "crystal", "gems", "gold"};

std::string resourcesJson(const ResourceSet & res)
{
	std::string s = "{";
	bool first = true;
	for (int i = 0; i < 7; ++i)
	{
		const int amount = res[GameResID(i)];
		if (!amount)
			continue;
		if (!first) s += ",";
		first = false;
		s += "\"" + std::string(gResNames[i]) + "\":" + std::to_string(amount);
	}
	s += "}";
	return s;
}

std::string buildingNameJson(const CGTownInstance * town, BuildingID bid)
{
	try
	{
		const auto * townType = town->getTown();
		auto it = townType->buildings.find(bid);
		if (it != townType->buildings.end() && it->second)
			return q(it->second->getNameTranslated());
	}
	catch (...)
	{
	}
	return "null";
}

const char * buildingStateName(EBuildingState state)
{
	switch (state)
	{
		case EBuildingState::ALLOWED: return "allowed";
		case EBuildingState::ALREADY_PRESENT: return "already_present";
		case EBuildingState::NO_RESOURCES: return "no_resources";
		case EBuildingState::PREREQUIRES: return "prerequires";
		case EBuildingState::MISSING_BASE: return "missing_base";
		case EBuildingState::CANT_BUILD_TODAY: return "cant_build_today";
		case EBuildingState::TOWN_NOT_OWNED: return "town_not_owned";
		case EBuildingState::NO_WATER: return "no_water";
		case EBuildingState::HAVE_CAPITAL: return "have_capital";
		case EBuildingState::FORBIDDEN: return "forbidden";
		case EBuildingState::ADD_MAGES_GUILD: return "add_mages_guild";
		case EBuildingState::BUILDING_ERROR: return "building_error";
		default: return "unknown";
	}
}

// Serialize the engine's LogicalExpression<BuildingID> as a JSON tree (all/any/none/leaf).
struct RequirementTreeWriter
{
	const CGTownInstance * town;

	std::string operator()(const CBuilding::TRequired::Value & bid) const
	{
		return "{\"building\":" + std::to_string(bid.getNum()) + ",\"name\":"
			+ buildingNameJson(town, bid) + ",\"built\":"
			+ (town->hasBuilt(bid) ? "true" : "false") + "}";
	}

	std::string operator()(const CBuilding::TRequired::OperatorAll & op) const
	{
		return "{\"all\":[" + children(op.expressions) + "]}";
	}

	std::string operator()(const CBuilding::TRequired::OperatorAny & op) const
	{
		return "{\"any\":[" + children(op.expressions) + "]}";
	}

	std::string operator()(const CBuilding::TRequired::OperatorNone & op) const
	{
		return "{\"none\":[" + children(op.expressions) + "]}";
	}

	template <typename T>
	std::string children(const T & expressions) const
	{
		std::string s;
		bool first = true;
		for (const auto & expression : expressions)
		{
			if (!first) s += ",";
			first = false;
			s += std::visit(*this, expression);
		}
		return s;
	}
};

// Missing requirement leaves (over-approximation for anyOf; the tree above is authoritative).
struct RequirementUnmetWriter
{
	const CGTownInstance * town;
	std::vector<BuildingID> * out;

	void operator()(const CBuilding::TRequired::Value & bid) const
	{
		if (!town->hasBuilt(bid))
			out->push_back(bid);
	}

	void operator()(const CBuilding::TRequired::OperatorAll & op) const { collect(op.expressions); }
	void operator()(const CBuilding::TRequired::OperatorAny & op) const { collect(op.expressions); }
	void operator()(const CBuilding::TRequired::OperatorNone & op) const { (void)op; }

	template <typename T>
	void collect(const T & expressions) const
	{
		for (const auto & expression : expressions)
			std::visit(*this, expression);
	}
};

std::string marketJson(const CGTownInstance * town, const std::shared_ptr<CCallback> & cb)
{
	const IMarket * market = cb->getMarket(town->id);
	if (!market)
		return "{\"available\":false}";
	// A town object implements IMarket even before its marketplace is built; gameplay
	// availability is the RESOURCE_RESOURCE trade mode, not the pointer being non-null.
	const bool resourceTrade = market->allowsTrade(EMarketMode::RESOURCE_RESOURCE);
	std::string s = "{\"available\":" + std::string(resourceTrade ? "true" : "false")
		+ ",\"modes\":[";
	bool first = true;
	for (const auto mode : market->availableModes())
	{
		if (!first) s += ",";
		first = false;
		s += std::to_string(static_cast<int>(mode));
	}
	s += "],\"resource_quotes\":[";
	bool any = false;
	if (market->allowsTrade(EMarketMode::RESOURCE_RESOURCE))
	{
		for (int sell = 0; sell < 7; ++sell)
		{
			for (int buy = 0; buy < 7; ++buy)
			{
				if (sell == buy)
					continue;
				int give = 0;
				int receive = 0;
				if (!market->getOffer(sell, buy, give, receive, EMarketMode::RESOURCE_RESOURCE))
					continue;
				if (any) s += ",";
				any = true;
				s += "{\"sell\":" + std::to_string(sell) + ",\"sell_name\":\""
					+ gResNames[sell] + "\",\"buy\":" + std::to_string(buy)
					+ ",\"buy_name\":\"" + gResNames[buy] + "\",\"give\":"
					+ std::to_string(give) + ",\"receive\":" + std::to_string(receive) + "}";
			}
		}
	}
	s += "]}";
	return s;
}

void doTownInfo(std::istringstream & in)
{
	int townIdx = -1;
	in >> townIdx;
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	auto towns = cb->getTownsInfo(true);
	if (townIdx < 0 || townIdx >= static_cast<int>(towns.size()))
	{
		appendResult("ERR bad_town_index");
		return;
	}
	const CGTownInstance * town = towns[townIdx];
	const auto * townType = town->getTown();
	if (!townType)
	{
		appendResult("ERR no_town_type");
		return;
	}
	const ResourceSet have = cb->getResourceAmount();
	std::string s = "{";
	s += "\"index\":" + std::to_string(townIdx);
	s += ",\"id\":" + std::to_string(town->id.getNum());
	s += ",\"name\":" + q(town->getNameTranslated());
	s += ",\"pos\":[" + std::to_string(town->pos.x) + "," + std::to_string(town->pos.y) + ","
		+ std::to_string(town->pos.z) + "]";
	s += ",\"resources\":" + resourcesJson(have);
	s += ",\"buildings\":[";
	bool first = true;
	int buildingCount = 0;
	for (const auto & entry : townType->buildings)
	{
		const BuildingID bid = entry.first;
		const CBuilding * building = entry.second.get();
		if (!building)
			continue;
		const EBuildingState state = cb->canBuildStructure(town, bid);
		const ResourceSet cost = town->getBuildingCost(bid);
		int costTotal = 0;
		for (int r = 0; r < 7; ++r)
			costTotal += cost[GameResID(r)];
		ResourceSet missing;
		bool affordable = true;
		for (int r = 0; r < 7; ++r)
		{
			const int needed = cost[GameResID(r)];
			const int held = have[GameResID(r)];
			if (held < needed)
			{
				affordable = false;
				missing[GameResID(r)] = needed - held;
			}
		}
		bool requirementsSatisfied = true;
		std::string requirementTree = "null";
		std::vector<BuildingID> unmet;
		try
		{
			requirementsSatisfied = building->requirements.test(
				[&](const BuildingID & required) { return town->hasBuilt(required); });
			requirementTree = std::visit(RequirementTreeWriter{town}, building->requirements.get());
			std::visit(RequirementUnmetWriter{town, &unmet}, building->requirements.get());
		}
		catch (...)
		{
		}
		if (!first) s += ",";
		first = false;
		++buildingCount;
		s += "{\"id\":" + std::to_string(bid.getNum());
		s += ",\"name\":" + buildingNameJson(town, bid);
		s += ",\"built\":" + std::string(town->hasBuilt(bid) ? "true" : "false");
		s += ",\"state\":{\"code\":" + std::to_string(static_cast<int>(state))
			+ ",\"name\":\"" + buildingStateName(state) + "\"}";
		s += ",\"can_build\":"
			+ std::string((state == EBuildingState::ALLOWED && costTotal > 0) ? "true" : "false");
		s += ",\"cost\":" + resourcesJson(cost);
		s += ",\"cost_total\":" + std::to_string(costTotal);
		s += ",\"affordable\":" + std::string(affordable ? "true" : "false");
		s += ",\"missing_resources\":" + resourcesJson(missing);
		s += ",\"requirements_satisfied\":"
			+ std::string(requirementsSatisfied ? "true" : "false");
		s += ",\"requirement_tree\":" + requirementTree;
		s += ",\"missing_requirement_candidates\":[";
		bool firstUnmet = true;
		for (const auto & required : unmet)
		{
			if (!firstUnmet) s += ",";
			firstUnmet = false;
			s += "{\"building\":" + std::to_string(required.getNum()) + ",\"name\":"
				+ buildingNameJson(town, required) + "}";
		}
		s += "]";
		s += ",\"mode\":" + std::to_string(static_cast<int>(building->mode));
		s += ",\"upgrade_of\":" + std::to_string(building->upgrade.getNum());
		s += ",\"market_building\":"
			+ std::string(building->IsTradeBuilding() ? "true" : "false");
		s += "}";
	}
	s += "]";
	s += ",\"market\":" + marketJson(town, cb);
	s += "}";
	writeTextFile("town_info.json", s);
	appendResult("OK town_info town=" + std::to_string(townIdx) + " buildings="
		+ std::to_string(buildingCount) + " (see town_info.json)");
}

const CArmedInstance * resolveArmed(std::istringstream & in, std::string & ownerName, int & idx,
	const std::shared_ptr<CCallback> & cb)
{
	in >> ownerName >> idx;
	if (ownerName == "hero")
	{
		auto heroes = cb->getHeroesInfo();
		if (idx >= 0 && idx < static_cast<int>(heroes.size()))
			return heroes[idx];
	}
	else if (ownerName == "town")
	{
		auto towns = cb->getTownsInfo(true);
		if (idx >= 0 && idx < static_cast<int>(towns.size()))
			return towns[idx];
	}
	return nullptr;
}

void doArmyInfo(std::istringstream & in)
{
	std::string ownerName;
	int idx = -1;
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	const CArmedInstance * armed = resolveArmed(in, ownerName, idx, cb);
	if (!armed)
	{
		appendResult("ERR bad_target");
		return;
	}
	std::string s = "{\"owner\":\"" + ownerName + "\",\"index\":" + std::to_string(idx);
	s += ",\"slots\":[";
	bool first = true;
	int upgradable = 0;
	for (const auto & slotEntry : armed->Slots())
	{
		const SlotID slot = slotEntry.first;
		const CStackInstance & stack = *slotEntry.second;
		UpgradeInfo info(stack.getCreatureID());
		bool hasUpgrades = false;
		try
		{
			cb->fillUpgradeInfo(armed, slot, info);
			hasUpgrades = info.canUpgrade();
		}
		catch (...)
		{
		}
		if (hasUpgrades)
			++upgradable;
		if (!first) s += ",";
		first = false;
		s += "{\"slot\":" + std::to_string(slot.getNum());
		s += ",\"creature\":" + std::to_string(stack.getCreatureID().getNum());
		s += ",\"count\":" + std::to_string(stack.getCount());
		s += ",\"upgradable\":" + std::string(hasUpgrades ? "true" : "false");
		s += ",\"upgrades\":[";
		bool firstUp = true;
		if (hasUpgrades)
		{
			for (const CreatureID & upgrade : info.getAvailableUpgrades())
			{
				if (!firstUp) s += ",";
				firstUp = false;
				s += "{\"creature\":" + std::to_string(upgrade.getNum());
				s += ",\"cost\":" + resourcesJson(info.getUpgradeCostsFor(upgrade));
				s += "}";
			}
		}
		s += "]}";
	}
	s += "],\"upgradable_slots\":" + std::to_string(upgradable) + "}";
	writeTextFile("army_info.json", s);
	appendResult("OK army_info owner=" + ownerName + " index=" + std::to_string(idx)
		+ " upgradable=" + std::to_string(upgradable) + " (see army_info.json)");
}


// ---------------------------------------------------------------- pending query
// The server blocks until the player answers some dialogs (level-up skill choice,
// treasure chest gold-vs-experience, teleports, ...).  In agent mode we never create the
// UI window; instead we publish the query in state.json and reply with `answer <n>`.
struct PendingQuery
{
	bool active = false;
	int64_t id = -1;
	std::string type;
	std::string text;
	bool canCancel = false;
	std::string primary;                 // level-up only
	std::vector<std::string> options;    // JSON fragments, one per option
};
std::mutex queryMutex;
PendingQuery pendingQuery;

// informational dialogs (events, week-of messages, level-up texts) are logged
// instead of shown: some of them block the client until acknowledged
std::mutex infoMutex;
std::deque<std::string> infoLog;

const char * componentTypeName(ComponentType type)
{
	switch (type)
	{
	case ComponentType::NONE: return "NONE";
	case ComponentType::PRIM_SKILL: return "PRIM_SKILL";
	case ComponentType::SEC_SKILL: return "SEC_SKILL";
	case ComponentType::RESOURCE: return "RESOURCE";
	case ComponentType::RESOURCE_PER_DAY: return "RESOURCE_PER_DAY";
	case ComponentType::CREATURE: return "CREATURE";
	case ComponentType::ARTIFACT: return "ARTIFACT";
	case ComponentType::SPELL_SCROLL: return "SPELL_SCROLL";
	case ComponentType::MANA: return "MANA";
	case ComponentType::EXPERIENCE: return "EXPERIENCE";
	case ComponentType::LEVEL: return "LEVEL";
	case ComponentType::SPELL: return "SPELL";
	case ComponentType::MORALE: return "MORALE";
	case ComponentType::LUCK: return "LUCK";
	case ComponentType::BUILDING: return "BUILDING";
	case ComponentType::HERO_PORTRAIT: return "HERO_PORTRAIT";
	case ComponentType::FLAG: return "FLAG";
	default: return "UNKNOWN";
	}
}

std::string componentJson(const Component & comp, int index)
{
	std::string s = "{\"index\":" + std::to_string(index);
	s += ",\"type\":\"" + std::string(componentTypeName(comp.type)) + "\"";
	// subType is a variant; expose the numeric id when it carries an entity identifier
	try
	{
		s += ",\"sub\":" + std::to_string(comp.subType.getNum());
	}
	catch (...)
	{
	}
	if (comp.value.has_value())
		s += ",\"value\":" + std::to_string(*comp.value);
	s += "}";
	return s;
}


std::string infoJson()
{
	std::lock_guard<std::mutex> lock(infoMutex);
	std::string s = "\"info\":[";
	bool first = true;
	for (const auto & line : infoLog)
	{
		if (!first) s += ",";
		first = false;
		s += q(line);
	}
	s += "]";
	return s;
}

std::string queryJson()
{
	std::lock_guard<std::mutex> lock(queryMutex);
	if (!pendingQuery.active)
		return "\"query\":null";

	std::string s = "\"query\":{";
	s += "\"id\":" + std::to_string(pendingQuery.id);
	s += ",\"type\":" + q(pendingQuery.type);
	s += ",\"text\":" + q(pendingQuery.text);
	s += ",\"can_cancel\":" + std::string(pendingQuery.canCancel ? "true" : "false");
	if (!pendingQuery.primary.empty())
		s += ",\"primary\":" + q(pendingQuery.primary);
	s += ",\"options\":[";
	for (size_t i = 0; i < pendingQuery.options.size(); ++i)
	{
		if (i) s += ",";
		s += pendingQuery.options[i];
	}
	s += "]}";
	return s;
}

void setPendingQuery(int64_t id, const std::string & type, const std::string & text,
                     bool canCancel, const std::string & primary, std::vector<std::string> options)
{
	std::lock_guard<std::mutex> lock(queryMutex);
	pendingQuery.active = true;
	pendingQuery.id = id;
	pendingQuery.type = type;
	pendingQuery.text = text;
	pendingQuery.canCancel = canCancel;
	pendingQuery.primary = primary;
	pendingQuery.options = std::move(options);
}

bool consumePendingQuery()
{
	std::lock_guard<std::mutex> lock(queryMutex);
	const bool wasActive = pendingQuery.active;
	pendingQuery.active = false;
	return wasActive;
}

std::string primarySkillName(PrimarySkill skill)
{
	// PrimarySkill is an identifier type, not an enum, so no switch here
	if (skill == PrimarySkill::ATTACK) return "ATTACK";
	if (skill == PrimarySkill::DEFENSE) return "DEFENSE";
	if (skill == PrimarySkill::SPELL_POWER) return "SPELL_POWER";
	if (skill == PrimarySkill::KNOWLEDGE) return "KNOWLEDGE";
	return "UNKNOWN";
}


int armyTotalCount(const CArmedInstance * armed)
{
	int total = 0;
	for (const auto & slot : armed->Slots())
		total += slot.second->getCount();
	return total;
}


// The pathfinder is expensive and unsafe to run on every tick (it crashed the client when
// called ~2x/second), so reachability is computed only when the agent asks for it.
std::string computeReachableJson(int heroIdx)
{
	auto cb = localCallback();
	if (!cb)
		return "{\"error\":\"no_game\"}";
	auto heroes = cb->getHeroesInfo();
	if (heroIdx < 0 || heroIdx >= static_cast<int>(heroes.size()))
		return "{\"error\":\"bad_hero_index\"}";
	if (CPlayerInterface::battleInt)
		return "{\"error\":\"in_battle\"}";
	auto * pi = localPlayerInterface();
	if (!pi)
		return "{\"error\":\"no_player_interface\"}";
	const CGHeroInstance * hero = heroes[heroIdx];
	auto info = pi->getPathsInfo(hero);
	if (!info)
		return "{\"error\":\"no_paths\"}";

	const int radius = 16;
	const int3 origin = hero->visitablePos();
	std::string tiles;
	int count = 0;
	bool first = true;
	for (int dx = -radius; dx <= radius; ++dx)
	{
		for (int dy = -radius; dy <= radius; ++dy)
		{
			if (std::abs(dx) + std::abs(dy) > radius)
				continue;
			const int3 tile(origin.x + dx, origin.y + dy, origin.z);
			if (tile == origin)
				continue;
			const CGPathNode * node = info->getPathInfo(tile);
			if (!node || node->turns != 0)
				continue;
			if (node->accessible == EPathAccessibility::NOT_SET || node->accessible == EPathAccessibility::BLOCKED)
				continue;
			if (!first) tiles += ",";
			first = false;
			tiles += "[" + std::to_string(tile.x) + "," + std::to_string(tile.y) + "," + std::to_string(tile.z) + "]";
			++count;
		}
	}
	return "{\"hero\":" + std::to_string(heroIdx)
		+ ",\"origin\":[" + std::to_string(origin.x) + "," + std::to_string(origin.y) + "," + std::to_string(origin.z) + "]"
		+ ",\"count\":" + std::to_string(count)
		+ ",\"tiles\":[" + tiles + "]}";
}


// ---------------------------------------------------------------- heavy read gate
std::mutex heavyMutex;
uint64_t lastHeavyMs = 0;
uint64_t lastCameraCenterMs = 0;
std::string cachedObjectsJson = "\"objects\":[]";
std::map<ObjectInstanceID, std::string> cachedTownJson;   // body without "index"/outer braces

uint64_t nowMs()
{
	return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
		std::chrono::steady_clock::now().time_since_epoch()).count());
}

bool heavyReadsAllowed()
{
	if (CPlayerInterface::battleInt)
		return false;
	std::lock_guard<std::mutex> lock(queryMutex);
	return !pendingQuery.active;
}

bool heavyRefreshDue()
{
	std::lock_guard<std::mutex> lock(heavyMutex);
	const uint64_t now = nowMs();
	if (now - lastHeavyMs < 2000)
		return false;
	lastHeavyMs = now;
	return true;
}

void invalidateViewCaches()
{
	// A new game / loaded save must never be described with the previous game's town and
	// object JSON.  Call this on every transition that replaces the game state.
	cachedTownJson.clear();
	cachedObjectsJson = "\"objects\":[]";
}

std::string objectsJson(const std::shared_ptr<CCallback> & cb)
{
	// Everything the player can see right now (the same set the map draws):
	// getAllVisitableObjs() already filters by this player's fog of war.
	std::string s = "\"objects\":[";
	bool first = true;
	for (const CGObjectInstance * obj : cb->getAllVisitableObjs())
	{
		if (!obj)
			continue;
		if (!first) s += ",";
		first = false;
		s += "{\"id\":" + std::to_string(obj->id.getNum());
		const int3 objAnchor = obj->pos;
		const int3 objVisit = obj->visitablePos();
		s += ",\"pos\":[" + std::to_string(objAnchor.x) + "," + std::to_string(objAnchor.y) + "," + std::to_string(objAnchor.z) + "]";
		s += ",\"visitable\":[" + std::to_string(objVisit.x) + "," + std::to_string(objVisit.y) + "," + std::to_string(objVisit.z) + "]";
		// T40/C11: same canonical names as heroes - anchor = sprite anchor (pos), tile = the tile a
		// hero has to stand on to interact with the object (visitablePos).  Additive only.
		s += ",\"anchor\":[" + std::to_string(objAnchor.x) + "," + std::to_string(objAnchor.y) + "," + std::to_string(objAnchor.z) + "]";
		s += ",\"tile\":[" + std::to_string(objVisit.x) + "," + std::to_string(objVisit.y) + "," + std::to_string(objVisit.z) + "]";
		s += ",\"type\":" + q(obj->getTypeName());
		s += ",\"name\":" + q(obj->getObjectName());
		if (const auto * creature = dynamic_cast<const CGCreature *>(obj))
		{
			const int cid = creature->getCreatureID().getNum();
			s += ",\"kind\":\"creature\"";
			s += ",\"creature\":" + std::to_string(cid);
			s += ",\"creature_name\":" + q(creatureName(cid));
			// D-002/D-007: the exact stack size is only player-visible when Visions applies.
			// Publish exactly what the game's own hover text shows (a quantity tier).
			if (const auto me = cb->getPlayerID())
				s += ",\"hover\":" + q(creature->getHoverText(*me));
		}
		else if (const auto * mine = dynamic_cast<const CGMine *>(obj))
		{
			// The flag colour of a mine is visible on the adventure map.
			s += ",\"kind\":\"mine\"";
			const PlayerColor mineOwner = mine->getOwner();
			if (mineOwner.isValidPlayer())
				s += ownerJson(mineOwner);
		}
		else if (const auto * town = dynamic_cast<const CGTownInstance *>(obj))
		{
			s += ",\"kind\":\"town\"";
			s += ownerJson(town->tempOwner);
			s += ",\"hero_in_garrison\":" + std::string(town->getGarrisonHero() ? "true" : "false");
			s += ",\"hero_visiting\":" + std::string(town->getVisitingHero() ? "true" : "false");
		}
		else if (const auto * hero = dynamic_cast<const CGHeroInstance *>(obj))
		{
			s += ",\"kind\":\"hero\"";
			s += ownerJson(hero->tempOwner);
			// D-007: level/army strength are not published blindly - only facts that the normal
			// UI gives without opening a hero screen.  Strength projection is a later work item.
		}
		else
		{
			s += ",\"kind\":\"object\"";
		}
		s += "}";
	}
	s += "]";
	return s;
}

std::string battleJson()
{
	// Player's own battle view only (CPlayerBattleCallback is constructed with our color)
	auto battleInt = CPlayerInterface::battleInt;
	if (!battleInt)
		return "\"battle\":{\"active\":false}";

	std::string s = "\"battle\":{\"active\":true";
	auto battle = battleInt->getBattle();
	if (battle)
	{
		// Which side do we actually control?  In a defensive battle we are side 1, so scripts
		// must not hardcode "side 0 = me" (R06).
		int mySide = -1;
		if (auto cb = localCallback())
		{
			const auto me = cb->getPlayerID();
			if (me)
			{
				for (const CStack * st : battle->battleGetStacks())
				{
					if (st && st->getOwner() == *me)
					{
						mySide = static_cast<int>(st->unitSide());
						break;
					}
				}
			}
		}
		s += ",\"my_side\":" + std::to_string(mySide);
		// W5/N3a (T33/P-B): round counter + our hero's spell/mana facts from the battle screen.
		try
		{
			s += ",\"round\":" + std::to_string(battle->getBattle()->getRound());
		}
		catch (...)
		{
		}
		if (const CGHeroInstance * myHero = battle->battleGetMyHero())
		{
			s += ",\"hero_mana\":" + std::to_string(myHero->mana);
			s += ",\"hero_mana_max\":" + std::to_string(myHero->manaLimit());
			s += ",\"hero_spellbook\":" + std::string(myHero->hasSpellbook() ? "true" : "false");
		}
		if (mySide == 0 || mySide == 1)
		{
			try
			{
				s += ",\"cast_spells\":" + std::to_string(
					battle->getBattle()->getCastSpells(static_cast<BattleSide>(mySide)));
			}
			catch (...)
			{
			}
		}
		s += ",\"active_stack\":" + std::to_string(battle->getBattle()->getActiveStackID());
		s += ",\"stacks\":[";
		bool first = true;
		for (const CStack * stack : battle->battleGetStacks())
		{
			if (!stack)
				continue;
			if (!first) s += ",";
			first = false;
			const auto hex = stack->getPosition();
			s += "{\"id\":" + std::to_string(stack->unitId());
			s += ",\"creature\":" + std::to_string(stack->creatureId().getNum());
			s += ",\"creature_name\":" + q(creatureName(stack->creatureId().getNum()));
			s += ",\"count\":" + std::to_string(stack->getCount());
			s += ",\"side\":" + std::to_string(static_cast<int>(stack->unitSide()));
			s += ",\"hex\":[" + std::to_string(hex.getX()) + "," + std::to_string(hex.getY()) + "]";
			// Combat facts a player can see (ammo, retaliation, stance); no hidden HP/army data.
			s += ",\"double_wide\":" + std::string(stack->doubleWide() ? "true" : "false");
			s += ",\"shots\":{\"available\":" + std::to_string(stack->shots.available())
				+ ",\"total\":" + std::to_string(stack->shots.total())
				+ ",\"limited\":" + std::string(stack->shots.isLimited() ? "true" : "false") + "}";
			s += ",\"counterattacks\":{\"available\":" + std::to_string(stack->counterAttacks.available())
				+ ",\"total\":" + std::to_string(stack->counterAttacks.total()) + "}";
			s += ",\"defending\":" + std::string(stack->defending ? "true" : "false");
			s += ",\"waited\":" + std::string(stack->waitedThisTurn ? "true" : "false");
			s += "}";
		}
		s += "]";
	}
	s += "}";
	return s;
}

std::string buildStateJson()
{
	auto cb = localCallback();
	std::string s = "{";
	if (!cb)
	{
		s += "\"in_game\":false,\"ts\":" + std::to_string(std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now().time_since_epoch()).count());
		s += "}";
		return s;
	}

	s += "\"in_game\":true";
	s += ",\"frames\":" + std::to_string(tickFrames.load());
	s += ",\"protocol\":2";
	s += ",\"session_id\":" + q(sessionId);
	if (const auto me = cb->getPlayerID())
	{
		s += ",\"player\":" + std::to_string(me->getNum());
		// own status only (the player learns win/loss immediately through the game-over screen);
		// other players' status is not exposed
		const EPlayerStatus status = cb->getPlayerStatus(*me);
		const char * statusName = "WRONG";
		if (status == EPlayerStatus::INGAME) statusName = "INGAME";
		else if (status == EPlayerStatus::LOSER) statusName = "LOSER";
		else if (status == EPlayerStatus::WINNER) statusName = "WINNER";
		s += ",\"status\":\"" + std::string(statusName) + "\"";
	}
	s += ",\"cmd_seq\":" + std::to_string(currentCmdSeq.load());
	s += ",\"ts_wall\":" + std::to_string(static_cast<int64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
		std::chrono::system_clock::now().time_since_epoch()).count()));
	if (auto * pi = localPlayerInterface())
		s += ",\"making_turn\":" + std::string(pi->makingTurn ? "true" : "false");
	s += ",\"date\":{";
	s += "\"day\":" + std::to_string(cb->getDate(Date::DAY));
	s += ",\"day_of_week\":" + std::to_string(cb->getDate(Date::DAY_OF_WEEK));
	s += ",\"week\":" + std::to_string(cb->getDate(Date::WEEK));
	s += ",\"month\":" + std::to_string(cb->getDate(Date::MONTH));
	s += "}";

	const ResourceSet resources = cb->getResourceAmount();
	s += ",\"resources\":{";
	static const char * const resNames[7] = {"wood", "mercury", "ore", "sulfur", "crystal", "gems", "gold"};
	for (int i = 0; i < 7; ++i)
	{
		if (i) s += ",";
		s += "\"" + std::string(resNames[i]) + "\":" + std::to_string(resources[GameResID(i)]);
	}
	s += "}";

	s += ",\"heroes\":[";
	{
		bool first = true;
		int index = 0;
		for (const CGHeroInstance * hero : cb->getHeroesInfo())
		{
			if (!first) s += ",";
			first = false;
			s += heroJson(hero, index++);
		}
	}
	s += "]";

	const bool heavy = heavyReadsAllowed() && heavyRefreshDue();

	s += ",\"towns\":[";
	{
		bool first = true;
		int index = 0;
		for (const CGTownInstance * town : cb->getTownsInfo(true))
		{
			if (!first) s += ",";
			first = false;
			// Heavy reads (full town fields: build options, legal-building scan, garrison, ...)
			// are gated to keep battles/queries responsive.  The cache holds only the town BODY
			// (no "index", no outer braces); the current list index is wrapped at output time, so
			// a town-order change can never leak a stale index on a non-heavy cache hit.
			auto it = cachedTownJson.find(town->id);
			if (it == cachedTownJson.end())
				it = cachedTownJson.emplace(town->id, townJsonFields(town, cb)).first;
			else if (heavy)
				it->second = townJsonFields(town, cb);
			s += "{\"index\":" + std::to_string(index) + "," + it->second + "}";
			++index;
		}
	}
	s += "]";

	if (heavy)
		cachedObjectsJson = objectsJson(cb);
	s += "," + cachedObjectsJson;
	s += "," + queryJson();
	s += "," + infoJson();
	s += "," + battleJson();
	s += "}";
	return s;
}


void doReachable(std::istringstream & in)
{
	// Disabled: getPathsInfo() is only safe inside a player action.  Calling it from the
	// bridge froze (and earlier crashed) the client, so movement planning uses move_hero,
	// which performs exactly one pathfinder call in the normal action path.
	appendResult("ERR reachable_disabled_use_move_hero");
}


void doProbe(std::istringstream & in)
{
	// Reachability query for one or more tiles with a *single* pathfinder run.
	// Uses exactly the calls move_hero makes (getPathsInfo + getPath), just without sending
	// the movement request, so it is safe on demand.  Format: probe <heroIdx> x1 y1 z1 [x2 y2 z2 ...]
	int heroIdx = 0;
	in >> heroIdx;
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	auto heroes = cb->getHeroesInfo();
	if (heroIdx < 0 || heroIdx >= static_cast<int>(heroes.size()))
	{
		appendResult("ERR bad_hero_index");
		return;
	}
	if (CPlayerInterface::battleInt)
	{
		appendResult("ERR in_battle");
		return;
	}
	auto * pi = localPlayerInterface();
	if (!pi)
	{
		appendResult("ERR no_player_interface");
		return;
	}
	const CGHeroInstance * hero = heroes[heroIdx];
	auto info = pi->getPathsInfo(hero);
	if (!info)
	{
		appendResult("ERR no_paths");
		return;
	}
	std::string out = "OK probe";
	int x = -1, y = -1, z = 0;
	while (in >> x >> y >> z)
	{
		CGPath path;
		const bool reachable = info->getPath(path, int3(x, y, z));
		out += " " + std::to_string(x) + "," + std::to_string(y) + "," + std::to_string(z)
			+ "=" + (reachable ? "1" : "0");
		if (reachable && !path.nodes.empty())
		{
			// Path facts the player can see in the UI: turn count, movement cost, path length and
			// whether the path uses a teleport/subterranean gate or embarks on a boat.
			const CGPathNode & start = path.currNode();
			const CGPathNode & last = path.lastNode();
			bool teleport = false;
			bool embark = false;
			for (const auto & node : path.nodes)
			{
				if (node.isTeleportAction())
					teleport = true;
				if (node.action == EPathNodeAction::EMBARK
					|| node.action == EPathNodeAction::DISEMBARK)
					embark = true;
			}
			const int moveCost = static_cast<int>(start.moveRemains)
				- static_cast<int>(last.moveRemains);
			out += " turns=" + std::to_string(static_cast<int>(last.turns))
				+ " cost=" + std::to_string(moveCost)
				+ " len=" + std::to_string(static_cast<int>(path.nodes.size()))
				+ " teleport=" + (teleport ? "1" : "0")
				+ " embark=" + (embark ? "1" : "0");
		}
	}
	appendResult(out);
}

void doMoveHero(std::istringstream & in)
{
	int idx = -1, x = -1, y = -1, z = 0;
	in >> idx >> x >> y >> z;
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	auto heroes = cb->getHeroesInfo();
	if (idx < 0 || idx >= static_cast<int>(heroes.size()))
	{
		appendResult("ERR bad_hero_index");
		return;
	}
	const int3 mapSize = cb->getMapSize();
	if (x < 0 || y < 0 || z < 0 || z > 1 || x >= mapSize.x || y >= mapSize.y)
	{
		appendResult("ERR bad_coords " + std::to_string(x) + " " + std::to_string(y) + " " + std::to_string(z));
		return;
	}
	const CGHeroInstance * hero = heroes[idx];
	const int3 visitableTarget(x, y, z);

	// A single-element path only moves the hero one tile, so ask the same pathfinder the
	// UI uses for the full route and send it as one request (this is also what makes the
	// hero walk into a monster and start a battle).
	auto * pi = localPlayerInterface();
	if (!pi)
	{
		appendResult("ERR no_player_interface");
		return;
	}
	auto pathsInfo = pi->getPathsInfo(hero);
	if (!pathsInfo)
	{
		appendResult("ERR no_paths");
		return;
	}
	CGPath path;
	if (!pathsInfo->getPath(path, visitableTarget))
	{
		appendResult("ERR no_path to " + visitableTarget.toString());
		return;
	}

	std::vector<int3> anchorPath;
	// CGPath stores nodes destination-first (nodes[0] = destination, nodes.back() = hero),
	// so walk it backwards to build the movement order - same as HeroMovementController.
	for (auto it = path.nodes.rbegin(); it != path.nodes.rend(); ++it)
	{
		const auto & node = *it;
		if (node.coord == hero->visitablePos())
			continue; // the hero itself
		anchorPath.push_back(hero->convertFromVisitablePos(node.coord));
		if (node.turns != 0)
			break; // cannot reach this turn; move as far as allowed
	}
	if (anchorPath.empty())
	{
		appendResult("ERR empty_path to " + visitableTarget.toString());
		return;
	}
	cb->moveHero(hero, anchorPath, false);
	appendResult("OK move_hero " + std::to_string(idx) + " target=" + visitableTarget.toString()
		+ " steps=" + std::to_string(anchorPath.size())
		+ " movement_left=" + std::to_string(hero->movementPointsRemaining()));
}

void doEndTurn()
{
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	cb->endTurn();
	appendResult("OK end_turn");
}

// ---------------------------------------------------------------- town UI session
// Town actions (recruit / build / garrison transfers) are performed through the same windows a
// human player opens: the town screen (CCastleInterface) and its recruitment window.  The
// packets sent are still the ones those windows send - only the clicks are automated.  This keeps
// the recording honest: you see the town, the recruitment dialog and the bought creatures instead
// of stacks silently appearing.
constexpr uint64_t TOWN_SCREEN_DWELL_FRAMES = 110;   // ~1.8 s at 60 fps
uint64_t townScreenCloseAt = 0;
// The puzzle-map window (opened by visiting an obelisk) has no server query behind it: a human
// closes it with the OK button, an agent would simply be stuck in front of it.  Show it for a
// moment so the recording contains it, then close it automatically.
constexpr uint64_t PUZZLE_WINDOW_DWELL_FRAMES = 150; // ~2.5 s at 60 fps
uint64_t puzzleWindowCloseAt = 0;

void closePuzzleWindow()
{
	puzzleWindowCloseAt = 0;
	for (auto & window : ENGINE->windows().findWindows<CPuzzleWindow>())
		window->close();
}

void closeTownScreen()
{
	if (auto * player = GAME->interface())
	{
		if (player->castleInt)
		{
			player->castleInt->close();
			player->castleInt = nullptr;
		}
	}
	townScreenCloseAt = 0;
}

bool townScreenOpenFor(const CGTownInstance * town)
{
	auto * player = GAME->interface();
	return player && player->castleInt && player->castleInt->town == town;
}

// Opens the town screen exactly like a player double-clicking their town on the adventure map.
// Returns false when the GUI is busy with another dialog, so callers can fall back to the
// packet-only path instead of fighting the dialog state machine.
bool openTownScreen(const CGTownInstance * town, bool keepForDwell)
{
	auto * player = GAME->interface();
	if (!player || !town)
		return false;
	if (!townScreenOpenFor(town))
	{
		const bool guiIdle = ENGINE->windows().topWindow<AdventureMapInterface>()
			|| ENGINE->windows().topWindow<CCastleInterface>();
		if (!guiIdle)
			return false;
		player->openTownWindow(town);   // closes a previous screen, pushes CCastleInterface
	}
	if (keepForDwell)
		townScreenCloseAt = tickFrames.load() + TOWN_SCREEN_DWELL_FRAMES;
	return townScreenOpenFor(town);
}

// The recruit dialog is pushed now and "BUY" is pressed a few frames later: a synchronous buy
// closes the window before a single frame is rendered, so the recording would never show the
// recruitment dialog a human player sees.  Only the timing changes - the packet is the same one
// the dialog itself sends.
constexpr int RECRUIT_DIALOG_FRAMES = 30;   // ~0.5 s at 60 fps: the dialog must be visible in the recording
struct PendingRecruitStep
{
	bool active = false;
	int framesLeft = 0;
	int64_t cmdSeq = 0;             // seq of the recruit command that queued this purchase
	int townIdx = -1;
	const CGTownInstance * town = nullptr;
	CreatureID creature = CreatureID::NONE;
	int zeroLevel = -1;
	int amount = 0;
	std::string dest = "town";      // "town" (garrison) or "hero" (visiting hero's army)
};

PendingRecruitStep pendingRecruit;

void flushPendingRecruit()
{
	if (!pendingRecruit.active)
		return;
	const PendingRecruitStep step = pendingRecruit;
	pendingRecruit = PendingRecruitStep();

	// The buy failed: never leave the recruitment window on the window stack.  The town screen
	// is auto-closed a moment later and closing it under an orphaned child window crashed the
	// client (seen in the first W1 micro-scene), so close the recruit/info windows here.
	const auto cleanupFailedRecruit = []() {
		for (auto & window : ENGINE->windows().findWindows<CRecruitmentWindow>())
			window->close();
		for (auto & dialog : ENGINE->windows().findWindows<CInfoWindow>())
			dialog->close();
	};

	auto recruitWindows = ENGINE->windows().findWindows<CRecruitmentWindow>();
	const int bought = recruitWindows.empty() ? 0
		: recruitWindows.back()->agentBuy(step.creature, step.amount);
	if (bought > 0)
	{
		townScreenCloseAt = tickFrames.load() + TOWN_SCREEN_DWELL_FRAMES;
		appendResultSeq(step.cmdSeq,
			"OK recruit_ui_bought town=" + std::to_string(step.townIdx) + " creature="
			+ std::to_string(step.creature.getNum()) + " amount=" + std::to_string(bought)
			+ " requested=" + std::to_string(step.amount) + " dest=" + step.dest);
		return;
	}
	if (bought < 0)
	{
		// the requested creature is not offered by this window (level/upgrade changed meanwhile)
		cleanupFailedRecruit();
		appendResultSeq(step.cmdSeq,
			"ERR recruit_ui_no_creature town=" + std::to_string(step.townIdx) + " creature="
			+ std::to_string(step.creature.getNum()) + " level="
			+ std::to_string(step.zeroLevel + 1));
		return;
	}
	// the window refused (nothing affordable / no free slot): do not send an illegal packet
	cleanupFailedRecruit();
	appendResultSeq(step.cmdSeq, "ERR recruit_ui_no_amount town=" + std::to_string(step.townIdx)
		+ " creature=" + std::to_string(step.creature.getNum())
		+ " requested=" + std::to_string(step.amount)
		+ " dest=" + step.dest);
}

void doBuild(std::istringstream & in)
{
	int townIdx = -1, building = -1;
	in >> townIdx >> building;
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	auto towns = cb->getTownsInfo(true);
	if (townIdx < 0 || townIdx >= static_cast<int>(towns.size()))
	{
		appendResult("ERR bad_town_index");
		return;
	}
	const auto state = cb->canBuildStructure(towns[townIdx], BuildingID(building));
	if (state != EBuildingState::ALLOWED)
	{
		appendResult(std::string("ERR cannot_build state=")
			+ std::to_string(static_cast<int>(state)) + " name=" + buildingStateName(state)
			+ " (see town_info)");
		return;
	}
	// build from the open town screen, like a player clicking the building in the town hall
	const bool ui = openTownScreen(towns[townIdx], true);
	cb->buildBuilding(towns[townIdx], BuildingID(building));
	appendResult("OK build " + std::to_string(building) + (ui ? " ui=1" : " ui=0"));
}

void doRecruit(std::istringstream & in)
{
	int townIdx = -1, level = -1, amount = -1;
	in >> townIdx >> level >> amount;
	// Optional 4th token: an explicit creature id (base or upgraded variant of that level).
	// Without it the level's base creature is used, which is what older callers expect.
	CreatureID requestedCreature = CreatureID::NONE;
	{
		int creatureId = -1;
		if (in >> creatureId)
		{
			if (creatureId < 0)
			{
				appendResult("ERR bad_creature");
				return;
			}
			requestedCreature = CreatureID(creatureId);
		}
	}
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	auto towns = cb->getTownsInfo(true);
	if (townIdx < 0 || townIdx >= static_cast<int>(towns.size()))
	{
		appendResult("ERR bad_town_index");
		return;
	}
	const CGTownInstance * town = towns[townIdx];
	if (level < 1 || level > static_cast<int>(town->creatures.size()) || town->creatures[level - 1].second.empty())
	{
		appendResult("ERR bad_level");
		return;
	}
	if (amount <= 0)
	{
		appendResult("ERR bad_amount");
		return;
	}
	const auto & variants = town->creatures[level - 1].second;
	CreatureID creature = variants.front();
	if (requestedCreature != CreatureID::NONE)
	{
		bool offered = false;
		for (const auto & variant : variants)
			if (variant == requestedCreature)
				offered = true;
		if (!offered)
		{
			appendResult("ERR bad_creature level=" + std::to_string(level)
				+ " creature=" + std::to_string(requestedCreature.getNum()));
			return;
		}
		creature = requestedCreature;
	}
	const int zeroLevel = level - 1;

	// The recruitment window and CBuyArmy take their destination implicitly: the garrisoned
	// hero's army if a hero is visiting, otherwise the town garrison.  Report which one is used
	// instead of leaving the caller to guess.
	const CArmedInstance * dst = town->getUpperArmy();
	const std::string dest = (dst == static_cast<const CArmedInstance *>(town)) ? "town" : "hero";

	if (!openTownScreen(town, true))
	{
		// GUI busy (dialog/battle on top): keep the packet-only fallback, same destination.
		cb->recruitCreatures(town, dst, creature, amount, zeroLevel);
		appendResult("OK recruit town=" + std::to_string(townIdx) + " creature=" + std::to_string(creature.getNum())
			+ " amount=" + std::to_string(amount) + " dest=" + dest + " ui=0");
		return;
	}

	// The recruitment window is exactly what the player gets by clicking the creature icon in the
	// town screen; its callback sends the same CBuyArmy packet this command used to send directly.
	auto recruitCb = [town, dst, zeroLevel](CreatureID id, int count) {
		GAME->interface()->cb->recruitCreatures(town, dst, id, count, zeroLevel);
	};
	ENGINE->windows().createAndPushWindow<CRecruitmentWindow>(town, zeroLevel, dst,
		recruitCb, nullptr, -87);
	// BUY is pressed from the frame tick a moment later, so the dialog is actually visible.
	// Keep the requesting command's seq: the BUY completes asynchronously and its reply must
	// never borrow the seq of a later command.
	pendingRecruit = PendingRecruitStep{};
	pendingRecruit.active = true;
	pendingRecruit.framesLeft = RECRUIT_DIALOG_FRAMES;
	pendingRecruit.cmdSeq = currentCmdSeq.load();
	pendingRecruit.townIdx = townIdx;
	pendingRecruit.town = town;
	pendingRecruit.creature = creature;
	pendingRecruit.zeroLevel = zeroLevel;
	pendingRecruit.amount = amount;
	pendingRecruit.dest = dest;
	appendResult("OK recruit_ui_open town=" + std::to_string(townIdx) + " creature="
		+ std::to_string(creature.getNum()) + " amount=" + std::to_string(amount)
		+ " dest=" + dest + " dialog_frames=" + std::to_string(RECRUIT_DIALOG_FRAMES));
}


// ---------------------------------------------------------------- battle helpers
// The UI builds a BattleAction and sends it through IBattleCallback::battleMakeUnitAction;
// we do exactly the same, which keeps the agent on the player path while avoiding the
// UI animation state machine (onHexLeftClicked is ignored until animations settle).
struct BattleCtx
{
	std::shared_ptr<BattleInterface> battleInt;
	std::shared_ptr<CPlayerBattleCallback> battle;
	const CStack * active = nullptr;
	bool mine = false; // true when the active stack belongs to our side

	bool acquire()
	{
		battleInt = CPlayerInterface::battleInt;
		if (!battleInt)
			return false;
		battle = battleInt->getBattle();
		if (!battle)
			return false;
		const int activeId = battle->getBattle()->getActiveStackID();
		active = battle->battleGetStackByID(activeId, true);
		if (!active)
			return false;
		// Never send battle actions while the other side is active: doing so corrupted the
		// server-side battle state machine (the battle then waited forever for the AI move).
		const auto myColor = battle->getPlayerID();
		mine = myColor.has_value() && active->getOwner() == *myColor;
		return true;
	}
};

std::string hexPairJson(const BattleHex & hex)
{
	if (!hex.isValid())
		return "[null,null]";
	return "[" + std::to_string(hex.getX()) + "," + std::to_string(hex.getY()) + "]";
}

// T35: spellcasting names/validation helpers.  The bridge never invents legality: it asks the same
// engine queries the spellbook UI uses and reports the engine's own problem names.
std::string spellCastProblemName(ESpellCastProblem problem)
{
	switch (problem)
	{
	case ESpellCastProblem::OK: return "ok";
	case ESpellCastProblem::NO_HERO_TO_CAST_SPELL: return "no_hero_to_cast_spell";
	case ESpellCastProblem::CASTS_PER_TURN_LIMIT: return "casts_per_turn_limit";
	case ESpellCastProblem::NO_SPELLBOOK: return "no_spellbook";
	case ESpellCastProblem::HERO_DOESNT_KNOW_SPELL: return "hero_doesnt_know_spell";
	case ESpellCastProblem::NOT_ENOUGH_MANA: return "not_enough_mana";
	case ESpellCastProblem::ADVMAP_SPELL_INSTEAD_OF_BATTLE_SPELL: return "adventure_spell_in_battle";
	case ESpellCastProblem::SPELL_LEVEL_LIMIT_EXCEEDED: return "spell_level_limit_exceeded";
	case ESpellCastProblem::NO_SPELLS_TO_DISPEL: return "no_spells_to_dispel";
	case ESpellCastProblem::NO_APPROPRIATE_TARGET: return "no_appropriate_target";
	case ESpellCastProblem::STACK_IMMUNE_TO_SPELL: return "stack_immune_to_spell";
	case ESpellCastProblem::WRONG_SPELL_TARGET: return "wrong_spell_target";
	case ESpellCastProblem::ONGOING_TACTIC_PHASE: return "ongoing_tactic_phase";
	case ESpellCastProblem::MAGIC_IS_BLOCKED: return "magic_is_blocked";
	default: return "invalid";
	}
}

std::string battleTargetKindName(PossiblePlayerBattleAction::Actions action)
{
	switch (action)
	{
	case PossiblePlayerBattleAction::NO_LOCATION: return "none";
	case PossiblePlayerBattleAction::AIMED_SPELL_CREATURE: return "unit";
	case PossiblePlayerBattleAction::ANY_LOCATION: return "hex";
	case PossiblePlayerBattleAction::FREE_LOCATION: return "hex";
	case PossiblePlayerBattleAction::OBSTACLE: return "hex";
	default: return "unsupported";
	}
}

// F2/P2 (audit 2026-10-27, T42): two-stage battle spells cannot be expressed as the single
// BattleAction this bridge sends - Teleport needs a source stack + a destination hex, Sacrifice
// needs a victim stack + the amount and then a target for the raised creatures.  Guessing a
// one-stage action either does nothing or corrupts the battle state, so both the spellbook surface
// (battle_actions: can_cast=false / reason=two_stage_unsupported / two_stage=true) and the action
// entry point (ERR spell_target_unsupported <name> two_stage, before any action is submitted) mark
// them explicitly.  Detection uses the engine SpellID constants plus the stable core config
// identifiers, so it does not depend on the localized display name.
bool isTwoStageSpell(const CSpell * spell)
{
	if (!spell)
		return false;
	if (spell->id.getNum() == SpellID::TELEPORT || spell->id.getNum() == SpellID::SACRIFICE)
		return true;
	return spell->identifier == "teleport" || spell->identifier == "sacrifice";
}

std::string problemText(spells::Problem & problem)
{
	std::vector<std::string> texts;
	problem.getAll(texts);
	std::string out;
	for (const auto & text : texts)
	{
		if (!out.empty())
			out += "; ";
		out += text;
	}
	return out;
}

// One target's legal options for the currently active stack, using the same engine queries the
// player UI uses (BattleFieldController / BattleActionsController):
//   melee:  battleCanAttackUnit + battleCanAttackHex + a reachable attack-from hex
//   shoot:  battleCanShoot(attacker, targetHex)
// The attack-from hex makes walk-and-attack explicit (two-hex creatures and long weapons use the
// engine's own direction logic instead of simple adjacency guesses).
struct BattleTargetLegality
{
	BattleHex attackFrom = BattleHex::INVALID;
	bool canMelee = false;
	bool walk = false;
	bool canShoot = false;
	bool adjacent = false;
};

BattleTargetLegality battleTargetLegality(const BattleCtx & ctx, const CStack * target)
{
	BattleTargetLegality out;
	if (!ctx.active || !target)
		return out;
	const BattleHex myHex = ctx.active->getPosition();
	const BattleHex targetHex = target->getPosition();
	if (!targetHex.isValid())
		return out;
	out.adjacent = myHex.getDistance(myHex, targetHex) == 1;
	out.canShoot = ctx.battle->battleCanShoot(ctx.active, targetHex);
	if (!ctx.battle->battleCanAttackUnit(ctx.active, target))
		return out;
	const BattleHexArray available = ctx.battle->battleGetAvailableHexes(ctx.active, false);
	if (!ctx.battle->battleCanAttackHex(available, ctx.active, targetHex))
		return out;
	for (int direction = 0; direction < 8; ++direction)
	{
		const auto dir = static_cast<BattleHex::EDir>(direction);
		if (!ctx.battle->battleCanAttackHex(available, ctx.active, targetHex, dir))
			continue;
		const BattleHex from = ctx.battle->fromWhichHexAttack(ctx.active, targetHex, dir);
		if (!from.isValid())
			continue;
		out.attackFrom = from;
		out.canMelee = true;
		out.walk = (from != myHex);
		break;
	}
	return out;
}

void sendBattleAction(const BattleAction & action)
{
	BattleCtx ctx;
	if (!ctx.acquire())
	{
		appendResult("ERR no_active_stack");
		return;
	}
	if (!ctx.mine)
	{
		appendResult("ERR not_my_turn");
		return;
	}
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	cb->battleMakeUnitAction(ctx.battleInt->getBattleID(), action);
}

void doBattleClick(std::istringstream & in)
{
	int x = -1, y = -1;
	in >> x >> y;
	auto battleInt = CPlayerInterface::battleInt;
	if (!battleInt)
	{
		appendResult("ERR no_battle");
		return;
	}
	const BattleHex hex(x, y);
	if (!hex.isValid())
	{
		appendResult("ERR bad_hex");
		return;
	}
	battleInt->actionsController->onHexLeftClicked(hex);
	appendResult("OK battle_click " + std::to_string(x) + " " + std::to_string(y));
}



void doBattleMove(std::istringstream & in)
{
	int x = -1, y = -1;
	in >> x >> y;
	BattleCtx ctx;
	if (!ctx.acquire())
	{
		appendResult("ERR no_active_stack");
		return;
	}
	if (!ctx.mine)
	{
		appendResult("ERR not_my_turn");
		return;
	}
	const BattleHex dest(x, y);
	if (!dest.isValid())
	{
		appendResult("ERR bad_hex");
		return;
	}
	const BattleHexArray available = ctx.battle->battleGetAvailableHexes(ctx.active, false);
	const BattleHex to = ctx.battle->toWhichHexMove(available, ctx.active, dest);
	if (!to.isValid() || ctx.battle->battleGetUnitByPos(to, true))
	{
		appendResult("ERR illegal_move hex=" + std::to_string(x) + "," + std::to_string(y)
			+ " (see battle_actions)");
		return;
	}
	auto cb = localCallback();
	cb->battleMakeUnitAction(ctx.battleInt->getBattleID(), BattleAction::makeMove(ctx.active, to));
	appendResult("OK battle_move " + std::to_string(to.getX()) + " " + std::to_string(to.getY()));
}

void doBattleAttack(std::istringstream & in)
{
	int targetId = -1;
	std::string mode = "auto";
	in >> targetId;
	in >> mode;
	if (mode != "auto" && mode != "melee" && mode != "shot")
	{
		appendResult("ERR bad_mode (auto|melee|shot)");
		return;
	}
	BattleCtx ctx;
	if (!ctx.acquire())
	{
		appendResult("ERR no_active_stack");
		return;
	}
	if (!ctx.mine)
	{
		appendResult("ERR not_my_turn");
		return;
	}
	const CStack * target = ctx.battle->battleGetStackByID(targetId, true);
	if (!target)
	{
		appendResult("ERR no_target_stack");
		return;
	}
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	const BattleTargetLegality legality = battleTargetLegality(ctx, target);
	const std::string suffix = " target=" + std::to_string(targetId)
		+ " can_melee=" + (legality.canMelee ? "1" : "0")
		+ " walk=" + (legality.walk ? "1" : "0")
		+ " can_shoot=" + (legality.canShoot ? "1" : "0");
	const auto sendMelee = [&]() {
		cb->battleMakeUnitAction(ctx.battleInt->getBattleID(),
			BattleAction::makeMeleeAttack(ctx.active, target->getPosition(),
				legality.attackFrom, false));
	};
	const auto sendShot = [&]() {
		cb->battleMakeUnitAction(ctx.battleInt->getBattleID(),
			BattleAction::makeShotAttack(ctx.active, target));
	};

	if (mode == "shot")
	{
		if (!legality.canShoot)
		{
			appendResult("ERR cannot_shoot" + suffix);
			return;
		}
		sendShot();
		appendResult("OK battle_attack " + std::to_string(targetId) + " shot" + suffix);
		return;
	}
	if (mode == "melee")
	{
		if (!legality.canMelee)
		{
			appendResult("ERR cannot_melee" + suffix);
			return;
		}
		sendMelee();
		appendResult("OK battle_attack " + std::to_string(targetId) + " melee from="
			+ std::to_string(legality.attackFrom.getX()) + "," + std::to_string(legality.attackFrom.getY())
			+ suffix);
		return;
	}
	// auto: melee if walk-and-attack is legal, otherwise a legal shot.  NEVER a silent move.
	if (legality.canMelee)
	{
		sendMelee();
		appendResult("OK battle_attack " + std::to_string(targetId) + " melee from="
			+ std::to_string(legality.attackFrom.getX()) + "," + std::to_string(legality.attackFrom.getY())
			+ suffix);
		return;
	}
	if (legality.canShoot)
	{
		sendShot();
		appendResult("OK battle_attack " + std::to_string(targetId) + " shot" + suffix);
		return;
	}
	appendResult("ERR cannot_attack" + suffix);
}

// Explicit "move towards the target but do not attack".  The old battle_attack silently fell
// back to this; now it is a separate command so the caller's intent is unambiguous.
void doBattleApproach(std::istringstream & in)
{
	int targetId = -1;
	in >> targetId;
	BattleCtx ctx;
	if (!ctx.acquire())
	{
		appendResult("ERR no_active_stack");
		return;
	}
	if (!ctx.mine)
	{
		appendResult("ERR not_my_turn");
		return;
	}
	const CStack * target = ctx.battle->battleGetStackByID(targetId, true);
	if (!target)
	{
		appendResult("ERR no_target_stack");
		return;
	}
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	const BattleHex myHex = ctx.active->getPosition();
	const BattleHex targetHex = target->getPosition();
	const BattleHexArray available = ctx.battle->battleGetAvailableHexes(ctx.active, false);
	BattleHex best = BattleHex::INVALID;
	int bestScore = 1 << 30;
	for (const BattleHex & hex : available)
	{
		const BattleHex to = ctx.battle->toWhichHexMove(available, ctx.active, hex);
		if (!to.isValid() || to == myHex)
			continue;
		if (ctx.battle->battleGetUnitByPos(to, true))
			continue;
		int score = myHex.getDistance(myHex, to);
		if (to.getDistance(to, targetHex) == 1)
			score -= 1000;                              // strongly prefer contact
		score += to.getDistance(to, targetHex);
		if (score < bestScore)
		{
			bestScore = score;
			best = to;
		}
	}
	if (!best.isValid())
	{
		appendResult("ERR no_approach_hex target=" + std::to_string(targetId));
		return;
	}
	cb->battleMakeUnitAction(ctx.battleInt->getBattleID(), BattleAction::makeMove(ctx.active, best));
	appendResult("OK battle_approach " + std::to_string(targetId)
		+ " hex=" + std::to_string(best.getX()) + "," + std::to_string(best.getY())
		+ " dist=" + std::to_string(best.getDistance(best, targetHex)));
}

// On-demand legal-action dump for the active stack (read-only; writes battle_actions.json).
// Uses the same legality queries as the player UI so the caller can choose between melee,
// walk-and-attack, shooting and an explicit approach without guessing.
void doBattleActions()
{
	BattleCtx ctx;
	if (!ctx.acquire())
	{
		appendResult("ERR no_active_stack");
		return;
	}
	const BattleHexArray available = ctx.battle->battleGetAvailableHexes(ctx.active, false);
	std::string s = "{";
	s += "\"active\":" + std::to_string(ctx.active->unitId());
	s += ",\"mine\":" + std::string(ctx.mine ? "true" : "false");
	s += ",\"can_shoot_in_principle\":"
		+ std::string(ctx.battle->battleCanShoot(ctx.active) ? "true" : "false");
	s += ",\"actor\":{\"id\":" + std::to_string(ctx.active->unitId());
	s += ",\"creature\":" + std::to_string(ctx.active->creatureId().getNum());
	s += ",\"count\":" + std::to_string(ctx.active->getCount());
	s += ",\"hex\":" + hexPairJson(ctx.active->getPosition());
	s += ",\"shots\":" + std::to_string(ctx.active->shots.available());
	s += ",\"counterattacks\":" + std::to_string(ctx.active->counterAttacks.available());
	s += "}";
	s += ",\"move_hexes\":[";
	bool first = true;
	for (const BattleHex & hex : available)
	{
		if (!first) s += ",";
		first = false;
		s += hexPairJson(hex);
	}
	s += "]";
	s += ",\"targets\":[";
	first = true;
	int meleeCount = 0;
	int shootCount = 0;
	for (const CStack * stack : ctx.battle->battleGetStacks())
	{
		if (!stack || !stack->alive() || stack->unitSide() == ctx.active->unitSide())
			continue;
		const BattleTargetLegality legality = battleTargetLegality(ctx, stack);
		if (legality.canMelee) ++meleeCount;
		if (legality.canShoot) ++shootCount;
		if (!first) s += ",";
		first = false;
		s += "{\"id\":" + std::to_string(stack->unitId());
		s += ",\"creature\":" + std::to_string(stack->creatureId().getNum());
		s += ",\"count\":" + std::to_string(stack->getCount());
		s += ",\"hex\":" + hexPairJson(stack->getPosition());
		s += ",\"adjacent\":" + std::string(legality.adjacent ? "true" : "false");
		s += ",\"can_melee\":" + std::string(legality.canMelee ? "true" : "false");
		s += ",\"walk\":" + std::string(legality.walk ? "true" : "false");
		s += ",\"attack_from\":" + hexPairJson(legality.attackFrom);
		s += ",\"can_shoot\":" + std::string(legality.canShoot ? "true" : "false");
		s += "}";
	}
	s += "]";
	// Turn order is player-visible in the battle UI; expose the current and next round only.
	std::vector<battle::Units> turns;
	ctx.battle->battleGetTurnOrder(turns, 24, 2);
	s += ",\"turn_order\":[";
	first = true;
	for (const auto & round : turns)
	{
		for (const auto * unit : round)
		{
			if (!unit) continue;
			if (!first) s += ",";
			first = false;
			s += std::to_string(unit->unitId());
		}
	}
	s += "]";
	// T35: hero spellcasting surface.  All legality/cost facts come from the engine queries the
	// battle spellbook itself uses (battleCanCastSpell / battleGetSpellCost / getCasterAction).
	if (const CGHeroInstance * hero = ctx.battle->battleGetMyHero())
	{
		const ESpellCastProblem general = ctx.battle->battleCanCastSpell(hero, spells::Mode::HERO);
		const BattleSide mySide = ctx.battle->battleGetMySide();
		const int castsUsed = ctx.battle->getBattle()->getCastSpells(mySide);
		s += ",\"hero\":{\"mana\":" + std::to_string(hero->mana);
		s += ",\"mana_max\":" + std::to_string(hero->manaLimit());
		s += ",\"spellbook\":" + std::string(hero->hasSpellbook() ? "true" : "false");
		s += ",\"can_cast\":"
			+ std::string((ctx.mine && general == ESpellCastProblem::OK) ? "true" : "false");
		s += ",\"reason\":" + q(ctx.mine ? spellCastProblemName(general)
			: std::string("not_my_turn"));
		s += ",\"casts_used\":" + std::to_string(castsUsed);
		s += "}";
		s += ",\"spells\":[";
		bool firstSpell = true;
		for (const SpellID & spellID : hero->getSpellsInSpellbook())
		{
			const CSpell * spell = spellById(spellID.getNum());
			if (!spell)
				continue;
			// The spellbook also holds adventure-map spells; those cannot be cast in battle.
			if (!spell->isCombat())
				continue;
			const PossiblePlayerBattleAction sel =
				ctx.battle->getCasterAction(spell, hero, spells::Mode::HERO);
			const std::string targetKind = battleTargetKindName(sel.get());
			const int cost = ctx.battle->battleGetSpellCost(spell, hero);
			// F2/P2 (T42): a two-stage spell is never castable through this bridge, regardless of
			// the turn/mana state; mark it explicitly so the legality list matches the action path.
			const bool twoStage = isTwoStageSpell(spell);
			std::string reason;
			if (twoStage)
				reason = "two_stage_unsupported";
			else if (general != ESpellCastProblem::OK)
				reason = spellCastProblemName(general);
			else if (!ctx.mine)
				reason = "not_my_turn";
			else if (targetKind == "unsupported")
				reason = "target_type_unsupported";
			else if (cost > hero->mana)
				reason = "not_enough_mana";
			if (!firstSpell) s += ",";
			firstSpell = false;
			s += "{\"id\":" + std::to_string(spellID.getNum());
			s += ",\"name\":" + q(spell->getNameTranslated());
			s += ",\"cost\":" + std::to_string(cost);
			s += ",\"can_cast\":" + std::string(reason.empty() ? "true" : "false");
			s += ",\"reason\":" + q(reason);
			s += ",\"target_kind\":" + q(targetKind);
			if (twoStage)
				s += ",\"two_stage\":true";       // F2/P2: additive, only for Teleport/Sacrifice
			s += "}";
		}
		s += "]";
	}
	s += "}";
	writeTextFile("battle_actions.json", s);
	appendResult("OK battle_actions active=" + std::to_string(ctx.active->unitId())
		+ " melee=" + std::to_string(meleeCount) + " shoot=" + std::to_string(shootCount)
		+ " (see battle_actions.json)");
}

void doBattleDefend()
{
	BattleCtx ctx;
	if (!ctx.acquire())
	{
		appendResult("ERR no_active_stack");
		return;
	}
	if (!ctx.mine)
	{
		appendResult("ERR not_my_turn");
		return;
	}
	auto cb = localCallback();
	cb->battleMakeUnitAction(ctx.battleInt->getBattleID(), BattleAction::makeDefend(ctx.active));
	appendResult("OK battle_defend");
}

void doBattleWait()
{
	BattleCtx ctx;
	if (!ctx.acquire())
	{
		appendResult("ERR no_active_stack");
		return;
	}
	if (!ctx.mine)
	{
		appendResult("ERR not_my_turn");
		return;
	}
	auto cb = localCallback();
	cb->battleMakeUnitAction(ctx.battleInt->getBattleID(), BattleAction::makeWait(ctx.active));
	appendResult("OK battle_wait");
}


void doBattleResultOk()
{
	// The victory/defeat window is not a popup and is not reachable through the normal
	// window stack; call its exit callback - exactly what pressing the OK button does.
	auto wins = ENGINE->windows().findWindows<BattleResultWindow>();
	if (wins.empty())
	{
		appendResult("ERR no_result_window");
		return;
	}
	wins.front()->bExitf();
	appendResult("OK battle_result_ok");
}





void doMarketTrade(std::istringstream & in)
{
	// mode: 0 = resource<->resource (classic marketplace).  sell/buy are resource indexes
	// (0 wood .. 6 gold) for mode 0.  Uses CCallback::trade - the same request the market
	// window sends when the player clicks the trade button.
	int townIdx = -1, mode = 0, sell = -1, buy = -1, amount = 1;
	in >> townIdx >> mode >> sell >> buy >> amount;
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	auto towns = cb->getTownsInfo(true);
	if (townIdx < 0 || townIdx >= static_cast<int>(towns.size()))
	{
		appendResult("ERR bad_town_index");
		return;
	}
	const auto marketMode = static_cast<EMarketMode>(mode);
	const IMarket * market = cb->getMarket(towns[townIdx]->id);
	if (!market)
	{
		appendResult("ERR no_market town=" + std::to_string(townIdx));
		return;
	}
	if (mode != static_cast<int>(EMarketMode::RESOURCE_RESOURCE))
	{
		appendResult("ERR unsupported_market_mode mode=" + std::to_string(mode));
		return;
	}
	if (sell < 0 || sell > 6 || buy < 0 || buy > 6 || sell == buy)
	{
		appendResult("ERR bad_resource sell=" + std::to_string(sell) + " buy=" + std::to_string(buy));
		return;
	}
	if (amount <= 0)
	{
		appendResult("ERR bad_amount");
		return;
	}
	if (!market->allowsTrade(marketMode))
	{
		appendResult("ERR market_mode_unsupported");
		return;
	}
	const ResourceSet have = cb->getResourceAmount();
	if (have[GameResID(sell)] < amount)
	{
		appendResult("ERR not_enough_resource sell=" + std::to_string(sell) + " have="
			+ std::to_string(have[GameResID(sell)]) + " amount=" + std::to_string(amount));
		return;
	}
	int give = 0;
	int receive = 0;
	const bool quoted = market->getOffer(sell, buy, give, receive, EMarketMode::RESOURCE_RESOURCE);
	cb->trade(towns[townIdx]->id, marketMode, TradeItemSell(GameResID(sell)),
	          TradeItemBuy(GameResID(buy)), amount, nullptr);
	appendResult("OK market_trade town=" + std::to_string(townIdx) + " mode=" + std::to_string(mode)
		+ " sell=" + std::to_string(sell) + " buy=" + std::to_string(buy)
		+ " amount=" + std::to_string(amount)
		+ (quoted ? (" est_receive=" + std::to_string(receive > 0 && give > 0
			? (amount * receive) / give : 0)) : " est_receive=-1"));
}



// T35: battle hero spellcasting.  The client spellbook builds a BattleAction with
// EActionType::HERO_SPELL (stackNumber=-1 for the hero) and sends it through
// CBattleCallback::battleMakeSpellAction; we do exactly the same, after asking the engine whether
// the cast is legal.  Only single-stage casts are supported (NO_LOCATION / AIMED_SPELL_CREATURE /
// ANY_LOCATION / FREE_LOCATION / OBSTACLE).
// F2/P2 (audit 2026-10-27, T42): two-stage spells (Teleport: stack+destination, Sacrifice:
// victim+amount+target) are refused explicitly by SpellID BEFORE any action is built/submitted -
// the engine's getCasterAction() cannot express the second decision, and submitting the first
// single-target action would be a silent no-op.  The refusal text is the promised
// `spell_target_unsupported <name> two_stage`; no mana is spent and no packet is sent.
void doBattleCastSpell(std::istringstream & in)
{
	int spellId = -1;
	std::string targetMode;
	int targetStackId = -1, targetX = -1, targetY = -1;
	in >> spellId;
	if (in)
		in >> targetMode;
	if (targetMode == "unit")
		in >> targetStackId;
	else if (targetMode == "hex")
		in >> targetX >> targetY;
	else
		targetMode = "none";
	if (spellId < 0)
	{
		appendResult("ERR bad_spell");
		return;
	}
	auto battleInt = CPlayerInterface::battleInt;
	if (!battleInt)
	{
		appendResult("ERR no_battle");
		return;
	}
	auto battle = battleInt->getBattle();
	if (!battle)
	{
		appendResult("ERR no_battle");
		return;
	}
	const CGHeroInstance * hero = battle->battleGetMyHero();
	if (!hero)
	{
		appendResult("ERR no_hero");
		return;
	}
	const CSpell * spell = spellById(spellId);
	if (!spell)
	{
		appendResult("ERR bad_spell");
		return;
	}
	// F2/P2 (T42): deterministic capability refusal, evaluated before any spellbook/mana/turn
	// legality query so the answer never depends on the hero's state.  The bridge cannot express
	// the second decision of a two-stage spell, so it refuses instead of submitting a one-stage
	// BattleAction: no mana is spent, no packet leaves the client, the reply is ERR (never OK).
	if (isTwoStageSpell(spell))
	{
		appendResult("ERR spell_target_unsupported " + spell->getNameTranslated() + " two_stage");
		return;
	}
	if (!spell->isCombat())
	{
		appendResult("ERR not_combat_spell");
		return;
	}
	if (!hero->spellbookContainsSpell(SpellID(spellId)))
	{
		appendResult("ERR spell_not_known");
		return;
	}
	BattleCtx ctx;
	if (!ctx.acquire())
	{
		appendResult("ERR no_active_stack");
		return;
	}
	if (!ctx.mine)
	{
		appendResult("ERR not_my_turn");
		return;
	}
	const ESpellCastProblem general = ctx.battle->battleCanCastSpell(hero, spells::Mode::HERO);
	if (general != ESpellCastProblem::OK)
	{
		appendResult("ERR cast_blocked " + spellCastProblemName(general));
		return;
	}
	{
		spells::detail::ProblemImpl castProblem;
		if (!spell->canBeCast(castProblem, ctx.battle.get(), spells::Mode::HERO, hero))
		{
			const std::string text = problemText(castProblem);
			appendResult("ERR spell_cannot_be_cast"
				+ (text.empty() ? std::string() : " " + text));
			return;
		}
	}
	const int cost = ctx.battle->battleGetSpellCost(spell, hero);
	if (cost < 0)
	{
		appendResult("ERR spell_cost_unknown");
		return;
	}
	if (hero->mana < cost)
	{
		appendResult("ERR not_enough_mana cost=" + std::to_string(cost)
			+ " mana=" + std::to_string(hero->mana));
		return;
	}
	const PossiblePlayerBattleAction sel =
		ctx.battle->getCasterAction(spell, hero, spells::Mode::HERO);
	const auto kind = sel.get();
	BattleAction action;
	action.actionType = EActionType::HERO_SPELL;
	action.stackNumber = -1;             // -1 = the hero, exactly as the battle spellbook sends it
	action.side = ctx.battle->battleGetMySide();
	action.spell = SpellID(spellId);
	std::string targetDesc = "none";
	switch (kind)
	{
	case PossiblePlayerBattleAction::NO_LOCATION:
		action.aimToHex(BattleHex::INVALID);
		break;
	case PossiblePlayerBattleAction::AIMED_SPELL_CREATURE:
	{
		if (targetMode != "unit")
		{
			appendResult("ERR target_required unit <stackId>");
			return;
		}
		const CStack * target = ctx.battle->battleGetStackByID(targetStackId, true);
		if (!target)
		{
			appendResult("ERR no_target_stack");
			return;
		}
		const BattleHex pos = target->getPosition();
		action.aimToHex(pos);
		targetDesc = "unit=" + std::to_string(targetStackId)
			+ " hex=" + std::to_string(pos.getX()) + "," + std::to_string(pos.getY());
		break;
	}
	case PossiblePlayerBattleAction::ANY_LOCATION:
	case PossiblePlayerBattleAction::FREE_LOCATION:
	case PossiblePlayerBattleAction::OBSTACLE:
	{
		if (targetMode != "hex")
		{
			appendResult("ERR target_required hex <x> <y>");
			return;
		}
		const BattleHex pos(targetX, targetY);
		if (!pos.isValid())
		{
			appendResult("ERR bad_hex");
			return;
		}
		action.aimToHex(pos);
		targetDesc = "hex=" + std::to_string(targetX) + "," + std::to_string(targetY);
		break;
	}
	default:
		appendResult("ERR spell_target_unsupported kind="
			+ std::to_string(static_cast<int>(kind))
			+ " target_kind=" + battleTargetKindName(kind));
		return;
	}
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	cb->battleMakeSpellAction(battleInt->getBattleID(), action);
	appendResult("OK battle_cast_spell spell=" + std::to_string(spellId)
		+ " cost=" + std::to_string(cost)
		+ " target=" + targetDesc
		+ " mana_before=" + std::to_string(hero->mana));
}

void doCastSpell(std::istringstream & in)
{
	// Adventure-map spell: <heroIdx> <spellId> [x y z].  Same request the spellbook UI sends
	// (CCallback::castSpell; pos is int3(-1,-1,-1) for no-target spells, a tile for ranged ones).
	int heroIdx = 0, spellId = -1, x = -1, y = -1, z = 0;
	in >> heroIdx >> spellId >> x >> y >> z;
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	auto heroes = cb->getHeroesInfo();
	if (heroIdx < 0 || heroIdx >= static_cast<int>(heroes.size()))
	{
		appendResult("ERR bad_hero_index");
		return;
	}
	const CGHeroInstance * hero = heroes[heroIdx];
	const CSpell * spell = spellById(spellId);
	if (!spell)
	{
		appendResult("ERR bad_spell");
		return;
	}
	if (!spell->isAdventure())
	{
		appendResult("ERR not_adventure_spell (combat spells are cast with battle_cast_spell)");
		return;
	}
	if (!hero->spellbookContainsSpell(SpellID(spellId)))
	{
		appendResult("ERR spell_not_known");
		return;
	}
	const int cost = hero->getSpellCost(spell);
	if (cost < 0)
	{
		appendResult("ERR spell_cost_unknown");
		return;
	}
	if (hero->mana < cost)
	{
		appendResult("ERR not_enough_mana cost=" + std::to_string(cost)
			+ " mana=" + std::to_string(hero->mana));
		return;
	}
	{
		spells::detail::ProblemImpl problem;
		if (!spell->getAdventureMechanics().canBeCast(problem, cb.get(), hero))
		{
			const std::string text = problemText(problem);
			appendResult("ERR adventure_cast_blocked"
				+ (text.empty() ? std::string() : " " + text));
			return;
		}
	}
	const bool ranged =
		spell->getAdventureMechanics().getEffectAs<AdventureSpellRangedEffect>(hero) != nullptr;
	int3 pos(-1, -1, -1);
	if (ranged)
	{
		if (x < 0 || y < 0)
		{
			appendResult("ERR target_required (this adventure spell needs x y z)");
			return;
		}
		pos = int3(x, y, z);
		spells::detail::ProblemImpl atProblem;
		if (!spell->getAdventureMechanics().canBeCastAt(atProblem, cb.get(), hero, pos))
		{
			const std::string text = problemText(atProblem);
			appendResult("ERR bad_target" + (text.empty() ? std::string() : " " + text));
			return;
		}
	}
	cb->castSpell(hero, SpellID(spellId), pos);
	appendResult("OK cast_spell hero=" + std::to_string(heroIdx) + " spell=" + std::to_string(spellId)
		+ " cost=" + std::to_string(cost)
		+ " ranged=" + std::string(ranged ? "1" : "0")
		+ " at=" + std::to_string(pos.x) + "," + std::to_string(pos.y) + "," + std::to_string(pos.z)
		+ " mana_before=" + std::to_string(hero->mana));
}

void doSaveGame(std::istringstream & in)
{
	// Save the running game to a named slot (no confirmation window in agent mode).
	// Names are resolved by the server's resource layer; pass e.g. "Saves/agent/day3".
	std::string name;
	in >> name;
	if (name.empty())
	{
		appendResult("ERR save_name_required");
		return;
	}
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	cb->save(name, false);
	appendResult("OK save_game " + name);
}

void doLoadGame(std::istringstream & in)
{
	// Quick-load a save into the running client (server reloads game state in place).
	std::string name;
	in >> name;
	if (name.empty())
	{
		appendResult("ERR load_name_required");
		return;
	}
	try
	{
		// The loaded save replaces the whole game state: cached JSON describes the old game.
		invalidateViewCaches();
		GAME->server().quickLoadGame(name);
		appendResult("OK load_game " + name);
	}
	catch (const std::exception & e)
	{
		appendResult(std::string("ERR load_failed ") + e.what());
	}
}

void doRecruitHero(std::istringstream & in)
{
	int townIdx = -1, heroIdx = 0;
	in >> townIdx >> heroIdx;
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	auto towns = cb->getTownsInfo(true);
	if (townIdx < 0 || townIdx >= static_cast<int>(towns.size()))
	{
		appendResult("ERR bad_town_index");
		return;
	}
	const CGTownInstance * town = towns[townIdx];

	// T40/C11 - deterministic refusals before the asynchronous HireHero request leaves the client.
	// The old code answered "OK recruit_hero" right after CCallback::recruitHero(), but that only
	// *submits* the request: HeroPoolProcessor::hireHero() silently refuses (CGameHandler::complain
	// -> INFO text, no reply packet) in several cases, so the bridge reported success while no
	// gold was spent and no hero appeared (C8/5D-4 root cause).  Each check below mirrors one
	// upstream server condition, so a doomed hire is now an explicit ERR the MCP reports as
	// rejected.  The success path is unchanged.
	if (const CGHeroInstance * visitor = town->getVisitingHero())
	{
		// upstream: town->getVisitingHero() && complain("There is visiting hero - no place!")
		appendResult("ERR recruit_hero_refused visiting_hero name=" + visitor->getNameTranslated());
		return;
	}
	if (!town->hasBuilt(BuildingID::TAVERN))
	{
		// upstream: !town->hasBuilt(BuildingID::TAVERN) && complain("No tavern!")
		appendResult("ERR recruit_hero_refused no_tavern");
		return;
	}
	{
		const auto me = cb->getPlayerID();
		if (me && cb->getPlayerRelations(town->tempOwner, *me) == PlayerRelations::ENEMIES)
		{
			// upstream: getPlayerRelations(...) == ENEMIES && complain("Can't buy hero in enemy town!")
			appendResult("ERR recruit_hero_refused enemy_town");
			return;
		}
	}
	const int gold = cb->getResourceAmount(EGameResID::GOLD);
	if (gold < GameConstants::HERO_GOLD_COST)
	{
		// upstream: playerState->resources[GOLD] < HERO_GOLD_COST && complain("Not enough gold...")
		appendResult("ERR recruit_hero_refused not_enough_gold cost="
			+ std::to_string(GameConstants::HERO_GOLD_COST) + " have=" + std::to_string(gold));
		return;
	}
	try
	{
		// Upstream also caps wandering heroes and heroes+garrisoned heroes separately.  Both caps
		// come from map settings (engine-provided), so the client can predict them exactly.
		const int64_t wanderingHeroes = cb->howManyHeroes(false);
		const int64_t allHeroes = cb->howManyHeroes(true);
		const int64_t capOnMap =
			cb->getSettings().getInteger(EGameSettings::HEROES_PER_PLAYER_ON_MAP_CAP);
		const int64_t capTotal =
			cb->getSettings().getInteger(EGameSettings::HEROES_PER_PLAYER_TOTAL_CAP);
		if (capOnMap > 0 && wanderingHeroes >= capOnMap)
		{
			appendResult("ERR recruit_hero_refused hero_cap_on_map cap="
				+ std::to_string(capOnMap) + " heroes=" + std::to_string(wanderingHeroes));
			return;
		}
		if (capTotal > 0 && allHeroes >= capTotal)
		{
			appendResult("ERR recruit_hero_refused hero_cap_total cap="
				+ std::to_string(capTotal) + " heroes=" + std::to_string(allHeroes));
			return;
		}
	}
	catch (...)
	{
		// A settings read must never make a valid hire unreachable: fall through to the server.
	}

	const auto available = cb->getAvailableHeroes(town);
	if (available.empty() || heroIdx < 0 || heroIdx >= static_cast<int>(available.size()))
	{
		appendResult("ERR no_such_available_hero");
		return;
	}
	cb->recruitHero(town, available[heroIdx], HeroTypeID::NONE);
	appendResult("OK recruit_hero town=" + std::to_string(townIdx) + " index=" + std::to_string(heroIdx)
		+ " name=" + available[heroIdx]->getNameTranslated()
		+ " cost=" + std::to_string(GameConstants::HERO_GOLD_COST)
		+ " gold_before=" + std::to_string(gold));
}

void doUpgradeCreature(std::istringstream & in)
{
	std::string ownerName;
	int idx = -1, slot = -1, newId = -1;
	in >> ownerName >> idx >> slot >> newId;
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	const CArmedInstance * armed = nullptr;
	if (ownerName == "hero")
	{
		auto heroes = cb->getHeroesInfo();
		if (idx >= 0 && idx < static_cast<int>(heroes.size())) armed = heroes[idx];
	}
	else if (ownerName == "town")
	{
		auto towns = cb->getTownsInfo(true);
		if (idx >= 0 && idx < static_cast<int>(towns.size())) armed = towns[idx];
	}
	if (!armed || !armed->Slots().count(SlotID(slot)))
	{
		appendResult("ERR bad_target");
		return;
	}
	const CStackInstance & stack = armed->getStack(SlotID(slot));
	UpgradeInfo info(stack.getCreatureID());
	try
	{
		cb->fillUpgradeInfo(armed, SlotID(slot), info);
	}
	catch (...)
	{
		appendResult("ERR upgrade_info_unavailable");
		return;
	}
	if (!info.canUpgrade())
	{
		appendResult("ERR not_upgradable creature=" + std::to_string(stack.getCreatureID().getNum())
			+ " (see army_info)");
		return;
	}
	const CreatureID wanted = newId >= 0 ? CreatureID(newId) : info.getUpgrade();
	bool allowed = false;
	for (const CreatureID & upgrade : info.getAvailableUpgrades())
		if (upgrade == wanted)
			allowed = true;
	if (!allowed)
	{
		appendResult("ERR bad_upgrade_creature creature=" + std::to_string(wanted.getNum())
			+ " (see army_info)");
		return;
	}
	const ResourceSet costPerUnit = info.getUpgradeCostsFor(wanted);
	ResourceSet totalCost;
	const ResourceSet have = cb->getResourceAmount();
	ResourceSet missing;
	for (int r = 0; r < 7; ++r)
	{
		totalCost[GameResID(r)] = costPerUnit[GameResID(r)] * stack.getCount();
		if (have[GameResID(r)] < totalCost[GameResID(r)])
			missing[GameResID(r)] = totalCost[GameResID(r)] - have[GameResID(r)];
	}
	if (missing[GameResID(0)] || missing[GameResID(1)] || missing[GameResID(2)]
		|| missing[GameResID(3)] || missing[GameResID(4)] || missing[GameResID(5)]
		|| missing[GameResID(6)])
	{
		appendResult("ERR cannot_afford missing=" + resourcesJson(missing));
		return;
	}
	// CCallback::upgradeCreature also only submits the request (it always returns false);
	// never treat that return value as failure - the MCP verifier checks the army state.
	cb->upgradeCreature(armed, SlotID(slot), wanted);
	appendResult("OK upgrade " + ownerName + " " + std::to_string(idx) + " slot="
		+ std::to_string(slot) + " creature=" + std::to_string(wanted.getNum())
		+ " count=" + std::to_string(stack.getCount()) + " cost=" + resourcesJson(totalCost));
}


void doSplit(std::istringstream & in)
{
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	std::string ownerName;
	int idx = -1;
	const CArmedInstance * armed = resolveArmed(in, ownerName, idx, cb);
	if (!armed)
	{
		appendResult("ERR bad_target");
		return;
	}
	int slot = -1, amount = -1;
	in >> slot >> amount;
	if (!armed->Slots().count(SlotID(slot)))
	{
		appendResult("ERR bad_slot");
		return;
	}
	const CStackInstance & stack = *armed->Slots().at(SlotID(slot));
	if (amount <= 0 || amount >= stack.getCount())
	{
		appendResult("ERR bad_amount amount=" + std::to_string(amount) + " count="
			+ std::to_string(stack.getCount()) + " (must be a strict fraction of the stack)");
		return;
	}
	const SlotID dest = armed->getFreeSlot();
	if (!dest.validSlot())
	{
		appendResult("ERR no_free_slot");
		return;
	}
	// CCallback::splitStack only submits the request (it always returns 0); the server decides.
	// Do not treat its return value as success/failure - the MCP verifier checks the army state.
	cb->splitStack(armed, armed, SlotID(slot), dest, amount);
	appendResult("OK split " + ownerName + " " + std::to_string(idx) + " slot="
		+ std::to_string(slot) + " amount=" + std::to_string(amount) + " new_slot="
		+ std::to_string(dest.getNum()) + " creature="
		+ std::to_string(stack.getCreatureID().getNum()));
}


void doMerge(std::istringstream & in)
{
	// Merge stack <srcSlot> of <srcOwner>#<srcIdx> into <dstSlot> of <dstOwner>#<dstIdx>.
	// Same creature type only; used to load a hero whose 7 army slots are already occupied.
	std::string srcOwner, dstOwner;
	int srcIdx = 0, srcSlot = -1, dstIdx = 0, dstSlot = -1;
	in >> srcOwner >> srcIdx >> srcSlot >> dstOwner >> dstIdx >> dstSlot;
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	auto pick = [&](const std::string & owner, int idx) -> const CArmedInstance * {
		if (owner == "hero")
		{
			auto hs = cb->getHeroesInfo();
			return (idx >= 0 && idx < static_cast<int>(hs.size())) ? hs[idx] : nullptr;
		}
		if (owner == "town")
		{
			auto ts = cb->getTownsInfo(true);
			return (idx >= 0 && idx < static_cast<int>(ts.size())) ? ts[idx] : nullptr;
		}
		return nullptr;
	};
	const CArmedInstance * src = pick(srcOwner, srcIdx);
	const CArmedInstance * dst = pick(dstOwner, dstIdx);
	if (!src || !dst)
	{
		appendResult("ERR bad_owner");
		return;
	}
	if (!src->Slots().count(SlotID(srcSlot)))
	{
		appendResult("ERR src_slot_empty");
		return;
	}
	if (!dst->Slots().count(SlotID(dstSlot)))
	{
		appendResult("ERR dst_slot_empty");
		return;
	}
	if (src->Slots().at(SlotID(srcSlot))->getCreatureID()
		!= dst->Slots().at(SlotID(dstSlot))->getCreatureID())
	{
		appendResult("ERR creature_mismatch");
		return;
	}
	cb->mergeStacks(src, dst, SlotID(srcSlot), SlotID(dstSlot));
	appendResult("OK merge " + srcOwner + " " + std::to_string(srcSlot)
		+ " -> " + dstOwner + " " + std::to_string(dstSlot));
}

void doTransfer(std::istringstream & in)
{
	// Moves/swaps one army slot between two armed objects (hero, town garrison, ...).
	// CCallback::swapCreatures sends ArrangeStacks - the packet the UI produces when the
	// player drags a stack from the garrison onto the hero.  If the destination slot is
	// empty this simply moves the whole stack.
	std::string srcOwner, dstOwner;
	int srcIdx = 0, srcSlot = -1, dstIdx = 0, dstSlot = -1;
	in >> srcOwner >> srcIdx >> srcSlot >> dstOwner >> dstIdx >> dstSlot;
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	auto pick = [&](const std::string & owner, int idx) -> const CArmedInstance * {
		if (owner == "hero")
		{
			auto heroes = cb->getHeroesInfo();
			if (idx >= 0 && idx < static_cast<int>(heroes.size()))
				return heroes[idx];
		}
		else if (owner == "town")
		{
			auto towns = cb->getTownsInfo(true);
			if (idx >= 0 && idx < static_cast<int>(towns.size()))
				return towns[idx];
		}
		return nullptr;
	};
	const CArmedInstance * src = pick(srcOwner, srcIdx);
	const CArmedInstance * dst = pick(dstOwner, dstIdx);
	if (!src || !dst)
	{
		appendResult("ERR bad_owner");
		return;
	}
	if (!src->Slots().count(SlotID(srcSlot)))
	{
		appendResult("ERR src_slot_empty");
		return;
	}
	// a human player moves stacks between the town garrison and the visiting hero on the town
	// screen, so show that screen while the ArrangeStacks packet is applied
	if (const auto * townSrc = dynamic_cast<const CGTownInstance *>(src))
		openTownScreen(townSrc, true);
	else if (const auto * townDst = dynamic_cast<const CGTownInstance *>(dst))
		openTownScreen(townDst, true);
	cb->swapCreatures(src, dst, SlotID(srcSlot), SlotID(dstSlot));
	appendResult("OK transfer " + srcOwner + " " + std::to_string(srcIdx) + " " + std::to_string(srcSlot)
		+ " -> " + dstOwner + " " + std::to_string(dstIdx) + " " + std::to_string(dstSlot));
}

void doSwapStacks(std::istringstream & in)
{
	// CCallback::swapCreatures sends ArrangeStacks - the very packet the UI produces when a
	// player drags one army slot onto another (merge/swap).  Same layer, no cheating.
	std::string ownerName;
	int idx = -1, slotA = -1, slotB = -1;
	in >> ownerName >> idx >> slotA >> slotB;
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	const CArmedInstance * armed = nullptr;
	if (ownerName == "hero")
	{
		auto heroes = cb->getHeroesInfo();
		if (idx < 0 || idx >= static_cast<int>(heroes.size()))
		{
			appendResult("ERR bad_hero_index");
			return;
		}
		armed = heroes[idx];
	}
	else if (ownerName == "town")
	{
		auto towns = cb->getTownsInfo(true);
		if (idx < 0 || idx >= static_cast<int>(towns.size()))
		{
			appendResult("ERR bad_town_index");
			return;
		}
		armed = towns[idx];
	}
	else
	{
		appendResult("ERR bad_owner");
		return;
	}

	if (!armed->Slots().count(SlotID(slotA)) || !armed->Slots().count(SlotID(slotB)))
	{
		appendResult("ERR empty_slot");
		return;
	}
	cb->swapCreatures(armed, armed, SlotID(slotA), SlotID(slotB));
	appendResult("OK swap_stacks " + ownerName + " " + std::to_string(idx) + " "
		+ std::to_string(slotA) + " " + std::to_string(slotB));
}


void doAnswerQuery(std::istringstream & in)
{
	long long value = 0;
	in >> value;
	if (!in)
	{
		appendResult("ERR bad_answer");
		return;
	}
	std::lock_guard<std::mutex> lock(queryMutex);
	if (!pendingQuery.active)
	{
		appendResult("ERR no_query");
		return;
	}
	const int64_t qid = pendingQuery.id;
	const std::string type = pendingQuery.type;
	const int optionCount = static_cast<int>(pendingQuery.options.size());
	bool valid = false;
	if (type == "level_up")
		valid = value >= 0 && value < optionCount;
	else if (type == "select")
		valid = (value == 0 && pendingQuery.canCancel) || (value >= 1 && value <= optionCount);
	else if (type == "yes_no" || type == "garrison" || type == "recruitment" || type == "info")
		valid = value >= 0 && value <= 1;
	else if (type == "teleport")
		valid = value >= -1 && value < optionCount;   // -1 = let the server choose a random exit
	if (!valid)
	{
		// keep the query pending: the agent may retry with a legal answer, it must not guess
		appendResult("ERR bad_answer " + type + " " + std::to_string(value)
			+ " options=" + std::to_string(optionCount));
		return;
	}
	pendingQuery.active = false;
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	cb->selectionMade(static_cast<int>(value), QueryID(qid));
	appendResult("OK answer " + type + " " + std::to_string(value));
}


// ---------------------------------------------------------------- UI driving
// The formal demo must show the real screens, so these commands drive the *UI layer*:
// open the town screen, dump the widget tree and click on widgets with synthetic mouse
// events (EventDispatcher) - the same path a player's mouse takes.
void collectWidgets(CIntObject * obj, int depth, int maxDepth, std::string & out, int & count)
{
	if (!obj)
		return;
	out += "{\"cls\":" + q(typeid(*obj).name());
	out += ",\"x\":" + std::to_string(obj->pos.x) + ",\"y\":" + std::to_string(obj->pos.y);
	out += ",\"w\":" + std::to_string(obj->pos.w) + ",\"h\":" + std::to_string(obj->pos.h);
	out += ",\"depth\":" + std::to_string(depth);
	out += ",\"children\":[";
	bool first = true;
	if (depth < maxDepth)
	{
		for (auto * child : obj->children)
		{
			if (count > 800)
				break;
			if (!first) out += ",";
			first = false;
			++count;
			collectWidgets(child, depth + 1, maxDepth, out, count);
		}
	}
	out += "]}";
}

void doUiTree(std::istringstream & in)
{
	int maxDepth = 3;
	in >> maxDepth;
	auto top = ENGINE->windows().topWindow<CIntObject>();
	std::string json = "{\"windows\":[";
	bool first = true;
	// topWindow is only the front one; also scan all stacked windows
	for (auto & w : ENGINE->windows().findWindows<CIntObject>())
	{
		if (!first) json += ",";
		first = false;
		int count = 0;
		json += "{\"root\":";
		collectWidgets(w.get(), 0, maxDepth, json, count);
		json += "}";
	}
	json += "]}";
	writeTextFile("ui_tree.json", json);
	appendResult(std::string("OK ui_tree windows=") + std::to_string(ENGINE->windows().findWindows<CIntObject>().size())
		+ " (see ui_tree.json)");
}

void doClick(std::istringstream & in)
{
	int x = -1, y = -1;
	in >> x >> y;
	if (x < 0 || y < 0)
	{
		appendResult("ERR bad_coords");
		return;
	}
	const Point pos(x, y);
	ENGINE->events().dispatchMouseMoved(Point(0, 0), pos);
	ENGINE->events().dispatchMouseLeftButtonPressed(pos, 0);
	ENGINE->events().dispatchMouseLeftButtonReleased(pos, 0);
	appendResult("OK click " + std::to_string(x) + " " + std::to_string(y));
}

void doOpenTown(std::istringstream & in)
{
	int townIdx = 0;
	in >> townIdx;
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	auto towns = cb->getTownsInfo(true);
	if (townIdx < 0 || townIdx >= static_cast<int>(towns.size()))
	{
		appendResult("ERR bad_town_index");
		return;
	}
	auto * pi = localPlayerInterface();
	if (!pi)
	{
		appendResult("ERR no_player_interface");
		return;
	}
	pi->openTownWindow(towns[townIdx]);
	appendResult("OK open_town " + std::to_string(townIdx) + " name=" + towns[townIdx]->getNameTranslated());
}

void doOpenHero(std::istringstream & in)
{
	// T40/C11: open the *real* hero screen (the same window the player gets by clicking the hero
	// portrait) so an agent/recording can show the equipment/army/spellbook UI.  The window is
	// left open on purpose: the caller closes it with `close_window_safe` (CHeroWindow is on the
	// safe list) or `key Escape`.  Mirrors doOpenTown's use of CPlayerInterface.
	int heroIdx = 0;
	in >> heroIdx;
	auto cb = localCallback();
	if (!cb)
	{
		appendResult("ERR no_game");
		return;
	}
	auto heroes = cb->getHeroesInfo();
	if (heroIdx < 0 || heroIdx >= static_cast<int>(heroes.size()))
	{
		appendResult("ERR bad_hero_index");
		return;
	}
	auto * pi = localPlayerInterface();
	if (!pi)
	{
		appendResult("ERR no_player_interface");
		return;
	}
	pi->openHeroWindow(heroes[heroIdx]);
	appendResult("OK open_hero " + std::to_string(heroIdx) + " name=" + heroes[heroIdx]->getNameTranslated());
}

void doKeyPress(std::istringstream & in)
{
	std::string key;
	in >> key;
	if (key.empty())
	{
		appendResult("ERR bad_key");
		return;
	}
	ENGINE->events().dispatchKeyPressed(key);
	ENGINE->events().dispatchKeyReleased(key);
	appendResult("OK key " + key);
}


void doCloseWindowSafe(); // defined below

void doCloseWindow()
{
	// Deprecated alias of close_window_safe.  The old implementation popped *any* top window,
	// including the adventure map itself; that corrupted the window stack and crashed
	// PlayerEndsTurn (CPlayerInterface::closeAllDialogs -> popWindows -> deactivate on empty stack).
	doCloseWindowSafe();
}


void doCloseWindowSafe()
{
	// UI automation must not blindly pop modal windows: some of them (e.g. hero exchange)
	// submit a server query when closed through their own callbacks.  Only windows we know
	// how to dismiss safely are popped here; anything else is reported as unsupported.
	if (ENGINE->windows().count() <= 1)
	{
		// The adventure map / main menu is the only window left: never pop it.
		appendResult("ERR unsafe_window root");
		return;
	}
	auto top = ENGINE->windows().topWindow<CIntObject>();
	if (!top)
	{
		appendResult("ERR no_window");
		return;
	}
	const std::string cls = typeid(*top).name();
	static const char * const safeTypes[] = {
		"CCastleInterface", "CRecruitmentWindow", "CTavernWindow", "CMarketWindow",
		"CBlacksmithDialog", "CMageGuildScreen", "CHallInterface", "CFortScreen",
		"QuickRecruitmentWindow", "WikiWindow", "CInfoWindow", "CRecruitWindow",
		// T40/C11: the hero screen opened by `open_hero` is a plain CWindowObject (no server
		// query behind it), so popping it is exactly what the window's own close button does.
		"CHeroWindow"
	};
	bool safe = false;
	for (const char * t : safeTypes)
	{
		if (cls.find(t) != std::string::npos)
		{
			safe = true;
			break;
		}
	}
	if (!safe)
	{
		appendResult("ERR unsupported_window " + cls);
		return;
	}
	ENGINE->windows().popWindow(top);
	appendResult("OK close_window_safe");
}


// ---------------------------------------------------------------- camera
// While the agent is not moving the mouse, VCMI may leave the adventure map scrolled to a
// corner (the view follows synthetic mouse positions / previous windows).  Keep the camera on
// the main hero so recordings and screenshots stay centred.
void centerCameraOnHero()
{
	if (CPlayerInterface::battleInt)
		return;                                  // battle view owns the screen
	auto * pi = localPlayerInterface();
	if (!pi || !pi->localState)
		return;
	auto view = ENGINE->windows().topWindow<AdventureMapInterface>();
	if (!view)
		return;
	const CGHeroInstance * current = pi->localState->getCurrentHero();
	if (!current)
		return;                                  // no selected hero -> leave the view alone
	const int3 pos = current->visitablePos();
	if (pos.x < 0 || pos.y < 0 || pos.z < 0)
		return;
	view->centerOnObject(current);
}

void doCenterHero()
{
	auto * pi = localPlayerInterface();
	if (!pi)
	{
		appendResult("ERR no_player_interface");
		return;
	}
	centerCameraOnHero();
	appendResult("OK center_hero");
}

void doCloseTop()
{
	// Dismiss the topmost *popup* (intro message, result dialog, ...) - the same thing the
	// player does by pressing OK/Enter.  Non-popup windows (adventure map, main menu) are
	// left alone: popping those left the GUI in a state where nothing was dispatched anymore.
	auto top = ENGINE->windows().topWindow<CIntObject>();
	if (!top)
	{
		appendResult("OK close_top (empty)");
		return;
	}
	auto popup = dynamic_cast<CWindowObject *>(top.get());
	if (!popup)
	{
		appendResult("OK close_top (not_popup)");
		return;
	}
	ENGINE->windows().popWindows(1);
	appendResult("OK close_top (popup)");
}

void executeCommandLine(const std::string & line)
{
	std::istringstream in(line);
	std::string op;
	in >> op;
	// Diagnostics are read-only: they must never advance a queued UI transaction (the delayed
	// recruitment BUY) or close an information window the caller may still be inspecting.
	const bool readOnly = (op == "ui_tree" || op == "probe" || op == "reachable"
		|| op == "ping" || op == "battle_actions" || op == "town_info" || op == "army_info");
	// Town actions below keep/drive the town screen; every other command must return to the
	// adventure map first, otherwise the recording would stay in the town while the hero moves.
	const bool keepsTownUi = (op == "recruit" || op == "build" || op == "transfer");
	if (!readOnly && pendingRecruit.active)
		flushPendingRecruit();      // keep command ordering: buy before the next action
	if (!readOnly && !keepsTownUi && townScreenCloseAt != 0)
		closeTownScreen();
	if (!readOnly && puzzleWindowCloseAt != 0)
		closePuzzleWindow();        // never let an info window cover an action the agent asked for
	if (op == "move_hero") doMoveHero(in);
	else if (op == "probe") doProbe(in);
	else if (op == "reachable") doReachable(in);
	else if (op == "end_turn") doEndTurn();
	else if (op == "build") doBuild(in);
	else if (op == "recruit") doRecruit(in);
	else if (op == "swap_stacks") doSwapStacks(in);
	else if (op == "split") doSplit(in);
	else if (op == "transfer") doTransfer(in);
	else if (op == "merge") doMerge(in);
	else if (op == "upgrade") doUpgradeCreature(in);
	else if (op == "recruit_hero") doRecruitHero(in);
	else if (op == "save_game") doSaveGame(in);
	else if (op == "load_game") doLoadGame(in);
	else if (op == "cast_spell") doCastSpell(in);
	else if (op == "market_trade") doMarketTrade(in);
	else if (op == "battle_click") doBattleClick(in);
	else if (op == "battle_move") doBattleMove(in);
	else if (op == "battle_attack") doBattleAttack(in);
	else if (op == "battle_approach") doBattleApproach(in);
	else if (op == "battle_cast_spell") doBattleCastSpell(in);
	else if (op == "battle_actions") doBattleActions();
	else if (op == "battle_defend") doBattleDefend();
	else if (op == "battle_wait") doBattleWait();
	else if (op == "battle_result_ok") doBattleResultOk();
	else if (op == "answer") doAnswerQuery(in);
	else if (op == "battle_wait") doBattleWait();
	else if (op == "close_top") doCloseTop();
	else if (op == "close_window") doCloseWindow();
	else if (op == "close_window_safe") doCloseWindowSafe();
	else if (op == "center_hero") doCenterHero();
	else if (op == "ui_tree") doUiTree(in);
	else if (op == "town_info") doTownInfo(in);
	else if (op == "army_info") doArmyInfo(in);
	else if (op == "click") doClick(in);
	else if (op == "open_town") doOpenTown(in);
	else if (op == "open_hero") doOpenHero(in);
	else if (op == "key") doKeyPress(in);
	else if (op == "ping") appendResult("OK ping");
	else if (!op.empty() && op[0] != '#')
		appendResult("ERR unknown_op " + op);
}

void pollCommands()
{
	// Commands are appended by the external agent.  We track *processed line count*
	// rather than a byte offset: tellg() returns -1 at EOF, which silently stopped all
	// later commands in the first version.
	const fs::path path = fs::path(bridgeDir) / "commands.txt";
	size_t processedLines = 0;
	while (!stopping.load())
	{
		std::this_thread::sleep_for(std::chrono::milliseconds(100));

		std::vector<std::string> lines;
		{
			std::ifstream f(path, std::ios::binary);
			if (!f)
				continue;
			std::string line;
			while (std::getline(f, line))
			{
				if (!line.empty() && line.back() == '\r')
					line.pop_back();
				lines.push_back(line);
			}
		}
		// drop a trailing partial line (file written but last newline not flushed yet)
		size_t complete = lines.size();
		if (complete > 0 && !lines.empty())
		{
			std::ifstream f(path, std::ios::binary | std::ios::ate);
			if (f)
			{
				const auto size = static_cast<std::streamoff>(f.tellg());
				if (size > 0)
				{
					f.seekg(size - 1);
					char lastChar = 0;
					f.get(lastChar);
					if (lastChar != '\n')
						complete = lines.size() - 1;
				}
			}
		}

		for (size_t i = processedLines; i < complete; ++i)
		{
			const std::string line = lines[i];
			if (line.empty())
				continue;
			const int64_t seq = static_cast<int64_t>(i) + 1;
			try
			{
				ENGINE->dispatchMainThread([line, seq]()
				{
					currentCmdSeq.store(seq);
					inCommand.store(true);
					try
					{
						executeCommandLine(line);
					}
					catch (...)
					{
						inCommand.store(false);
						throw;
					}
					inCommand.store(false);
				});
			}
			catch (const std::exception & e)
			{
				appendResult(std::string("ERR dispatch_failed ") + e.what());
			}
			catch (...)
			{
				appendResult("ERR dispatch_failed unknown");
			}
		}
		processedLines = complete;
	}
}


void autoDismissBattleResults()
{
	// With quick combat (and after a manual battle) the result window is modal: the player
	// has to press OK.  In agent mode nobody presses it, which froze the whole turn.
	auto wins = ENGINE->windows().findWindows<BattleResultWindow>();
	if (!wins.empty())
	{
		wins.front()->bExitf();
		appendResult("AUTO battle_result_ok");
	}
}

// ---- H3 "advanced options" for --agent-start -------------------------------------------
// The MCP writes <bridge dir>/start_options.txt before launching the client:
//   difficulty=0..4        player=<color index>
//   town=<FactionID|-1>    hero=<HeroTypeID|-1>   bonus=<-1|0|1|2>
//   turn_time=<sec>  battle_time=<sec>  unit_time=<sec>
// All keys are optional; the file is applied to the lobby right after the map is pushed.
struct AgentStartOptions
{
	bool present = false;
	int difficulty = -1;      // -1 = keep the client setting (settings.general.lastDifficulty)
	int playerColor = -1;     // -1 = keep the default human color of the map
	int town = -2;            // -2 = keep map default, -1 = FACTION_RANDOM
	int hero = -2;            // -2 = keep map default, -1 = HERO_RANDOM
	int bonus = -2;           // -2 = keep map default, -1 = RANDOM, 0=ARTIFACT, 1=GOLD, 2=RESOURCE
	int turnTimeSec = -1;     // 回合时限 (0 = unlimited)
	int battleTimeSec = -1;
	int unitTimeSec = -1;
};

AgentStartOptions startOptions;
bool startOptionStage1Done = false;
bool startOptionStage2Done = false;
bool startOptionVerifyDone = false;

std::string trimStartOptionLine(const std::string & s)
{
	const auto first = s.find_first_not_of(" \t\r\n");
	if (first == std::string::npos)
		return {};
	const auto last = s.find_last_not_of(" \t\r\n");
	return s.substr(first, last - first + 1);
}

void loadStartOptions()
{
	startOptions = AgentStartOptions();
	startOptionStage1Done = startOptionStage2Done = startOptionVerifyDone = false;
	if (bridgeDir.empty())
		return;
	const fs::path path = fs::path(bridgeDir) / "start_options.txt";
	std::ifstream file(path);
	if (!file)
		return;
	std::string line;
	while (std::getline(file, line))
	{
		const auto eq = line.find('=');
		if (eq == std::string::npos)
			continue;
		const std::string key = trimStartOptionLine(line.substr(0, eq));
		const std::string val = trimStartOptionLine(line.substr(eq + 1));
		if (key.empty() || val.empty())
			continue;
		const int iv = std::atoi(val.c_str());
		if (key == "difficulty")
			startOptions.difficulty = iv;
		else if (key == "player")
			startOptions.playerColor = iv;
		else if (key == "town")
			startOptions.town = iv;
		else if (key == "hero")
			startOptions.hero = iv;
		else if (key == "bonus")
			startOptions.bonus = iv;
		else if (key == "turn_time")
			startOptions.turnTimeSec = iv;
		else if (key == "battle_time")
			startOptions.battleTimeSec = iv;
		else if (key == "unit_time")
			startOptions.unitTimeSec = iv;
	}
	startOptions.present = true;
	appendResult("OK start_options_loaded file=" + path.string());
}

PlayerColor firstHumanColor(const StartInfo & si)
{
	PlayerColor human = PlayerColor::CANNOT_DETERMINE;
	for (const auto & entry : si.playerInfos)
		if (entry.second.isControlledByHuman())
			human = entry.first;
	return human;
}

void applyStartOptionsStage1()
{
	// difficulty + which player slot is the human player
	if (!startOptions.present)
		return;
	auto & srv = GAME->server();
	if (!srv.si)
		return;
	if (startOptions.difficulty >= 0 && startOptions.difficulty <= 4)
	{
		srv.setDifficulty(static_cast<ui8>(startOptions.difficulty));
		appendResult("OK start_option difficulty=" + std::to_string(startOptions.difficulty));
	}
	if (startOptions.playerColor >= 0)
	{
		const PlayerColor target(startOptions.playerColor);
		if (!srv.si->playerInfos.count(target))
		{
			appendResult("WARN start_option player=" + std::to_string(startOptions.playerColor)
				+ " not_present_on_map");
			return;
		}
		if (firstHumanColor(*srv.si) == target)
		{
			appendResult("OK start_option player=" + std::to_string(startOptions.playerColor) + " already_human");
			return;
		}
		srv.setPlayer(target);
		appendResult("OK start_option player=" + std::to_string(startOptions.playerColor));
	}
}

void applyStartOptionsStage2()
{
	// starting town / hero / bonus / turn timer of the human player
	if (!startOptions.present)
		return;
	auto & srv = GAME->server();
	if (!srv.si)
		return;
	const PlayerColor human = firstHumanColor(*srv.si);
	if (human == PlayerColor::CANNOT_DETERMINE)
	{
		appendResult("WARN start_option no_human_player");
		return;
	}
	if (startOptions.town >= -1)
	{
		srv.setPlayerOption(static_cast<ui8>(LobbyChangePlayerOption::TOWN_ID), startOptions.town, human);
		appendResult("OK start_option town=" + std::to_string(startOptions.town));
	}
	if (startOptions.hero >= -1)
	{
		srv.setPlayerOption(static_cast<ui8>(LobbyChangePlayerOption::HERO_ID), startOptions.hero, human);
		appendResult("OK start_option hero=" + std::to_string(startOptions.hero));
	}
	if (startOptions.bonus >= -2)
	{
		srv.setPlayerOption(static_cast<ui8>(LobbyChangePlayerOption::BONUS_ID), startOptions.bonus, human);
		appendResult("OK start_option bonus=" + std::to_string(startOptions.bonus));
	}
	if (startOptions.turnTimeSec >= 0 || startOptions.battleTimeSec >= 0 || startOptions.unitTimeSec >= 0)
	{
		TurnTimerInfo timer = srv.si->turnTimerInfo;
		if (startOptions.turnTimeSec >= 0)
			timer.turnTimer = startOptions.turnTimeSec * 1000;
		if (startOptions.battleTimeSec >= 0)
			timer.battleTimer = startOptions.battleTimeSec * 1000;
		if (startOptions.unitTimeSec >= 0)
			timer.unitTimer = startOptions.unitTimeSec * 1000;
		srv.setTurnTimerInfo(timer);
		appendResult("OK start_option turn_time=" + std::to_string(startOptions.turnTimeSec) + "s");
	}
}

void verifyAndLogStartOptions()
{
	if (!startOptions.present)
		return;
	auto & srv = GAME->server();
	if (!srv.si)
		return;
	std::string line = "OK start_options_effective difficulty=" + std::to_string(srv.si->difficulty);
	const PlayerColor human = firstHumanColor(*srv.si);
	if (human != PlayerColor::CANNOT_DETERMINE)
	{
		line += " player=" + std::to_string(human.getNum());
		const auto it = srv.si->playerInfos.find(human);
		if (it != srv.si->playerInfos.end())
		{
			line += " town=" + std::to_string(it->second.castle.getNum());
			line += " hero=" + std::to_string(it->second.hero.getNum());
			line += " bonus=" + std::to_string(static_cast<int>(it->second.bonus));
		}
	}
	line += " turn_time_ms=" + std::to_string(srv.si->turnTimerInfo.turnTimer);
	appendResult(line);
}

void startGameStep()
{
	if (startPhase == StartPhase::Idle || startPhase == StartPhase::Done || startPhase == StartPhase::Failed)
		return;

	if (startPhase == StartPhase::Reset)
	{
		try
		{
			invalidateViewCaches();   // new game / loaded save: drop the previous game's caches
			startMapInfo = std::make_shared<CMapInfo>();
			// "--agent-start Saves/agent/day3" (or a *.vsgm1 path) resumes a save instead of
			// starting a new game, so long debug games survive client restarts.
			const bool isSaveGame = startMapName.rfind("Saves/", 0) == 0
				|| (startMapName.size() > 6
					&& startMapName.compare(startMapName.size() - 6, 6, ".vsgm1") == 0);
			if (isSaveGame)
			{
				GAME->server().resetStateForLobby(EStartMode::LOAD_GAME, ESelectionScreen::loadGame,
					EServerMode::LOCAL, {});
				startMapInfo->saveInit(ResourcePath(startMapName, EResType::SAVEGAME));
				appendResult("OK start_phase lobby (load_save)");
			}
			else
			{
				GAME->server().resetStateForLobby(EStartMode::NEW_GAME, ESelectionScreen::newGame,
					EServerMode::LOCAL, {});
				loadStartOptions();   // H3 advanced options for this new game (optional file)
				startMapInfo->mapInit(startMapName);
				appendResult("OK start_phase lobby");
			}
			GAME->server().startLocalServerAndConnect(false);
			startPhase = StartPhase::WaitLobby;
			startTicks = 0;
		}
		catch (const std::exception & e)
		{
			appendResult(std::string("ERR start_failed ") + e.what());
			startPhase = StartPhase::Failed;
		}
		catch (...)
		{
			appendResult("ERR start_failed unknown");
			startPhase = StartPhase::Failed;
		}
		return;
	}

	if (startPhase == StartPhase::WaitLobby)
	{
		if (ENGINE->windows().topWindow<CLobbyScreen>())
		{
			startPhase = StartPhase::PushMap;
			startTicks = 0;
		}
		else if (++startTicks > 20 * 60)
		{
			appendResult("ERR lobby_timeout");
			startPhase = StartPhase::Failed;
		}
		return;
	}

	if (startPhase == StartPhase::PushMap)
	{
		if (startTicks == 0 && startMapInfo)
			GAME->server().setMapInfo(startMapInfo);
		++startTicks;
		// staged application gives the local server a few frames to broadcast the lobby state
		if (!startOptionStage1Done && startTicks >= 5)
		{
			applyStartOptionsStage1();
			startOptionStage1Done = true;
		}
		if (!startOptionStage2Done && startTicks >= 20)
		{
			applyStartOptionsStage2();
			startOptionStage2Done = true;
		}
		if (!startOptionVerifyDone && startTicks >= 32)
		{
			verifyAndLogStartOptions();
			startOptionVerifyDone = true;
		}
		if (startTicks >= 45)
		{
			startPhase = StartPhase::SendStart;
			startTicks = 0;
		}
		return;
	}

	if (startPhase == StartPhase::SendStart)
	{
		try
		{
			GAME->server().sendStartGame(false, false);
			startPhase = StartPhase::Done;
			appendResult("OK start_game_sent");
		}
		catch (...)
		{
			if (++startTicks > 20 * 60)
			{
				appendResult("ERR start_game_timeout");
				startPhase = StartPhase::Failed;
			}
		}
	}
}

void scheduleStateTick()
{
	// self-rearming main-thread task: writes state every STATE_INTERVAL_MS
	ENGINE->dispatchMainThread([]() {
		static auto last = std::chrono::steady_clock::now() - std::chrono::milliseconds(STATE_INTERVAL_MS);
		const auto now = std::chrono::steady_clock::now();
		tickFrames.fetch_add(1);
		autoDismissBattleResults();
		startGameStep();
		// press BUY in the open recruitment dialog after a short visible dwell
		if (pendingRecruit.active && --pendingRecruit.framesLeft <= 0)
			flushPendingRecruit();
		// informational client windows (puzzle map) must not stay in front of the map
		if (puzzleWindowCloseAt != 0 && tickFrames.load() >= puzzleWindowCloseAt)
			closePuzzleWindow();
		// leave the town screen after a short visible dwell (recording shows it, then the map)
		if (townScreenCloseAt != 0 && tickFrames.load() >= townScreenCloseAt)
			closeTownScreen();

		if (now - last >= std::chrono::milliseconds(STATE_INTERVAL_MS))
		{
			last = now;
		

		writeStateFile(buildStateJson());
		}
		if (!stopping.load())
			scheduleStateTick();
	});
}
}

void AgentBridge::queueGameStart(const std::string & mapName)
{
	startMapName = mapName;
	startPhase = StartPhase::Reset;
	startTicks = 0;
}


bool AgentBridge::handleLevelUpQuery(const CGHeroInstance * hero, PrimarySkill primary,
                                     const std::vector<SecondarySkill> & skills, int64_t queryID)
{
	if (!started.load())
		return false;
	// The primary skill point is applied automatically; only the secondary skill choice is
	// a query.  Reply index = position in this (unsorted) vector, as CLevelWindow does.
	if (skills.empty())
		return true;
	std::vector<std::string> options;
	for (size_t i = 0; i < skills.size(); ++i)
	{
		std::string o = "{\"index\":" + std::to_string(i);
		o += ",\"type\":\"SEC_SKILL\"";
		o += ",\"sub\":" + std::to_string(skills[i].getNum());
		// T33/P-B: the level-up choice was previously a bare numeric id, i.e. the player had to
		// guess which skill each option was.  The UI shows the name; expose the same fact.
		const std::string name = skillName(skills[i].getNum());
		if (!name.empty())
			o += ",\"name\":" + q(name);
		o += "}";
		options.push_back(o);
	}
	setPendingQuery(queryID, "level_up", "hero levelled up", false, primarySkillName(primary), options);
	appendResult("QUERY level_up " + std::to_string(queryID) + " options=" + std::to_string(skills.size()));
	return true;
}

bool AgentBridge::handleBlockingDialog(const std::string & text, const std::vector<Component> & components,
                                       int64_t askID, bool selection, bool cancel)
{
	if (!started.load())
		return false;
	std::vector<std::string> options;
	for (size_t i = 0; i < components.size(); ++i)
		options.push_back(componentJson(components[i], static_cast<int>(i) + 1));
	const std::string type = selection ? "select" : "yes_no";
	setPendingQuery(askID, type, text, cancel || !selection, {}, options);
	appendResult("QUERY " + type + " " + std::to_string(askID) + " options=" + std::to_string(components.size()));
	return true;
}


bool AgentBridge::handleInfoDialog(const std::string & text)
{
	if (!started.load())
		return false;
	{
		std::lock_guard<std::mutex> lock(infoMutex);
		infoLog.push_back(text);
		while (infoLog.size() > 20)
			infoLog.pop_front();
	}
	appendResult("INFO " + text);
	return true;
}

bool AgentBridge::handleGarrisonDialog(int64_t queryID)
{
	if (!started.load())
		return false;
	// The garrison window is not a question: opening and closing it answers "done".
	auto cb = localCallback();
	if (cb)
		cb->selectionMade(1, QueryID(queryID));
	appendResult("OK garrison_dialog_closed");
	return true;
}

void AgentBridge::notePuzzleMap()
{
	if (!started.load())
		return;
	// Shown for a moment (so it ends up in the recording), then closed by the frame tick - an
	// agent cannot click the OK button and would otherwise sit in front of this window.
	puzzleWindowCloseAt = tickFrames.load() + PUZZLE_WINDOW_DWELL_FRAMES;
	appendResult("INFO puzzle_map_window (auto-close)");
}

bool AgentBridge::handleTeleportDialog(const std::vector<std::string> & exits, bool impassable, int64_t askID)
{
	if (!started.load())
		return false;
	std::vector<std::string> options;
	for (size_t i = 0; i < exits.size(); ++i)
	{
		// D-002/D-007: the normal UI does not expose every channel coordinate (some exits may be
		// unexplored), so publish only a 0-based choice index; -1 means "let the server pick".
		std::string o = "{\"index\":" + std::to_string(i);
		o += ",\"type\":\"TELEPORT_EXIT\"}";
		options.push_back(o);
	}
	setPendingQuery(askID, "teleport", impassable ? "teleport (impassable?)" : "teleport", false, {}, options);
	appendResult("QUERY teleport " + std::to_string(askID) + " options=" + std::to_string(exits.size()));
	return true;
}

bool AgentBridge::enabled()
{
	return started.load();
}

void AgentBridge::start(const std::string & dir)
{
	if (started.exchange(true))
		return;
	bridgeDir = dir;
	sessionId = "s" + std::to_string(static_cast<int64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
		std::chrono::system_clock::now().time_since_epoch()).count()));
	std::error_code ec;
	fs::create_directories(bridgeDir, ec);

	{
		std::lock_guard<std::mutex> lock(ioMutex);
		std::ofstream f(fs::path(bridgeDir) / "results.log", std::ios::binary | std::ios::trunc);
		f << "# agent bridge started\n";
	}
	writeStateFile("{\"in_game\":false,\"bridge\":\"starting\"}");

	scheduleStateTick();
	poller = std::thread(pollCommands);
	poller.detach();
	logGlobal->info("AgentBridge started, directory: %s", dir);
}

void AgentBridge::stop()
{
	stopping.store(true);
}

