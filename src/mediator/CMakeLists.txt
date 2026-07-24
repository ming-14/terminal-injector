# mediator 静态库
# Phase 1：仅含空声明，Phase 2 实现管道服务端 + 双向转发
# Phase 5：增加 WtSizeWatcher 监听 WT resize
# Phase 12：增加 ChildSession（子进程管道实例 + 接收线程）
# 详见 docs/phases/02-injector-modes.md
add_library(terminjector_mediator STATIC
    Mediator.cpp
    VtPassThrough.cpp
    WtSizeWatcher.cpp
    ChildSession.cpp
    InputRecordToVt.cpp
)
target_include_directories(terminjector_mediator PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}
)
target_link_libraries(terminjector_mediator PUBLIC terminjector_common)
