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
#
# 按目标位数分派：
#   x64  → 主产物（terminal_injector.exe / injected.dll / mediator …）
#   Win32 → 只构建 32 位中继 DLL（relay32）
#
# 为什么需要两个构建树：
#   中继 DLL 必须与它要注入的 32 位进程（如 C:\Windows\py.exe）同位数，
#   而主产物是 x64。一个 CMake 构建树的平台固定，无法同时产出两种位数，
#   故用两份配置：
#     cmake -S . -B build   -A x64     # 主产物
#     cmake -S . -B build32 -A Win32   # relay32
#   build.ps1 会自动跑这两步，并把 relay32.dll 拷到 x64 输出目录。
#
# TODO(32bit-target)：让 32 位程序本身也能作为注入目标（injected.dll 出 32 位
#   版本，hook/translator/state/lineedit 全栈需在 WOW64 下验证）。当前未实现，
#   见 docs/2026-09-23-child-injection-32bit-report.md。
if(CMAKE_SIZEOF_VOID_P EQUAL 8)
    add_subdirectory(src/common)
    add_subdirectory(src/injector)
    add_subdirectory(src/mediator)
    add_subdirectory(src/dll)
    add_subdirectory(src/app)
else()
    message(STATUS "32-bit configuration: building relay32 only")
    add_subdirectory(src/common)
    add_subdirectory(src/relay32)
endif()

message(STATUS "=== terminal-injector configured ===")
message(STATUS "Build type: ${CMAKE_BUILD_TYPE}")
message(STATUS "Architecture: ${CMAKE_GENERATOR_PLATFORM}")
message(STATUS "Pointer size: ${CMAKE_SIZEOF_VOID_P} bytes")
