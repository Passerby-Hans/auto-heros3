#!/usr/bin/env python3
"""Synthetic animated Tk window for the S1c OBS window_capture/WGC self-test.

Purpose: give the capture host a completely synthetic, audio-free, game-free
top-level window whose title/class/PID it can target through libobs'
window helpers.  The window redraws a moving pattern every ~40 ms so captured
frames are verifiably changing (not a static image).

Once the window is mapped the script writes a small JSON "ready" file
(hwnd / pid / title / class / size) that the PowerShell evidence runner uses to
pass --window-pid/--window-title to obs-media-selftest.exe.

This is a test helper only: no audio, no game, no network, no product code.
"""
import argparse
import ctypes
import json
import math
import os
import sys
import time

import tkinter as tk

user32 = ctypes.windll.user32
GA_ROOT = 2


def get_top_level_hwnd(root):
    # Tk's winfo_id() is the content/child HWND; walk up to the top-level one.
    hwnd = int(root.winfo_id())
    top = user32.GetAncestor(hwnd, GA_ROOT)
    return int(top) if top else hwnd


def get_window_title(hwnd):
    n = int(user32.GetWindowTextLengthW(hwnd))
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def get_window_class(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def get_window_exe(hwnd):
    """Best-effort executable name (base name) for the window's process."""
    kernel32 = ctypes.windll.kernel32
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        size = ctypes.c_ulong(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(handle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", default="VCMI WGC Synthetic")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--x", type=int, default=140)
    parser.add_argument("--y", type=int, default=140)
    parser.add_argument("--ready-file", default="")
    args = parser.parse_args()

    w, h = args.width, args.height
    root = tk.Tk()
    root.title(args.title)
    root.geometry("%dx%d+%d+%d" % (w, h, args.x, args.y))
    root.resizable(False, False)
    canvas = tk.Canvas(root, width=w, height=h, bg="#101820", highlightthickness=0)
    canvas.pack()

    state = {"frame": 0, "start": time.time()}

    def color_for(frame, base):
        # Smoothly cycling colour, always bright enough that YMAX is high.
        r = base + int(90 * (0.5 + 0.5 * math.sin(frame * 0.11)))
        g = base + int(90 * (0.5 + 0.5 * math.sin(frame * 0.11 + 2.1)))
        b = base + int(90 * (0.5 + 0.5 * math.sin(frame * 0.11 + 4.2)))
        return "#%02x%02x%02x" % (min(r, 255), min(g, 255), min(b, 255))

    def tick():
        frame = state["frame"]
        state["frame"] = frame + 1
        elapsed = time.time() - state["start"]

        canvas.configure(bg=color_for(frame, 20))
        canvas.delete("all")

        # Static grid lines (stable reference), then moving objects on top.
        for x in range(0, w, 64):
            canvas.create_line(x, 0, x, h, fill="#55606e")
        for y in range(0, h, 64):
            canvas.create_line(0, y, w, y, fill="#55606e")

        # Moving yellow block (horizontal sweep).
        bx = int((frame * 9) % (w - 140))
        by = int(h // 2 + 70 * math.sin(frame * 0.09))
        canvas.create_rectangle(bx, by, bx + 130, by + 80, fill="#ffcc33", outline="")

        # Moving cyan block (opposite direction).
        cx = w - 50 - int((frame * 7) % (w - 120))
        canvas.create_rectangle(cx, 40, cx + 60, 150, fill="#22e0ff", outline="")

        # Moving red circle.
        rx = int(w / 2 + 180 * math.sin(frame * 0.07))
        ry = int(h / 2 + 140 * math.cos(frame * 0.05))
        canvas.create_oval(rx - 36, ry - 36, rx + 36, ry + 36, fill="#ff3355", outline="")

        canvas.create_text(
            16,
            16,
            anchor="nw",
            fill="#ffffff",
            font=("Segoe UI", 18, "bold"),
            text="VCMI WGC Synthetic   frame %d   t=%.2fs" % (frame, elapsed),
        )
        canvas.create_text(
            16,
            h - 16,
            anchor="sw",
            fill="#ffffff",
            font=("Consolas", 12),
            text="synthetic animated window - no audio / no game / no playback",
        )
        root.after(40, tick)

    def write_ready_file():
        root.update_idletasks()
        hwnd = get_top_level_hwnd(root)
        info = {
            "hwnd": hwnd,
            "hwnd_hex": "0x%x" % hwnd,
            "pid": os.getpid(),
            "title": get_window_title(hwnd),
            "class": get_window_class(hwnd),
            "exe": get_window_exe(hwnd),
            "width": w,
            "height": h,
            "x": args.x,
            "y": args.y,
            "script": os.path.basename(__file__),
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if args.ready_file:
            tmp = args.ready_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(info, f, indent=2)
            os.replace(tmp, args.ready_file)
        print("WINDOW_READY " + json.dumps(info, sort_keys=True), flush=True)

    # Start the animation and write the ready file once the window is mapped
    # (300 ms is enough for Tk to map/raise the toplevel and for DWM to know it).
    root.after(40, tick)
    root.after(300, write_ready_file)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
