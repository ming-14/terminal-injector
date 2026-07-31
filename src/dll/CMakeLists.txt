# injected.dll 共享库
# 详见 docs/phases/01-scaffold.md 4.7 与 docs/phases/03-dll-framework.md
# Phase 5：增加 CursorHooks/BufferHooks/DllRecvLoop
# Phase 12：增加 ProcessHooks（CreateProcess Hook + 子进程注入）
# Phase 6：增加 VtToInputRecord（VT→INPUT_RECORD 翻译）+ InputHooks（输入 API Hook）
# Phase 8：增加 FontHooks（字体缓存）+ WaitHooks（Wait 句柄假映射）
# Phase 9：增加 ProtectionHooks（防越狱）+ HandleRegistry（假句柄管理）
# Phase 10：增加 StatePoller（后台状态轮询线程）+ BatchSender（VtOutput 小包合并）
# Phase 11：增加 Unloader（管道断开/Shutdown 触发主动卸载）
add_library(injected_dll SHARED
    dllmain.cpp
    HookManager.cpp
    LazyInit.cpp
    DllRecvLoop.cpp
    Unloader.cpp
    state/StateSnapshot.cpp
    state/ConsoleState.cpp
    state/VirtualConsoleState.cpp
    state/HandleRegistry.cpp
    state/StatePoller.cpp
    translator/Color.cpp
    translator/ConsoleToVt.cpp
    translator/VtEscape.cpp
    translator/VtToInputRecord.cpp
    hooks/HookCommon.cpp
    hooks/OutputHooks.cpp
    hooks/CursorHooks.cpp
    hooks/BufferHooks.cpp
    hooks/ProcessHooks.cpp
    hooks/InputHooks.cpp
    hooks/ModeHooks.cpp
    hooks/SignalHooks.cpp
    hooks/FontHooks.cpp
    hooks/WaitHooks.cpp
    hooks/ProtectionHooks.cpp
    BatchSender.cpp
    lineedit/LineEditor.cpp
    lineedit/TabCompleter.cpp
    ${CMAKE_SOURCE_DIR}/third_party/wcwidth/wcwidth.c
)
target_include_directories(injected_dll PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_SOURCE_DIR}/third_party/wcwidth
)
target_link_libraries(injected_dll PRIVATE
    terminjector_common
    # MinHook 通过 terminjector_common PUBLIC 传递
)
# 输出名 injected.dll
set_target_properties(injected_dll PROPERTIES
    OUTPUT_NAME "injected"
    # DLL 默认走 CONSOLE 子系统，但 DllMain 不应有控制台
    # 子系统设置仅对 EXE 有效，DLL 不需要
)
# 启用 PDB 生成（cdb 调试器符号解析需要，否则无法 bp/dd 符号）
if(MSVC)
    target_compile_options(injected_dll PRIVATE /Zi)
    target_link_options(injected_dll PRIVATE /DEBUG)
endif()
