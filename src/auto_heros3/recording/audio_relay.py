# -*- coding: utf-8 -*-
"""Relay VCMI's SDL disk-audio named pipe into a plain raw file.

SDL's disk driver opens its output with a deny-read share mode, so the recorder cannot read the
file while VCMI is running.  A named pipe is writable by SDL and readable by us; this small relay
creates the pipe, receives headerless s16le/44100/2 PCM, and appends it to a normal raw file that
allows concurrent reads.  It never touches an audio endpoint.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--pipe', required=True, help='pipe name, without \\.\\pipe\\ prefix')
    ap.add_argument('--out', required=True, help='raw file for the recorder')
    ap.add_argument('--ready', required=True, help='ready marker written before waiting for client')
    a = ap.parse_args(argv)
    import pywintypes
    import win32file
    import win32pipe

    pipe_name = '\\\\.\\pipe\\' + a.pipe
    ready = Path(a.ready)
    out_path = Path(a.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        ready.unlink()
    except OSError:
        pass
    server = win32pipe.CreateNamedPipe(
        pipe_name,
        win32pipe.PIPE_ACCESS_INBOUND,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
        1, 1 << 20, 1 << 20, 0, None)
    ready.write_text('ready', encoding='utf-8')
    out = open(out_path, 'wb', buffering=0)
    try:
        win32pipe.ConnectNamedPipe(server, None)
        while True:
            try:
                _hr, data = win32file.ReadFile(server, 65536)
            except pywintypes.error:
                break
            if not data:
                break
            out.write(data)
        out.flush()
        return 0
    finally:
        try:
            out.close()
        except Exception:
            pass
        try:
            win32file.CloseHandle(server)
        except Exception:
            pass
        try:
            ready.unlink()
        except OSError:
            pass


if __name__ == '__main__':
    raise SystemExit(main())
