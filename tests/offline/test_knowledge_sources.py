# -*- coding: utf-8 -*-
"""B1.1 knowledge source/cards contract: synthetic-fixture offline suite.

No game client, no network, no real manuals/game data.  Everything lives in a temporary
directory fixture with tiny synthetic sources that keep the *format* of the real ones
(tab-separated legacy tables, JSON-with-comments core config, page-marked manual text).

Covered
-------
1. Builder contract: source manifest hashing + present/missing status, cards for a
   base/upgrade creature pair, a neutral creature (lore-only manual evidence) and two
   spells (one normal, one two-stage bridge refusal), coverage report and ruleset output.
2. Stable IDs / aliases: base vs upgrade are distinct cards, plural + localized aliases,
   upgrade relations point at the namespaced ids.
3. Spell proficiency: four proficiency levels are separate facts (cost/power), manual
   formula facts exist, availability keeps "rule exists / bridge executable / currently
   castable" as three distinct claims with distinct verification levels.
4. Version conflict: a changed local source is never silently overwritten; the existing
   snapshot is preserved until the caller explicitly accepts the change.
5. Missing data: a missing required source fails loudly (no cards written); a missing
   legacy entity fails; an empty proficiency column becomes an explicit ``missing`` entry.
6. Manual disagreement: a mismatching manual row becomes a ``conflicts`` entry and the
   fact keeps the rule-table value (manual is not silently trusted).
7. Schema rejections: unsourced facts, unknown source ids, duplicate fact keys, missing
   availability, unverified aliases, invalid coverage/ruleset manifests.
8. Traceability: every published SourceRef resolves; a tampered locator/quote fails.

Run: python -X utf8 tools/test_knowledge_sources.py
Exit code 0 only when every check passes.
"""
from __future__ import annotations

import contextlib
import copy
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from auto_heros3.knowledge import build as K  # noqa: E402
from auto_heros3.knowledge import schema  # noqa: E402
from auto_heros3.knowledge import sources as S  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition, detail: str = "") -> None:
    RESULTS.append((name, bool(condition), detail))
    print(("PASS " if condition else "FAIL ") + name + (f"  ({detail})" if detail else ""))


def _write(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)
    return path


def _write_json(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


# --------------------------------------------------------------------------------------
# synthetic fixture (format-compatible, content tiny)
# --------------------------------------------------------------------------------------
_CRTRAITS_HEADER = (
    "Name\t\tCost\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\t\n"
    "Singular\tPlural\tWood\tMercury\tOre\tSulfur\tCrystal\tGems\tGold\tFight Value\t"
    "AI Value\tGrowth\tHorde Growth\tHit Points\tSpeed\tAttack\tDefense\tLow\tHigh\t"
    "Shots\tSpells\tLow\tHigh\tAbility Text\t"
    "\"Attributes (Reference only, do not change these values)\"\n"
)
_CRTRAITS_COLUMNS = ["Name", "Plural", "Wood", "Mercury", "Ore", "Sulfur", "Crystal",
                     "Gems", "Gold", "Fight", "AI", "Growth", "Horde", "HP", "Speed",
                     "Attack", "Defense", "DmgLow", "DmgHigh", "Shots", "Spells",
                     "AdvLow", "AdvHigh", "AbilityText", "Attributes"]


def _creature_row(name: str, plural: str, gold: int, fight: int, ai: int, growth: int,
                  hp: int, speed: int, attack: int, defense: int, dmg_low: int,
                  dmg_high: int, abilities: str = "", attributes: str = "0") -> str:
    values = [name, plural, "0", "0", "0", "0", "0", "0", str(gold), str(fight), str(ai),
              str(growth), "0", str(hp), str(speed), str(attack), str(defense),
              str(dmg_low), str(dmg_high), "0", "0", "20", "50", abilities, attributes]
    return "\t".join(values)


def _sptraits_header() -> str:
    names = ["Name", "Abbreviated Name", "Level", "Earth", "Water", "Fire", "Air",
             "None", "Basic", "Advanced", "Expert", "Power",
             "None", "Basic", "Advanced", "Expert"]
    factions = ["Castle", "Rampart", "Tower", "Inferno", "Necropolis", "Dungeon",
                "Stronghold", "Fortress", "Conflux"]
    names.extend(factions)
    names.extend(["None", "Basic", "Advanced", "Expert", "Normal", "Basic", "Advanced",
                  "Expert", "Attributes"])
    return "\t".join(names) + "\n"


def _spell_row(name: str, level: int, schools: str, costs: list[str], power: int,
               powers: list[str], descriptions: list[str], attributes: str) -> str:
    # schools string: 4 chars, x = has school (Earth, Water, Fire, Air)
    values = [name, name, str(level)]
    values.extend("x" if flag == "x" else " " for flag in schools)
    values.extend(costs)
    values.extend([str(power)])
    values.extend(powers)
    values.extend(["0"] * 9)                 # faction gain chance columns
    values.extend(["1", "1", "1", "1"])      # AI values
    values.extend(descriptions)              # right-click text per proficiency
    values.append(attributes)
    assert len(values) == 34, len(values)
    return "\t".join(values) + "\n"


@dataclass
class Fixture:
    root: Path
    specs: Path
    out: Path
    lines: dict[str, int] = field(default_factory=dict)

    def spec_path(self, name: str) -> Path:
        return self.specs / name


def _build_fixture(base: Path, *, manual_attack: str = "A t t a c k 4 6",
                   blank_expert_cost: bool = False,
                   ruleset_evidence: list[dict] | None = None,
                   entity_legacy_name: str | None = None) -> Fixture:
    root = base / "repo"
    specs = root / "doc" / "knowledge" / "sources"
    out = root / "doc" / "knowledge" / "rulesets"
    specs.mkdir(parents=True, exist_ok=True)

    # ---- manual.player: one creature stat table page + spell reference page --------
    player: list[str] = []
    lines: dict[str, int] = {}

    def add(text: str) -> int:
        player.append(text)
        return len(player)

    add("## 第1页")
    add("")
    lines["name_line"] = add("N a m e Pi ke m a n H a l b e r d i e r")
    lines["cost"] = add("C o s t 60 Gold 75 Gold")
    lines["damage"] = add("D a m a g e 1 - 3 2 - 3")
    lines["shots"] = add("S h o t s N o n e N o n e")
    lines["attack"] = add(manual_attack)
    lines["defense"] = add("D e f e n s e 5 5")
    lines["health"] = add("H e a l t h 1 0 1 0")
    lines["speed"] = add("S p e e d Extra Slow (4 hexes/turn) Slow (5 hexes/turn)")
    add("")
    add("## 第2页")
    add("")
    lines["magic_name"] = add("Magic Arrow Ty p e C o m b a tC o s t 5")
    lines["magic_duration"] = add("D u r a t i o n I n s t a n t")
    lines["magic_basic"] = add(
        "B a s i cE f f e c t T arget, enemy troop receives ((P ower x 10) + 10) damage.")
    lines["magic_advanced"] = add(
        "Ad v a n c e dE f f e c t T arget, enemy troop receives ((P ower x 10) + 20) damage.")
    lines["magic_expert"] = add(
        "E x p e r tE f f e c t T arget, enemy troop receives ((P ower x 10) + 30) damage.")
    add("")
    lines["teleport_name"] = add("Te l e p o r t")
    lines["teleport_type"] = add("Ty p e C o m b a t")
    lines["teleport_cost"] = add("C o s t 1 5")
    lines["teleport_duration"] = add("D u r a t i o n I n s t a n t")
    lines["teleport_basic"] = add(
        "Basic Effect T arget, allied troop instantly moves to an unoccupied target hex. "
        "Troop cannot teleport over walls or moats.")
    lines["teleport_advanced"] = add(
        "Advanced Effect Same as Basic Effect, except troop cannot teleport over walls.")
    lines["teleport_expert"] = add(
        "Expert Effect T roop can move to any unoccupied hex.")
    _write(specs / "manual_player.md", "\n".join(player) + "\n")

    # ---- manual.ab: neutral creature lore lines ------------------------------------
    ab = ["## 第1页", ""]
    lore_start = len(ab) + 1
    ab.extend(["Peasants are the most common folk of the realm.",
               "They are weak but cheap and numerous.",
               "Necromancers prize them as fodder."])
    lore_end = len(ab)
    _write(specs / "manual_ab.md", "\n".join(ab) + "\n")

    # ---- legacy tables -------------------------------------------------------------
    rows = [
        _creature_row("Pikeman", "Pikemen", 60, 100, 80, 14, 10, 4, 4, 5, 1, 3),
        _creature_row("Halberdier", "Halberdiers", 75, 115, 115, 14, 10, 5, 6, 5, 2, 3),
        _creature_row("Peasant", "Peasants", 10, 15, 15, 25, 1, 3, 1, 1, 1, 1),
    ]
    _write(specs / "crtraits.txt", _CRTRAITS_HEADER + "\n".join(rows) + "\n",
           encoding="latin-1")

    magic_costs = ["5", "4", "4", ("    " if blank_expert_cost else "4")]
    # real SPTRAITS.TXT has a junk first row + a header row; the parser starts two rows
    # after the header, so the fixture mirrors that shape.
    sptraits = "\t" * 33 + "\n" + _sptraits_header() + "\n"
    sptraits += _spell_row(
        "Magic Arrow", 1, "xxxx", magic_costs, 10, ["10", "10", "20", "30"],
        ["{Magic Arrow} Bolt of magic.", "{Basic Magic Arrow} Bolt of magic.",
         "{Advanced Magic Arrow} Harder.", "{Expert Magic Arrow} Hardest."],
        "COMBAT_SPELL|CREATURE_TARGET")
    sptraits += _spell_row(
        "Teleport", 3, " x  ", ["15", "12", "6", "3"], 0, ["0", "0", "0", "0"],
        ["{Teleport} Move an ally.", "{Basic Teleport} Move an ally.",
         "{Advanced Teleport} Move an ally over walls.",
         "{Expert Teleport} Move anywhere."],
        "COMBAT_SPELL|CREATURE_TARGET")
    _write(specs / "sptraits.txt", sptraits, encoding="latin-1")

    # ---- core config JSONs (JSONC: comments + trailing comma) ----------------------
    _write(specs / "castle.json", """{
\t// synthetic castle config (JSONC: comment + trailing comma must be tolerated)
\t"pikeman" : {
\t\t"index": 0,
\t\t"level": 1,
\t\t"faction": "castle",
\t\t"upgrades": ["halberdier"],
\t\t"abilities": { "cavalryChargeImmunity": { "type" : "CHARGE_IMMUNITY" } },
\t},
\t"halberdier" : {
\t\t"index": 1,
\t\t"level": 1,
\t\t"faction": "castle",
\t},
}
""")
    _write(specs / "neutral.json", """{
\t"peasant" : { "index": 139, "level": 1, "faction": "neutral" }
}
""")
    _write(specs / "spells.json", """{
\t"magicArrow" : {
\t\t"index": 15,
\t\t"targetType": "CREATURE",
\t\t"levels" : {
\t\t\t"base": {
\t\t\t\t"range" : "0",
\t\t\t\t"battleEffects" : { "directDamage" : {"type":"core:damage"} },
\t\t\t\t"targetModifier":{"smart":true}
\t\t\t},
\t\t\t"advanced": { "battleEffects" : { "directDamage" : {"type":"core:damage","val":20} } }
\t\t},
\t\t"flags" : { "damage": true, "negative": true },
\t\t"targetCondition" : { },
\t},
\t"teleport" : {
\t\t"index": 63,
\t\t"targetType": "CREATURE",
\t\t"levels" : {
\t\t\t"base": { "range" : "0", "battleEffects" : { "teleport" : {"type":"core:teleport"} } },
\t\t\t"advanced" : { "battleEffects" : { "teleport" : {"isMoatPassable": true} } },
\t\t\t"expert" : { "battleEffects" : { "teleport" : {"isWallPassable": true, "isMoatPassable": true} } }
\t\t},
\t\t"flags" : { "positive": true },
\t\t"targetCondition" : { "noneOf" : { "bonus.SIEGE_WEAPON": "absolute" } }
\t}
}
""")

    # ---- localization -----------------------------------------------------------------
    _write_json(specs / "translation.json", {
        "creatures.core.pikeman.name.singular": "枪兵",
        "creatures.core.pikeman.name.plural": "枪兵",
        "creatures.core.halberdier.name.singular": "戟兵",
        "creatures.core.halberdier.name.plural": "戟兵",
        "creatures.core.peasant.name.singular": "农民",
        "creatures.core.peasant.name.plural": "农民",
        "spell.core.magicArrow.name": "魔法神箭",
        "spell.core.magicArrow.description.basic": "{初级魔法神箭} 发射魔法闪电。",
        "spell.core.teleport.name": "瞬间移动",
    })

    # ---- engine source stubs (bridge capability lines + spell damage formula line) ----
    bridge_lines = [f"// synthetic bridge stub line {index}" for index in range(1, 2601)]
    bridge_lines[1949] = "// two-stage refusal: TELEPORT / SACRIFICE"
    bridge_lines[2559] = "// generic single-stage battle spell path"
    _write(specs / "bridge_stub.cpp", "\n".join(bridge_lines) + "\n")
    handler_lines = [f"// synthetic spell handler stub line {index}"
                     for index in range(1, 501)]
    handler_lines[422] = ("return basePowerMultiplier * getBasePower() + "
                          "levelPowerMultiplier * getLevelPower(effectLevel);")
    _write(specs / "spell_handler_stub.cpp", "\n".join(handler_lines) + "\n")

    # ---- source spec -------------------------------------------------------------------
    sources = [
        {"source_id": "source.game_data.crtraits", "kind": "game_data",
         "title": "synthetic CRTRAITS", "path": "doc/knowledge/sources/crtraits.txt",
         "priority": 1, "encoding": "latin-1", "version": "fixture"},
        {"source_id": "source.game_data.sptraits", "kind": "game_data",
         "title": "synthetic SPTRAITS", "path": "doc/knowledge/sources/sptraits.txt",
         "priority": 1, "encoding": "latin-1", "version": "fixture"},
        {"source_id": "source.localization.chinese", "kind": "localization",
         "title": "synthetic localization",
         "path": "doc/knowledge/sources/translation.json",
         "priority": 4, "version": "fixture"},
        {"source_id": "source.manual.player", "kind": "manual",
         "title": "synthetic player manual",
         "path": "doc/knowledge/sources/manual_player.md",
         "priority": 3, "version": "fixture"},
        {"source_id": "source.manual.ab", "kind": "manual",
         "title": "synthetic AB manual",
         "path": "doc/knowledge/sources/manual_ab.md",
         "priority": 3, "version": "fixture"},
        {"source_id": "source.creatures.castle", "kind": "config",
         "title": "synthetic castle config",
         "path": "doc/knowledge/sources/castle.json",
         "priority": 1, "version": "fixture"},
        {"source_id": "source.creatures.neutral", "kind": "config",
         "title": "synthetic neutral config",
         "path": "doc/knowledge/sources/neutral.json",
         "priority": 1, "version": "fixture"},
        {"source_id": "source.spells.offensive", "kind": "config",
         "title": "synthetic spell config",
         "path": "doc/knowledge/sources/spells.json",
         "priority": 1, "version": "fixture"},
        {"source_id": "source.bridge.agent", "kind": "engine_source",
         "title": "synthetic bridge source",
         "path": "doc/knowledge/sources/bridge_stub.cpp",
         "priority": 2, "version": "fixture"},
        {"source_id": "source.engine.spell_handler", "kind": "engine_source",
         "title": "synthetic spell handler source",
         "path": "doc/knowledge/sources/spell_handler_stub.cpp",
         "priority": 2, "version": "fixture"},
        {"source_id": "source.engine_export.creatures", "kind": "engine_export",
         "title": "missing engine export (optional by contract)",
         "path": "var/engine-exports/creatures.json",
         "priority": 0, "required": False},
    ]
    _write_json(specs / "source_spec.json",
                {"schema_version": "b1.1", "sources": sources})

    # ---- ruleset spec -----------------------------------------------------------------
    ruleset = {
        "schema_version": "b1.1",
        "ruleset_id": "fixture-ruleset",
        "display_name": "synthetic fixture ruleset",
        "engine": {"name": "VCMI", "version": "fixture"},
        "content": [{"kind": "builtin_game_data", "name": "synthetic fixture data",
                     "evidence": ruleset_evidence or []}],
        "active_binding": {"status": "unverified_static_config",
                           "reason": "synthetic fixture; no runtime receipt"},
        "source_priority": ["engine_export", "game_data", "config", "engine_source",
                            "manual", "localization"],
    }
    _write_json(specs / "ruleset_spec.json", ruleset)

    # ---- entity spec -------------------------------------------------------------------
    creatures = [
        {"id": "core:pikeman", "legacy_name": entity_legacy_name or "Pikeman",
         "json": {"source_id": "source.creatures.castle", "key": "pikeman"},
         "manual": {"source_id": "source.manual.player", "page": 1,
                    "name_line": lines["name_line"], "column": 0,
                    "rows": {key: lines[key] for key in
                             ("cost", "damage", "shots", "attack", "defense", "health",
                              "speed")}}},
        {"id": "core:halberdier", "legacy_name": "Halberdier",
         "json": {"source_id": "source.creatures.castle", "key": "halberdier"},
         "manual": {"source_id": "source.manual.player", "page": 1,
                    "name_line": lines["name_line"], "column": 1,
                    "rows": {key: lines[key] for key in
                             ("cost", "damage", "shots", "attack", "defense", "health",
                              "speed")}}},
        {"id": "core:peasant", "legacy_name": "Peasant",
         "json": {"source_id": "source.creatures.neutral", "key": "peasant"},
         "manual": {"source_id": "source.manual.ab", "page": 1,
                    "description_lines": [[lore_start, lore_end]]}},
    ]
    spells = [
        {"id": "core:magicArrow", "legacy_name": "Magic Arrow",
         "json": {"source_id": "source.spells.offensive", "key": "magicArrow"},
         "manual": {"source_id": "source.manual.player", "page": 2,
                    "name_line": lines["magic_name"], "type_line": lines["magic_name"],
                    "cost_line": lines["magic_name"],
                    "duration_line": lines["magic_duration"],
                    "effect_lines": {"basic": [lines["magic_basic"]],
                                     "advanced": [lines["magic_advanced"]],
                                     "expert": [lines["magic_expert"]]}},
         "formulas": {"basic": "((Power x 10) + 10) damage",
                      "advanced": "((Power x 10) + 20) damage",
                      "expert": "((Power x 10) + 30) damage"}},
        {"id": "core:teleport", "legacy_name": "Teleport",
         "json": {"source_id": "source.spells.offensive", "key": "teleport"},
         "manual": {"source_id": "source.manual.player", "page": 2,
                    "name_line": lines["teleport_name"],
                    "type_line": lines["teleport_type"],
                    "cost_line": lines["teleport_cost"],
                    "duration_line": lines["teleport_duration"],
                    "effect_lines": {"basic": [lines["teleport_basic"]],
                                     "advanced": [lines["teleport_advanced"]],
                                     "expert": [lines["teleport_expert"]]}},
         "formulas": {"basic": "target allied troop moves to an unoccupied hex",
                      "advanced": "cannot pass walls",
                      "expert": "any unoccupied hex"}},
    ]
    _write_json(specs / "entity_spec.json", {"schema_version": "b1.1",
                                             "creatures": creatures, "spells": spells})
    return Fixture(root=root, specs=specs, out=out, lines=lines)


@contextlib.contextmanager
def fixture(**variants):
    with tempfile.TemporaryDirectory(prefix="b1-knowledge-") as tmp:
        yield _build_fixture(Path(tmp), **variants)


def _cards_by_id(result: K.BuildResult) -> dict[str, dict]:
    return {card["id"]: card for card in result.cards}


def _fact_value(card: dict, key: str):
    for item in card["facts"]:
        if item["key"] == key:
            return item["value"]
    return "<<absent>>"


def _fact(card: dict, key: str) -> dict | None:
    for item in card["facts"]:
        if item["key"] == key:
            return item
    return None


# ======================================================================================
# 1. builder + cards
# ======================================================================================
def test_build_and_cards() -> None:
    with fixture() as fx:
        result = K.build(fx.root, fx.specs, fx.out)
        cards = _cards_by_id(result)
        check("1-1 fixture build produces the five planned cards",
              sorted(cards) == ["core:halberdier", "core:magicArrow", "core:peasant",
                                "core:pikeman", "core:teleport"], str(sorted(cards)))
        known = {item["source_id"] for item in result.manifest["sources"]}
        try:
            schema.validate_cards(result.cards, known)
            valid = True
        except schema.SchemaError as exc:
            valid, detail = False, str(exc)
            print("   validation error:", exc)
        check("1-2 every generated card passes the schema", valid, "" if valid else detail)

        pikeman = cards["core:pikeman"]
        check("1-3 pikeman names carry English singular/plural and zh",
              pikeman["names"]["en"]["singular"] == "Pikeman"
              and pikeman["names"]["en"]["plural"] == "Pikemen"
              and pikeman["names"]["zh"]["singular"] == "枪兵", str(pikeman["names"]))
        check("1-4 stats facts equal the synthetic legacy table",
              _fact_value(pikeman, "stats.attack") == 4
              and _fact_value(pikeman, "stats.defense") == 5
              and _fact_value(pikeman, "stats.hit_points") == 10
              and _fact_value(pikeman, "stats.cost.gold") == 60,
              f"attack={_fact_value(pikeman, 'stats.attack')} "
              f"cost={_fact_value(pikeman, 'stats.cost.gold')}")
        check("1-5 a matched manual row upgrades the fact confidence to cross_checked",
              (_fact(pikeman, "stats.attack") or {}).get("confidence") == "cross_checked",
              str((_fact(pikeman, "stats.attack") or {}).get("confidence")))
        check("1-6 the ability from the JSONC config is published",
              _fact_value(pikeman, "ability.cavalry_charge_immunity") ==
              {"type": "CHARGE_IMMUNITY"},
              str(_fact_value(pikeman, "ability.cavalry_charge_immunity")))
        check("1-7 pikeman is a base creature that upgrades to the namespaced id",
              pikeman["creature"]["base_or_upgrade"] == "base"
              and pikeman["creature"]["upgrade_of"] is None
              and pikeman["creature"]["upgrades_to"] == ["core:halberdier"],
              str(pikeman["creature"]))
        check("1-8 aliases are verified and sourced (plural + localized)",
              any(alias["text"] == "Pikemen" and alias["kind"] == "plural"
                  for alias in pikeman["aliases"])
              and any(alias["text"] == "枪兵" and alias["lang"] == "zh"
                      for alias in pikeman["aliases"])
              and all(alias["verified"] is True for alias in pikeman["aliases"]))
        check("1-9 manual_evidence records all seven matched rows",
              len(pikeman["manual_evidence"]["checks"]) == 7
              and all(item["status"] == "match"
                      for item in pikeman["manual_evidence"]["checks"]),
              str([item["status"] for item in pikeman["manual_evidence"]["checks"]]))

        halberdier = cards["core:halberdier"]
        check("1-10 the upgrade card is distinct and points back at its base",
              halberdier["id"] != pikeman["id"]
              and halberdier["creature"]["base_or_upgrade"] == "upgrade"
              and halberdier["creature"]["upgrade_of"] == "core:pikeman"
              and _fact_value(halberdier, "stats.attack") == 6,
              str(halberdier["creature"]))
        check("1-11 base and upgrade values are not mixed up (attack 4 vs 6)",
              _fact_value(pikeman, "stats.attack") == 4
              and _fact_value(halberdier, "stats.attack") == 6)

        peasant = cards["core:peasant"]
        lore = _fact(peasant, "lore.manual_description")
        check("1-12 a neutral creature keeps the manual lore as an explicit manual_only fact",
              lore is not None and "Peasants are the most common folk" in lore["value"]
              and lore["confidence"] == "manual_only", str(lore))
        check("1-13 the neutral card declares the missing numeric cross check",
              any(item["key"] == "manual_numeric_cross_check"
                  for item in peasant["missing"]), str(peasant["missing"]))

        magic = cards["core:magicArrow"]
        for proficiency in schema.PROFICIENCIES:
            check(f"1-14 magic arrow cost fact exists for proficiency {proficiency}",
                  _fact_value(magic, f"spell.cost.{proficiency}") != "<<absent>>",
                  str(_fact_value(magic, f"spell.cost.{proficiency}")))
        check("1-15 proficiency costs are distinct facts, not one merged value",
              [_fact_value(magic, f"spell.cost.{p}") for p in schema.PROFICIENCIES]
              == [5, 4, 4, 4],
              str([_fact_value(magic, f"spell.cost.{p}") for p in schema.PROFICIENCIES]))
        check("1-16 level power facts distinguish basic/advanced/expert",
              [_fact_value(magic, f"spell.level_power.{p}") for p in
               schema.PROFICIENCIES] == [10, 10, 20, 30],
              str([_fact_value(magic, f"spell.level_power.{p}") for p in
                   schema.PROFICIENCIES]))
        check("1-17 manual formulas are published per proficiency",
              _fact_value(magic, "spell.formula.basic") == "((Power x 10) + 10) damage"
              and _fact_value(magic, "spell.formula.expert") == "((Power x 10) + 30) damage")
        check("1-18 duration and right-click text are sourced from manual/SPTRAITS",
              _fact_value(magic, "spell.duration") == "Instant"
              and "Bolt of magic" in str(_fact_value(magic, "spell.effect.none")))
        check("1-19 the missing none-level formula is explicit, not fabricated",
              any(item["key"] == "spell.formula.none" for item in magic["missing"])
              and _fact_value(magic, "spell.formula.none") == "<<absent>>")

        availability = magic["spell"]["availability"]
        check("1-20 rule-exists / bridge-executable / castable-now are three separate claims",
              set(availability) == {"rule_exists", "bridge_executable", "castable_now"})
        check("1-21 a normal single-stage spell is code-path executable, not live-verified",
              availability["bridge_executable"]["value"] is True
              and availability["bridge_executable"]["verification"] == "code_path_only",
              str(availability["bridge_executable"]))
        check("1-22 castable_now stays unverified (live state not known statically)",
              availability["castable_now"]["value"] == "unverified"
              and availability["castable_now"]["verification"] == "not_verified")
        teleport = cards["core:teleport"]
        teleport_availability = teleport["spell"]["availability"]
        check("1-23 a two-stage spell is explicitly not bridge-executable",
              teleport_availability["bridge_executable"]["value"] is False
              and "two_stage" in str(teleport_availability["bridge_executable"]["reason"]),
              str(teleport_availability["bridge_executable"]))
        check("1-24 the two-stage refusal and the generic path cite different bridge lines",
              {ref["locator"] for ref in
               teleport_availability["bridge_executable"]["sources"]}
              != {ref["locator"] for ref in availability["bridge_executable"]["sources"]})

        # manifest / ruleset / coverage outputs
        check("1-25 the source manifest records hashes for present sources",
              all(isinstance(item["sha256"], str) and len(item["sha256"]) == 64
                  for item in result.manifest["sources"]
                  if item["status"] == "present"),
              str(len(result.manifest["sources"])))
        check("1-26 the optional missing engine export is explicit, not silently absent",
              [item["source_id"] for item in result.manifest["sources"]
               if item["status"] == "missing"] == ["source.engine_export.creatures"]
              and "var/engine-exports/creatures.json" in
              result.coverage["missing_sources"][0]["reason"])
        ruleset_file = result.out_dir / "ruleset.json"
        coverage_file = result.out_dir / "coverage.json"
        check("1-27 ruleset.json and coverage.json are written and parse as JSON",
              ruleset_file.exists() and coverage_file.exists()
              and json.loads(ruleset_file.read_text(encoding="utf-8"))["ruleset_id"]
              == "fixture-ruleset")
        check("1-28 the ruleset pins the source manifest sha256",
              result.ruleset["source_manifest"]["sha256"]
              == S.manifest_sha256(result.manifest)
              and len(result.ruleset["source_manifest"]["sha256"]) == 64)
        check("1-29 coverage totals count cards/facts and the missing entries",
              result.coverage["totals"]["cards"] == 5
              and result.coverage["totals"]["facts"] > 30
              and result.coverage["totals"]["missing"] > 0,
              str(result.coverage["totals"]))
        check("1-30 every card ref of every card is traceable", result.traceability_refs > 0,
              str(result.traceability_refs))
        check("1-31 a second build with unchanged sources is idempotent",
              [card["id"] for card in K.build(fx.root, fx.specs, fx.out).cards]
              == [card["id"] for card in result.cards])


# ======================================================================================
# 2. version conflict
# ======================================================================================
def _rewrite_crtraits(fx: Fixture, attack: int) -> None:
    rows = [
        _creature_row("Pikeman", "Pikemen", 60, 100, 80, 14, 10, 4, attack, 5, 1, 3),
        _creature_row("Halberdier", "Halberdiers", 75, 115, 115, 14, 10, 5, 6, 5, 2, 3),
        _creature_row("Peasant", "Peasants", 10, 15, 15, 25, 1, 3, 1, 1, 1, 1),
    ]
    _write(fx.specs / "crtraits.txt", _CRTRAITS_HEADER + "\n".join(rows) + "\n",
           encoding="latin-1")


def test_version_conflict() -> None:
    with fixture() as fx:
        first = K.build(fx.root, fx.specs, fx.out)
        pikeman_path = fx.out / "fixture-ruleset" / "creatures" / "pikeman.json"
        before = json.loads(pikeman_path.read_text(encoding="utf-8"))
        _rewrite_crtraits(fx, attack=9)
        raised = False
        try:
            K.build(fx.root, fx.specs, fx.out)
        except S.SourceVersionConflictError as exc:
            raised = True
            conflict_text = str(exc)
        check("2-1 a changed local source raises SourceVersionConflictError", raised,
              conflict_text[:160] if raised else "no error")
        check("2-2 the conflict names the changed source and field",
              raised and "source.game_data.crtraits" in conflict_text
              and "sha256" in conflict_text, conflict_text[:200] if raised else "")
        check("2-3 the existing card snapshot is preserved (no silent overwrite)",
              json.loads(pikeman_path.read_text(encoding="utf-8")) == before)

        accepted = K.build(fx.root, fx.specs, fx.out, accept_source_change=True)
        accepted_pikeman = _cards_by_id(accepted)["core:pikeman"]
        check("2-4 an explicit source update rebuilds with the new rule-table value",
              _fact_value(accepted_pikeman, "stats.attack") == 9,
              str(_fact_value(accepted_pikeman, "stats.attack")))
        check("2-5 the stale manual row becomes an explicit conflict, not a silent value",
              any(item["key"] == "stats.attack" for item in accepted_pikeman["conflicts"])
              and any(item["status"] == "mismatch"
                      for item in accepted_pikeman["manual_evidence"]["checks"]),
              str([item["key"] for item in accepted_pikeman["conflicts"]]))
        check("2-6 the conflicting fact stays with the rule-table value and is not cross_checked",
              (_fact(accepted_pikeman, "stats.attack") or {}).get("value") == 9
              and (_fact(accepted_pikeman, "stats.attack") or {}).get(
                  "confidence") != "cross_checked",
              str(_fact(accepted_pikeman, "stats.attack"))[:200])


# ======================================================================================
# 3. missing sources / entities / proficiency columns
# ======================================================================================
def test_missing_required_source() -> None:
    with fixture() as fx:
        (fx.specs / "manual_player.md").unlink()
        raised = False
        try:
            K.build(fx.root, fx.specs, fx.out)
        except S.MissingSourceError as exc:
            raised = True
            detail = str(exc)
        check("3-1 a missing required source fails the build", raised, detail[:200] if raised else "no error")
        check("3-2 the failure names the missing source and path",
              raised and "source.manual.player" in detail and "manual_player.md" in detail,
              detail[:200] if raised else "")
        check("3-3 no cards are written when sources are missing",
              not (fx.out / "fixture-ruleset" / "creatures").exists())


def test_missing_legacy_entity() -> None:
    with fixture(entity_legacy_name="Ghost") as fx:
        raised = False
        try:
            K.build(fx.root, fx.specs, fx.out)
        except S.MissingSourceError as exc:
            raised = True
            detail = str(exc)
        check("3-4 an entity absent from the legacy table fails instead of inventing a card",
              raised and "Ghost" in detail, detail[:200] if raised else "no error")


def test_missing_proficiency_is_explicit() -> None:
    with fixture(blank_expert_cost=True) as fx:
        result = K.build(fx.root, fx.specs, fx.out)
        magic = _cards_by_id(result)["core:magicArrow"]
        check("3-5 an empty proficiency cost becomes an explicit missing entry",
              any(item["key"] == "spell.cost.expert" for item in magic["missing"]),
              str([item["key"] for item in magic["missing"]]))
        check("3-6 no fabricated zero cost is published for the empty column",
              _fact_value(magic, "spell.cost.expert") == "<<absent>>",
              str(_fact_value(magic, "spell.cost.expert")))
        check("3-7 the other proficiency costs still exist",
              _fact_value(magic, "spell.cost.basic") == 4
              and _fact_value(magic, "spell.cost.advanced") == 4)


# ======================================================================================
# 4. manual disagreement
# ======================================================================================
def test_manual_disagreement() -> None:
    with fixture(manual_attack="A t t a c k 9 9") as fx:
        result = K.build(fx.root, fx.specs, fx.out)
        pikeman = _cards_by_id(result)["core:pikeman"]
        conflicts = [item for item in pikeman["conflicts"] if item["key"] == "stats.attack"]
        checks = [item for item in pikeman["manual_evidence"]["checks"]
                  if item["row"] == "attack"]
        check("4-1 a manual/rule-table mismatch is recorded as a conflict", bool(conflicts),
              str(conflicts)[:240])
        check("4-2 the manual check status is mismatch", checks and
              checks[0]["status"] == "mismatch", str(checks))
        check("4-3 the fact keeps the rule-table value and is not cross_checked",
              _fact_value(pikeman, "stats.attack") == 4
              and (_fact(pikeman, "stats.attack") or {}).get("confidence") == "rule_table",
              str(_fact(pikeman, "stats.attack"))[:200])
        check("4-4 both disagreeing sources are attached to the conflict",
              len(conflicts[0]["sources"]) == 2 and
              {ref["source_id"] for ref in conflicts[0]["sources"]}
              == {"source.game_data.crtraits", "source.manual.player"},
              str(conflicts[0]["sources"]))


# ======================================================================================
# 5. schema rejections
# ======================================================================================
def _expect_schema_error(label: str, fn) -> None:
    try:
        fn()
        check(label, False, "no SchemaError raised")
    except schema.SchemaError:
        check(label, True)


def test_schema_rejections() -> None:
    with fixture() as fx:
        result = K.build(fx.root, fx.specs, fx.out)
        known = {item["source_id"] for item in result.manifest["sources"]}
        base = _cards_by_id(result)["core:pikeman"]

        def mutate(**changes):
            card = copy.deepcopy(base)
            card.update(changes)
            return card

        check("5-1 a valid card passes validate_card", (lambda: schema.validate_card(
            copy.deepcopy(base), known))() is None)
        _expect_schema_error("5-2 a fact without sources is rejected", lambda: (
            schema.validate_card(mutate(facts=[{"key": "stats.attack", "value": 4,
                                               "confidence": "rule_table"}]), known)))
        bad_source = mutate(facts=[{**base["facts"][0],
                                    "sources": [{"source_id": "source.unknown",
                                                 "locator": "line 1"}]}])
        _expect_schema_error("5-3 an unknown source id is rejected",
                             lambda: schema.validate_card(bad_source, known))
        duplicate = mutate(facts=[base["facts"][0], copy.deepcopy(base["facts"][0])])
        _expect_schema_error("5-4 duplicate fact keys are rejected",
                             lambda: schema.validate_card(duplicate, known))
        _expect_schema_error("5-5 an unknown kind is rejected",
                             lambda: schema.validate_card(mutate(kind="artifact"), known))
        _expect_schema_error("5-6 a base creature declaring upgrade_of is rejected",
                             lambda: schema.validate_card(
                                 mutate(creature={**base["creature"],
                                                  "upgrade_of": "core:halberdier"}),
                                 known))
        _expect_schema_error("5-7 an unverified alias is rejected",
                             lambda: schema.validate_card(
                                 mutate(aliases=[{**base["aliases"][0],
                                                  "verified": False}]), known))
        _expect_schema_error("5-8 a null fact value is rejected",
                             lambda: schema.validate_fact(
                                 {"key": "stats.attack", "value": None,
                                  "confidence": "rule_table",
                                  "sources": [base["facts"][0]["sources"][0]]}, known))
        _expect_schema_error("5-9 an invalid coverage status is rejected",
                             lambda: schema.coverage("probably-fine"))
        magic = copy.deepcopy(_cards_by_id(result)["core:magicArrow"])
        del magic["spell"]["availability"]
        _expect_schema_error("5-10 a spell card without availability is rejected",
                             lambda: schema.validate_card(magic, known))
        bad_claim = copy.deepcopy(_cards_by_id(result)["core:magicArrow"])
        bad_claim["spell"]["availability"]["castable_now"]["verification"] = "trust-me"
        _expect_schema_error("5-11 an unknown availability verification level is rejected",
                             lambda: schema.validate_card(bad_claim, known))
        bad_ruleset = copy.deepcopy(result.ruleset)
        bad_ruleset["source_manifest"]["sha256"] = "not-a-hash"
        _expect_schema_error("5-12 a ruleset with an unpinned source manifest is rejected",
                             lambda: schema.validate_ruleset(bad_ruleset, known))
        bad_entity = copy.deepcopy(
            json.loads((fx.specs / "entity_spec.json").read_text(encoding="utf-8")))
        bad_entity["creatures"][0]["manual"]["rows"]["attack"] = 0
        _expect_schema_error("5-13 a non-positive manual line locator is rejected",
                             lambda: K.validate_entity_spec(bad_entity))


# ======================================================================================
# 6. traceability
# ======================================================================================
def test_traceability() -> None:
    with fixture() as fx:
        result = K.build(fx.root, fx.specs, fx.out)
        verified = S.assert_cards_traceable(result.manifest, result.cards, fx.root)
        check("6-1 every published source ref resolves in its declared source",
              verified > 0, str(verified))
        pikeman = _cards_by_id(result)["core:pikeman"]
        tampered_locator = copy.deepcopy(pikeman)
        tampered_locator["facts"][0]["sources"][0]["locator"] = "line 99999"
        try:
            S.assert_cards_traceable(result.manifest, [tampered_locator], fx.root)
            check("6-2 a locator outside the source is rejected", False, "no error")
        except S.TraceabilityError:
            check("6-2 a locator outside the source is rejected", True)
        tampered_quote = copy.deepcopy(pikeman)
        tampered_quote["facts"][0]["sources"].append(
            {"source_id": "source.manual.player", "locator": "page 1 line 5",
             "quote": "this text is not in the manual"})
        try:
            S.assert_cards_traceable(result.manifest, [tampered_quote], fx.root)
            check("6-3 a quote that is not present in the source is rejected", False,
                  "no error")
        except S.TraceabilityError:
            check("6-3 a quote that is not present in the source is rejected", True)


def test_ruleset_evidence_traceability() -> None:
    bad_ref = [{"source_id": "source.game_data.sptraits", "locator": "line 999999"}]
    with fixture(ruleset_evidence=bad_ref) as fx:
        raised = False
        try:
            K.build(fx.root, fx.specs, fx.out)
        except S.TraceabilityError:
            raised = True
        check("6-4 unverifiable ruleset evidence fails the build instead of being published",
              raised)
        check("6-5 no card output is published when evidence verification fails",
              not (fx.out / "fixture-ruleset" / "creatures").exists())


# ======================================================================================
# main
# ======================================================================================
def main() -> int:
    test_build_and_cards()
    test_version_conflict()
    test_missing_required_source()
    test_missing_legacy_entity()
    test_missing_proficiency_is_explicit()
    test_manual_disagreement()
    test_schema_rejections()
    test_traceability()
    test_ruleset_evidence_traceability()
    failed = [item for item in RESULTS if not item[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
