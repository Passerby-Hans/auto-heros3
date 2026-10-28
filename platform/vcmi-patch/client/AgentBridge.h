/*
 * AgentBridge.h, part of VCMI engine — agent integration patch (not upstream)
 *
 * Purpose: expose a *player-equivalent* window into a running game so an external
 * LLM agent can (a) read what the player is allowed to see and (b) perform the same
 * actions a player performs.  Two doors, nothing else:
 *
 *   PlayerView   : JSON snapshot built exclusively from the visibility-checked
 *                  callbacks (CGameInfoCallback / CPlayerSpecificInfoCallback) and
 *                  the player's own battle callback.
 *   PlayerAction : line-based commands executed by calling the very same methods the
 *                  UI calls (IGameActionCallback for the adventure map,
 *                  BattleActionsController::onHexLeftClicked for battles).
 *
 * Enabled only with `--agent-view <dir>`; no network, no GM commands, no save access.
 */
#pragma once

#include <string>
#include <vector>
#include "../lib/constants/EntityIdentifiers.h"
#include "../lib/constants/Enumerations.h"
#include "../lib/networkPacks/Component.h"

class CGHeroInstance;
struct Component;

VCMI_LIB_NAMESPACE_BEGIN
class QueryID;
VCMI_LIB_NAMESPACE_END

class AgentBridge
{
public:
	/// Start the bridge writing into `dir` (creates it if needed).
	/// Safe to call before a game is loaded; the bridge reports "in_game": false until then.
	static void start(const std::string & dir);

	/// Ask the bridge to start a normal single-player game from `mapName`
	/// (e.g. "Maps/Arrogance").  Driven frame by frame on the GUI thread so that the
	/// player interface is created with a live GUI - doing the same thing before the
	/// main loop crashes the client.
	static void queueGameStart(const std::string & mapName);

	/// True if the bridge was started (i.e. --agent-view was given)
	static bool enabled();

	/// Stop the poller thread (used on shutdown); idempotent
	static void stop();

	// ---- query interception (blocking dialogs the server waits for) ----------
	// Each returns true if the bridge consumed the query (no UI must be created).
	// The external agent answers with the `answer <n>` / `cancel` commands.
	static bool handleLevelUpQuery(const CGHeroInstance * hero, PrimarySkill primary,
	                               const std::vector<SecondarySkill> & skills, int64_t queryID);
	static bool handleBlockingDialog(const std::string & text, const std::vector<Component> & components,
	                                 int64_t askID, bool selection, bool cancel);
	static bool handleGarrisonDialog(int64_t queryID);
	static bool handleInfoDialog(const std::string & text);
	static bool handleTeleportDialog(const std::vector<std::string> & exits, bool impassable, int64_t askID);

	/// Client-side informational windows (no server query behind them, so nothing to answer):
	/// the puzzle-map window opened by visiting an obelisk is shown for a moment and then
	/// closed automatically instead of waiting for a click that an agent cannot make.
	static void notePuzzleMap();
};
