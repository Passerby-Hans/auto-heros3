# -*- coding: utf-8 -*-
"""B1.1 source manifest: declared evidence, sha256 pins, missing status, version conflicts.

The manifest is the single authority for "where does a card fact come from".  Every
declared source has a stable ``source_id`` (used by card ``SourceRef``s), a kind
(manual / rule card / game data / engine source / localization / config / engine export),
a local path, and either a ``present`` status with sha256 + byte size or an explicit
``missing`` status with a reason.  A missing source is never silently skipped: the
builder either reports the affected facts as missing or refuses to build.

Version conflicts: when a manifest already exists, ``assert_sources_compatible`` compares
the freshly hashed sources with the recorded pins and raises
:class:`SourceVersionConflictError` instead of overwriting the old snapshot.  That is how
"a new game data / manual version must not silently replace the old values" is enforced.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .schema import SchemaError, normalize_ws

SCHEMA_VERSION = "b1.1"

# Source kinds.  ``engine_export`` is deliberately listed even though this batch has no
# engine export: the missing entry documents what B1.2 still needs.
SOURCE_KINDS = (
    "manual",
    "rule_card",
    "tactic_example",
    "game_data",
    "config",
    "engine_source",
    "localization",
    "engine_export",
)
SOURCE_STATUSES = ("present", "missing")

# Lower number = higher authority for the same field.  Mirrors the plan's source order:
# loaded/effective rule data first, original manuals second, supplementary material last.
SOURCE_PRIORITIES = {"engine_export": 0, "game_data": 1, "config": 1, "engine_source": 2,
                     "manual": 3, "localization": 4, "rule_card": 5, "tactic_example": 6}

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class MissingSourceError(SchemaError):
    """A required local source file is absent; refusing to fabricate values."""

    def __init__(self, missing: list[dict]):
        self.missing = missing
        detail = ", ".join(f"{item['source_id']} ({item['path']})" for item in missing)
        super().__init__(f"missing required source(s): {detail}")


class SourceVersionConflictError(SchemaError):
    """Sources changed since the existing manifest was written; refusing to overwrite."""

    def __init__(self, diffs: list[dict]):
        self.diffs = diffs
        detail = "; ".join(
            f"{item['source_id']}: {item['field']} {item.get('old')!r} -> {item.get('new')!r}"
            for item in diffs[:6])
        super().__init__(f"source version conflict, existing snapshot preserved: {detail}")


class TraceabilityError(SchemaError):
    """A card SourceRef does not actually resolve in its declared source."""


# --------------------------------------------------------------------------------------
# source specs (the ignored local input file)
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    kind: str
    title: str
    path: str
    priority: int
    version: str | None = None
    member: str | None = None
    encoding: str = "utf-8"
    provenance: str | None = None
    notes: str | None = None
    required: bool = True
    external: bool = False

    @property
    def is_archive(self) -> bool:
        return self.path.lower().endswith(".zip")

    @property
    def is_json(self) -> bool:
        return self.path.lower().endswith((".json", ".jsonc")) or (
            self.is_archive and (self.member or "").lower().endswith(".json"))

    def to_dict(self) -> dict:
        data: dict = {
            "source_id": self.source_id,
            "kind": self.kind,
            "title": self.title,
            "path": self.path,
            "priority": self.priority,
        }
        for name in ("version", "member", "encoding", "provenance", "notes"):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        if not self.required:
            data["required"] = False
        if self.external:
            data["external"] = True
        return data


def _spec_from_dict(item: dict, where: str) -> SourceSpec:
    if not isinstance(item, dict):
        raise SchemaError(f"{where}: must be an object")
    for required in ("source_id", "kind", "title", "path", "priority"):
        value = item.get(required)
        if not isinstance(value, str) and required != "priority":
            raise SchemaError(f"{where}: field {required!r} must be a non-empty string")
        if required == "priority" and not isinstance(value, int):
            raise SchemaError(f"{where}: field 'priority' must be an int")
    if not re.fullmatch(r"[a-z][a-z0-9_.\-]*", item["source_id"]):
        raise SchemaError(f"{where}: source_id {item['source_id']!r} is not a contract id")
    if item["kind"] not in SOURCE_KINDS:
        raise SchemaError(f"{where}: kind must be one of {SOURCE_KINDS}")
    external = item.get("external", False)
    if not isinstance(external, bool):
        raise SchemaError(f"{where}: external must be a bool")
    if not external and isinstance(item["path"], str) and (
            item["path"].startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", item["path"])):
        raise SchemaError(
            f"{where}: relative path expected (set external=true for machine-local files)")
    encoding = item.get("encoding", "utf-8")
    if not isinstance(encoding, str) or not encoding:
        raise SchemaError(f"{where}: encoding must be a non-empty string")
    return SourceSpec(
        source_id=item["source_id"],
        kind=item["kind"],
        title=item["title"],
        path=item["path"],
        priority=item["priority"],
        version=item.get("version"),
        member=item.get("member"),
        encoding=encoding,
        provenance=item.get("provenance"),
        notes=item.get("notes"),
        required=item.get("required", True) is not False,
        external=external,
    )


def load_source_specs(path: str | Path) -> list[SourceSpec]:
    """Load ``source_spec.json`` (``{"schema_version", "sources": [...]}``)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise SchemaError(f"{path}: schema_version must be {SCHEMA_VERSION!r}")
    items = data.get("sources")
    if not isinstance(items, list) or not items:
        raise SchemaError(f"{path}: 'sources' must be a non-empty list")
    specs: list[SourceSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        spec = _spec_from_dict(item, f"{Path(path).name} sources[{index}]")
        if spec.source_id in seen:
            raise SchemaError(f"{path}: duplicate source_id {spec.source_id!r}")
        seen.add(spec.source_id)
        specs.append(spec)
    return specs


# --------------------------------------------------------------------------------------
# hashing / manifest building
# --------------------------------------------------------------------------------------
def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_source_path(source: dict, root: str | Path) -> Path:
    """Absolute path of a declared source (repo-relative or machine-local external)."""
    path = Path(source["path"])
    return path if path.is_absolute() else Path(root) / path


def build_manifest(specs: Iterable[SourceSpec], root: str | Path,
                   *, now: str | None = None) -> dict:
    """Hash every declared source; absent files become explicit ``missing`` entries."""
    root_path = Path(root)
    sources: list[dict] = []
    for spec in specs:
        absolute = Path(spec.path) if spec.external else root_path / spec.path
        entry = spec.to_dict()
        entry["sha256"] = None
        entry["bytes"] = None
        if not absolute.exists():
            entry["status"] = "missing"
            entry["missing_reason"] = f"local source file is absent: {spec.path}"
        else:
            entry["status"] = "present"
            entry["bytes"] = absolute.stat().st_size
            entry["sha256"] = sha256_file(absolute)
            if spec.is_archive:
                try:
                    with zipfile.ZipFile(absolute) as archive:
                        names = set(archive.namelist())
                except (OSError, zipfile.BadZipFile) as exc:
                    raise SchemaError(f"{spec.path}: unreadable zip archive: {exc}") from exc
                if spec.member and spec.member not in names:
                    entry["status"] = "missing"
                    entry["missing_reason"] = (
                        f"archive entry {spec.member!r} is absent from {spec.path}")
        sources.append(entry)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": now or _utc_now(),
        "priority_order": ["engine_export", "game_data", "config", "engine_source",
                           "manual", "localization", "rule_card", "tactic_example"],
        "sources": sources,
    }
    return manifest


def manifest_sha256(manifest: dict) -> str:
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return sha256_bytes(canonical)


def validate_manifest(manifest: dict) -> None:
    if not isinstance(manifest, dict):
        raise SchemaError("source manifest: must be an object")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise SchemaError(f"source manifest: schema_version must be {SCHEMA_VERSION!r}")
    if not isinstance(manifest.get("generated_at_utc"), str) or not _TS_RE.match(
            manifest["generated_at_utc"]):
        raise SchemaError("source manifest: generated_at_utc must be ISO-8601 UTC")
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise SchemaError("source manifest: 'sources' must be a non-empty list")
    seen: set[str] = set()
    for index, item in enumerate(sources):
        where = f"source manifest sources[{index}]"
        if not isinstance(item, dict):
            raise SchemaError(f"{where}: must be an object")
        for required in ("source_id", "kind", "title", "path", "priority", "status"):
            if required not in item:
                raise SchemaError(f"{where}: missing field {required!r}")
        if not re.fullmatch(r"[a-z][a-z0-9_.\-]*", item["source_id"]):
            raise SchemaError(f"{where}: invalid source_id {item['source_id']!r}")
        if item["kind"] not in SOURCE_KINDS:
            raise SchemaError(f"{where}: invalid kind {item['kind']!r}")
        if item["status"] not in SOURCE_STATUSES:
            raise SchemaError(f"{where}: invalid status {item['status']!r}")
        if item["source_id"] in seen:
            raise SchemaError(f"{where}: duplicate source_id {item['source_id']!r}")
        seen.add(item["source_id"])
        if item["status"] == "present":
            if not isinstance(item.get("sha256"), str) or not re.fullmatch(
                    r"[0-9a-f]{64}", item["sha256"]):
                raise SchemaError(f"{where}: present source needs a 64-hex sha256")
            if not isinstance(item.get("bytes"), int) or item["bytes"] < 0:
                raise SchemaError(f"{where}: present source needs a byte size")
        else:
            if not item.get("missing_reason"):
                raise SchemaError(f"{where}: missing source needs an explicit reason")


def source_ids(manifest: dict) -> set[str]:
    return {item["source_id"] for item in manifest.get("sources", [])}


def source_by_id(manifest: dict) -> dict[str, dict]:
    return {item["source_id"]: item for item in manifest.get("sources", [])}


def require_present(manifest: dict, required_ids: Iterable[str]) -> None:
    """Raise :class:`MissingSourceError` when any required source is not present."""
    by_id = source_by_id(manifest)
    missing: list[dict] = []
    for source_id in required_ids:
        entry = by_id.get(source_id)
        if entry is None:
            missing.append({"source_id": source_id, "path": "<undeclared>",
                            "reason": "source id is not declared in the manifest"})
        elif entry["status"] != "present":
            missing.append({"source_id": source_id, "path": entry["path"],
                            "reason": entry.get("missing_reason", "source is missing")})
    if missing:
        raise MissingSourceError(missing)


def diff_manifests(existing: dict, current: dict) -> list[dict]:
    """Field-level differences between two manifests, ignoring generation timestamps."""
    old = source_by_id(existing)
    new = source_by_id(current)
    diffs: list[dict] = []
    for source_id in sorted(set(old) | set(new)):
        if source_id not in new:
            diffs.append({"source_id": source_id, "field": "presence",
                          "old": "declared", "new": "absent"})
            continue
        if source_id not in old:
            diffs.append({"source_id": source_id, "field": "presence",
                          "old": "absent", "new": "declared"})
            continue
        for field_name in ("sha256", "bytes", "status"):
            if old[source_id].get(field_name) != new[source_id].get(field_name):
                diffs.append({"source_id": source_id, "field": field_name,
                              "old": old[source_id].get(field_name),
                              "new": new[source_id].get(field_name)})
    return diffs


def assert_sources_compatible(existing: dict, current: dict) -> None:
    """Refuse to overwrite an existing data snapshot when a source changed."""
    diffs = diff_manifests(existing, current)
    if diffs:
        raise SourceVersionConflictError(diffs)


def load_manifest(path: str | Path) -> dict:
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_manifest(manifest)
    return manifest


# --------------------------------------------------------------------------------------
# reading sources for traceability checks
# --------------------------------------------------------------------------------------
def _strip_jsonc(text: str) -> str:
    """Remove ``//`` / ``/* */`` comments and trailing commas (VCMI config dialect)."""
    out: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    escaped = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            while index < length and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            index += 2
            while index + 1 < length and not (text[index] == "*" and text[index + 1] == "/"):
                index += 1
            index += 2
            continue
        out.append(char)
        index += 1
    stripped = "".join(out)
    return re.sub(r",(\s*[}\]])", r"\1", stripped)


def load_jsonc(path: str | Path) -> Any:
    text = Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_strip_jsonc(text))


def read_source_text(source: dict, root: str | Path) -> str:
    """Read a declared source as text (zip member / plain file / JSONC raw text)."""
    absolute = resolve_source_path(source, root)
    if not absolute.exists():
        raise TraceabilityError(
            f"source {source['source_id']} is missing: {source['path']}")
    if source["path"].lower().endswith(".zip"):
        member = source.get("member")
        if not member:
            raise TraceabilityError(
                f"source {source['source_id']}: archive source without a member")
        with zipfile.ZipFile(absolute) as archive:
            raw = archive.read(member)
        return raw.decode(source.get("encoding", "utf-8"))
    return absolute.read_text(encoding=source.get("encoding", "utf-8"))


def load_source_json(source: dict, root: str | Path) -> Any:
    absolute = resolve_source_path(source, root)
    if not absolute.exists():
        raise TraceabilityError(
            f"source {source['source_id']} is missing: {source['path']}")
    if source["path"].lower().endswith(".zip"):
        return json.loads(read_source_text(source, root))
    return load_jsonc(absolute)


_POINTER_ESCAPES = (("~1", "/"), ("~0", "~"))


def resolve_json_pointer(document: Any, pointer: str) -> Any:
    """RFC-6901 pointer (``#/a/b``) resolution with a friendly error."""
    if not pointer.startswith("#/"):
        raise TraceabilityError(f"not a JSON pointer locator: {pointer!r}")
    token_text = pointer[2:]
    if token_text == "":
        return document
    node = document
    for raw_token in token_text.split("/"):
        token = raw_token
        for escaped, literal in _POINTER_ESCAPES:
            token = token.replace(escaped, literal)
        if isinstance(node, dict):
            if token not in node:
                raise TraceabilityError(f"JSON pointer {pointer!r}: key {token!r} not found")
            node = node[token]
        elif isinstance(node, list):
            try:
                node = node[int(token)]
            except (ValueError, IndexError) as exc:
                raise TraceabilityError(
                    f"JSON pointer {pointer!r}: bad list index {token!r}") from exc
        else:
            raise TraceabilityError(
                f"JSON pointer {pointer!r}: cannot descend into {type(node).__name__}")
    return node


_LINE_RE = re.compile(r"(?:^|\b)line (\d+)")
_PAGE_RE = re.compile(r"(?:^|\b)page (\d+)")


def _page_markers(lines: list[str]) -> list[tuple[int, int]]:
    markers: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = re.match(r"^## 第(\d+)页$", line.strip())
        if match:
            markers.append((index + 1, int(match.group(1))))
    return markers


def _manual_page_matches(source_id: str, lines: list[str]) -> bool:
    """Manual sources use ``## 第N页`` page markers; other text sources do not need them."""
    return any(re.match(r"^## 第(\d+)页$", line.strip()) for line in lines)


def resolve_text_locator(source: dict, locator: str, root: str | Path,
                         *, quote: str | None = None) -> str:
    """Resolve a text locator and (when given) verify the quote really appears near it.

    Supported locators: ``line 37`` / ``line 37 column Attack`` / ``page 84 line 3888``.
    Returns the cited line's raw text.
    """
    text = read_source_text(source, root)
    lines = text.splitlines()
    line_match = _LINE_RE.search(locator)
    if not line_match:
        raise TraceabilityError(
            f"{source['source_id']}: text locator needs 'line N': {locator!r}")
    line_no = int(line_match.group(1))
    if not (1 <= line_no <= len(lines)):
        raise TraceabilityError(
            f"{source['source_id']}: cited line {line_no} outside 1..{len(lines)}")
    page_match = _PAGE_RE.search(locator)
    if page_match:
        page = int(page_match.group(1))
        markers = _page_markers(lines)
        if not markers:
            raise TraceabilityError(
                f"{source['source_id']}: page locator used on a source without page markers")
        start = None
        end = len(lines)
        for marker_line, marker_page in markers:
            if marker_page == page:
                start = marker_line
            elif start is not None:
                end = marker_line - 1
                break
        if start is None:
            raise TraceabilityError(
                f"{source['source_id']}: page {page} not found in source")
        if not (start <= line_no <= end):
            raise TraceabilityError(
                f"{source['source_id']}: cited line {line_no} is not on page {page} "
                f"(page spans {start}..{end})")
    if quote is not None:
        line_text = normalize_ws(lines[line_no - 1])
        quote_text = normalize_ws(quote)
        if quote_text not in line_text:
            # allow multi-line quotes that start on the cited line
            window = normalize_ws("\n".join(lines[line_no - 1:line_no - 1 + 10]))
            if quote_text not in window:
                raise TraceabilityError(
                    f"{source['source_id']} line {line_no}: quote not found: "
                    f"{quote[:120]!r}")
    return lines[line_no - 1]


def assert_ref_traceable(manifest: dict, ref: dict, root: str | Path) -> str:
    """Verify one card SourceRef resolves in its declared source; returns a short context."""
    by_id = source_by_id(manifest)
    source_id = ref["source_id"]
    if source_id not in by_id:
        raise TraceabilityError(f"unknown source_id {source_id!r} in card ref")
    source = by_id[source_id]
    if source["status"] != "present":
        raise TraceabilityError(
            f"card ref cites a missing source {source_id!r}: {source.get('missing_reason')}")
    locator = ref["locator"]
    if "#/" in locator:
        pointer = locator[locator.index("#/"):]
        document = load_source_json(source, root)
        value = resolve_json_pointer(document, pointer)
        return json.dumps(value, ensure_ascii=False)[:200]
    return resolve_text_locator(source, locator, root, quote=ref.get("quote"))


def assert_cards_traceable(manifest: dict, cards: Iterable[dict], root: str | Path) -> int:
    """Verify every SourceRef of every card (facts, aliases, structural, availability)."""
    checked = 0
    for card in cards:
        refs: list[dict] = []
        refs.extend(card.get("sources", []))
        for alias in card.get("aliases", []):
            refs.append({"source_id": alias.get("source_id"),
                         "locator": alias.get("locator", "line 1")})
        for item in card.get("facts", []):
            refs.extend(item.get("sources", []))
        for item in card.get("conflicts", []):
            refs.extend(item.get("sources", []))
        manual_evidence = card.get("manual_evidence") or {}
        if manual_evidence.get("source_id"):
            refs.append({"source_id": manual_evidence["source_id"],
                         "locator": manual_evidence.get("locator", "line 1")})
        block = card.get("creature") or card.get("spell") or {}
        refs.extend(block.get("sources", []))
        availability = block.get("availability") or {}
        for claim in availability.values():
            refs.extend(claim.get("sources", []))
        for ref in refs:
            # alias refs use a synthetic ref without a quote; everything else keeps its quote
            assert_ref_traceable(manifest, ref, root)
            checked += 1
    return checked


# --------------------------------------------------------------------------------------
# small helper used by the builder and tests
# --------------------------------------------------------------------------------------
def missing_sources(manifest: dict) -> list[dict]:
    return [item for item in manifest.get("sources", []) if item["status"] == "missing"]


@dataclass
class SourceIndex:
    """Convenience view over a manifest: spec map + read helpers bound to a root."""

    manifest: dict
    root: Path
    specs: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def from_parts(cls, specs: Iterable[SourceSpec], manifest: dict,
                   root: str | Path) -> "SourceIndex":
        by_id = source_by_id(manifest)
        spec_map = {spec.source_id: {**spec.to_dict(), **by_id[spec.source_id]}
                    for spec in specs if spec.source_id in by_id}
        return cls(manifest=manifest, root=Path(root), specs=spec_map)

    def text(self, source_id: str) -> str:
        return read_source_text(self.specs[source_id], self.root)

    def json(self, source_id: str) -> Any:
        return load_source_json(self.specs[source_id], self.root)

    def ref(self, source_id: str, locator: str, quote: str | None = None) -> dict:
        from .schema import source_ref
        return source_ref(source_id, locator, quote)
