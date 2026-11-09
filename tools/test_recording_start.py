"""Compatibility entry for the offline contract suite."""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).resolve().parents[1] / "tests/offline/test_recording_start.py"), run_name="__main__")
