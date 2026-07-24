# 编译选项配置
# 详见 docs/phases/01-scaffold.md 4.1.3

# 通用编译选项（MSVC）
if(MSVC)
    add_compile_options(
        /W4              # 高警告级别
        /utf-8           # 源码与执行字符集 UTF-8（中文注释/字符串必需）
        /permissive-     # 严格标准
        /Zc:__cplusplus  # 正确上报 __cplusplus 宏
        /EHsc            # C++ 异常
        /Wv:18           # 仅用 VS18 支持的 API
        /nologo          # 抑制 cl 启动横幅
    )

    # Debug/Release 差异
    if(CMAKE_BUILD_TYPE STREQUAL "Debug")
        add_compile_options(/Zi /Od /MDd /RTC1)
        add_compile_definitions(_DEBUG TERMINJECTOR_DEBUG=1)
    else()
        add_compile_options(/O2 /MD /GS)
        add_compile_definitions(NDEBUG)
    endif()
endif()

# 链接选项
add_link_options(/SUBSYSTEM:CONSOLE /nologo)

# DLL 模块标记（在 add_library DLL 时由各自 CMakeLists 补 /SUBSYSTEM:WINDOWS）
