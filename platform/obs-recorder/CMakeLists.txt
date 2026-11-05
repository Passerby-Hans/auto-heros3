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
