# -*- coding: utf-8 -*-
"""Per-process audio mute for the game (music+sfx) via Core Audio (pycaw).

Does not touch in-game settings; reversible anytime.
"""
from __future__ import annotations
import sys


def sessions(pid: int):
    from pycaw.pycaw import AudioUtilities
    return [s for s in AudioUtilities.GetAllSessions()
            if s.Process is not None and s.Process.pid == pid]


def set_mute(pid: int, mute: bool) -> int:
    """Mute/unmute all audio sessions of pid. Returns count of sessions."""
    n = 0
    for ctrl in sessions(pid):
        vol = ctrl.SimpleAudioVolume
        vol.SetMute(1 if mute else 0, None)
        n += 1
    return n


def status(pid: int):
    out = []
    for ctrl in sessions(pid):
        v = ctrl.SimpleAudioVolume
        out.append({'pid': pid, 'muted': bool(v.GetMute()),
                    'appvol': round(v.GetMasterVolume(), 2)})
    return out


if __name__ == '__main__':
    from agent.h3.mem import find_pids
    pids = find_pids()
    if not pids:
        print('game not running'); sys.exit(1)
    pid = pids[0]
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'mute'
    if cmd in ('mute', 'unmute'):
        n = set_mute(pid, cmd == 'mute')
        print(f'{cmd}: {n} session(s)')
    print('status:', status(pid))
