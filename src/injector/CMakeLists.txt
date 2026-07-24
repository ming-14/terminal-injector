# injector 静态库
# Phase 1：仅含空声明，Phase 2 实现 CreateRemoteThread 注入
# 详见 docs/phases/02-injector-modes.md
add_library(terminjector_injector STATIC
    Injector.cpp
    ProcessHelper.cpp
)
target_include_directories(terminjector_injector PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}
)
target_link_libraries(terminjector_injector PUBLIC terminjector_common)
