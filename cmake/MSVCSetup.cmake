# MSVC 工具链配置
# cl.exe 路径优先用 -DTERMINJECTOR_MSVC_BASE=... 显式指定；
# 未指定时用 vswhere 探测 VS 安装目录并取最新 MSVC 版本（不硬编码本机路径）。
# 详见 docs/phases/01-scaffold.md 4.1.2

set(TERMINJECTOR_MSVC_BASE ""
    CACHE PATH "MSVC 工具链根目录（留空时自动 vswhere 探测）")

if(NOT TERMINJECTOR_MSVC_BASE)
    # vswhere：VS 官方安装探测工具，随 VS Installer 分发，路径含空格需引号
    set(_vswhere "$ENV{ProgramFiles\(x86\)}/Microsoft Visual Studio/Installer/vswhere.exe")
    set(_vsroot "")
    if(EXISTS "${_vswhere}")
        execute_process(
            COMMAND "${_vswhere}" -latest -products "*"
                    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64
                    -property installationPath
            OUTPUT_VARIABLE _vsroot
            OUTPUT_STRIP_TRAILING_WHITESPACE
            ERROR_QUIET)
    endif()
    if(_vsroot)
        # VC/Tools/MSVC 下可能并存多个版本目录，按名称排序取最新
        file(GLOB _msvc_versions "${_vsroot}/VC/Tools/MSVC/*")
        list(SORT _msvc_versions)
        list(GET _msvc_versions -1 _latest)
        if(_latest)
            set(TERMINJECTOR_MSVC_BASE "${_latest}")
        endif()
    endif()
endif()

# 校验路径存在
if(NOT EXISTS "${TERMINJECTOR_MSVC_BASE}/bin/Hostx64/x64/cl.exe")
    message(WARNING
        "MSVC cl.exe 未找到: ${TERMINJECTOR_MSVC_BASE}/bin/Hostx64/x64/cl.exe\n"
        "请确认 Visual Studio 已安装 VC++ 工具集，或通过 -DTERMINJECTOR_MSVC_BASE=... 指定。")
endif()

message(STATUS "MSVC base: ${TERMINJECTOR_MSVC_BASE}")