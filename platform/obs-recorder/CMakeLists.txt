cmake_minimum_required(VERSION 3.20)
project(obs-recorder LANGUAGES C CXX)

# S0/S1a host scaffold: MSVC + Windows only.  obs.dll is loaded dynamically at
# runtime, so no OBS SDK headers/import library are required to build this.
if(NOT MSVC)
  message(FATAL_ERROR "obs-recorder targets MSVC on Windows (dynamic obs.dll probe).")
endif()

# Note on MSVC header-dependency tracking:
# CMake/Ninja parse cl.exe /showIncludes output.  On non-English MSVC installs
# that prefix is localized ("注意: 包含文件:" here).  scripts/build.ps1 detects
# the exact prefix and passes -DCMAKE_CL_SHOWINCLUDES_PREFIX=<prefix>; without it
# header edits would not trigger recompiles (stale incremental builds).
if(MSVC AND NOT CMAKE_CL_SHOWINCLUDES_PREFIX)
  message(WARNING
    "CMAKE_CL_SHOWINCLUDES_PREFIX is not set. On a non-English MSVC install "
    "header dependency tracking may be incomplete; prefer scripts/build.ps1.")
endif()

add_executable(obs-recorder
  src/main.cpp
  src/json_util.cpp
  src/win_util.cpp
  src/obs_probe.cpp
  src/ipc_pipe.cpp
)

target_compile_features(obs-recorder PRIVATE cxx_std_17)
target_compile_definitions(obs-recorder PRIVATE
  UNICODE
  _UNICODE
  WIN32_LEAN_AND_MEAN
  NOMINMAX
  _WIN32_WINNT=0x0A00
  OBS_RECORDER_BUILD_TYPE="${CMAKE_BUILD_TYPE}"
)
target_compile_options(obs-recorder PRIVATE /W4 /permissive- /EHsc /utf-8)
target_link_libraries(obs-recorder PRIVATE advapi32 version)

set_target_properties(obs-recorder PROPERTIES
  OUTPUT_NAME "obs-recorder"
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
)

# ---------------------------------------------------------------------------
# A0/A1: wire protocol + absolute QPC scheduling core.
#
# This target is independent of libobs/SDL/OBS and is built by the normal
# media build so the offline contract is reproducible.  It never touches game
# state or a physical audio endpoint.
# ---------------------------------------------------------------------------
add_executable(audio-core-tests
  tests/audio_core_tests.cpp
  tests/audio_wire_c_test.c
  src/audio/audio_clock.cpp
  src/audio/audio_clock_c.c
  src/audio/framed_core.cpp
  ../audio-protocol/src/a3_audio_protocol.cpp
  ../audio-protocol/src/a3_audio_protocol_c.c
)
target_compile_features(audio-core-tests PRIVATE cxx_std_17)
target_compile_definitions(audio-core-tests PRIVATE
  UNICODE
  _UNICODE
  WIN32_LEAN_AND_MEAN
  NOMINMAX
  _WIN32_WINNT=0x0A00
)
target_compile_options(audio-core-tests PRIVATE /W4 /permissive- /EHsc /utf-8)
target_include_directories(audio-core-tests PRIVATE
  "${CMAKE_CURRENT_SOURCE_DIR}/src/audio"
  "${CMAKE_CURRENT_SOURCE_DIR}/../audio-protocol/include"
)
set_target_properties(audio-core-tests PROPERTIES
  OUTPUT_NAME "audio-core-tests"
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
)


# N1-N3: deterministic framed parser/stream/timeline tests (no libobs).
add_executable(framed-core-tests
  tests/framed_core_tests.cpp
  src/audio/framed_core.cpp
  src/audio/audio_clock_c.c
  ../audio-protocol/src/a3_audio_protocol.cpp
  ../audio-protocol/src/a3_audio_protocol_c.c
)
target_compile_features(framed-core-tests PRIVATE cxx_std_17)
target_compile_definitions(framed-core-tests PRIVATE
  UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX _WIN32_WINNT=0x0A00)
target_compile_options(framed-core-tests PRIVATE /W4 /permissive- /EHsc /utf-8)
target_include_directories(framed-core-tests PRIVATE
  "${CMAKE_CURRENT_SOURCE_DIR}/src/audio"
  "${CMAKE_CURRENT_SOURCE_DIR}/../audio-protocol/include")
set_target_properties(framed-core-tests PROPERTIES
  OUTPUT_NAME "framed-core-tests"
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

# Private output stop-fence policy: no libobs, codecs, clock or device access.
add_executable(stop-policy-tests custom-output/tests/test-stop-policy.c)
target_compile_options(stop-policy-tests PRIVATE /W4 /utf-8)
set_target_properties(stop-policy-tests PROPERTIES
  OUTPUT_NAME "stop-policy-tests"
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

add_executable(split-policy-tests custom-output/tests/test-split-policy.c)
target_compile_options(split-policy-tests PRIVATE /W4 /utf-8)
set_target_properties(split-policy-tests PROPERTIES
  OUTPUT_NAME "split-policy-tests"
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

# Shared-memory snapshot/identity policy; no libobs or media.
add_executable(live-audio-policy-tests
  tests/live_audio_policy_tests.cpp
  src/audio/framed_core.cpp src/audio/audio_clock_c.c
  ../audio-protocol/src/a3_audio_protocol.cpp
  ../audio-protocol/src/a3_audio_protocol_c.c)
target_compile_features(live-audio-policy-tests PRIVATE cxx_std_17)
target_compile_definitions(live-audio-policy-tests PRIVATE
  WIN32_LEAN_AND_MEAN NOMINMAX _WIN32_WINNT=0x0A00)
target_compile_options(live-audio-policy-tests PRIVATE /W4 /permissive- /EHsc /utf-8)
target_include_directories(live-audio-policy-tests PRIVATE
  "${CMAKE_CURRENT_SOURCE_DIR}/src/audio"
  "${CMAKE_CURRENT_SOURCE_DIR}/../audio-protocol/include")
set_target_properties(live-audio-policy-tests PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

# First IPC connection ownership; no libobs, media, or Windows process mutation.
add_executable(owner-policy-tests tests/ipc_owner_policy_tests.cpp)
target_compile_features(owner-policy-tests PRIVATE cxx_std_17)
target_compile_options(owner-policy-tests PRIVATE /W4 /permissive- /EHsc /utf-8)
set_target_properties(owner-policy-tests PROPERTIES
  OUTPUT_NAME "owner-policy-tests"
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

# ---------------------------------------------------------------------------
# S1b: libobs media self-test executable (obs-media-selftest.exe)
#
# This target is the first one in this directory that LINKS libobs instead of
# dlopen()ing it.  It is opt-in so the S0/S1a scaffold still builds on hosts
# without the generated OBS SDK import library:
#
#   -DOBS_RECORDER_BUILD_MEDIA_SELFTEST=ON
#   -DOBS_SDK_LIB=<path to obs.lib generated from the installed obs.dll>
#   -DOBS_SDK_GENERATED_DIR=<dir containing the generated obsconfig.h>
#   -DOBS_SOURCE_LIBOBS_DIR=<OBS 32.1.0 source libobs header dir>
#   -DOBS_RUNTIME_BIN_DIR=<installed OBS bin\64bit directory>
#
# scripts/build-media-selftest.ps1 generates obs.lib/obsconfig.h from the
# installed OBS and passes all of the above.  Nothing generated is committed:
# everything lands in the (git-ignored) build directory.
# ---------------------------------------------------------------------------
option(OBS_RECORDER_BUILD_MEDIA_SELFTEST
  "Build obs-media-selftest.exe (links libobs, records in-memory A/V to split MP4)" OFF)

if(OBS_RECORDER_BUILD_MEDIA_SELFTEST)
  if(NOT OBS_SDK_LIB)
    message(FATAL_ERROR "OBS_SDK_LIB (generated obs.lib path) is required.")
  endif()
  if(NOT OBS_SDK_GENERATED_DIR)
    message(FATAL_ERROR "OBS_SDK_GENERATED_DIR (generated obsconfig.h dir) is required.")
  endif()
  if(NOT OBS_SOURCE_LIBOBS_DIR)
    set(OBS_SOURCE_LIBOBS_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../../vendor/obs-studio/libobs")
  endif()
  if(NOT EXISTS "${OBS_SOURCE_LIBOBS_DIR}/obs.h")
    message(FATAL_ERROR
      "OBS source headers not found at '${OBS_SOURCE_LIBOBS_DIR}'. "
      "Clone OBS 32.1.0 into vendor/obs-studio or pass -DOBS_SOURCE_LIBOBS_DIR.")
  endif()
  if(NOT OBS_RUNTIME_BIN_DIR)
    set(OBS_RUNTIME_BIN_DIR "C:/Program Files/obs-studio/bin/64bit")
  endif()

  add_executable(obs-media-selftest
    src/media/media_selftest.cpp
    src/media/gen_av_source.cpp
    src/media/raw_audio_source.cpp
    src/media/framed_audio_source.cpp
    src/audio/framed_core.cpp
    src/audio/audio_clock_c.c
    ../audio-protocol/src/a3_audio_protocol.cpp
    ../audio-protocol/src/a3_audio_protocol_c.c
    src/media/panel_source.cpp
    src/json_util.cpp
  )
  target_compile_features(obs-media-selftest PRIVATE cxx_std_17)
  target_compile_definitions(obs-media-selftest PRIVATE
    UNICODE
    _UNICODE
    WIN32_LEAN_AND_MEAN
    NOMINMAX
    _WIN32_WINNT=0x0A00
  )
  target_compile_options(obs-media-selftest PRIVATE /W3 /permissive- /EHsc /utf-8)
  target_include_directories(obs-media-selftest PRIVATE
    "${OBS_SOURCE_LIBOBS_DIR}"
    "${OBS_SDK_GENERATED_DIR}"
    "${CMAKE_CURRENT_SOURCE_DIR}/src/audio"
    "${CMAKE_CURRENT_SOURCE_DIR}/../audio-protocol/include"
  )
  target_link_libraries(obs-media-selftest PRIVATE
    "${OBS_SDK_LIB}"
    advapi32
    avrt
    gdi32
    version
  )
  set_target_properties(obs-media-selftest PROPERTIES
    OUTPUT_NAME "obs-media-selftest"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
  )

  # libobs itself is linked through the generated import library, so obs.dll
  # must be resolvable when the process starts.  The plugin modules are loaded
  # from the installed OBS tree by absolute path, but their dependencies
  # (avcodec/swscale/w32-pthreads/libx264/...) are resolved from the
  # application directory, so stage the required runtime DLLs next to the exe.
  # This is local build output only (git-ignored), never committed.
  set(OBS_MEDIA_RUNTIME_DLLS
    obs.dll
    libobs-winrt.dll
    libcurl.dll
    avcodec-61.dll
    avformat-61.dll
    avdevice-61.dll
    avfilter-10.dll
    avutil-59.dll
    swscale-8.dll
    swresample-5.dll
    libx264-164.dll
    librist.dll
    srt.dll
    w32-pthreads.dll
    zlib.dll
  )
  foreach(dll IN LISTS OBS_MEDIA_RUNTIME_DLLS)
    if(NOT EXISTS "${OBS_RUNTIME_BIN_DIR}/${dll}")
      message(FATAL_ERROR "Required OBS runtime DLL missing: ${OBS_RUNTIME_BIN_DIR}/${dll}")
    endif()
    add_custom_command(TARGET obs-media-selftest POST_BUILD
      COMMAND "${CMAKE_COMMAND}" -E copy_if_different
        "${OBS_RUNTIME_BIN_DIR}/${dll}" "$<TARGET_FILE_DIR:obs-media-selftest>/${dll}"
      COMMENT "Staging OBS runtime ${dll}"
    )
  endforeach()

  # Product-facing executable name.  Keep obs-media-selftest.exe as the
  # compatibility/test name (existing smoke scripts and historical evidence use
  # it); the final recorder CLI is the same binary under the product name.
  add_custom_command(TARGET obs-media-selftest POST_BUILD
    COMMAND "${CMAKE_COMMAND}" -E copy_if_different
      "$<TARGET_FILE:obs-media-selftest>"
      "$<TARGET_FILE_DIR:obs-media-selftest>/auto-heros3-recorder.exe"
    COMMENT "Publishing auto-heros3-recorder.exe"
  )

  # Source-only native smoke: no output, no recording, no media decode.
  add_executable(framed-source-smoke
    tests/framed_source_smoke.cpp
    src/media/framed_audio_source.cpp
    src/audio/framed_core.cpp
    src/audio/audio_clock_c.c
    ../audio-protocol/src/a3_audio_protocol.cpp
    ../audio-protocol/src/a3_audio_protocol_c.c
  )
  target_compile_features(framed-source-smoke PRIVATE cxx_std_17)
  target_compile_definitions(framed-source-smoke PRIVATE
    UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX _WIN32_WINNT=0x0A00)
  target_compile_options(framed-source-smoke PRIVATE /W3 /permissive- /EHsc /utf-8)
  target_include_directories(framed-source-smoke PRIVATE
    "${OBS_SOURCE_LIBOBS_DIR}" "${OBS_SDK_GENERATED_DIR}"
    "${CMAKE_CURRENT_SOURCE_DIR}/src/audio"
    "${CMAKE_CURRENT_SOURCE_DIR}/src/media"
    "${CMAKE_CURRENT_SOURCE_DIR}/../audio-protocol/include")
  target_link_libraries(framed-source-smoke PRIVATE "${OBS_SDK_LIB}" advapi32 avrt gdi32 version)
  set_target_properties(framed-source-smoke PROPERTIES
    OUTPUT_NAME "framed-source-smoke"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  foreach(dll IN LISTS OBS_MEDIA_RUNTIME_DLLS)
    add_custom_command(TARGET framed-source-smoke POST_BUILD
      COMMAND "${CMAKE_COMMAND}" -E copy_if_different
        "${OBS_RUNTIME_BIN_DIR}/${dll}" "$<TARGET_FILE_DIR:framed-source-smoke>/${dll}")
  endforeach()

endif()
