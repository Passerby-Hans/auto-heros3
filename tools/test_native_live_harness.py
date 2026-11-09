"""Compatibility entry for offline native promotion harness contract tests."""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).resolve().parents[1] /
                  'tests/offline/test_native_live_harness.py'), run_name='__main__')
