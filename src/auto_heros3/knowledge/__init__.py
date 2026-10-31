"""Static knowledge data contract (B1.1): sources, rulesets and entity cards.

This package does NOT query the game and does NOT register MCP tools (that is B1.2).
It only defines the data contract for the first creature/spell knowledge batch and a
builder that turns locally available public rule tables + manuals into cards.

Layers
------
``schema``   validation for source refs, facts, cards and ruleset manifests.
``sources``  source manifest (manuals / rule cards / game data / engine code) with
             sha256 pins, present-vs-missing status and version-conflict handling.
``build``    offline builder: local public sources -> source manifest + ruleset + cards.

Data products (cards, manifests) are written under the git-ignored
``doc/knowledge/rulesets/<ruleset-id>/`` and ``doc/knowledge/sources/`` trees; only this
code, its synthetic-fixture tests and a compatibility shell are tracked.
"""
from __future__ import annotations

__all__ = ["schema", "sources", "build"]
