"""Offline compatibility entry for shared audio delivery acceptance checks."""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).resolve().parents[1] / "tests/offline/test_shared_audio_delivery.py"),
              run_name="__main__")
