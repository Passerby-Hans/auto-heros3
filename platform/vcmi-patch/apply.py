# -*- coding: utf-8 -*-
"""Apply the agent bridge patch to a VCMI 1.7.5 source tree.

Tracked source of truth: `platform/vcmi-patch/` (this directory).
Target: `vendor/vcmi-src/` (gitignored checkout; may be re-cloned at any time).
Every step is idempotent: re-running only applies what is missing.

Patches
-------
1. copy client/AgentBridge.{h,cpp}                    -> PlayerView / PlayerAction
2. client/CMakeLists.txt: add those two files to vcmiclientcommon_{SRCS,HEADERS}
3. clientapp/EntryPoint.cpp: --agent-view / --agent-start options, bridge start,
   and queueGameStart() so the bridge can start a single-player game frame by frame
   from inside the main loop (starting it before the loop crashes the client, because
   CPlayerInterface is created with no GUI)
4. conan-msvc/conan_toolchain.cmake: drop VS-only CMAKE_GENERATOR_* (we build with Ninja)
5. lib/clientapp/serverapp/vcmiqt CMakeLists: skip the Windows version resource, because
   CMake 4.1's RC wrapper mangles the zh-CN MSVC include prefix (fails with RC1107)

If `vendor/vcmi-src` is re-cloned, run this script and then tools/vcmi_build.ps1.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]                      # repo root
TARGET = ROOT / 'vendor' / 'vcmi-src'


def log(msg: str) -> None:
    print(f'[apply] {msg}')


def patch_text(path: Path, old: str, new: str, tag: str) -> bool:
    if not path.exists():
        log(f'SKIP {path.name} (not found)')
        return False
    text = path.read_text(encoding='utf-8')
    if new in text:
        return False
    if text.count(old) != 1:
        log(f'WARN {path.name}: anchor for {tag!r} found {text.count(old)} times, skipping')
        return False
    path.write_text(text.replace(old, new, 1), encoding='utf-8')
    log(f'patched {path.relative_to(TARGET)} ({tag})')
    return True


def copy_bridge_sources() -> None:
    dst = TARGET / 'client'
    for name in ('AgentBridge.h', 'AgentBridge.cpp'):
        src = HERE / 'client' / name
        target = dst / name
        if not target.exists() or target.read_bytes() != src.read_bytes():
            shutil.copy2(src, target)
            log(f'copied client/{name}')


def patch_cmake() -> None:
    patch_text(TARGET / 'client' / 'CMakeLists.txt',
               '\tCPlayerInterface.cpp\n',
               '\tCPlayerInterface.cpp\n\tAgentBridge.cpp\n',
               'agent bridge source')
    patch_text(TARGET / 'client' / 'CMakeLists.txt',
               '\tCPlayerInterface.h\n',
               '\tCPlayerInterface.h\n\tAgentBridge.h\n',
               'agent bridge header')


BRIDGE_START = (
    '\t\t// --- agent bridge (PlayerView / PlayerAction); enabled only with --agent-view ---\n'
    '\t\tif(vm.count("agent-view"))\n'
    '\t\t\tAgentBridge::start(vm["agent-view"].as<std::string>());\n'
    '\t\tif(vm.count("agent-start"))\n'
    '\t\t\tAgentBridge::queueGameStart(vm["agent-start"].as<std::string>());\n'
    '\n'
    '\t\tconst auto & runMainLoop = []()'
)


def patch_entrypoint() -> None:
    ep = TARGET / 'clientapp' / 'EntryPoint.cpp'
    patch_text(ep,
               '#include "../client/ClientCommandManager.h"',
               '#include "../client/AgentBridge.h"\n#include "../client/ClientCommandManager.h"',
               'include')
    patch_text(ep,
               '\t\t("savefrequency", po::value<si64>(), "limit auto save creation to each N days");',
               '\t\t("savefrequency", po::value<si64>(), "limit auto save creation to each N days")\n'
               '\t\t("agent-view", po::value<std::string>(), "agent bridge: directory for state.json / commands.txt")\n'
               '\t\t("agent-start", po::value<std::string>(), "agent bridge: auto-start this map as a normal single-player game");',
               'cli options')
    patch_text(ep, '\t\tconst auto & runMainLoop = []()', BRIDGE_START, 'bridge start')


def patch_toolchain() -> None:
    tc = TARGET / 'conan-msvc' / 'conan_toolchain.cmake'
    patch_text(tc, 'set(CMAKE_GENERATOR_PLATFORM "x64" CACHE STRING "" FORCE)',
               '# patched-by-agent: removed (Ninja generator)', 'platform var')
    patch_text(tc, 'set(CMAKE_GENERATOR_TOOLSET "v142" CACHE STRING "" FORCE)',
               '# patched-by-agent: removed (Ninja generator)', 'toolset var')


def patch_version_resources() -> None:
    patches = [
        ('lib/CMakeLists.txt', 'target_sources(vcmi PRIVATE ${vcmi_ICON})',
         '# patched-by-agent: dev build skips version resource\n\t# target_sources(vcmi PRIVATE ${vcmi_ICON})'),
        ('clientapp/CMakeLists.txt', 'target_sources(vcmiclient PRIVATE "${client_VERSIONINFO_RC}")',
         '# patched-by-agent: dev build skips version resource\n\t# target_sources(vcmiclient PRIVATE "${client_VERSIONINFO_RC}")'),
        ('serverapp/CMakeLists.txt', 'target_sources(vcmiserver PRIVATE "${server_VERSIONINFO_RC}")',
         '# patched-by-agent: dev build skips version resource\n\t# target_sources(vcmiserver PRIVATE "${server_VERSIONINFO_RC}")'),
        ('vcmiqt/CMakeLists.txt', 'target_sources(vcmiqt PRIVATE ${vcmiqt_ICON})',
         '# patched-by-agent: dev build skips version resource\n\t# target_sources(vcmiqt PRIVATE ${vcmiqt_ICON})'),
    ]
    for rel, old, new in patches:
        patch_text(TARGET / rel, old, new, 'version resource')



def patch_query_interception() -> None:
    """Agent mode consumes blocking server queries instead of opening windows.

    The bridge then publishes them in state.json and replies with `answer <n>` /
    `cancel` (level-up: 0-based index into the secondary-skill list; selection
    dialogs: 1-based component index, 0 = cancel; yes/no: 1 = yes, 0 = no).
    Without this the game blocks forever on the first level-up or treasure chest.
    """
    cpp = TARGET / 'client' / 'CPlayerInterface.cpp'
    patch_text(cpp,
               '#include "CPlayerInterface.h"',
               '#include "CPlayerInterface.h"\n#include "AgentBridge.h"',
               'bridge include')
    patch_text(cpp,
               '\tEVENT_HANDLER_CALLED_BY_CLIENT;\n\twaitWhileDialog();\n\tENGINE->sound().playSound(soundBase::heroNewLevel);\n'
               '\tENGINE->windows().createAndPushWindow<CLevelWindow>',
               '\tEVENT_HANDLER_CALLED_BY_CLIENT;\n\twaitWhileDialog();\n'
               '\tif (AgentBridge::handleLevelUpQuery(hero, pskill, skills, queryID.getNum()))\n'
               '\t\treturn;\n'
               '\tENGINE->sound().playSound(soundBase::heroNewLevel);\n'
               '\tENGINE->windows().createAndPushWindow<CLevelWindow>',
               'level up query')
    patch_text(cpp,
               '\tEVENT_HANDLER_CALLED_BY_CLIENT;\n\twaitWhileDialog();\n\n\tmovementController->requestMovementAbort();',
               '\tEVENT_HANDLER_CALLED_BY_CLIENT;\n\twaitWhileDialog();\n'
               '\tif (AgentBridge::handleBlockingDialog(text, components, askID.getNum(), selection, cancel))\n'
               '\t\treturn;\n\n'
               '\tmovementController->requestMovementAbort();',
               'blocking dialog query')
    patch_text(cpp,
               '\tif (AgentBridge::handleBlockingDialog(text, components, askID.getNum(), selection, cancel))',
               '\tif (AgentBridge::handleBlockingDialog(text, components, askID.getNum(), selection, cancel))',
               'noop')
    # teleport dialog: build a text list of exits
    patch_text(cpp,
               'void CPlayerInterface::showTeleportDialog(const CGHeroInstance * hero, TeleportChannelID channel, TTeleportExitsList exits, bool impassable, QueryID askID)\n{\n',
               'void CPlayerInterface::showTeleportDialog(const CGHeroInstance * hero, TeleportChannelID channel, TTeleportExitsList exits, bool impassable, QueryID askID)\n{\n'
               '\t{ std::vector<std::string> exitTexts; for (const auto & e : exits) exitTexts.push_back(e.second.toString()); '
               'if (AgentBridge::handleTeleportDialog(exitTexts, impassable, askID.getNum())) return; }\n',
               'teleport query')
    # garrison window: opening+closing it is how the player says "done"
    patch_text(cpp,
               'void CPlayerInterface::showGarrisonDialog(const CArmedInstance * up, const CGHeroInstance * down, bool removableUnits, QueryID queryID, const MetaString & customTitle)\n{\n',
               'void CPlayerInterface::showGarrisonDialog(const CArmedInstance * up, const CGHeroInstance * down, bool removableUnits, QueryID queryID, const MetaString & customTitle)\n{\n'
               '\tif (AgentBridge::handleGarrisonDialog(queryID.getNum()))\n\t\treturn;\n',
               'garrison dialog')
    # info windows: some of them make the client spin until acknowledged (week-of events,
    # "you found X" texts) - in agent mode just log the text
    patch_text(cpp,
               'void CPlayerInterface::showInfoDialog(EInfoWindowMode type, const std::string &text, const std::vector<Component> & components, int soundID)\n{\n',
               'void CPlayerInterface::showInfoDialog(EInfoWindowMode type, const std::string &text, const std::vector<Component> & components, int soundID)\n{\n'
               '\tif (AgentBridge::handleInfoDialog(text))\n\t\treturn;\n',
               'info dialog')
    patch_text(cpp,
               'void CPlayerInterface::showInfoDialogAndWait(std::vector<Component> & components, const MetaString & text)\n{\n',
               'void CPlayerInterface::showInfoDialogAndWait(std::vector<Component> & components, const MetaString & text)\n{\n'
               '\tif (AgentBridge::handleInfoDialog(text.toString()))\n\t\treturn;\n',
               'info dialog and wait')

    # recruitment window: same - answer "done" so the server is not blocked
    patch_text(cpp,
               'void CPlayerInterface::showRecruitmentDialog(const CGDwelling *dwelling, const CArmedInstance *dst, int level, QueryID queryID)\n{\n',
               'void CPlayerInterface::showRecruitmentDialog(const CGDwelling *dwelling, const CArmedInstance *dst, int level, QueryID queryID)\n{\n'
               '\tif (AgentBridge::handleGarrisonDialog(queryID.getNum()))\n\t\treturn;\n',
               'recruitment dialog')



def patch_battle_debug() -> None:
    """Temporary diagnostics for battle-interface creation.

    Some battles leave a server-side CBattleQuery open while CPlayerInterface::battleInt
    stays null, which blocks the whole turn.  Log what the client decided so the cause is
    visible in VCMI_Client_log.txt (quick combat / auto fight / missing human interface).
    """
    cpp = TARGET / 'client' / 'Client.cpp'
    patch_text(cpp,
               '\tif(!settings["session"]["headless"].Bool())\n\t{\n\t\tif(att || def)\n\t\t{\n'
               '\t\t\tCPlayerInterface::battleInt = std::make_shared<BattleInterface>',
               '\tlogGlobal->info("[battle-debug] att=%d def=%d quickCombat=%d forceQuickCombat=%d autoFight=%d", '
               'att ? 1 : 0, def ? 1 : 0, settings["adventure"]["quickCombat"].Bool() ? 1 : 0, '
               'settings["adventure"]["forceQuickCombat"].Bool() ? 1 : 0, '
               '(att && att->isAutoFightOn) || (def && def->isAutoFightOn) ? 1 : 0);\n'
               '\tif(!settings["session"]["headless"].Bool())\n\t{\n\t\tif(att || def)\n\t\t{\n'
               '\t\t\tCPlayerInterface::battleInt = std::make_shared<BattleInterface>',
               'battle debug log')


def patch_debug_cheat() -> None:
    """Debug-only development cheat channel (documented in doc/DECISIONS.md D-011).

    The launcher exports AUTO_HEROS3_CHEAT_FILE; the server thread polls that file and applies
    the requested debug lines to the human player.  Inert without the env variable, so normal
    runs are unaffected.  Used only for agent development runs.

    Supported lines (T35 extension):
      <resource> <amount>              set the human player's resource to an absolute amount
      allspells <heroIdx|all>          learn every spell the hero can normally learn
      learnspell <heroIdx|all> <id>    learn one spell
      mana <heroIdx|all> <amount>      set the hero's mana to an absolute amount

    The MCP deliberately has no cheat interface for spells/mana (D-011): this is a test-harness
    channel only; the launcher's cheat.txt path lives inside the isolated state dir.
    """
    hdr = TARGET / 'server' / 'CGameHandler.h'
    patch_text(hdr,
               '\tvoid start(bool resume);\n\tvoid tick(int millisecondsPassed);\n',
               '\tvoid start(bool resume);\n\tvoid tick(int millisecondsPassed);\n'
               '\t/// Debug-only (AUTO_HEROS3_CHEAT_FILE): applies requested resource amounts to the human player.\n'
               '\t/// Inert unless the environment variable points at a writable file.\n'
               '\tvoid processDebugCheatFile();\n',
               'debug cheat declaration')

    cpp = TARGET / 'server' / 'CGameHandler.cpp'
    # Idempotent include normalisation: earlier versions of this patch inserted the include block
    # in two steps, which could leave a duplicate block behind on an already-patched tree.
    anchor_includes = '#include "StdInc.h"\n#include "CGameHandler.h"\n'
    previous_blocks = (
        anchor_includes + '\n'
        '#include <chrono>\n#include <cstdlib>\n#include <fstream>\n#include <map>\n'
        '#include <sstream>\n#include <string>\n',
        anchor_includes + '\n'
        '#include <chrono>\n#include <cstdlib>\n#include <fstream>\n#include <map>\n'
        '#include <algorithm>\n#include <set>\n#include <sstream>\n#include <vector>\n'
        '#include <string>\n',
        # T39: block written by the previous patcher version (before the artifact cheat).
        anchor_includes + '\n'
        '#include <chrono>\n#include <cstdlib>\n#include <fstream>\n'
        '#include <map>\n#include <algorithm>\n#include <set>\n'
        '#include <sstream>\n#include <vector>\n#include <string>\n',
    )
    wanted_includes = (anchor_includes + '\n'
                       '#include <chrono>\n#include <cstdlib>\n#include <fstream>\n'
                       '#include <map>\n#include <algorithm>\n#include <set>\n'
                       '#include <sstream>\n#include <vector>\n#include <string>\n'
                       '#include "../lib/entities/artifact/CArtHandler.h"\n')
    text = cpp.read_text(encoding='utf-8')
    changed = False
    for block in previous_blocks:
        while block in text:
            text = text.replace(block, anchor_includes, 1)
            changed = True
    if wanted_includes not in text:
        if text.count(anchor_includes) != 1:
            log('WARN CGameHandler.cpp: cannot normalise debug cheat includes (anchor count '
                f'{text.count(anchor_includes)})')
        else:
            text = text.replace(anchor_includes, wanted_includes, 1)
            changed = True
    if changed:
        cpp.write_text(text, encoding='utf-8')
        log('patched server/CGameHandler.cpp (debug cheat includes)')

    old_impl = (
        'void CGameHandler::tick(int millisecondsPassed)\n'
        '{\n'
        '\tturnTimerHandler->update(millisecondsPassed);\n'
        '\tprocessDebugCheatFile();\n'
        '}\n'
        '\n'
        'void CGameHandler::processDebugCheatFile()\n'
        '{\n'
        '\t// Debug-only resource cheats for agent development.  Inert unless the launcher points\n'
        '\t// AUTO_HEROS3_CHEAT_FILE at a writable file.  Content is read, the file truncated, then\n'
        '\t// requested amounts are applied as deltas to the human player.\n'
        '\tstatic auto lastPoll = std::chrono::steady_clock::time_point{};\n'
        '\tconst auto now = std::chrono::steady_clock::now();\n'
        '\tif (now - lastPoll < std::chrono::milliseconds(200))\n'
        '\t\treturn;\n'
        '\tlastPoll = now;\n'
        '\n'
        '\tconst char * cheatPath = std::getenv("AUTO_HEROS3_CHEAT_FILE");\n'
        '\tif (cheatPath == nullptr || *cheatPath == \'\\0\')\n'
        '\t\treturn;\n'
        '\n'
        '\tstd::ifstream in(cheatPath);\n'
        '\tif (!in)\n'
        '\t\treturn;\n'
        '\tstd::stringstream buffer;\n'
        '\tbuffer << in.rdbuf();\n'
        '\tin.close();\n'
        '\tconst std::string content = buffer.str();\n'
        '\tif (content.empty())\n'
        '\t\treturn;\n'
        '\t{\n'
        '\t\tstd::ofstream truncate(cheatPath, std::ios::trunc);\n'
        '\t}\n'
        '\n'
        '\tPlayerColor human = PlayerColor::NEUTRAL;\n'
        '\tfor (const auto & player : gameState().players)\n'
        '\t{\n'
        '\t\tif (player.second.human)\n'
        '\t\t{\n'
        '\t\t\thuman = player.first;\n'
        '\t\t\tbreak;\n'
        '\t\t}\n'
        '\t}\n'
        '\tif (!human.isValidPlayer() || !gameState().players.count(human))\n'
        '\t\treturn;\n'
        '\n'
        '\tstatic const std::map<std::string, int> names = {\n'
        '\t\t{"wood", 0}, {"mercury", 1}, {"ore", 2}, {"sulfur", 3},\n'
        '\t\t{"crystal", 4}, {"gems", 5}, {"gold", 6},\n'
        '\t};\n'
        '\tconst TResources & resources = gameState().players.at(human).resources;\n'
        '\tstd::istringstream lines(content);\n'
        '\tstd::string name;\n'
        '\tlong long value = 0;\n'
        '\twhile (lines >> name >> value)\n'
        '\t{\n'
        '\t\tconst auto it = names.find(name);\n'
        '\t\tif (it == names.end())\n'
        '\t\t\tcontinue;\n'
        '\t\tconst GameResID which(it->second);\n'
        '\t\tconst int delta = static_cast<int>(value) - resources[which];\n'
        '\t\tif (delta != 0)\n'
        '\t\t\tgiveResource(human, which, delta);\n'
        '\t}\n'
        '\tLOG_TRACE(logGlobal);\n'
        '}\n'
    )
    impl = (
        'void CGameHandler::tick(int millisecondsPassed)\n'
        '{\n'
        '\tturnTimerHandler->update(millisecondsPassed);\n'
        '\tprocessDebugCheatFile();\n'
        '}\n'
        '\n'
        'void CGameHandler::processDebugCheatFile()\n'
        '{\n'
        '\t// Debug-only development cheats for agent runs.  Inert unless the launcher points\n'
        '\t// AUTO_HEROS3_CHEAT_FILE at a writable file.  Supported lines:\n'
        '\t//   <resource> <amount>            set the human player resource to an absolute amount\n'
        '\t//   allspells <heroIdx|all>        learn every spell the hero can normally learn\n'
        '\t//   learnspell <heroIdx|all> <id>  learn one spell\n'
        '\t//   allartifacts <heroIdx|all>     give all equippable artifacts (VCMI vcmiforgeofnoldorking equivalent)\n'
        '\t//   mana <heroIdx|all> <amount>    set the hero mana to an absolute amount\n'
        '\t// The MCP deliberately has no spell/mana/artifact cheat interface: test harness channel only.\n'
        '\tstatic auto lastPoll = std::chrono::steady_clock::time_point{};\n'
        '\tconst auto now = std::chrono::steady_clock::now();\n'
        '\tif (now - lastPoll < std::chrono::milliseconds(200))\n'
        '\t\treturn;\n'
        '\tlastPoll = now;\n'
        '\n'
        '\tconst char * cheatPath = std::getenv("AUTO_HEROS3_CHEAT_FILE");\n'
        '\tif (cheatPath == nullptr || *cheatPath == \'\\0\')\n'
        '\t\treturn;\n'
        '\n'
        '\tstd::ifstream in(cheatPath);\n'
        '\tif (!in)\n'
        '\t\treturn;\n'
        '\tstd::stringstream buffer;\n'
        '\tbuffer << in.rdbuf();\n'
        '\tin.close();\n'
        '\tconst std::string content = buffer.str();\n'
        '\tif (content.empty())\n'
        '\t\treturn;\n'
        '\t{\n'
        '\t\tstd::ofstream truncate(cheatPath, std::ios::trunc);\n'
        '\t}\n'
        '\n'
        '\tPlayerColor human = PlayerColor::NEUTRAL;\n'
        '\tfor (const auto & player : gameState().players)\n'
        '\t{\n'
        '\t\tif (player.second.human)\n'
        '\t\t{\n'
        '\t\t\thuman = player.first;\n'
        '\t\t\tbreak;\n'
        '\t\t}\n'
        '\t}\n'
        '\tif (!human.isValidPlayer() || !gameState().players.count(human))\n'
        '\t\treturn;\n'
        '\n'
        '\t// Hero indices are the ordered list the client publishes as state.json "heroes"\n'
        '\t// (CPlayerSpecificInfoCallback::getHeroesInfo() == PlayerState::getHeroes()).\n'
        '\tstd::vector<const CGHeroInstance *> heroes = gameState().players.at(human).getHeroes();\n'
        '\n'
        '\tstatic const std::map<std::string, int> names = {\n'
        '\t\t{"wood", 0}, {"mercury", 1}, {"ore", 2}, {"sulfur", 3},\n'
        '\t\t{"crystal", 4}, {"gems", 5}, {"gold", 6},\n'
        '\t};\n'
        '\tconst TResources & resources = gameState().players.at(human).resources;\n'
        '\tstd::istringstream lines(content);\n'
        '\tstd::string token;\n'
        '\twhile (lines >> token)\n'
        '\t{\n'
        '\t\tif (token == "allspells" || token == "learnspell" || token == "mana"\n'
        '\t\t\t|| token == "allartifacts" || token == "vcmiforgeofnoldorking" || token == "vcmiartifacts")\n'
        '\t\t{\n'
        '\t\t\tstd::string selector;\n'
        '\t\t\tlines >> selector;\n'
        '\t\t\tstd::vector<const CGHeroInstance *> selected;\n'
        '\t\t\tif (selector == "all")\n'
        '\t\t\t\tselected = heroes;\n'
        '\t\t\telse\n'
        '\t\t\t{\n'
        '\t\t\t\tstd::istringstream indexStream(selector);\n'
        '\t\t\t\tint index = -1;\n'
        '\t\t\t\tindexStream >> index;\n'
        '\t\t\t\tif (index >= 0 && index < static_cast<int>(heroes.size()))\n'
        '\t\t\t\t\tselected.push_back(heroes[index]);\n'
        '\t\t\t}\n'
        '\t\t\tif (token == "allartifacts" || token == "vcmiforgeofnoldorking" || token == "vcmiartifacts")\n'
        '\t\t\t{\n'
        '\t\t\t\t// VCMI cheat equivalent (vcmiforgeofnoldorking / vcmiartifacts): native\n'
        '\t\t\t\t// PlayerMessageProcessor::cheatGiveArtifacts loops from artifact id 3 upward.\n'
        '\t\t\t\tfor (const auto * hero : selected)\n'
        '\t\t\t\t{\n'
        '\t\t\t\t\tif (!hero)\n'
        '\t\t\t\t\t\tcontinue;\n'
        '\t\t\t\t\tfor (int g = 3; g < static_cast<int>(LIBRARY->arth->objects.size()); ++g)\n'
        '\t\t\t\t\t{\n'
        '\t\t\t\t\t\tif (!LIBRARY->arth->objects[g])\n'
        '\t\t\t\t\t\t\tcontinue;\n'
        '\t\t\t\t\t\tgiveHeroNewArtifact(hero, ArtifactID(g), ArtifactPosition::FIRST_AVAILABLE);\n'
        '\t\t\t\t\t}\n'
        '\t\t\t\t}\n'
        '\t\t\t\tcontinue;\n'
        '\t\t\t}\n'
        '\t\t\tif (token == "mana")\n'
        '\t\t\t{\n'
        '\t\t\t\tlong long amount = 0;\n'
        '\t\t\t\tlines >> amount;\n'
        '\t\t\t\tif (amount < 0)\n'
        '\t\t\t\t\tamount = 0;\n'
        '\t\t\t\tfor (const auto * hero : selected)\n'
        '\t\t\t\t{\n'
        '\t\t\t\t\tSetMana pack(hero->id, static_cast<si32>(amount),\n'
        '\t\t\t\t\t\tChangeValueMode::ABSOLUTE);\n'
        '\t\t\t\t\tsendAndApply(pack);\n'
        '\t\t\t\t}\n'
        '\t\t\t\tcontinue;\n'
        '\t\t\t}\n'
        '\t\t\tif (token == "learnspell")\n'
        '\t\t\t{\n'
        '\t\t\t\tint spellId = -1;\n'
        '\t\t\t\tlines >> spellId;\n'
        '\t\t\t\tconst spells::Spell * spell = nullptr;\n'
        '\t\t\t\ttry\n'
        '\t\t\t\t{\n'
        '\t\t\t\t\tspell = LIBRARY->spells()->getById(SpellID(spellId));\n'
        '\t\t\t\t}\n'
        '\t\t\t\tcatch (...)\n'
        '\t\t\t\t{\n'
        '\t\t\t\t\tspell = nullptr;\n'
        '\t\t\t\t}\n'
        '\t\t\t\tif (!spell)\n'
        '\t\t\t\t\tcontinue;\n'
        '\t\t\t\tfor (const auto * hero : selected)\n'
        '\t\t\t\t{\n'
        '\t\t\t\t\tChangeSpells cs;\n'
        '\t\t\t\t\tcs.hid = hero->id;\n'
        '\t\t\t\t\tcs.learn = 1;\n'
        '\t\t\t\t\tcs.spells.insert(spell->getId());\n'
        '\t\t\t\t\tsendAndApply(cs);\n'
        '\t\t\t\t}\n'
        '\t\t\t\tcontinue;\n'
        '\t\t\t}\n'
        '\t\t\t// allspells: debug cheat - learn every normal spell regardless of wisdom/allowed\n'
        '\t\t\tfor (const auto * hero : selected)\n'
        '\t\t\t{\n'
        '\t\t\t\tstd::set<SpellID> toLearn;\n'
        '\t\t\t\tLIBRARY->spells()->forEach([&toLearn](const spells::Spell * entity,\n'
        '\t\t\t\t\tbool & stop)\n'
        '\t\t\t\t{\n'
        '\t\t\t\t\tif (entity && !entity->isSpecial() && !entity->isCreatureAbility())\n'
        '\t\t\t\t\t\ttoLearn.insert(entity->getId());\n'
        '\t\t\t\t});\n'
        '\t\t\t\tif (toLearn.empty())\n'
        '\t\t\t\t\tcontinue;\n'
        '\t\t\t\tChangeSpells cs;\n'
        '\t\t\t\tcs.hid = hero->id;\n'
        '\t\t\t\tcs.learn = 1;\n'
        '\t\t\t\tcs.spells = toLearn;\n'
        '\t\t\t\tsendAndApply(cs);\n'
        '\t\t\t}\n'
        '\t\t\tcontinue;\n'
        '\t\t}\n'
        '\t\tlong long value = 0;\n'
        '\t\tlines >> value;\n'
        '\t\tconst auto it = names.find(token);\n'
        '\t\tif (it == names.end())\n'
        '\t\t\tcontinue;\n'
        '\t\tconst GameResID which(it->second);\n'
        '\t\tconst int delta = static_cast<int>(value) - resources[which];\n'
        '\t\tif (delta != 0)\n'
        '\t\t\tgiveResource(human, which, delta);\n'
        '\t}\n'
        '\tLOG_TRACE(logGlobal);\n'
        '}\n'
    )
    # Upgrade an already-applied older version in place (fresh trees take the insert path below).
    patch_text_upgrade(
        cpp, (old_impl,), impl,
        'void CGameHandler::tick(int millisecondsPassed)\n'
        '{\n\tturnTimerHandler->update(millisecondsPassed);\n}\n',
        'debug cheat tick hook')

    # Some older checkouts/trees carry a slightly different previous cheat block (e.g. a
    # re-cloned vendor tree patched by an older apply.py).  Replace that block by its markers
    # instead of silently leaving a stale implementation in place.
    text = cpp.read_text(encoding='utf-8')
    if impl not in text:
        start = text.find('void CGameHandler::tick(int millisecondsPassed)')
        fn = text.find('void CGameHandler::processDebugCheatFile()', start) if start >= 0 else -1
        if start >= 0 and fn >= 0:
            marker = '\n\tLOG_TRACE(logGlobal);\n}\n'
            tail = text.find(marker, fn)
            if tail >= 0:
                end = tail + len(marker)
                cpp.write_text(text[:start] + impl + text[end:], encoding='utf-8')
                log('replaced previous debug cheat implementation (marker upgrade)')


def patch_agent_ui() -> None:
    """Agent mode: hide the green in-game debug console from screenshots/recordings."""
    cpp = TARGET / 'client' / 'adventureMap' / 'CInGameConsole.cpp'
    patch_text(cpp,
               '#include "CInGameConsole.h"',
               '#include "CInGameConsole.h"\n#include "../AgentBridge.h"',
               'agent bridge include')
    patch_text(cpp,
               'void CInGameConsole::show(Canvas & to)\n{\n',
               'void CInGameConsole::show(Canvas & to)\n{\n'
               '\tif (AgentBridge::enabled())\n\t\treturn; // agent mode: hide debug log overlay\n',
               'agent console hide')


def patch_text_upgrade(path: Path, old_versions: tuple[str, ...], new: str, anchor: str,
                       tag: str) -> bool:
    """Insert *new*, migrating an earlier version of the same patch when one is present.

    Plain patch_text() cannot upgrade an already-applied snippet (its anchor is gone), so this
    helper replaces a known previous version first and falls back to inserting before `anchor`.
    """
    if not path.exists():
        log(f'SKIP {path.name} (not found)')
        return False
    text = path.read_text(encoding='utf-8')
    if new in text:
        return False
    for old in old_versions:
        if old and old in text:
            path.write_text(text.replace(old, new, 1), encoding='utf-8')
            log(f'upgraded {path.relative_to(TARGET)} ({tag})')
            return True
    if text.count(anchor) != 1:
        log(f'WARN {path.name}: anchor for {tag!r} found {text.count(anchor)} times, skipping')
        return False
    path.write_text(text.replace(anchor, new + anchor, 1), encoding='utf-8')
    log(f'patched {path.relative_to(TARGET)} ({tag})')
    return True


def patch_recruitment_ui() -> None:
    """AgentBridge: let the bridge press "BUY" in the real recruitment window.

    The bridge opens the town screen and the recruitment window (so the recording shows the
    town UI a human would use) and then needs to select the exact creature card, set the slider
    and press BUY programmatically; those members are private, hence this tiny public helper on
    CRecruitmentWindow.
    """
    header = TARGET / 'client' / 'windows' / 'GUIClasses.h'
    old_decl = (
        '\t/// AgentBridge helper: set the slider to amount (clamped to affordable/available)\n'
        '\t/// and press BUY. Returns the amount actually bought (0 = nothing happened).\n'
        '\tint agentBuy(int amount);\n'
    )
    new_decl = (
        '\t/// AgentBridge helper: select the requested creature card (base or upgraded variant\n'
        '\t/// of this level), set the slider to amount (clamped to affordable/available) and\n'
        '\t/// press BUY.\n'
        '\t/// Returns the amount actually bought; 0 = nothing affordable/no slot;\n'
        '\t/// -1 = the requested creature is not offered by this window.\n'
        '\tint agentBuy(CreatureID creature, int amount);\n'
    )
    patch_text_upgrade(header, (old_decl,), new_decl,
                       '\tvoid availableCreaturesChanged();\n',
                       'recruitment window helper (header)')

    old_impl = (
        'int CRecruitmentWindow::agentBuy(int amount)\n'
        '{\n'
        '\tif(!selected)\n'
        '\t\treturn 0;\n'
        '\tconst int maxAmount = slider->getAmount(); // min(available, affordable) as computed above\n'
        '\tconst int buyAmount = amount > 0 ? std::min(amount, maxAmount) : maxAmount;\n'
        '\tif(buyAmount <= 0)\n'
        '\t\treturn 0;\n'
        '\tslider->setValue(buyAmount, true); // triggers sliderMoved -> labels, cost box\n'
        '\tbuy();\n'
        '\treturn buyAmount;\n'
        '}\n\n'
    )
    # v2 (2026-10-25, superseded): CreatureID + setValue.  The micro-scene proved that
    # CSlider::setValue() ignores its argument, so v2 bought the slider maximum.
    old_impl_v2 = (
        'int CRecruitmentWindow::agentBuy(CreatureID creature, int amount)\n'
        '{\n'
        '\t// The window lists every available variant of the level (base + upgrades, reverse order)\n'
        '\t// and defaults to the first card.  Select the exact creature the caller asked for\n'
        '\t// instead of silently buying whichever card happens to be selected.\n'
        '\tstd::shared_ptr<CCreatureCard> chosen;\n'
        '\tfor(auto & card : cards)\n'
        '\t\tif(card && card->creature && card->creature->getId() == creature)\n'
        '\t\t{\n'
        '\t\t\tchosen = card;\n'
        '\t\t\tbreak;\n'
        '\t\t}\n'
        '\tif(!chosen)\n'
        '\t\treturn -1;\n'
        '\tif(selected != chosen)\n'
        '\t\tselect(chosen); // updates slider range, labels and cost box for this card\n'
        '\tif(!selected)\n'
        '\t\treturn 0;\n'
        '\tconst int maxAmount = slider->getAmount(); // min(available, affordable) as computed above\n'
        '\tconst int buyAmount = amount > 0 ? std::min(amount, maxAmount) : maxAmount;\n'
        '\tif(buyAmount <= 0)\n'
        '\t\treturn 0;\n'
        '\tslider->setValue(buyAmount, true); // triggers sliderMoved -> labels, cost box\n'
        '\tbuy();\n'
        '\treturn buyAmount;\n'
        '}\n\n'
    )
    new_impl = (
        'int CRecruitmentWindow::agentBuy(CreatureID creature, int amount)\n'
        '{\n'
        '\t// The window lists every available variant of the level (base + upgrades, reverse order)\n'
        '\t// and defaults to the first card.  Select the exact creature the caller asked for\n'
        '\t// instead of silently buying whichever card happens to be selected.\n'
        '\tstd::shared_ptr<CCreatureCard> chosen;\n'
        '\tfor(auto & card : cards)\n'
        '\t\tif(card && card->creature && card->creature->getId() == creature)\n'
        '\t\t{\n'
        '\t\t\tchosen = card;\n'
        '\t\t\tbreak;\n'
        '\t\t}\n'
        '\tif(!chosen)\n'
        '\t\treturn -1;\n'
        '\tif(selected != chosen)\n'
        '\t\tselect(chosen); // updates slider range, labels and cost box for this card\n'
        '\tif(!selected)\n'
        '\t\treturn 0;\n'
        '\tconst int maxAmount = slider->getAmount(); // min(available, affordable) as computed above\n'
        '\tconst int buyAmount = amount > 0 ? std::min(amount, maxAmount) : maxAmount;\n'
        '\tif(buyAmount <= 0)\n'
        '\t\treturn 0;\n'
        '\t// NOTE: CSlider::setValue(to, ...) currently ignores `to` (it calls scrollTo(value)),\n'
        '\t// so use scrollTo() directly; otherwise buy() would send the full slider maximum.\n'
        '\tslider->scrollTo(buyAmount, true); // triggers sliderMoved -> labels, cost box\n'
        '\tbuy();\n'
        '\treturn buyAmount;\n'
        '}\n\n'
    )
    cpp = TARGET / 'client' / 'windows' / 'GUIClasses.cpp'
    patch_text_upgrade(cpp, (old_impl_v2, old_impl), new_impl,
                       'void CRecruitmentWindow::showAll(Canvas & to)\n{',
                       'recruitment window helper (impl)')


def patch_puzzle_map_window() -> None:
    """Agent mode: the obelisk puzzle map is informational and has no server query behind it.

    A human closes it with the OK button; the agent would be stuck in front of it. The bridge
    notes the window when it is created and the frame tick closes it after a visible dwell.
    """
    cpp = TARGET / 'client' / 'CPlayerInterface.cpp'
    patch_text(cpp,
               '\tdouble ratio = 0;\n\tint3 grailPos = cb->getGrailPos(&ratio);\n\n'
               '\tENGINE->windows().createAndPushWindow<CPuzzleWindow>(grailPos, ratio);',
               '\tdouble ratio = 0;\n\tint3 grailPos = cb->getGrailPos(&ratio);\n\n'
               '\tAgentBridge::notePuzzleMap();\n'
               '\tENGINE->windows().createAndPushWindow<CPuzzleWindow>(grailPos, ratio);',
               'puzzle map window')


def main() -> int:
    if not TARGET.exists():
        print(f'VCMI source tree not found: {TARGET}', file=sys.stderr)
        return 1
    copy_bridge_sources()
    patch_cmake()
    patch_entrypoint()
    patch_toolchain()
    patch_version_resources()
    patch_query_interception()
    patch_battle_debug()
    patch_debug_cheat()
    patch_agent_ui()
    patch_recruitment_ui()
    patch_puzzle_map_window()
    log('done')
    return 0


if __name__ == '__main__':
    sys.exit(main())
