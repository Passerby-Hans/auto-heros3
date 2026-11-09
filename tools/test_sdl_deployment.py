"""Compatibility shim for the file-only SDL deployment contract tests."""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).resolve().parents[1] /
                   'tests/offline/test_sdl_deployment.py'), run_name='__main__')
