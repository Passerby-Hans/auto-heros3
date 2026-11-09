"""Offline compatibility entry for the private SDL recipe lock checks."""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).resolve().parents[1] / "tests/offline/test_sdl_source_lock.py"),
              run_name="__main__")
