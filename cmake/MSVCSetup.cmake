# MSVC 工具链配置
# cl.exe 路径由 AGENTS.md 指定：VS18 Community / MSVC 14.51.36231
# 详见 docs/phases/01-scaffold.md 4.1.2

set(TERMINJECTOR_MSVC_BASE
    "C:/Program Files/Microsoft Visual Studio/18/Community/VC/Tools/MSVC/14.51.36231"
    CACHE PATH "MSVC 14.51 工具链根目录")

# 校验路径存在
if(NOT EXISTS "${TERMINJECTOR_MSVC_BASE}/bin/Hostx64/x64/cl.exe")
    message(WARNING
        "MSVC cl.exe 未在预期路径找到: ${TERMINJECTOR_MSVC_BASE}/bin/Hostx64/x64/cl.exe\n"
        "请确认 Visual Studio 18 Community 已安装，或通过 -DTERMINJECTOR_MSVC_BASE=... 指定。")
endif()

message(STATUS "MSVC base: ${TERMINJECTOR_MSVC_BASE}")

# 若 CMake 未自动检测到编译器，提示用户使用 Developer Command Prompt
# 通常用 -G "Visual Studio 18 2022" -A x64 时 CMake 会自动定位
