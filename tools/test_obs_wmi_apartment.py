"""Compatibility entry for the offline contract suite."""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).resolve().parents[1] / "tests/offline/test_obs_wmi_apartment.py"), run_name="__main__")
