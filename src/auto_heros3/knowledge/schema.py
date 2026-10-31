# -*- coding: utf-8 -*-
"""B1.1 data contract: source refs, facts, cards, ruleset/source manifests.

Why a separate schema module: the same contract is exercised by three producers/consumers
that must not drift apart:

* ``build.py`` (offline builder, writes the first batch),
* ``tests/offline/test_knowledge_sources.py`` (synthetic fixtures: the only tracked copy),
* B1.2's future ``knowledge_search``/``knowledge_get`` (reads the generated cards).

Design rules enforced here
--------------------------
* Every fact carries at least one :class:`SourceRef`.  A card value without a source is
  rejected, so a hand-edited card cannot silently lose traceability.
* A source id must be declared in the source manifest (``known_source_ids`` argument).
* ``missing`` and ``conflicts`` are explicit lists; an absent value is never an empty
  success.  ``coverage.status == "missing_source"`` is only valid when the card says why.
* Cards are plain JSON-compatible dicts (they are data, not runtime objects), so the same
  validator runs on generated cards and on fixtures.
* The three availability dimensions required by the plan are separate claims with their
  own verification level: rule exists / bridge executable / currently castable.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "b1.1"
CARD_KINDS = ("creature", "spell")
PROFICIENCIES = ("none", "basic", "advanced", "expert")
SPELL_TYPES = ("combat", "adventure", "ability")
BASE_OR_UPGRADE = ("base", "upgrade")

# Fact provenance labels.  They describe HOW the fact was established, not how much we
# like the value:
#   cross_checked       two independent public sources agree (e.g. legacy TXT + manual page)
#   engine_config       read from the loaded VCMI core config JSON (abilities, effects)
#   engine_source       read from VCMI/AgentBridge source code (formulas, capability refusals)
#   rule_table          original-game legacy table extracted from the local LOD archives
#   manual_only         only the manual states it; rule tables do not expose the field
#   derived_note        computed from other sourced values; the derivation is in ``note``
FACT_CONFIDENCE = (
    "cross_checked",
    "engine_config",
    "engine_source",
    "rule_table",
    "manual_only",
    "derived_note",
)

# Card coverage status.  ``complete`` means every dimension this batch promises is sourced;
# it never means "verified live in the running engine".
COVERAGE_STATUSES = ("complete", "partial", "manual_only", "missing_source")

# How a claim was verified.  Deliberately never upgrades a code path to a live test:
#   engine_exported  read back from the running engine (NOT available in this batch)
#   rule_table       stated by a public rule table (game data / core config)
#   static_config    stated by a local configuration file (enabled mods, language, ...)
#   code_path_only   the engine/bridge source shows the code path exists
#   manual_only      a manual states it
#   not_verified     no verification in this batch
VERIFICATION_LEVELS = ("engine_exported", "rule_table", "static_config", "code_path_only",
                       "manual_only", "not_verified")

_ID_RE = re.compile(r"^[a-z][a-z0-9_.\-]*:[A-Za-z0-9_.\-]+$")
_SOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9_.\-]*$")
_FACT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_\-]+)+$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class SchemaError(ValueError):
    """A card / manifest / spec violates the B1.1 data contract."""


# --------------------------------------------------------------------------------------
# source references + facts
# --------------------------------------------------------------------------------------
def source_ref(source_id: str, locator: str, quote: str | None = None,
               member: str | None = None) -> dict:
    """One traceable pointer into a declared source.

    ``locator`` formats used by this batch (see ``sources.resolve_text_locator``):
      * ``line 37`` / ``line 37 column Attack``           legacy text tables
      * ``page 84 line 3888``                            extracted manuals
      * ``#/pikeman/abilities/cavalryChargeImmunity``    JSON pointer into a config file
      * ``zip#/creatures.core.pikeman.name.singular``    JSON pointer inside a mod archive
    """
    ref: dict = {"source_id": source_id, "locator": locator}
    if quote is not None:
        ref["quote"] = quote
    if member is not None:
        ref["member"] = member
    return ref


def fact(key: str, value: Any, *, sources: Iterable[dict],
         confidence: str = "engine_config", unit: str | None = None,
         note: str | None = None) -> dict:
    """One sourced claim.  ``key`` namespaces the claim (``stats.attack``, ``spell.cost.basic``)."""
    item = {
        "key": key,
        "value": value,
        "confidence": confidence,
        "sources": list(sources),
    }
    if unit is not None:
        item["unit"] = unit
    if note is not None:
        item["note"] = note
    return item


def missing(key: str, reason: str, *, severity: str = "info") -> dict:
    """An explicitly unknown field.  Never replaced by a guessed default."""
    return {"key": key, "reason": reason, "severity": severity}


def conflict(key: str, reason: str, *, sources: Iterable[dict] = (),
             values: dict | None = None) -> dict:
    """Two sources disagree.  The card keeps both values in ``values`` and does not pick silently."""
    item: dict = {"key": key, "reason": reason, "sources": list(sources)}
    if values is not None:
        item["values"] = values
    return item


def coverage(status: str, *, verified_dimensions: Iterable[str] = (),
             unverified_dimensions: Iterable[str] = ()) -> dict:
    if status not in COVERAGE_STATUSES:
        raise SchemaError(f"invalid coverage status {status!r}; expected {COVERAGE_STATUSES}")
    return {
        "status": status,
        "verified_dimensions": list(verified_dimensions),
        "unverified_dimensions": list(unverified_dimensions),
    }


def availability_claim(value: Any, *, verification: str, sources: Iterable[dict] = (),
                       reason: str | None = None) -> dict:
    if verification not in VERIFICATION_LEVELS:
        raise SchemaError(
            f"invalid verification {verification!r}; expected {VERIFICATION_LEVELS}")
    claim: dict = {
        "value": value,
        "verification": verification,
        "sources": list(sources),
    }
    if reason is not None:
        claim["reason"] = reason
    return claim


# --------------------------------------------------------------------------------------
# validation helpers
# --------------------------------------------------------------------------------------
def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SchemaError(message)


def _require_str(obj: dict, key: str, where: str) -> str:
    value = obj.get(key)
    _require(isinstance(value, str) and value.strip() != "",
             f"{where}: field {key!r} must be a non-empty string")
    return value


def normalize_ws(text: str) -> str:
    """Whitespace-normalized text used for quote traceability checks."""
    return re.sub(r"\s+", " ", text).strip()


def validate_source_ref(ref: dict, known_source_ids: set[str] | None = None,
                        where: str = "source_ref") -> None:
    _require(isinstance(ref, dict), f"{where}: must be an object")
    source_id = _require_str(ref, "source_id", where)
    _require(_SOURCE_ID_RE.match(source_id) is not None,
             f"{where}: source_id {source_id!r} is not a contract id")
    if known_source_ids is not None:
        _require(source_id in known_source_ids,
                 f"{where}: unknown source_id {source_id!r} (not in source manifest)")
    locator = _require_str(ref, "locator", where)
    _require(locator.strip() != "", f"{where}: locator must not be blank")
    if "quote" in ref:
        _require(isinstance(ref["quote"], str), f"{where}: quote must be a string")
        _require(ref["quote"].strip() != "", f"{where}: quote must not be blank when present")


def validate_fact(item: dict, known_source_ids: set[str] | None = None,
                  where: str = "fact") -> None:
    _require(isinstance(item, dict), f"{where}: must be an object")
    key = _require_str(item, "key", where)
    _require(_FACT_KEY_RE.match(key) is not None,
             f"{where}: fact key {key!r} must look like 'group.name'")
    _require("value" in item, f"{where} {key}: 'value' is required (use missing() instead of null)")
    _require(item["value"] is not None, f"{where} {key}: value None is not allowed; use missing()")
    confidence = _require_str(item, "confidence", where)
    _require(confidence in FACT_CONFIDENCE,
             f"{where} {key}: invalid confidence {confidence!r}; expected {FACT_CONFIDENCE}")
    refs = item.get("sources")
    _require(isinstance(refs, list) and refs, f"{where} {key}: at least one source ref required")
    for index, ref in enumerate(refs):
        validate_source_ref(ref, known_source_ids, f"{where} {key} sources[{index}]")
    if "unit" in item:
        _require(isinstance(item["unit"], str), f"{where} {key}: unit must be a string")
    if "note" in item:
        _require(isinstance(item["note"], str), f"{where} {key}: note must be a string")


def _validate_missing(items: Any, where: str) -> None:
    _require(isinstance(items, list), f"{where}: 'missing' must be a list")
    for index, item in enumerate(items):
        sub = f"{where} missing[{index}]"
        _require(isinstance(item, dict), f"{sub}: must be an object")
        _require_str(item, "key", sub)
        _require_str(item, "reason", sub)
        _require(item.get("severity", "info") in ("info", "warning", "blocking"),
                 f"{sub}: severity must be info|warning|blocking")


def _validate_conflicts(items: Any, where: str) -> None:
    _require(isinstance(items, list), f"{where}: 'conflicts' must be a list")
    for index, item in enumerate(items):
        sub = f"{where} conflicts[{index}]"
        _require(isinstance(item, dict), f"{sub}: must be an object")
        _require_str(item, "key", sub)
        _require_str(item, "reason", sub)
        refs = item.get("sources", [])
        _require(isinstance(refs, list), f"{sub}: sources must be a list")
        for ref_index, ref in enumerate(refs):
            validate_source_ref(ref, None, f"{sub} sources[{ref_index}]")


def _validate_name_block(names: Any, where: str) -> None:
    _require(isinstance(names, dict) and names, f"{where}: 'names' must be a non-empty object")
    _require("en" in names, f"{where}: names.en is required")
    for lang, value in names.items():
        _require(isinstance(value, (str, dict)), f"{where}: names.{lang} must be str or object")
        if isinstance(value, dict):
            _require(any(isinstance(v, str) and v.strip() for v in value.values()),
                     f"{where}: names.{lang} object must contain a non-empty name")


def _validate_aliases(aliases: Any, known_source_ids: set[str] | None, where: str) -> None:
    _require(isinstance(aliases, list), f"{where}: 'aliases' must be a list")
    for index, alias in enumerate(aliases):
        sub = f"{where} aliases[{index}]"
        _require(isinstance(alias, dict), f"{sub}: must be an object")
        _require_str(alias, "text", sub)
        _require(alias.get("lang") in ("en", "zh"), f"{sub}: lang must be en|zh")
        _require_str(alias, "kind", sub)
        _require(alias.get("verified") is True,
                 f"{sub}: only verified aliases may be published (verified must be True)")
        validate_source_ref({"source_id": alias.get("source_id", ""),
                             "locator": alias.get("locator", "card alias")},
                            known_source_ids, sub)


def _validate_creature_block(block: Any, known_source_ids: set[str] | None,
                             where: str) -> None:
    _require(isinstance(block, dict), f"{where}: 'creature' block must be an object")
    _require_str(block, "faction", f"{where}.creature")
    _require(isinstance(block.get("level"), int) and block["level"] >= 1,
             f"{where}.creature: level must be a positive int")
    _require(isinstance(block.get("engine_index"), int) and block["engine_index"] >= 0,
             f"{where}.creature: engine_index must be a non-negative int")
    base_or_upgrade = block.get("base_or_upgrade")
    _require(base_or_upgrade in BASE_OR_UPGRADE,
             f"{where}.creature: base_or_upgrade must be base|upgrade")
    upgrade_of = block.get("upgrade_of")
    upgrades_to = block.get("upgrades_to")
    _require(upgrades_to is None or (isinstance(upgrades_to, list)
                                     and all(isinstance(item, str) for item in upgrades_to)),
             f"{where}.creature: upgrades_to must be a list of ids")
    if base_or_upgrade == "base":
        _require(upgrade_of is None,
                 f"{where}.creature: base creature must not declare upgrade_of")
    else:
        _require(isinstance(upgrade_of, str) and ":" in upgrade_of,
                 f"{where}.creature: upgrade creature must declare a namespaced upgrade_of")
    block_refs = block.get("sources")
    _require(isinstance(block_refs, list) and block_refs,
             f"{where}.creature: structural claims need sources")
    for index, ref in enumerate(block_refs):
        validate_source_ref(ref, known_source_ids, f"{where}.creature.sources[{index}]")


def _validate_availability(availability: Any, known_source_ids: set[str] | None,
                           where: str) -> None:
    _require(isinstance(availability, dict), f"{where}: 'availability' must be an object")
    for dimension in ("rule_exists", "bridge_executable", "castable_now"):
        claim = availability.get(dimension)
        _require(isinstance(claim, dict), f"{where}.availability: {dimension} claim required")
        _require("value" in claim, f"{where}.availability.{dimension}: value required")
        _require(claim.get("verification") in VERIFICATION_LEVELS,
                 f"{where}.availability.{dimension}: invalid verification")
        refs = claim.get("sources", [])
        _require(isinstance(refs, list), f"{where}.availability.{dimension}: sources must be list")
        for index, ref in enumerate(refs):
            validate_source_ref(ref, known_source_ids,
                                 f"{where}.availability.{dimension}.sources[{index}]")
        if dimension == "castable_now" and claim.get("value") != "unverified":
            _require(bool(claim.get("reason")),
                     f"{where}.availability.castable_now: non-unverified claims need a reason")


def _validate_spell_block(block: Any, known_source_ids: set[str] | None, where: str) -> None:
    _require(isinstance(block, dict), f"{where}: 'spell' block must be an object")
    _require(isinstance(block.get("level"), int) and block["level"] >= 1,
             f"{where}.spell: level must be a positive int")
    schools = block.get("schools")
    _require(isinstance(schools, list) and schools
             and all(isinstance(item, str) for item in schools),
             f"{where}.spell: schools must be a non-empty list")
    _require(block.get("type") in SPELL_TYPES,
             f"{where}.spell: type must be one of {SPELL_TYPES}")
    _require_str(block, "target_type", f"{where}.spell")
    attributes = block.get("attributes")
    _require(attributes is None or (isinstance(attributes, list)
                                   and all(isinstance(item, str) for item in attributes)),
             f"{where}.spell: attributes must be a list of strings")
    block_refs = block.get("sources")
    _require(isinstance(block_refs, list) and block_refs,
             f"{where}.spell: structural claims need sources")
    for index, ref in enumerate(block_refs):
        validate_source_ref(ref, known_source_ids, f"{where}.spell.sources[{index}]")
    _validate_availability(block.get("availability"), known_source_ids, f"{where}.spell")


# --------------------------------------------------------------------------------------
# cards
# --------------------------------------------------------------------------------------
CARD_REQUIRED_FIELDS = (
    "schema_version", "kind", "id", "ruleset_id", "names", "aliases", "sources",
    "facts", "missing", "conflicts", "coverage",
)


def validate_card(card: dict, known_source_ids: set[str] | None = None) -> None:
    """Validate one card dict (generated card or synthetic fixture)."""
    _require(isinstance(card, dict), "card: must be an object")
    for field in CARD_REQUIRED_FIELDS:
        _require(field in card, f"card: required field {field!r} is missing")
    where = f"card {card.get('id', '<no id>')}"
    _require(card["schema_version"] == SCHEMA_VERSION,
             f"{where}: schema_version must be {SCHEMA_VERSION!r}")
    kind = card["kind"]
    _require(kind in CARD_KINDS, f"{where}: kind must be one of {CARD_KINDS}")
    card_id = _require_str(card, "id", where)
    _require(_ID_RE.match(card_id) is not None,
             f"{where}: id {card_id!r} must be a namespaced stable id (scope:identifier)")
    _require_str(card, "ruleset_id", where)
    _validate_name_block(card["names"], where)
    _validate_aliases(card["aliases"], known_source_ids, where)

    refs = card["sources"]
    _require(isinstance(refs, list) and refs, f"{where}: card-level sources required")
    for index, ref in enumerate(refs):
        validate_source_ref(ref, known_source_ids, f"{where}.sources[{index}]")

    facts = card["facts"]
    _require(isinstance(facts, list) and facts, f"{where}: at least one fact is required")
    keys: set[str] = set()
    for index, item in enumerate(facts):
        validate_fact(item, known_source_ids, f"{where}.facts[{index}]")
        if item["key"] in keys:
            raise SchemaError(f"{where}: duplicate fact key {item['key']!r}")
        keys.add(item["key"])

    _validate_missing(card["missing"], where)
    _validate_conflicts(card["conflicts"], where)

    manual_evidence = card.get("manual_evidence")
    if manual_evidence is not None:
        _require(isinstance(manual_evidence, dict),
                 f"{where}: manual_evidence must be an object when present")
        _require_str(manual_evidence, "source_id", f"{where}.manual_evidence")
        validate_source_ref(
            {"source_id": manual_evidence["source_id"],
             "locator": manual_evidence.get("locator", "line 1")},
            known_source_ids, f"{where}.manual_evidence")
        checks = manual_evidence.get("checks", [])
        _require(isinstance(checks, list), f"{where}.manual_evidence: checks must be a list")
        for index, item in enumerate(checks):
            sub = f"{where}.manual_evidence.checks[{index}]"
            _require(isinstance(item, dict), f"{sub}: must be an object")
            _require_str(item, "row", sub)
            _require(item.get("status") in ("match", "mismatch", "unparsed", "not_applicable"),
                     f"{sub}: status must be match|mismatch|unparsed|not_applicable")
            if "line" in item:
                _require(isinstance(item["line"], int) and item["line"] >= 1,
                         f"{sub}: line must be a positive int")

    cov = card["coverage"]
    _require(isinstance(cov, dict) and cov.get("status") in COVERAGE_STATUSES,
             f"{where}: coverage.status must be one of {COVERAGE_STATUSES}")
    for field in ("verified_dimensions", "unverified_dimensions"):
        value = cov.get(field, [])
        _require(isinstance(value, list) and all(isinstance(item, str) for item in value),
                 f"{where}: coverage.{field} must be a list of strings")

    if kind == "creature":
        _validate_creature_block(card.get("creature"), known_source_ids, where)
    else:
        _validate_spell_block(card.get("spell"), known_source_ids, where)


def validate_cards(cards: Iterable[dict],
                   known_source_ids: set[str] | None = None) -> None:
    """Validate a whole batch: per-card contract + unique ids + ruleset consistency."""
    seen: set[str] = set()
    rulesets: set[str] = set()
    for card in cards:
        validate_card(card, known_source_ids)
        if card["id"] in seen:
            raise SchemaError(f"duplicate card id {card['id']!r}")
        seen.add(card["id"])
        rulesets.add(card["ruleset_id"])
    _require(len(rulesets) <= 1,
             f"one batch must not silently mix rulesets: {sorted(rulesets)}")


# --------------------------------------------------------------------------------------
# ruleset + source manifest contracts
# --------------------------------------------------------------------------------------
def validate_ruleset_spec(spec: dict) -> None:
    _require(isinstance(spec, dict), "ruleset: must be an object")
    _require_str(spec, "ruleset_id", "ruleset")
    _require(re.match(r"^[A-Za-z0-9][A-Za-z0-9_.\-+]*$", spec["ruleset_id"]) is not None,
             "ruleset: ruleset_id must be a filename-safe id")
    _require_str(spec, "display_name", "ruleset")
    engine = spec.get("engine")
    _require(isinstance(engine, dict), "ruleset: engine block required")
    _require_str(engine, "name", "ruleset.engine")
    _require_str(engine, "version", "ruleset.engine")
    content = spec.get("content")
    _require(isinstance(content, list) and content, "ruleset: content list required")
    for index, item in enumerate(content):
        _require(isinstance(item, dict), f"ruleset.content[{index}]: must be an object")
        _require_str(item, "kind", f"ruleset.content[{index}]")
        _require_str(item, "name", f"ruleset.content[{index}]")
    binding = spec.get("active_binding")
    _require(isinstance(binding, dict), "ruleset: active_binding block required")
    _require(binding.get("status") in ("engine_confirmed", "unverified_static_config"),
             "ruleset: active_binding.status must be engine_confirmed|unverified_static_config")
    _require_str(binding, "reason", "ruleset.active_binding")
    priority = spec.get("source_priority")
    _require(isinstance(priority, list) and priority
             and all(isinstance(item, str) for item in priority),
             "ruleset: source_priority must be a non-empty list")


def validate_ruleset(ruleset: dict, known_source_ids: set[str] | None = None) -> None:
    """Validate the built ruleset manifest (spec + pinned source manifest hash + coverage)."""
    _require(isinstance(ruleset, dict), "ruleset: must be an object")
    _require(ruleset.get("schema_version") == SCHEMA_VERSION,
             f"ruleset: schema_version must be {SCHEMA_VERSION!r}")
    validate_ruleset_spec(ruleset)
    _require(isinstance(ruleset.get("created_at_utc"), str)
             and _TS_RE.match(ruleset["created_at_utc"]) is not None,
             "ruleset: created_at_utc must be an ISO-8601 UTC timestamp")
    manifest = ruleset.get("source_manifest")
    _require(isinstance(manifest, dict), "ruleset: source_manifest block required")
    _require_str(manifest, "sha256", "ruleset.source_manifest")
    _require(re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"]) is not None,
             "ruleset.source_manifest: sha256 must be 64 lowercase hex chars")
    coverage = ruleset.get("coverage")
    _require(isinstance(coverage, dict), "ruleset: coverage block required")
    _require(isinstance(coverage.get("cards_total"), int),
             "ruleset.coverage: cards_total must be an int")


def load_json_file(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json_file(path: str | Path, data: Any) -> None:
    """Deterministic UTF-8 JSON (stable key order, 1-space indent, trailing newline)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=False) + "\n",
                      encoding="utf-8")


def load_cards(directory: str | Path) -> list[dict]:
    """Load every ``*.json`` card in a directory tree (sorted by path)."""
    root = Path(directory)
    if not root.exists():
        raise SchemaError(f"card directory does not exist: {root}")
    cards = []
    for path in sorted(root.rglob("*.json")):
        cards.append(load_json_file(path))
    return cards
