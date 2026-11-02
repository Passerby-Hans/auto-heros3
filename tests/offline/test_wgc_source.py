# -*- coding: utf-8 -*-
"""AV.1b WGC crop contract (pure synthetic frames; no COM/WGC session).

Verifies the recorder's client-area crop for the two geometries measured in P3/P4:
* 1024x768 window frame is 1028x815, client at (2,45);
* 1920x1080 borderless fullscreen client is (0,0).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from auto_heros3.recording.wgc_source import Frame, crop_client_bgra  # noqa: E402
import subprocess  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    RESULTS.append((name, bool(ok), str(detail)))


def test_window_crop() -> None:
    arr = np.zeros((815, 1028, 4), dtype=np.uint8)
    # client (7,5) -> window frame (9,50); BGRA (B=10,G=20,R=30)
    arr[50, 9] = (10, 20, 30, 255)
    img = crop_client_bgra(arr, (1024, 768))
    check('window_crop_size', img.size == (1024, 768), str(img.size))
    check('window_crop_offset', img.getpixel((7, 5)) == (30, 20, 10),
          str(img.getpixel((7, 5))))
    check('window_crop_corner', img.getpixel((0, 0)) == (0, 0, 0))


def test_fullscreen_crop() -> None:
    arr = np.zeros((1080, 1920, 4), dtype=np.uint8)
    arr[5, 7] = (1, 2, 3, 255)
    img = crop_client_bgra(arr, (1920, 1080))
    check('fullscreen_crop_size', img.size == (1920, 1080), str(img.size))
    check('fullscreen_crop_offset', img.getpixel((7, 5)) == (3, 2, 1),
          str(img.getpixel((7, 5))))


def test_explicit_crop() -> None:
    arr = np.zeros((20, 30, 4), dtype=np.uint8)
    arr[6, 8] = (4, 5, 6, 255)
    img = crop_client_bgra(arr, (10, 10), crop=(4, 2, 10, 10))
    check('explicit_crop_size', img.size == (10, 10), str(img.size))
    check('explicit_crop_value', img.getpixel((4, 4)) == (6, 5, 4),
          str(img.getpixel((4, 4))))


def test_invalid() -> None:
    try:
        crop_client_bgra(np.zeros((10, 10, 4), dtype=np.uint8), (20, 20))
        check('reject_small_frame', False, 'no error')
    except ValueError:
        check('reject_small_frame', True)
    try:
        crop_client_bgra(np.zeros((20, 20, 4), dtype=np.uint8), (10, 10),
                         crop=(15, 0, 10, 10))
        check('reject_outside_crop', False, 'no error')
    except ValueError:
        check('reject_outside_crop', True)
    try:
        crop_client_bgra(np.zeros((20, 20), dtype=np.uint8), (10, 10))
        check('reject_2d_frame', False, 'no error')
    except ValueError:
        check('reject_2d_frame', True)
    check('frame_dataclass', Frame(None, 0.0, 1, 2, 3).serial == 1)  # type: ignore[arg-type]


def test_dpi_helper() -> None:
    # Run in a child so this suite never changes its own process DPI awareness.
    code = ('import sys; sys.path.insert(0, r"%s"); '
            'from auto_heros3.recording.dpi import enable_per_monitor_v2; '
            'print(enable_per_monitor_v2())' % (ROOT / 'src'))
    proc = subprocess.run([sys.executable, '-X', 'utf8', '-c', code],
                          capture_output=True, text=True, timeout=30)
    check('dpi_helper_available', proc.returncode == 0,
          f'rc={proc.returncode} {proc.stderr[-120:]}')
    check('dpi_helper_bool', proc.stdout.strip() in ('True', 'False'), proc.stdout.strip())


def main() -> int:
    test_window_crop()
    test_fullscreen_crop()
    test_explicit_crop()
    test_invalid()
    test_dpi_helper()
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f'  ({detail})' if detail else ''))
    failed = [r for r in RESULTS if not r[1]]
    print(f'\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
