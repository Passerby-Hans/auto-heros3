# -*- coding: utf-8 -*-
"""WGC frame source for the overlay recorder.

The old recorder polls ``PrintWindow`` once per output frame.  That works off-screen but cannot
reach 60 fps for a live game.  ``windows-capture`` delivers frames from the Desktop Window
Manager on its own thread; this module keeps the newest complete client frame and lets the
recorder compose at its own fixed 60 Hz cadence (missing source slots are explicit duplicates,
never fabricated content).

The crop rule was measured against the pinned VCMI window in AV.1b P3/P4:

* 1024x768 windowed: WGC frame 1028x815, client at (2,45);
* 1920x1080 borderless fullscreen: WGC frame 1920x1080, client at (0,0).

For a window frame the left/right border is ``(frame_w - client_w) // 2`` and the title bar is
``frame_h - client_h - border``.  A caller can pass an explicit ``crop=(x,y,w,h)`` for unusual
styles.  This module never writes game state and never sends commands.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import numpy as np
from PIL import Image


class WgcUnavailable(RuntimeError):
    """Binding missing or capture session could not be established."""


@dataclass(frozen=True)
class Frame:
    image: Image.Image
    received_monotonic: float
    serial: int
    width: int
    height: int


def crop_client_bgra(frame, client_size, crop=None) -> Image.Image:
    """Crop a WGC BGRA buffer to the client area and return an RGB PIL image.

    Pure function (no COM/thread state) so the geometry contract is testable offline.
    """
    arr = np.asarray(frame)
    if arr.ndim != 3 or arr.shape[2] < 3:
        raise ValueError(f'expected HxWx3/4 frame, got {arr.shape!r}')
    h, w = int(arr.shape[0]), int(arr.shape[1])
    cw, ch = int(client_size[0]), int(client_size[1])
    if cw <= 0 or ch <= 0:
        raise ValueError(f'invalid client size {client_size!r}')
    if crop is None:
        if cw > w or ch > h:
            raise ValueError(f'frame {w}x{h} smaller than client {cw}x{ch}')
        border = max(0, (w - cw) // 2)
        top = h - ch - border
        if top < 0:
            raise ValueError(f'cannot find client band in frame {w}x{h} for {cw}x{ch}')
        x, y = border, top
    else:
        x, y, cw, ch = (int(v) for v in crop)
    if x < 0 or y < 0 or x + cw > w or y + ch > h:
        raise ValueError(f'crop {(x, y, cw, ch)} outside frame {w}x{h}')
    rgb = np.ascontiguousarray(arr[y:y + ch, x:x + cw, :3][:, :, ::-1])
    return Image.fromarray(rgb, 'RGB')


def binding_available() -> bool:
    try:
        import windows_capture  # noqa: F401
        return True
    except Exception:
        return False


class WgcFrameSource:
    """Newest-frame holder around ``windows_capture.WindowsCapture``."""

    def __init__(self, hwnd: int, client_size: tuple[int, int],
                 crop: tuple[int, int, int, int] | None = None,
                 first_frame_timeout: float = 5.0):
        self.hwnd = int(hwnd)
        self.client_size = (int(client_size[0]), int(client_size[1]))
        self.crop = crop
        self.first_frame_timeout = first_frame_timeout
        self._lock = threading.Lock()
        self._latest: Frame | None = None
        self._cap = None
        self._control = None
        self._errors: list[str] = []
        self._closed = False

    def start(self) -> None:
        try:
            from windows_capture import WindowsCapture
        except Exception as exc:  # pragma: no cover - depends on optional binding
            raise WgcUnavailable(f'windows_capture import failed: {exc}') from exc
        cap = WindowsCapture(window_hwnd=self.hwnd, cursor_capture=False,
                             draw_border=False, minimum_update_interval=0)

        @cap.event
        def on_frame_arrived(frame, control):  # noqa: ANN001
            try:
                t = time.monotonic()
                image = crop_client_bgra(frame.frame_buffer, self.client_size, self.crop)
                with self._lock:
                    self._latest = Frame(image=image, received_monotonic=t,
                                         serial=(self._latest.serial + 1 if self._latest else 1),
                                         width=image.width, height=image.height)
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    if len(self._errors) < 20:
                        self._errors.append(f'{type(exc).__name__}: {exc}')

        @cap.event
        def on_closed():  # noqa: ANN001
            with self._lock:
                if len(self._errors) < 20:
                    self._errors.append('WGC closed')

        self._cap = cap
        self._control = cap.start_free_threaded()
        deadline = time.monotonic() + self.first_frame_timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._latest is not None:
                    return
            if self._control.is_finished():
                break
            time.sleep(0.02)
        self.close()
        raise WgcUnavailable('no first WGC frame within timeout')

    def latest(self) -> Frame | None:
        with self._lock:
            return self._latest

    def errors(self) -> list[str]:
        with self._lock:
            return list(self._errors)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        control = self._control
        try:
            if control is not None:
                control.stop()
                control.wait()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                if len(self._errors) < 20:
                    self._errors.append(f'close: {type(exc).__name__}: {exc}')
        self._cap = None
        self._control = None
