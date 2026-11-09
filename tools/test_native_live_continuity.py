"""Explicit live-only entry point; never invoked by the offline suite."""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).resolve().parents[1] / "tests/live/test_native_live_continuity.py"),
              run_name="__main__")
