# -*- coding: utf-8 -*-
"""B1.1 offline card builder: local public sources -> cards + manifests (no game start).

What is tracked vs local-only
-----------------------------
Tracked (this file): parsing of the public data formats, the merge/cross-check logic and
the output contract.  Local-only (git-ignored ``doc/``): the actual source files, the
entity/source/ruleset specs and every generated card.  A fresh clone therefore fails with
:class:`MissingSourceError` until the local sources are provided - it never fabricates data.

Inputs (``doc/knowledge/sources/``)
-----------------------------------
``source_spec.json``   declared sources (kind, path, priority, optional pin/provenance).
``ruleset_spec.json``  ruleset identity + evidence refs + active-binding status.
``entity_spec.json``   first batch: 18 creatures + 10 spells, with manual page/line locators.

Outputs
-------
``doc/knowledge/sources/source_manifest.json``        hashed source manifest (present/missing)
``doc/knowledge/rulesets/<ruleset-id>/ruleset.json``  ruleset manifest + coverage totals
``doc/knowledge/rulesets/<ruleset-id>/creatures/*.json``
``doc/knowledge/rulesets/<ruleset-id>/spells/*.json``
``doc/knowledge/rulesets/<ruleset-id>/coverage.json`` per-card coverage/missing summary

Effective-data rule (mirrors the engine)
----------------------------------------
For creatures: original-game ``CRTRAITS.TXT`` (from the local SoD LOD) is loaded first and
the VCMI core creature JSON is merged over it (``ContentTypeHandler.cpp``); combat numbers
come from the legacy row, structural fields/abilities from the core JSON.  For spells:
``SPTRAITS.TXT`` provides level/school/cost/power/right-click text, the core JSON provides
target/effects, and ``levels.base`` is inherited into every proficiency level
(``CSpellHandler.cpp::beforeValidate``).  This builder replays that merge offline; the
result is explicitly labelled as NOT engine-verified (no runtime export in this batch).

Usage (from the repository root)::

    $env:PYTHONPATH='src'
    .venv\\Scripts\\python.exe -X utf8 -m auto_heros3.knowledge.build
    # optional: --specs-dir doc/knowledge/sources --out-dir doc/knowledge/rulesets
    #           --accept-source-change  (only after a deliberate source update)
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import schema
from .schema import (
    SCHEMA_VERSION,
    PROFICIENCIES,
    SchemaError,
    conflict,
    coverage,
    fact,
    missing,
    source_ref,
)
from .sources import (
    MissingSourceError,
    SourceIndex,
    SourceVersionConflictError,
    assert_cards_traceable,
    assert_ref_traceable,
    assert_sources_compatible,
    build_manifest,
    load_manifest,
    load_source_specs,
    manifest_sha256,
    missing_sources,
    require_present,
    source_by_id,
)

DEFAULT_SPECS_DIR = "doc/knowledge/sources"
DEFAULT_OUT_DIR = "doc/knowledge/rulesets"
MANIFEST_REL = "doc/knowledge/sources/source_manifest.json"

# CRTRAITS.TXT column layout (CCreatureHandler.cpp::loadLegacyData order)
_CRTRAITS_COLUMNS = {
    "plural": 1, "wood": 2, "mercury": 3, "ore": 4, "sulfur": 5, "crystal": 6,
    "gems": 7, "gold": 8, "fight_value": 9, "ai_value": 10, "growth": 11,
    "horde_growth": 12, "hit_points": 13, "speed": 14, "attack": 15, "defense": 16,
    "damage_min": 17, "damage_max": 18, "shots": 19, "spell_points": 20,
    "adv_map_min": 21, "adv_map_max": 22, "ability_text": 23, "attributes": 24,
}
_RESOURCE_FIELDS = ("wood", "mercury", "ore", "sulfur", "crystal", "gems", "gold")

# SPTRAITS.TXT column layout (CSpellHandler.cpp::loadLegacyData order)
_SPTRAITS_PROFICIENCY_COLUMNS = {"none": 7, "basic": 8, "advanced": 9, "expert": 10}
_SPTRAITS_POWER_COLUMNS = {"none": 12, "basic": 13, "advanced": 14, "expert": 15}
_SPTRAITS_AI_COLUMNS = {"none": 25, "basic": 26, "advanced": 27, "expert": 28}
_SPTRAITS_DESCRIPTION_COLUMNS = {"none": 29, "basic": 30, "advanced": 31, "expert": 32}

_MANUAL_ROW_LABELS = {
    "cost": "COST",
    "damage": "DAMAGE",
    "shots": "SHOTS",
    "attack": "ATTACK",
    "defense": "DEFENSE",
    "health": "HEALTH",
    "speed": "SPEED",
}

_DURATION_CANONICAL = {
    "INSTANT": "Instant",
    "COMBAT": "Combat",
    "PERMANENT": "Permanent",
    "CURRENTBATTLE": "Current Battle",
    "1DAY": "1 Day",
    "1ROUND/POWER": "1 Round/Power",
    "2ROUNDS/POWER": "2 Rounds/Power",
    "UNTILTOUCHED": "Until Touched",
    "SPECIAL": "Special",
}

_NUMBER_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven",
    8: "eight", 9: "nine", 10: "ten", 20: "twenty", 30: "thirty", 50: "fifty",
}

# Canonical per-proficiency effect formulas, transcribed from the manual blocks cited in
# ``entity_spec.json``.  The builder re-checks every number against the cited block, so a
# changed manual/extraction fails instead of silently keeping a stale formula.
_FORMULA_NUMBER_RE = re.compile(r"\d+")

_EFFECTIVE_MISSING_REASON = (
    "本批未做引擎运行时导出：数值由本机 SoD legacy TXT + VCMI core JSON 按引擎 merge 逻辑"
    "离线推导，未经引擎加载回执；若后续导出与快照冲突，必须展示冲突而非静默覆盖")

_CREATURE_BRIDGE_MISSING_REASON = (
    "现有桥只暴露玩家可见的军队/升级成本/英雄法术书等切片，不导出静态兵种属性；"
    "静态属性查询当前只能来自离线资料，B1.2 的接口不声称 bridge 已暴露这些字段")


class BuildError(SchemaError):
    """A local spec/source cannot be turned into contract-valid cards."""


# ======================================================================================
# small utilities
# ======================================================================================
def deep_merge(base: Any, override: Any) -> Any:
    """Replay ``JsonUtils::inherit``/``merge``: dicts merge recursively, other types replace."""
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = deep_merge(merged[key], value) if key in merged else value
        return merged
    return override


def _int_field(value: Any, where: str, *, allow_blank: bool = False) -> int | None:
    text = str(value).strip()
    if allow_blank and text == "":
        return None
    if not re.fullmatch(r"-?\d+", text):
        raise BuildError(f"{where}: expected an integer, got {value!r}")
    return int(text)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text).upper()


def _csv_rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text), delimiter="\t", quotechar='"'))


def _write_json(path: Path, data: Any) -> None:
    schema.write_json_file(path, data)


def _rel(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


# ======================================================================================
# legacy table parsers (original game data)
# ======================================================================================
def parse_crtraits(text: str) -> dict[str, dict]:
    """Parse CRTRAITS.TXT (tab separated, 25 columns) into ``{lower_name: row}``."""
    rows = _csv_rows(text)
    header = None
    for index, row in enumerate(rows):
        if row and row[0].strip() == "Name":
            header = index
            break
    if header is None:
        raise BuildError("CRTRAITS.TXT: 'Name' header row not found")
    parsed: dict[str, dict] = {}
    for index in range(header + 2, len(rows)):
        row = rows[index]
        if len(row) < 25:
            continue
        name = row[0].strip()
        if not name:
            continue
        entry: dict = {"line": index + 1, "name": name, "plural": row[1].strip()}
        for field, column in _CRTRAITS_COLUMNS.items():
            if field == "plural":
                continue
            if field in ("ability_text", "attributes"):
                entry[field] = row[column].strip()
            else:
                entry[field] = _int_field(row[column], f"CRTRAITS line {index + 1} {field}",
                                          allow_blank=True)
        key = name.lower()
        if key in parsed:
            raise BuildError(f"CRTRAITS.TXT: duplicate creature row {name!r}")
        parsed[key] = entry
    if not parsed:
        raise BuildError("CRTRAITS.TXT: no creature rows parsed")
    return parsed


def parse_sptraits(text: str) -> dict[str, dict]:
    """Parse SPTRAITS.TXT into ``{lower_name: row}`` including per-proficiency columns."""
    rows = _csv_rows(text)
    header = None
    for index, row in enumerate(rows):
        if row and row[0].strip() == "Name":
            header = index
            break
    if header is None:
        raise BuildError("SPTRAITS.TXT: 'Name' header row not found")
    section = None
    parsed: dict[str, dict] = {}
    for index in range(header + 2, len(rows)):
        row = rows[index]
        if len(row) < 34:
            continue
        name = row[0].strip()
        if not name:
            continue
        lowered = name.lower()
        if lowered in ("adventure spells", "combat spells", "creature abilities"):
            section = {"adventure spells": "adventure", "combat spells": "combat",
                       "creature abilities": "ability"}[lowered]
            continue
        entry = {
            "line": index + 1,
            "name": name,
            "abbreviated_name": row[1].strip(),
            "level": _int_field(row[2], f"SPTRAITS line {index + 1} level", allow_blank=True),
            "schools": [school for school, column in
                        (("earth", 3), ("water", 4), ("fire", 5), ("air", 6))
                        if row[column].strip().lower() == "x"],
            "cost": {prof: _int_field(row[column],
                                      f"SPTRAITS line {index + 1} cost.{prof}",
                                      allow_blank=True)
                     for prof, column in _SPTRAITS_PROFICIENCY_COLUMNS.items()},
            "power": _int_field(row[11], f"SPTRAITS line {index + 1} power", allow_blank=True),
            "level_power": {prof: _int_field(row[column],
                                             f"SPTRAITS line {index + 1} power.{prof}",
                                             allow_blank=True)
                            for prof, column in _SPTRAITS_POWER_COLUMNS.items()},
            "ai_value": {prof: _int_field(row[column], "SPTRAITS ai", allow_blank=True)
                         for prof, column in _SPTRAITS_AI_COLUMNS.items()},
            "description": {prof: row[column].strip()
                            for prof, column in _SPTRAITS_DESCRIPTION_COLUMNS.items()},
            "attributes": row[33].strip(),
            "type": section or "combat",
        }
        if lowered in parsed:
            raise BuildError(f"SPTRAITS.TXT: duplicate spell row {name!r}")
        parsed[lowered] = entry
    if not parsed:
        raise BuildError("SPTRAITS.TXT: no spell rows parsed")
    return parsed


# ======================================================================================
# manual access
# ======================================================================================
class ManualView:
    """Line-oriented view of an extracted manual (``## 第N页`` page markers)."""

    def __init__(self, source_id: str, text: str):
        self.source_id = source_id
        self.lines = text.splitlines()
        self.markers: list[tuple[int, int]] = []
        for index, line in enumerate(self.lines):
            match = re.match(r"^## 第(\d+)页$", line.strip())
            if match:
                self.markers.append((index + 1, int(match.group(1))))

    @property
    def line_count(self) -> int:
        return len(self.lines)

    def line(self, line_no: int) -> str:
        if not 1 <= line_no <= len(self.lines):
            raise BuildError(f"manual {self.source_id}: line {line_no} out of range")
        return self.lines[line_no - 1]

    def page_for(self, line_no: int) -> int:
        page = 0
        for marker_line, marker_page in self.markers:
            if marker_line <= line_no:
                page = marker_page
            else:
                break
        if not page:
            raise BuildError(
                f"manual {self.source_id}: line {line_no} precedes the first page marker")
        return page

    def require_line(self, line_no: int, contains: str | None = None) -> str:
        raw = self.line(line_no)
        if contains is not None and _normalize(contains) not in _normalize(raw):
            raise BuildError(
                f"manual {self.source_id} line {line_no}: expected {contains!r} in {raw!r}")
        return raw

    def block_quote(self, lines: Iterable[int]) -> tuple[str, int]:
        numbers = list(lines)
        if not numbers:
            raise BuildError(f"manual {self.source_id}: empty quote block")
        raw = "\n".join(self.line(number) for number in numbers)
        return raw, numbers[0]

    def ref(self, page: int, line_no: int, quote: str | None = None) -> dict:
        return source_ref(self.source_id, f"page {page} line {line_no}", quote)


def _duration_value(raw: str) -> str:
    normalized = _normalize(raw)
    marker = "DURATION"
    if marker in normalized:
        normalized = normalized.split(marker, 1)[1]
    return _DURATION_CANONICAL.get(normalized, normalized.title())


def _manual_effect_lines(manual: ManualView, page: int, line_numbers: list[int]) -> tuple[dict, str]:
    quote, first_line = manual.block_quote(line_numbers)
    return manual.ref(page, first_line, quote), quote


# ======================================================================================
# source/spec loading
# ======================================================================================
def _load_json_spec(path: Path) -> dict:
    if not path.exists():
        raise MissingSourceError([{"source_id": path.name, "path": str(path),
                                   "reason": "build spec file is absent"}])
    return json.loads(path.read_text(encoding="utf-8"))


def validate_entity_spec(spec: dict) -> None:
    if not isinstance(spec, dict) or spec.get("schema_version") != SCHEMA_VERSION:
        raise SchemaError(f"entity_spec: schema_version must be {SCHEMA_VERSION!r}")
    for group in ("creatures", "spells"):
        items = spec.get(group)
        if not isinstance(items, list) or not items:
            raise SchemaError(f"entity_spec: '{group}' must be a non-empty list")
        seen: set[str] = set()
        for index, item in enumerate(items):
            where = f"entity_spec.{group}[{index}]"
            if not isinstance(item, dict):
                raise SchemaError(f"{where}: must be an object")
            card_id = item.get("id")
            if not isinstance(card_id, str) or ":" not in card_id:
                raise SchemaError(f"{where}: id must be a namespaced stable id")
            if card_id in seen:
                raise SchemaError(f"{where}: duplicate id {card_id!r}")
            seen.add(card_id)
            if not isinstance(item.get("legacy_name"), str) or not item["legacy_name"].strip():
                raise SchemaError(f"{where}: legacy_name required")
            json_spec = item.get("json")
            if not isinstance(json_spec, dict) or not isinstance(
                    json_spec.get("source_id"), str) or not isinstance(json_spec.get("key"), str):
                raise SchemaError(f"{where}: json {{source_id, key}} required")
            manual = item.get("manual")
            if not isinstance(manual, dict) or not isinstance(manual.get("source_id"), str):
                raise SchemaError(f"{where}: manual.source_id required")
            if group == "creatures":
                has_rows = isinstance(manual.get("rows"), dict) and manual["rows"]
                has_description = isinstance(manual.get("description_lines"), list) and (
                    manual["description_lines"])
                if not (has_rows or has_description):
                    raise SchemaError(
                        f"{where}: manual needs 'rows' (stat table) or 'description_lines'")
                if not isinstance(manual.get("page"), int):
                    raise SchemaError(f"{where}: manual.page must be an int")
                if has_rows:
                    if not isinstance(manual.get("name_line"), int):
                        raise SchemaError(f"{where}: manual.name_line required for stat tables")
                    if manual.get("column") not in (0, 1):
                        raise SchemaError(f"{where}: manual.column must be 0|1 for stat tables")
                    for row_key, line_no in manual["rows"].items():
                        if row_key not in _MANUAL_ROW_LABELS:
                            raise SchemaError(f"{where}: unknown manual row {row_key!r}")
                        if not isinstance(line_no, int) or line_no < 1:
                            raise SchemaError(f"{where}: manual.rows.{row_key} must be an int")
            else:
                for field_name in ("name_line", "type_line", "cost_line", "duration_line"):
                    if not isinstance(manual.get(field_name), int):
                        raise SchemaError(f"{where}: manual.{field_name} must be an int")
                effect_lines = manual.get("effect_lines")
                if not isinstance(effect_lines, dict) or not all(
                        isinstance(effect_lines.get(prof), list) and effect_lines[prof]
                        for prof in ("basic", "advanced", "expert")):
                    raise SchemaError(
                        f"{where}: manual.effect_lines needs basic/advanced/expert line lists")
                formulas = item.get("formulas")
                if not isinstance(formulas, dict) or not formulas:
                    raise SchemaError(f"{where}: formulas required for spells")
                for prof in formulas:
                    if prof not in ("basic", "advanced", "expert"):
                        raise SchemaError(f"{where}: formulas key must be basic|advanced|expert")


def load_build_specs(specs_dir: Path) -> tuple[list, dict, dict]:
    sources = load_source_specs(specs_dir / "source_spec.json")
    ruleset = _load_json_spec(specs_dir / "ruleset_spec.json")
    entity = _load_json_spec(specs_dir / "entity_spec.json")
    schema.validate_ruleset_spec(ruleset)
    validate_entity_spec(entity)
    declared = {spec.source_id: spec for spec in sources}
    used = {item["json"]["source_id"] for item in entity["creatures"] + entity["spells"]}
    used |= {item["manual"]["source_id"] for item in entity["creatures"] + entity["spells"]}
    unknown = sorted(used - set(declared))
    if unknown:
        raise SchemaError(f"entity_spec references undeclared sources: {unknown}")
    return sources, ruleset, entity


# ======================================================================================
# manual numeric cross checks
# ======================================================================================
def _manual_row_values(row_kind: str, raw: str) -> tuple[list | None, str]:
    """Parse the two manual columns for one stat row; returns (values, normalized_rest).

    Manual extraction keeps intra-number spaces (``1 0 1 2`` for 10/12), so values are
    compared against the expected pair from the rule table instead of being trusted alone.
    """
    label = _MANUAL_ROW_LABELS[row_kind]
    normalized = _normalize(raw)
    if not normalized.startswith(label):
        return None, normalized
    rest = normalized[len(label):]
    if row_kind == "cost":
        digits = re.findall(r"\d+", rest)
        return [int(item) for item in digits[:2]], rest
    if row_kind == "damage":
        return [[(int(a), int(b)) for a, b in re.findall(r"(\d+)-(\d+)", rest)][:2]], rest
    if row_kind == "speed":
        return [int(item) for item in re.findall(r"\((\d+)", rest)][:2], rest
    if row_kind == "shots":
        if rest.count("NONE") >= 2:
            return [0, 0], rest
        return [int(item) for item in re.findall(r"\d+", rest)][:2], rest
    # attack/defense/health: two values with optional intra-number spaces
    expected = re.findall(r"\d+", rest)
    return [int(item) for item in expected[:2]], rest


def _manual_pair_matches(row_kind: str, raw: str, expected: list) -> tuple[str, dict]:
    values, rest = _manual_row_values(row_kind, raw)
    if values is None:
        return "unparsed", {"reason": "manual row does not start with its label"}
    if row_kind == "cost":
        status = "match" if values == expected else "mismatch"
        return status, {"manual": values, "rule_table": expected}
    if row_kind == "damage":
        expected_norm = "".join(str(item) for item in expected)
        status = "match" if rest.startswith(expected_norm) else "mismatch"
        return status, {"manual": rest, "rule_table": expected_norm}
    if row_kind == "speed":
        status = "match" if values == expected else "mismatch"
        return status, {"manual": values, "rule_table": expected}
    if row_kind == "shots":
        if all(item == 0 for item in expected):
            status = "match" if rest.count("NONE") >= 2 else "mismatch"
        else:
            expected_norm = "".join(str(item) for item in expected)
            status = "match" if rest.startswith(expected_norm) else "mismatch"
        return status, {"manual": rest, "rule_table": expected}
    # attack/defense/health: manual extraction may split digits ("1 0 1 2" -> "1012")
    expected_norm = "".join(str(item) for item in expected)
    if rest.startswith(expected_norm):
        return "match", {"manual": rest, "rule_table": expected_norm}
    return "mismatch", {"manual": rest, "rule_table": expected_norm}


def _formula_consistent(formula: str, block_text: str) -> bool:
    """Every number in the formula must appear (as digits or an English word) in the block."""
    normalized_block = _normalize(block_text)
    lowered = normalized_block.lower()
    for number_text in _FORMULA_NUMBER_RE.findall(formula):
        number = int(number_text)
        if number_text in normalized_block:
            continue
        word = _NUMBER_WORDS.get(number)
        if word is not None and word.upper() in normalized_block:
            continue
        return False
    return True


# ======================================================================================
# build context
# ======================================================================================
@dataclass
class BuildContext:
    root: Path
    index: SourceIndex
    ruleset_id: str
    crtraits: dict[str, dict]
    sptraits: dict[str, dict]
    translation: dict[str, str]
    manuals: dict[str, ManualView]
    card_ids: dict[str, str]  # json key -> namespaced card id (scope:key)
    upgrade_of: dict[str, str]
    upgrades_to: dict[str, list[str]]

    def manual(self, source_id: str) -> ManualView:
        if source_id not in self.manuals:
            raise BuildError(f"manual source {source_id!r} is not loaded")
        return self.manuals[source_id]

    def zh(self, key: str) -> str | None:
        value = self.translation.get(key)
        return value if isinstance(value, str) and value.strip() else None

    def json_document(self, source_id: str) -> dict:
        document = self.index.json(source_id)
        if not isinstance(document, dict):
            raise BuildError(f"config source {source_id} is not a JSON object")
        return document


def _build_context(root: Path, index: SourceIndex, ruleset_spec: dict,
                   entity_spec: dict) -> BuildContext:
    crtraits = parse_crtraits(index.text("source.game_data.crtraits"))
    sptraits = parse_sptraits(index.text("source.game_data.sptraits"))
    translation = index.json("source.localization.chinese")
    if not isinstance(translation, dict):
        raise BuildError("chinese translation source is not a JSON object")
    manuals = {source_id: ManualView(source_id, index.text(source_id))
               for source_id in ("source.manual.player", "source.manual.ab")
               if source_id in index.specs}
    card_ids: dict[str, str] = {}
    upgrade_of: dict[str, str] = {}
    upgrades_to: dict[str, list[str]] = {}
    for item in entity_spec["creatures"]:
        scope = item["id"].split(":", 1)[0]
        key = item["json"]["key"]
        card_ids[f"{item['json']['source_id']}:{key}"] = item["id"]
        document = index.json(item["json"]["source_id"])
        node = document.get(key)
        if not isinstance(node, dict):
            raise MissingSourceError([{
                "source_id": item["json"]["source_id"], "path": key,
                "reason": f"creature {key!r} is absent from the declared config"}])
        for upgrade_key in node.get("upgrades", []) or []:
            upgrade_id = f"{scope}:{upgrade_key}"
            upgrades_to.setdefault(item["id"], []).append(upgrade_id)
            upgrade_of[upgrade_id] = item["id"]
    return BuildContext(root=root, index=index, ruleset_id=ruleset_spec["ruleset_id"],
                        crtraits=crtraits, sptraits=sptraits, translation=translation,
                        manuals=manuals, card_ids=card_ids, upgrade_of=upgrade_of,
                        upgrades_to=upgrades_to)


# ======================================================================================
# creature cards
# ======================================================================================
def _creature_aliases(ctx: BuildContext, plan: dict, legacy: dict, node: dict,
                      zh_singular: str | None, zh_plural: str | None) -> list[dict]:
    legacy_source = "source.game_data.crtraits"
    json_source = plan["json"]["source_id"]
    pointer = f"#/{plan['json']['key']}"
    aliases: list[dict] = []
    if legacy["plural"] and legacy["plural"] != legacy["name"]:
        aliases.append({"text": legacy["plural"], "lang": "en", "kind": "plural",
                        "source_id": legacy_source,
                        "locator": f"line {legacy['line']} column Plural",
                        "verified": True})
    if zh_singular:
        aliases.append({"text": zh_singular, "lang": "zh", "kind": "localized_name",
                        "source_id": "source.localization.chinese",
                        "locator": f"#/creatures.core.{plan['json']['key']}.name.singular",
                        "verified": True})
    if zh_plural and zh_plural != zh_singular:
        aliases.append({"text": zh_plural, "lang": "zh", "kind": "localized_plural",
                        "source_id": "source.localization.chinese",
                        "locator": f"#/creatures.core.{plan['json']['key']}.name.plural",
                        "verified": True})
    for compatibility_id in node.get("compatibilityIdentifiers", []) or []:
        aliases.append({"text": compatibility_id, "lang": "en",
                        "kind": "compatibility_identifier",
                        "source_id": json_source,
                        "locator": f"{pointer}/compatibilityIdentifiers",
                        "verified": True})
    return aliases


def _creature_stat_facts(ctx: BuildContext, plan: dict, legacy: dict, node: dict,
                         manual: dict | None,
                         pair: dict | None) -> tuple[list[dict], list[dict], list[dict], dict]:
    """Build the combat-number facts, cross-checking the manual stat table.

    ``pair`` is ``{"base": legacy_row|None, "upgrade": legacy_row|None}`` for the
    base/upgrade pair the manual table prints side by side (column 0/1).
    Returns (facts, conflicts, missing_items, manual_evidence).
    """
    legacy_source = "source.game_data.crtraits"
    facts: list[dict] = []
    conflicts: list[dict] = []
    missing_items: list[dict] = []
    checks: list[dict] = []
    json_source = plan["json"]["source_id"]
    json_pointer = f"#/{plan['json']['key']}"

    def rule_table_fact(key: str, value: Any, column: str, unit: str | None = None,
                        note: str | None = None) -> dict:
        refs = [source_ref(legacy_source, f"line {legacy['line']} column {column}")]
        return fact(key, value, sources=refs, confidence="rule_table", unit=unit, note=note)

    numeric_plan = [
        ("stats.attack", "attack", "Attack", None),
        ("stats.defense", "defense", "Defense", None),
        ("stats.damage_min", "damage_min", "Damage Minimum", None),
        ("stats.damage_max", "damage_max", "Damage Maximum", None),
        ("stats.hit_points", "hit_points", "Hit Points", None),
        ("stats.speed", "speed", "Speed", "speed"),
        ("stats.shots", "shots", "Shots", "shots"),
        ("stats.growth", "growth", "Growth", "per week"),
        ("stats.horde_growth", "horde_growth", "Horde Growth", "per week"),
        ("stats.fight_value", "fight_value", "Fight Value", None),
        ("stats.ai_value", "ai_value", "AI Value", None),
        ("advmap.amount_min", "adv_map_min", "Adv. Map Low", None),
        ("advmap.amount_max", "adv_map_max", "Adv. Map High", None),
    ]
    for fact_key, field, column, unit in numeric_plan:
        value = legacy[field]
        if value is None:
            missing_items.append(missing(
                fact_key, f"SoD legacy CRTRAITS 的 {column} 字段为空，未填写推测值"))
            continue
        if fact_key == "stats.shots" and "shots" in node:
            json_shots = int(node["shots"])
            refs = [source_ref(legacy_source, f"line {legacy['line']} column {column}"),
                    source_ref(json_source, f"{json_pointer}/shots")]
            if json_shots != value:
                conflicts.append(conflict(
                    fact_key, "legacy CRTRAITS 与 core JSON 的 shots 不一致；采用 core JSON 生效值",
                    sources=refs, values={"legacy": value, "core_json": json_shots}))
            value = json_shots
            facts.append(fact(fact_key, value, sources=refs,
                              confidence="engine_config" if json_shots != legacy[field]
                              else "cross_checked", unit=unit))
        else:
            facts.append(rule_table_fact(fact_key, value, column, unit))
    for resource in _RESOURCE_FIELDS:
        amount = legacy[resource]
        if amount is None:
            missing_items.append(missing(
                f"stats.cost.{resource}",
                f"SoD legacy CRTRAITS 的 Cost ({resource.capitalize()}) 字段为空"))
            continue
        if amount or resource == "gold":
            facts.append(rule_table_fact(f"stats.cost.{resource}", amount,
                                         f"Cost ({resource.capitalize()})",
                                         "gold" if resource == "gold" else resource))
    if legacy["spell_points"]:
        facts.append(rule_table_fact("stats.spell_points", legacy["spell_points"],
                                     "Spells", "spell points"))

    manual_evidence: dict = {"source_id": (manual or {}).get("source_id", ""),
                             "locator": "", "checks": []}
    if not manual or not manual.get("rows"):
        return facts, conflicts, missing_items, manual_evidence
    page = manual["page"]
    manual_view = ctx.manual(manual["source_id"])
    manual_evidence = {"source_id": manual["source_id"],
                       "locator": f"page {page} line {manual['name_line']}",
                       "page": page, "column": manual.get("column"),
                       "checks": checks}
    base_row = (pair or {}).get("base")
    upgrade_row = (pair or {}).get("upgrade")
    for row_kind, line_no in manual["rows"].items():
        raw = manual_view.line(line_no)
        label = _MANUAL_ROW_LABELS[row_kind]
        if _normalize(raw)[:len(label)] != label:
            checks.append({"row": row_kind, "line": line_no, "status": "unparsed",
                           "reason": "manual line does not start with its label"})
            missing_items.append(missing(
                f"manual_check.{row_kind}",
                f"手册行 {line_no} 的标签与预期不符，未能做数值交叉核对"))
            continue
        if base_row is None or upgrade_row is None:
            checks.append({"row": row_kind, "line": line_no, "status": "not_applicable",
                           "reason": "base/upgrade partner not in this batch"})
            continue
        expected_pair = list(_expected_pair_values(row_kind, base_row, upgrade_row))
        status, detail = _manual_pair_matches(row_kind, raw, expected_pair)
        checks.append({"row": row_kind, "line": line_no, "status": status,
                       **({"values": detail} if status == "mismatch" else {})})
        if status == "match":
            for item in facts:
                if item["key"] in _ROW_TO_FACT_KEYS[row_kind]:
                    item["sources"].append(source_ref(
                        manual["source_id"], f"page {page} line {line_no}", raw.strip()))
                    item["confidence"] = "cross_checked"
        elif status == "mismatch":
            conflicts.append(conflict(
                _ROW_TO_FACT_KEYS[row_kind][0],
                f"手册第 {page} 页与 SoD legacy 规则表数值不一致；"
                "卡片保留规则表值并同时展示手册原文",
                sources=[source_ref("source.game_data.crtraits",
                                    f"line {legacy['line']} column {row_kind}"),
                         source_ref(manual["source_id"],
                                    f"page {page} line {line_no}", raw.strip())],
                values=detail))
            missing_items.append(missing(
                f"manual_check.{row_kind}",
                f"手册行 {line_no} 与规则表不一致，需要人工复核来源版本"))
    return facts, conflicts, missing_items, manual_evidence


_ROW_TO_FACT_KEYS = {
    "cost": ("stats.cost.gold",),
    "damage": ("stats.damage_min", "stats.damage_max"),
    "shots": ("stats.shots",),
    "attack": ("stats.attack",),
    "defense": ("stats.defense",),
    "health": ("stats.hit_points",),
    "speed": ("stats.speed",),
}


def _expected_pair_values(row_kind: str, base: dict, upgraded: dict) -> tuple[Any, Any]:
    """Expected (base column, upgrade column) values for one manual stat row."""
    if row_kind == "cost":
        return base["gold"], upgraded["gold"]
    if row_kind == "damage":
        # the manual prints one value when min == max ("50"), otherwise "1 - 3"
        def column_text(low: int, high: int) -> str:
            return str(low) if low == high else f"{low}-{high}"
        return (column_text(base["damage_min"], base["damage_max"]),
                column_text(upgraded["damage_min"], upgraded["damage_max"]))
    if row_kind == "shots":
        return base["shots"], upgraded["shots"]
    field = "hit_points" if row_kind == "health" else row_kind
    return base[field], upgraded[field]


def _expand_line_blocks(blocks: list) -> list[int]:
    """Expand manual line blocks: ``[start, end]`` ranges or flat line numbers."""
    expanded: list[int] = []
    for block in blocks:
        if isinstance(block, list) and len(block) == 2 and block[1] >= block[0]:
            expanded.extend(range(block[0], block[1] + 1))
        elif isinstance(block, list):
            expanded.extend(block)
        else:
            expanded.append(block)
    return expanded


def build_creature_card(ctx: BuildContext, plan: dict, entity_spec: dict) -> dict:
    json_source = plan["json"]["source_id"]
    document = ctx.json_document(json_source)
    node = document.get(plan["json"]["key"])
    if not isinstance(node, dict):
        raise MissingSourceError([{"source_id": json_source, "path": plan["json"]["key"],
                                   "reason": "creature JSON entry is missing"}])
    legacy = ctx.crtraits.get(plan["legacy_name"].lower())
    if legacy is None:
        raise MissingSourceError([{
            "source_id": "source.game_data.crtraits", "path": plan["legacy_name"],
            "reason": f"legacy CRTRAITS row {plan['legacy_name']!r} is missing"}])

    card_id = plan["id"]
    upgrade_of = ctx.upgrade_of.get(card_id)
    upgrades_to = ctx.upgrades_to.get(card_id, [])
    base_or_upgrade = "upgrade" if upgrade_of else "base"

    zh_key = f"creatures.core.{plan['json']['key']}"
    zh_singular = ctx.zh(f"{zh_key}.name.singular")
    zh_plural = ctx.zh(f"{zh_key}.name.plural")
    names: dict = {"en": {"singular": legacy["name"], "plural": legacy["plural"]}}
    if zh_singular:
        names["zh"] = {"singular": zh_singular, "plural": zh_plural or zh_singular}

    # locate the base/upgrade partner that shares the printed manual stat table
    partner_plan = None
    if upgrade_of:
        partner_id = upgrade_of
    elif upgrades_to:
        partner_id = upgrades_to[0]
    else:
        partner_id = None
    if partner_id is not None:
        partner_plan = next((item for item in entity_spec["creatures"]
                             if item["id"] == partner_id), None)
    pair: dict | None = None
    if partner_plan is not None:
        partner_legacy = ctx.crtraits.get(partner_plan["legacy_name"].lower())
        if partner_legacy is not None:
            if upgrade_of:
                pair = {"base": partner_legacy, "upgrade": legacy}
            else:
                pair = {"base": legacy, "upgrade": partner_legacy}

    manual_spec = plan["manual"]
    manual_view = ctx.manual(manual_spec["source_id"])
    # validate the name/description locators before using them
    if manual_spec.get("rows"):
        manual_view.require_line(manual_spec["name_line"])
        for row_kind, line_no in manual_spec["rows"].items():
            manual_view.require_line(line_no, _MANUAL_ROW_LABELS[row_kind])
    description_lines = manual_spec.get("description_lines") or []
    lore_quote = None
    lore_line = None
    if description_lines:
        flattened = _expand_line_blocks(description_lines)
        for line_no in flattened:
            manual_view.require_line(line_no)
        lore_quote, lore_line = manual_view.block_quote(flattened)

    facts, conflicts, missing_items, manual_evidence = _creature_stat_facts(
        ctx, plan, legacy, node, manual_spec, pair)

    json_source_ref = source_ref(json_source, f"#/{plan['json']['key']}")
    manual_refs: list[dict] = []
    if manual_spec.get("name_line"):
        manual_name_ref = manual_view.ref(manual_spec["page"], manual_spec["name_line"],
                                          manual_view.line(
                                              manual_spec["name_line"]).strip())
        manual_refs.append(manual_name_ref)
    if lore_quote:
        manual_refs.append(manual_view.ref(manual_spec["page"], lore_line, lore_quote))
    translation_ref = source_ref("source.localization.chinese", f"#/{zh_key}.name.singular")
    legacy_ref = source_ref("source.game_data.crtraits", f"line {legacy['line']}")

    abilities = node.get("abilities")
    if isinstance(abilities, dict):
        for ability_key, ability_value in abilities.items():
            safe_key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", ability_key).lower()
            safe_key = re.sub(r"[^a-z0-9_\-]+", "_", safe_key).strip("_")
            facts.append(fact(f"ability.{safe_key}", ability_value,
                              sources=[source_ref(json_source,
                                                  f"#/{plan['json']['key']}/abilities/"
                                                  f"{ability_key}")],
                              confidence="engine_config"))
    if node.get("doubleWide") is True:
        facts.append(fact("traits.double_wide", True,
                          sources=[source_ref(json_source,
                                              f"#/{plan['json']['key']}/doubleWide")],
                          confidence="engine_config"))
    if node.get("hasDoubleWeek") is True:
        facts.append(fact("traits.double_week", True,
                          sources=[source_ref(json_source,
                                              f"#/{plan['json']['key']}/hasDoubleWeek")],
                          confidence="engine_config"))
    if lore_quote:
        facts.append(fact("lore.manual_description", lore_quote,
                          sources=[manual_view.ref(manual_spec["page"], lore_line,
                                                   lore_quote)],
                          confidence="manual_only",
                          note="手册描述文字（抽取原样保留），不是数值事实"))

    missing_items.append(missing("effective_loaded_values", _EFFECTIVE_MISSING_REASON,
                                 severity="warning"))
    missing_items.append(missing("bridge_stats_export", _CREATURE_BRIDGE_MISSING_REASON))
    if zh_singular is None:
        missing_items.append(missing("names.zh", "本机中文本地化未提供该兵种名称",
                                     severity="warning"))
    if not manual_spec.get("rows"):
        missing_items.append(missing(
            "manual_numeric_cross_check",
            "该兵种在本批引用的手册页是描述文字；AB/SoD 手册抽取列错位，未做完整数值交叉核对"))
        manual_evidence = {"source_id": manual_spec["source_id"],
                           "locator": f"page {manual_spec['page']} line {lore_line}",
                           "page": manual_spec["page"], "checks": []}

    matched = sum(1 for item in manual_evidence.get("checks", [])
                  if item["status"] == "match")
    partial = any(item["status"] in ("mismatch", "unparsed")
                  for item in manual_evidence.get("checks", []))
    status = "partial" if (partial or not zh_singular or not manual_spec.get("rows")) \
        else "complete"
    verified_dimensions = ["names", "stats", "upgrade_relations", "abilities",
                           "source_traceability"]
    if matched:
        verified_dimensions.append(f"manual_numeric_cross_check({matched} rows)")
    card = {
        "schema_version": SCHEMA_VERSION,
        "kind": "creature",
        "id": card_id,
        "ruleset_id": ctx.ruleset_id,
        "names": names,
        "aliases": _creature_aliases(ctx, plan, legacy, node, zh_singular, zh_plural),
        "creature": {
            "faction": node.get("faction", "neutral"),
            "level": int(node.get("level", 1)),
            "engine_index": int(node.get("index", -1)),
            "base_or_upgrade": base_or_upgrade,
            "upgrade_of": upgrade_of,
            "upgrades_to": upgrades_to,
            "sources": [json_source_ref, legacy_ref],
        },
        "sources": [legacy_ref, json_source_ref, *manual_refs, translation_ref],
        "facts": facts,
        "missing": missing_items,
        "conflicts": conflicts,
        "coverage": coverage(status, verified_dimensions=verified_dimensions,
                             unverified_dimensions=["engine_loaded_effective_values"]),
        "manual_evidence": manual_evidence,
    }
    return card


# ======================================================================================
# spell cards
# ======================================================================================
@dataclass
class SpellMerge:
    levels: dict[str, dict]
    level_refs: dict[str, list[dict]]


def _merge_spell_levels(json_source: str, json_key: str, node: dict) -> SpellMerge:
    """Replay CSpellHandler::beforeValidate: levels.base inherited into all four levels."""
    levels_node = node.get("levels")
    if not isinstance(levels_node, dict):
        raise BuildError(f"spell {json_key}: JSON has no levels object")
    base = levels_node.get("base", {})
    if not isinstance(base, dict):
        raise BuildError(f"spell {json_key}: levels.base is not an object")
    merged_levels: dict[str, dict] = {}
    level_refs: dict[str, list[dict]] = {}
    for proficiency in PROFICIENCIES:
        override = levels_node.get(proficiency, {})
        if not isinstance(override, dict):
            raise BuildError(f"spell {json_key}: levels.{proficiency} is not an object")
        merged_levels[proficiency] = deep_merge(base, override)
        refs = [source_ref(json_source, f"#/{json_key}/levels/base")]
        if proficiency in levels_node:
            refs.append(source_ref(json_source, f"#/{json_key}/levels/{proficiency}"))
        level_refs[proficiency] = refs
    return SpellMerge(levels=merged_levels, level_refs=level_refs)


def _spell_availability(ctx: BuildContext, plan: dict, node: dict,
                        sptraits_line: int) -> dict:
    bridge = "source.bridge.agent"
    json_source = plan["json"]["source_id"]
    json_pointer = f"#/{plan['json']['key']}"
    rule_refs = [source_ref("source.game_data.sptraits", f"line {sptraits_line}"),
                 source_ref(json_source, json_pointer)]
    two_stage = plan["json"]["key"] in ("teleport", "sacrifice")
    if two_stage:
        bridge_claim = schema.availability_claim(
            False, verification="code_path_only",
            sources=[source_ref(bridge, "line 1950"),
                     source_ref(bridge, "line 1952"),
                     source_ref(bridge, "line 2565"),
                     source_ref(bridge, "line 2567")],
            reason="两阶段战斗法术：桥在提交任何动作前以 ERR spell_target_unsupported "
                   "<name> two_stage 明确拒绝（不扣蓝、不发包）")
    else:
        bridge_claim = schema.availability_claim(
            True, verification="code_path_only",
            sources=[source_ref(bridge, "line 2565"),
                     source_ref(bridge, "line 2570"),
                     source_ref(bridge, "line 2575")],
            reason="单阶段战斗法术：通用 battle_cast_spell 路径（两阶段检查之后）"
                   "接受该法术；本批未在对局中逐法术实测")
    return {
        "rule_exists": schema.availability_claim(
            True, verification="rule_table", sources=rule_refs,
            reason="SPTRAITS.TXT + VCMI core spell JSON 均声明该法术"),
        "bridge_executable": bridge_claim,
        "castable_now": schema.availability_claim(
            "unverified", verification="not_verified",
            reason="需要实时英雄魔法书/法力/战场状态；静态卡不声称本局当前可施放，"
                   "B1.2/live 由 battle_actions 的 can_cast/mana 判定"),
    }


def build_spell_card(ctx: BuildContext, plan: dict) -> dict:
    json_source = plan["json"]["source_id"]
    document = ctx.json_document(json_source)
    node = document.get(plan["json"]["key"])
    if not isinstance(node, dict):
        raise MissingSourceError([{"source_id": json_source, "path": plan["json"]["key"],
                                   "reason": "spell JSON entry is missing"}])
    legacy = ctx.sptraits.get(plan["legacy_name"].lower())
    if legacy is None:
        raise MissingSourceError([{
            "source_id": "source.game_data.sptraits", "path": plan["legacy_name"],
            "reason": f"legacy SPTRAITS row {plan['legacy_name']!r} is missing"}])

    json_key = plan["json"]["key"]
    sptraits_line = legacy["line"]
    sptraits_ref = source_ref("source.game_data.sptraits", f"line {sptraits_line}")
    json_ref = source_ref(json_source, f"#/{json_key}")
    manual_spec = plan["manual"]
    manual_view = ctx.manual(manual_spec["source_id"])
    page = manual_spec["page"]
    manual_view.require_line(manual_spec["name_line"])

    zh_name = ctx.zh(f"spell.core.{json_key}.name")
    names: dict = {"en": legacy["name"]}
    if zh_name:
        names["zh"] = zh_name
    aliases: list[dict] = [
        {"text": json_key, "lang": "en", "kind": "engine_identifier",
         "source_id": json_source, "locator": f"#/{json_key}", "verified": True},
    ]
    if legacy["abbreviated_name"] and legacy["abbreviated_name"] != legacy["name"]:
        aliases.append({"text": legacy["abbreviated_name"], "lang": "en",
                        "kind": "abbreviated_name", "source_id": "source.game_data.sptraits",
                        "locator": f"line {sptraits_line} column Abbreviated Name",
                        "verified": True})
    if zh_name:
        aliases.append({"text": zh_name, "lang": "zh", "kind": "localized_name",
                        "source_id": "source.localization.chinese",
                        "locator": f"#/spell.core.{json_key}.name", "verified": True})

    merged = _merge_spell_levels(json_source, json_key, node)
    facts: list[dict] = []
    conflicts: list[dict] = []
    missing_items: list[dict] = []

    # ---- costs / power / right-click text from SPTRAITS ---------------------------------
    for proficiency in PROFICIENCIES:
        cost = legacy["cost"][proficiency]
        cost_ref = source_ref("source.game_data.sptraits",
                              f"line {sptraits_line} column Cost ({proficiency})")
        if cost is None:
            missing_items.append(missing(
                f"spell.cost.{proficiency}",
                f"SPTRAITS.TXT 该法术 {proficiency} 熟练度耗蓝列为空，未填写推测值"))
        elif proficiency == "none":
            manual_cost_line = manual_view.line(manual_spec["cost_line"])
            normalized = _normalize(manual_cost_line)
            match = re.search(r"COST(\d+)", normalized)
            if match:
                manual_cost = int(match.group(1))
                if manual_cost == cost:
                    facts.append(fact("spell.cost.none", cost,
                                      sources=[cost_ref,
                                               manual_view.ref(page,
                                                               manual_spec["cost_line"],
                                                               manual_cost_line.strip())],
                                      confidence="cross_checked", unit="mana"))
                else:
                    facts.append(fact("spell.cost.none", cost, sources=[cost_ref],
                                      confidence="rule_table", unit="mana"))
                    conflicts.append(conflict(
                        "spell.cost.none",
                        "手册只列基础耗蓝，其数值与 SPTRAITS 无熟练度耗蓝不一致",
                        sources=[cost_ref,
                                 manual_view.ref(page, manual_spec["cost_line"],
                                                 manual_cost_line.strip())],
                        values={"sptraits_none": cost, "manual": manual_cost}))
            else:
                facts.append(fact("spell.cost.none", cost, sources=[cost_ref],
                                  confidence="rule_table", unit="mana",
                                  note="手册耗蓝行未能解析，未做交叉核对"))
        else:
            facts.append(fact(f"spell.cost.{proficiency}", cost, sources=[cost_ref],
                              confidence="rule_table", unit="mana",
                              note="手册只列基础 Cost；各熟练度耗蓝来自 SPTRAITS.TXT"))
        level_power = legacy["level_power"][proficiency]
        if level_power is None:
            missing_items.append(missing(
                f"spell.level_power.{proficiency}",
                f"SPTRAITS.TXT 该法术 {proficiency} 熟练度 Power 列为空，未填写推测值"))
        else:
            facts.append(fact(f"spell.level_power.{proficiency}", level_power,
                              sources=[source_ref(
                                  "source.game_data.sptraits",
                                  f"line {sptraits_line} column Power ({proficiency})")],
                              confidence="rule_table",
                              note="SPTRAITS 熟练度 Power 列（原版规则表原始字段）"))
        description = legacy["description"][proficiency]
        facts.append(fact(f"spell.effect.{proficiency}", description,
                          sources=[source_ref(
                              "source.game_data.sptraits",
                              f"line {sptraits_line} column Right Click Text ({proficiency})")],
                          confidence="rule_table",
                          note="游戏内右键描述原文（SPTRAITS Right Click Text）"))
    if legacy["power"] is None:
        missing_items.append(missing("spell.base_power",
                                     "SPTRAITS.TXT 基础 Power 列为空，未填写推测值"))
    else:
        facts.append(fact("spell.base_power", legacy["power"],
                          sources=[source_ref("source.game_data.sptraits",
                                              f"line {sptraits_line} column Power")],
                          confidence="rule_table",
                          note="SPTRAITS 基础 Power 列；与熟练度 Power 相乘/相加的公式见 "
                               "spell handler"))

    # ---- manual effect formulas --------------------------------------------------------
    manual_effect_refs: list[dict] = []
    for proficiency in ("basic", "advanced", "expert"):
        line_numbers = manual_spec["effect_lines"][proficiency]
        ref, quote = _manual_effect_lines(manual_view, page, line_numbers)
        manual_effect_refs.append(ref)
        formula = plan["formulas"].get(proficiency)
        if formula is None:
            continue
        consistent = _formula_consistent(formula, quote)
        if not consistent:
            conflicts.append(conflict(
                f"spell.formula.{proficiency}",
                "entity_spec 中的规范公式与手册抽取文本不一致（数字对照）。公式可能过期或手册行错位",
                sources=[ref],
                values={"formula": formula, "manual_quote": quote}))
        facts.append(fact(f"spell.formula.{proficiency}", formula, sources=[ref],
                          confidence="manual_only",
                          note="手册公式的规范化转写；原文见 source quote"))
    missing_items.append(missing(
        "spell.formula.none",
        "手册只列 Basic/Advanced/Expert 三档效果，未单列无熟练度效果；SPTRAITS 提供 none 档"
        "右键描述与 Power 数值，但没有独立公式文字"))
    duration_line = manual_spec["duration_line"]
    duration_raw = manual_view.line(duration_line)
    facts.append(fact("spell.duration", _duration_value(duration_raw),
                      sources=[manual_view.ref(page, duration_line, duration_raw.strip())],
                      confidence="manual_only"))
    manual_cost_line = manual_view.line(manual_spec["cost_line"])
    manual_type_line = manual_view.line(manual_spec["type_line"])
    normalized_type = _normalize(manual_type_line)
    manual_type = ("combat" if "COMBAT" in normalized_type else
                   "adventure" if "ADVENTURE" in normalized_type else None)
    if manual_type is not None and manual_type != legacy["type"]:
        conflicts.append(conflict(
            "spell.type", "手册类型与 SPTRAITS 段落类型不一致",
            sources=[manual_view.ref(page, manual_spec["type_line"],
                                     manual_type_line.strip()), sptraits_ref],
            values={"manual": manual_type, "sptraits": legacy["type"]}))

    # ---- merged core-config mechanics ---------------------------------------------------
    for proficiency in PROFICIENCIES:
        merged_level = merged.levels[proficiency]
        refs = merged.level_refs[proficiency]
        range_value = merged_level.get("range")
        if range_value is not None:
            facts.append(fact(f"spell.range.{proficiency}", range_value, sources=refs,
                              confidence="engine_config",
                              note="range 0=单目标，X=全体；levels.base 已继承到该熟练度"))
        mechanics: dict = {}
        for field_name in ("effects", "cumulativeEffects", "battleEffects",
                           "targetModifier", "adventureEffect"):
            value = merged_level.get(field_name)
            if value:
                mechanics[field_name] = value
        if mechanics:
            facts.append(fact(f"spell.mechanics.{proficiency}", mechanics, sources=refs,
                              confidence="engine_config",
                              note="VCMI core JSON levels.base 与该熟练度合并后的效果结构；"
                                   "具体数值由引擎效果实现解释"))
    flags = node.get("flags")
    if isinstance(flags, dict) and flags:
        facts.append(fact("spell.flags", flags,
                          sources=[source_ref(json_source, f"#/{json_key}/flags")],
                          confidence="engine_config"))
    counters = node.get("counters")
    if isinstance(counters, dict) and counters:
        facts.append(fact("spell.counters", counters,
                          sources=[source_ref(json_source, f"#/{json_key}/counters")],
                          confidence="engine_config"))
    target_condition = node.get("targetCondition")
    if isinstance(target_condition, dict) and target_condition:
        facts.append(fact("spell.target_condition", target_condition,
                          sources=[source_ref(json_source,
                                              f"#/{json_key}/targetCondition")],
                          confidence="engine_config"))
    if json_key == "magicArrow":
        facts.append(fact(
            "spell.damage_formula_engine",
            "damage = caster_effect_power * base_power + level_power(proficiency)",
            sources=[source_ref("source.engine.spell_handler", "line 423"),
                     source_ref("source.game_data.sptraits",
                                f"line {sptraits_line} column Power"),
                     source_ref("source.game_data.sptraits",
                                f"line {sptraits_line} column Power (Basic)")],
            confidence="engine_source",
            note="CSpell::calculateDamage -> calculateRawEffectValue；base_power=10、"
                 "level_power=10/10/20/30 与手册 ((Power x 10)+10/20/30) 一致"))

    # ---- localized right-click descriptions -------------------------------------------
    for proficiency in PROFICIENCIES:
        zh_description = ctx.zh(f"spell.core.{json_key}.description.{proficiency}")
        if zh_description:
            facts.append(fact(f"spell.description_zh.{proficiency}", zh_description,
                              sources=[source_ref(
                                  "source.localization.chinese",
                                  f"#/spell.core.{json_key}.description.{proficiency}")],
                              confidence="rule_table",
                              note="本机中文本地化的游戏内描述原文"))

    missing_items.append(missing("effective_loaded_values", _EFFECTIVE_MISSING_REASON,
                                 severity="warning"))
    missing_items.append(missing(
        "castable_now",
        "静态卡不声称本局当前可施放：英雄是否已学、法力是否足够、战场目标是否合法都需要"
        "实时观察（battle_actions / hero 法术书）"))
    missing_items.append(missing(
        "bridge_live_castability",
        "bridge_executable 只证明代码路径存在；本批未在真实战斗中逐法术实测，"
        "不能把 code path 当作已验证施放", severity="warning"))
    if zh_name is None:
        missing_items.append(missing("names.zh", "本机中文本地化未提供该法术名称",
                                     severity="warning"))

    status = "complete" if zh_name is not None else "partial"
    card = {
        "schema_version": SCHEMA_VERSION,
        "kind": "spell",
        "id": plan["id"],
        "ruleset_id": ctx.ruleset_id,
        "names": names,
        "aliases": aliases,
        "spell": {
            "level": legacy["level"],
            "schools": legacy["schools"],
            "type": legacy["type"],
            "target_type": node.get("targetType", "NO_TARGET"),
            "attributes": [item for item in legacy["attributes"].split("|") if item],
            "sources": [sptraits_ref, json_ref],
            "availability": _spell_availability(ctx, plan, node, sptraits_line),
        },
        "sources": [sptraits_ref, json_ref,
                    manual_view.ref(page, manual_spec["name_line"])]
                   + manual_effect_refs,
        "facts": facts,
        "missing": missing_items,
        "conflicts": conflicts,
        "coverage": coverage(
            status,
            verified_dimensions=["names", "level", "school", "cost", "power",
                                 "right_click_text", "target", "effects_structure",
                                 "source_traceability"],
            unverified_dimensions=["engine_loaded_effective_values", "castable_now",
                                   "bridge_live_castability"]),
    }
    return card


# ======================================================================================
# build orchestration
# ======================================================================================
@dataclass
class BuildResult:
    ruleset_id: str
    out_dir: Path
    manifest_path: Path
    cards: list[dict]
    ruleset: dict
    coverage: dict
    manifest: dict
    traceability_refs: int


def _ruleset_evidence_refs(ruleset_spec: dict) -> list[dict]:
    refs: list[dict] = []
    refs.extend(ruleset_spec.get("engine", {}).get("version_evidence", []))
    for item in ruleset_spec.get("content", []):
        refs.extend(item.get("evidence", []))
    refs.extend(ruleset_spec.get("active_binding", {}).get("evidence", []))
    refs.extend(ruleset_spec.get("effective_data", {}).get("merge_evidence", []))
    return refs


def _coverage_report(ruleset_id: str, cards: list[dict], manifest: dict,
                     manifest_sha: str, created_at: str) -> dict:
    by_kind: dict[str, int] = {}
    by_confidence: dict[str, int] = {}
    facts_total = 0
    card_rows = []
    missing_total = 0
    conflicts_total = 0
    for card in cards:
        by_kind[card["kind"]] = by_kind.get(card["kind"], 0) + 1
        facts_total += len(card["facts"])
        missing_total += len(card["missing"])
        conflicts_total += len(card["conflicts"])
        for item in card["facts"]:
            by_confidence[item["confidence"]] = by_confidence.get(item["confidence"], 0) + 1
        card_rows.append({
            "id": card["id"],
            "kind": card["kind"],
            "coverage_status": card["coverage"]["status"],
            "facts": len(card["facts"]),
            "missing": [item["key"] for item in card["missing"]],
            "conflicts": [item["key"] for item in card["conflicts"]],
            "manual_checks": [item["status"] for item in
                              card.get("manual_evidence", {}).get("checks", [])],
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "ruleset_id": ruleset_id,
        "created_at_utc": created_at,
        "source_manifest_sha256": manifest_sha,
        "totals": {
            "cards": len(cards),
            "cards_by_kind": by_kind,
            "facts": facts_total,
            "facts_by_confidence": by_confidence,
            "missing": missing_total,
            "conflicts": conflicts_total,
            "sources_present": sum(1 for item in manifest["sources"]
                                   if item["status"] == "present"),
            "sources_missing": len(missing_sources(manifest)),
        },
        "missing_sources": [{"source_id": item["source_id"], "kind": item["kind"],
                             "reason": item.get("missing_reason")}
                            for item in missing_sources(manifest)],
        "cards": card_rows,
    }


def build(root: str | Path, specs_dir: str | Path | None = None,
          out_dir: str | Path | None = None, *, accept_source_change: bool = False,
          now: str | None = None) -> BuildResult:
    """Build the first knowledge batch.  Fails loudly when required local sources are absent."""
    root_path = Path(root)
    specs_path = root_path / (specs_dir or DEFAULT_SPECS_DIR)
    out_root = root_path / (out_dir or DEFAULT_OUT_DIR)

    source_specs, ruleset_spec, entity_spec = load_build_specs(specs_path)
    manifest = build_manifest(source_specs, root_path, now=now)
    required_ids = [spec.source_id for spec in source_specs if spec.required]
    require_present(manifest, required_ids)
    # also fail on missing evidence sources referenced by the ruleset spec
    declared = source_by_id(manifest)
    used_ids = {ref["source_id"] for ref in _ruleset_evidence_refs(ruleset_spec)}
    used_ids |= {item["json"]["source_id"] for item in
                 entity_spec["creatures"] + entity_spec["spells"]}
    used_ids |= {item["manual"]["source_id"] for item in
                 entity_spec["creatures"] + entity_spec["spells"]}
    undeclared = sorted(used_ids - set(declared))
    if undeclared:
        raise SchemaError(f"specs reference undeclared sources: {undeclared}")
    require_present(manifest, [source_id for source_id in used_ids
                               if declared[source_id].get("required", True)])

    manifest_path = root_path / MANIFEST_REL
    if manifest_path.exists():
        existing = load_manifest(manifest_path)
        if not accept_source_change:
            assert_sources_compatible(existing, manifest)
    _write_json(manifest_path, manifest)
    manifest_sha = manifest_sha256(manifest)

    index = SourceIndex.from_parts(source_specs, manifest, root_path)
    ctx = _build_context(root_path, index, ruleset_spec, entity_spec)

    cards: list[dict] = []
    for plan in entity_spec["creatures"]:
        cards.append(build_creature_card(ctx, plan, entity_spec))
    for plan in entity_spec["spells"]:
        cards.append(build_spell_card(ctx, plan))
    schema.validate_cards(cards, set(declared))
    # every ruleset evidence ref and every card ref must actually resolve
    for ref in _ruleset_evidence_refs(ruleset_spec):
        assert_ref_traceable(manifest, ref, root_path)
    traceability_count = assert_cards_traceable(manifest, cards, root_path)

    ruleset_out = out_root / ruleset_spec["ruleset_id"]
    for card in cards:
        key = card["id"].split(":", 1)[1]
        subdir = "creatures" if card["kind"] == "creature" else "spells"
        _write_json(ruleset_out / subdir / f"{key}.json", card)

    created_at = now or _utc_now()
    ruleset = dict(ruleset_spec)
    ruleset["schema_version"] = SCHEMA_VERSION
    ruleset["created_at_utc"] = created_at
    ruleset["source_manifest"] = {"path": MANIFEST_REL, "sha256": manifest_sha}
    coverage_report = _coverage_report(ruleset_spec["ruleset_id"], cards, manifest,
                                       manifest_sha, created_at)
    ruleset["coverage"] = {
        "cards_total": coverage_report["totals"]["cards"],
        "cards_by_kind": coverage_report["totals"]["cards_by_kind"],
        "facts_total": coverage_report["totals"]["facts"],
        "facts_by_confidence": coverage_report["totals"]["facts_by_confidence"],
        "missing_total": coverage_report["totals"]["missing"],
        "conflicts_total": coverage_report["totals"]["conflicts"],
        "coverage_report": _rel(root_path, ruleset_out / "coverage.json"),
        "missing_sources": coverage_report["missing_sources"],
    }
    schema.validate_ruleset(ruleset, set(declared))
    _write_json(ruleset_out / "ruleset.json", ruleset)
    _write_json(ruleset_out / "coverage.json", coverage_report)

    return BuildResult(ruleset_id=ruleset_spec["ruleset_id"], out_dir=ruleset_out,
                       manifest_path=manifest_path, cards=cards, ruleset=ruleset,
                       coverage=coverage_report, manifest=manifest,
                       traceability_refs=traceability_count)


def _utc_now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_root() -> Path:
    # src/auto_heros3/knowledge/build.py -> repo root
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the B1.1 knowledge cards from local public sources")
    parser.add_argument("--root", default=None, help="repository root (default: auto)")
    parser.add_argument("--specs-dir", default=DEFAULT_SPECS_DIR,
                        help="input specs/sources directory (default: doc/knowledge/sources)")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                        help="output rulesets directory (default: doc/knowledge/rulesets)")
    parser.add_argument("--accept-source-change", action="store_true",
                        help="deliberately replace an existing source manifest snapshot")
    parser.add_argument("--json", action="store_true", help="print the build summary as JSON")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else _default_root()
    try:
        result = build(root, args.specs_dir, args.out_dir,
                       accept_source_change=args.accept_source_change)
    except (MissingSourceError, SourceVersionConflictError, SchemaError) as exc:
        print(f"ERROR: {exc}")
        return 1
    summary = {
        "ruleset_id": result.ruleset_id,
        "out_dir": _rel(root, result.out_dir),
        "source_manifest": _rel(root, result.manifest_path),
        "cards": len(result.cards),
        "cards_by_kind": result.coverage["totals"]["cards_by_kind"],
        "facts": result.coverage["totals"]["facts"],
        "facts_by_confidence": result.coverage["totals"]["facts_by_confidence"],
        "missing": result.coverage["totals"]["missing"],
        "conflicts": result.coverage["totals"]["conflicts"],
        "missing_sources": result.coverage["missing_sources"],
        "traceability_refs": result.traceability_refs,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    else:
        print(f"ruleset: {result.ruleset_id}")
        print(f"output : {summary['out_dir']}")
        print(f"cards  : {len(result.cards)} ({summary['cards_by_kind']})")
        print(f"facts  : {summary['facts']} {summary['facts_by_confidence']}")
        print(f"issues : missing={summary['missing']} conflicts={summary['conflicts']}")
        print(f"traceability refs verified: {result.traceability_refs}")
        print(f"missing sources: {[item['source_id'] for item in summary['missing_sources']]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
