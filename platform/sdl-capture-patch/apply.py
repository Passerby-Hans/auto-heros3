# -*- coding: utf-8 -*-
"""Apply the private SDL 2.32.10 a3capture backend to vendor/sdl-2.32.10.

The source tree is git-ignored.  This script is the tracked, reproducible patch
entry point and is safe to run repeatedly.
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def patch_text(path: Path, old: str, new: str, marker: str) -> None:
    text = path.read_text(encoding="utf-8")
    if marker in text:
        print(f"already patched: {path}")
        return
    if old not in text:
        raise SystemExit(f"patch anchor not found in {path}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"patched: {path}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sdl-dir", default="vendor/sdl-2.32.10")
    ap.add_argument("--patch-dir", default="platform/sdl-capture-patch")
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[2]
    sdl = (root / args.sdl_dir).resolve()
    patch = (root / args.patch_dir).resolve()
    if not (sdl / "include" / "SDL.h").exists():
        raise SystemExit(f"SDL source not found: {sdl}")

    target = sdl / "src" / "audio" / "a3capture"
    target.mkdir(parents=True, exist_ok=True)
    for src in (patch / "src" / "a3capture").glob("*"):
        shutil.copy2(src, target / src.name)
    # Share the A1 QPC deadline implementation with the C backend.
    clock_dir = root / "platform" / "obs-recorder" / "src" / "audio"
    for name in ("audio_clock_c.c", "audio_clock_c.h"):
        shutil.copy2(clock_dir / name, target / name)
    protocol_dir = root / "platform" / "audio-protocol"
    for name in ("include/a3_audio_wire.h", "include/a3_audio_protocol_c.h", "include/a3_audio_live.h",
                 "src/a3_audio_protocol_c.c"):
        shutil.copy2(protocol_dir / name, target / Path(name).name)
    print(f"copied backend + shared C clock/wire -> {target}")

    patch_text(
        sdl / "include" / "SDL_config.h.cmake",
        "#cmakedefine SDL_AUDIO_DRIVER_DISK @SDL_AUDIO_DRIVER_DISK@",
        "#cmakedefine SDL_AUDIO_DRIVER_DISK @SDL_AUDIO_DRIVER_DISK@\n"
        "#cmakedefine SDL_AUDIO_DRIVER_A3CAPTURE @SDL_AUDIO_DRIVER_A3CAPTURE@",
        "SDL_AUDIO_DRIVER_A3CAPTURE",
    )

    old_cmake = """  if(SDL_DISKAUDIO)
    set(SDL_AUDIO_DRIVER_DISK 1)
    file(GLOB DISKAUDIO_SOURCES ${SDL2_SOURCE_DIR}/src/audio/disk/*.c)
    list(APPEND SOURCE_FILES ${DISKAUDIO_SOURCES})
    set(HAVE_DISKAUDIO TRUE)
    set(HAVE_SDL_AUDIO TRUE)
  endif()
"""
    new_cmake = old_cmake + """  set(SDL_A3CAPTUREAUDIO ON)
  if(SDL_A3CAPTUREAUDIO)
    set(SDL_AUDIO_DRIVER_A3CAPTURE 1)
    file(GLOB A3CAPTUREAUDIO_SOURCES ${SDL2_SOURCE_DIR}/src/audio/a3capture/*.c)
    list(APPEND SOURCE_FILES ${A3CAPTUREAUDIO_SOURCES})
    set(HAVE_A3CAPTUREAUDIO TRUE)
    set(HAVE_SDL_AUDIO TRUE)
  endif()
"""
    patch_text(sdl / "CMakeLists.txt", old_cmake, new_cmake,
               "SDL_AUDIO_DRIVER_A3CAPTURE")

    # The localized MSVC /showIncludes prefix is passed to rc.exe as an extra
    # argument by this CMake version, producing RC1107.  The spike does not need
    # the version resource; keep it out of the private build until the RC rule
    # is made locale-safe.
    patch_text(
        sdl / "CMakeLists.txt",
        "  file(GLOB VERSION_SOURCES ${SDL2_SOURCE_DIR}/src/main/windows/*.rc)",
        "  set(VERSION_SOURCES \"\")  # A3PRIVATE: no RC in the locale-sensitive spike build",
        "A3PRIVATE: no RC",
    )

    patch_text(
        sdl / "src" / "audio" / "SDL_sysaudio.h",
        "extern AudioBootStrap DISKAUDIO_bootstrap;",
        "extern AudioBootStrap DISKAUDIO_bootstrap;\n"
        "extern AudioBootStrap A3CAPTURE_bootstrap;",
        "A3CAPTURE_bootstrap",
    )

    patch_text(
        sdl / "src" / "audio" / "SDL_audio.c",
        "static const AudioBootStrap *const bootstrap[] = {\n",
        "static const AudioBootStrap *const bootstrap[] = {\n"
        "#ifdef SDL_AUDIO_DRIVER_A3CAPTURE\n"
        "    &A3CAPTURE_bootstrap,\n"
        "#endif\n",
        "A3CAPTURE_bootstrap",
    )
    print("a3capture patch applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
