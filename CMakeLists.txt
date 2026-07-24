# terminal-injector 顶层 CMake 配置
# 详见 docs/phases/00-overview.md 与 01-scaffold.md
cmake_minimum_required(VERSION 3.20)

project(terminal_injector
    VERSION 0.1.0
    DESCRIPTION "DLL 注入式终端劫持器，将已运行的控制台程序接管到 Windows Terminal"
    LANGUAGES C CXX)

# C++ 标准
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# 默认 Release，开发时可 -DCMAKE_BUILD_TYPE=Debug
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

# 加载工具链与编译选项模块
list(APPEND CMAKE_MODULE_PATH "${CMAKE_SOURCE_DIR}/cmake")
include(MSVCSetup)
include(CompilerFlags)
include(FindMinHook)

# 输出目录统一（便于运行时找到 exe/dll）
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)
set(CMAKE_LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)
set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/lib)

# 子模块
add_subdirectory(src/common)
add_subdirectory(src/injector)
add_subdirectory(src/mediator)
add_subdirectory(src/dll)
add_subdirectory(src/app)

message(STATUS "=== terminal-injector configured ===")
message(STATUS "Build type: ${CMAKE_BUILD_TYPE}")
message(STATUS "Architecture: ${CMAKE_GENERATOR_PLATFORM}")
