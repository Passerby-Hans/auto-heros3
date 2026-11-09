"""Compatibility entry for the offline native owner contract."""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).resolve().parents[1] / "tests/offline/test_obs_process_ownership.py"), run_name="__main__")
