"""Private SDL recipe/source-lock checks; copies sources only into a temp tree.

Never builds/loads a DLL, initializes SDL, or touches the live vendor runtime.
Transport concurrency/PCM acceptance belongs to the native and explicit live tests.
"""
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "platform/sdl-capture-patch"
RESULTS = []


def check(name, ok):
    RESULTS.append((name, bool(ok)))


def main():
    lock = json.loads((PATCH / "source-lock.json").read_text(encoding="utf-8"))
    inputs = lock["patch_inputs"]
    check("inputs_unique", len(inputs) == len(set(inputs)))
    check("inputs_relative_workspace", all(not Path(p).is_absolute() and ".." not in Path(p).parts
                                           for p in inputs))
    expected = {"platform/audio-protocol/include/a3_audio_live.h",
                "platform/audio-protocol/include/a3_audio_wire.h",
                "platform/audio-protocol/include/a3_audio_protocol_c.h",
                "platform/audio-protocol/src/a3_audio_protocol_c.c",
                "platform/sdl-capture-patch/src/a3capture/SDL_a3capture.c",
                "platform/sdl-capture-patch/tests/a3capture_smoke.c"}
    check("shared_transport_and_pcm_fixture_pinned", expected <= set(inputs))
    contents = [(ROOT / p).read_text(encoding="utf-8").replace("\r\n", "\n") for p in inputs]
    recipe = "\n---A3PATCH---\n".join(contents)
    actual = hashlib.sha256(recipe.encode("utf-8")).hexdigest()
    check("normalized_recipe_matches_lock", actual == lock["patch_sha256"])
    changed = contents.copy()
    changed[inputs.index("platform/audio-protocol/include/a3_audio_live.h")] += "\nchanged\n"
    check("shared_header_mutation_changes_recipe", hashlib.sha256(
        "\n---A3PATCH---\n".join(changed).encode()).hexdigest() != lock["patch_sha256"])
    check("newline_normalization_stable", all(c == c.replace("\n", "\r\n").replace("\r\n", "\n")
                                               for c in contents))

    spec = importlib.util.spec_from_file_location("a3_sdl_apply", PATCH / "apply.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory(prefix="a3-sdl-source-") as tmp:
        sdl = Path(tmp)
        (sdl / "include").mkdir()
        (sdl / "src/audio").mkdir(parents=True)
        (sdl / "include/SDL.h").write_text("/* fixture */\n")
        (sdl / "include/SDL_config.h.cmake").write_text(
            "#cmakedefine SDL_AUDIO_DRIVER_DISK @SDL_AUDIO_DRIVER_DISK@\n")
        (sdl / "src/audio/SDL_sysaudio.h").write_text("extern AudioBootStrap DISKAUDIO_bootstrap;\n")
        (sdl / "src/audio/SDL_audio.c").write_text("static const AudioBootStrap *const bootstrap[] = {\n};\n")
        (sdl / "CMakeLists.txt").write_text(
            "  if(SDL_DISKAUDIO)\n    set(SDL_AUDIO_DRIVER_DISK 1)\n"
            "    file(GLOB DISKAUDIO_SOURCES ${SDL2_SOURCE_DIR}/src/audio/disk/*.c)\n"
            "    list(APPEND SOURCE_FILES ${DISKAUDIO_SOURCES})\n"
            "    set(HAVE_DISKAUDIO TRUE)\n    set(HAVE_SDL_AUDIO TRUE)\n  endif()\n"
            "  file(GLOB VERSION_SOURCES ${SDL2_SOURCE_DIR}/src/main/windows/*.rc)\n")
        with patch.object(sys, "argv", ["apply.py", "--sdl-dir", str(sdl)]), redirect_stdout(io.StringIO()):
            check("temp_recipe_apply", module.main() == 0)
            first = {p.relative_to(sdl): p.read_bytes() for p in sdl.rglob("*") if p.is_file()}
            check("temp_recipe_reapply", module.main() == 0)
            second = {p.relative_to(sdl): p.read_bytes() for p in sdl.rglob("*") if p.is_file()}
        check("recipe_idempotent", first == second)
        for name in ("a3_audio_live.h", "a3_audio_wire.h", "a3_audio_protocol_c.h"):
            check("copied_" + name, (sdl / "src/audio/a3capture" / name).read_bytes() ==
                  (ROOT / "platform/audio-protocol/include" / name).read_bytes())
    for name, ok in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    passed = sum(ok for _, ok in RESULTS)
    print(f"{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
