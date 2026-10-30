# tests/ layout (B0.5)

- `offline/` — deterministic offline suites (no game client); run through
  `tools/run_offline_suite.py` (explicit whitelist) or directly with the venv Python.
- `live/` — suites that start the real VCMI client / write real save state. They must be
  selected explicitly, one at a time, with isolated AUTO_HEROS3_* directories.
- `tools/test_*.py` are compatibility shims that forward to these files (historical
  commands keep working); the canonical copies are here.
