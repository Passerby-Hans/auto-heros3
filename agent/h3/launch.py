# -*- coding: utf-8 -*-
"""One-command game bootstrap for the agent:
launch -> wait window -> mute audio -> decouple cursor -> ready.

Usage: uv run python -m agent.h3.launch
After this, the game runs in background, the real mouse is untouched,
and input goes through agent.h3.binput (PostMessage).
"""
import subprocess, sys, time

GAME = r"D:\Heros3\Heroes3.exe"
CWD = r"D:\Heros3"


def wait_window(timeout=30):
    from agent.h3.capture import h3_hwnds
    t0 = time.time()
    while time.time() - t0 < timeout:
        wins = h3_hwnds()
        if wins:
            return wins[0][0]
        time.sleep(1.0)
    return None


def main(already_running=False):
    if not already_running:
        subprocess.Popen([GAME], cwd=CWD)
        print('game launching...')
    hwnd = wait_window()
    if not hwnd:
        print('ERROR: game window not found'); sys.exit(1)
    print(f'window ready hwnd={hwnd:#x}')
    time.sleep(4)  # let init finish (menus build)
    # mute
    try:
        from agent.h3.audio import set_mute
        from agent.h3.mem import find_pids
        n = set_mute(find_pids()[0], True)
        print(f'audio muted ({n} session)')
    except Exception as e:
        print(f'audio mute skipped: {e}')
    # cursor decoupling (IAT patch of ddraw ClipCursor/SetCursorPos)
    try:
        from agent.h3.decouple import decouple, release_clip
        decouple()
    except Exception as e:
        print(f'decouple warning: {e}')
        release_clip()
    # virtual cursor (GetCursorPos IAT redirect; memory-only, per-launch)
    # NOTE: hwnd=... also installs the GetForegroundWindow stub (fg-stub), which
    # is what stops cnc-ddraw from freezing rendering while unfocused. Never
    # launch without it (session 12 finding).
    try:
        from agent.h3.vcursor import install
        from agent.h3.mem import Process
        print('vcursor:', install(p := Process.attach(), hwnd=hwnd))
    except Exception as e:
        print(f'vcursor warning: {e}')
    # park window off-screen so the real mouse can never interfere
    try:
        import ctypes
        from agent.h3.capture import h3_hwnds
        ctypes.windll.user32.SetWindowPos(h3_hwnds()[0][0], 0, -850, 100, 0, 0, 0x0001 | 0x0004 | 0x0010)
        print('window parked off-screen')
    except Exception as e:
        print(f'park warning: {e}')
    print('READY: background play enabled, real cursor free.')


if __name__ == '__main__':
    main(already_running='--running' in sys.argv)
