cmake_minimum_required(VERSION 3.20)
project(obs-recorder LANGUAGES CXX)

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
  )
  target_link_libraries(obs-media-selftest PRIVATE
    "${OBS_SDK_LIB}"
    advapi32
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
endif()
