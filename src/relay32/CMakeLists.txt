# relay32：32 位中继 DLL（Phase 23）
#
# 为什么需要它：
#   32 位进程（如 C:\Windows\py.exe）无法被 x64 injected.dll 注入 ——
#   跨位数 LoadLibraryW 必然失败 —— 于是它拉起的 64 位孙进程会漏网，
#   输出写进目标自己的 ConHost，WT 里一片空白（本 bug 的现象）。
#   relay32.dll 作为 32 位进程内的"中继"，只负责：
#     捕获 CreateProcessW/A → 强制 CREATE_SUSPENDED → 上报 mediator →
#     等回执（期间子进程保持冻结，注入 race-free）→ ResumeThread
#
# 构建方式：
#   本目标只在 32 位配置下构建（见根 CMakeLists 的位数分派）：
#     cmake -S . -B build32 -A Win32
#   build.ps1 会自动配置/构建它，并把 relay32.dll 拷到 x64 输出目录，
#   因为 injected.dll 需要在同目录找到它（ProcessHooks 按同目录定位）。
#
# 复用说明：
#   - terminjector_common：协议 / 命名管道 / 日志 / RemoteCall
#   - src/dll/RemoteParams.cpp：RemotePipeSetup 导出与 PipeParams 存储。
#     父 DLL 用同一个 RemoteCallExport 给两个 DLL 传参，导出名与参数结构
#     必须完全一致，因此共用同一份源码而非重写。
add_library(relay32 SHARED
    relay_main.cpp
    child_inject.cpp
    relay32.def
    ${CMAKE_SOURCE_DIR}/src/dll/RemoteParams.cpp
)

target_include_directories(relay32 PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_SOURCE_DIR}/src
    ${CMAKE_SOURCE_DIR}/src/common
    ${CMAKE_SOURCE_DIR}/src/dll
)

target_link_libraries(relay32 PRIVATE terminjector_common)

# 输出名 relay32.dll
set_target_properties(relay32 PROPERTIES OUTPUT_NAME "relay32")

# 启用 PDB 生成（与 injected.dll 一致，便于 cdb 调试）
if(MSVC)
    target_compile_options(relay32 PRIVATE /Zi)
    target_link_options(relay32 PRIVATE /DEBUG)
endif()

# ---------------------------------------------------------------------------
# relay32inject.exe：32 位引导注入器
#
# x64 injected.dll 捕获到 32 位子进程时，无法自己注入（跨位数 LoadLibraryW
# 必然失败），于是 fork 本程序代劳 —— 同位数注入 + RemotePipeSetup 下发参数。
# 与 relay32.dll 共用 child_inject.cpp 这份注入原语，避免两条路径走样。
# ---------------------------------------------------------------------------
add_executable(relay32inject
    inject_main.cpp
    child_inject.cpp
)

target_include_directories(relay32inject PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_SOURCE_DIR}/src
    ${CMAKE_SOURCE_DIR}/src/common
    ${CMAKE_SOURCE_DIR}/src/dll
)

target_link_libraries(relay32inject PRIVATE terminjector_common)

set_target_properties(relay32inject PROPERTIES OUTPUT_NAME "relay32inject")

if(MSVC)
    target_compile_options(relay32inject PRIVATE /Zi)
    target_link_options(relay32inject PRIVATE /DEBUG)
endif()
