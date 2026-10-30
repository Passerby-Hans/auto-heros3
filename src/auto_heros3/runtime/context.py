# -*- coding: utf-8 -*-
"""RuntimeContext: the single owner of the MCP server's mutable process state (B0.4c).

GPT6 review guidance (2026-10-30): before splitting stateful services, declare **one owner**
for the lease token, the mutation gate (lock / owner / thread-local depth), the events lock,
the recorder process handle and the runtime paths, so the old module and the context never
maintain duplicate mutable copies.

The MCP server module keeps no duplicate globals for these values.  The historical attribute
names (``M._lease_token``, ``M._MUTATION_LOCK``, ``M._MUTATION_LOCAL``, ``M._recorder`` ...)
are served by a small module-class bridge that reads/writes this context, so the offline
fixtures keep patching the very state the implementation reads.

The paths object is an :class:`auto_heros3.paths.RuntimePaths` (resolved once at import time
from the TEST-ONLY ``AUTO_HEROS3_*`` overrides; see ``runtime_paths_from_env``).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from auto_heros3.paths import RuntimePaths


@dataclass
class RuntimeContext:
    """Owns the MCP server's mutable runtime state (exactly one instance per process)."""

    paths: RuntimePaths
    # Single-writer lease: the in-memory token is authoritative; <state>/lease.json is a mirror.
    lease_token: str | None = None
    # Mutation gate (D-014): serialized mutating tools.  The local thread depth makes nested
    # calls from the same thread idempotent; the lock + owner serialize cross-thread calls.
    mutation_lock: threading.Lock = field(default_factory=threading.Lock)
    mutation_owner: dict | None = None
    mutation_local: threading.local = field(default_factory=threading.local)
    # Decision-event store lock (events.jsonl / events_state.json read-modify-replace).
    events_lock: threading.Lock = field(default_factory=threading.Lock)
    # Recorder process handle: {'proc': Popen | None, 'dir': Path | None, 'started': float}
    recorder: dict = field(default_factory=lambda: {"proc": None, "dir": None, "started": 0.0})
