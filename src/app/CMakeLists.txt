# terminal_injector 可执行
# 详见 docs/phases/01-scaffold.md 4.7 与 4.8
add_executable(terminal_injector main.cpp)
# main.cpp 用 #include "injector/Injector.h" / "mediator/Mediator.h" 形式，
# 需将 src 根目录加入 include 路径
target_include_directories(terminal_injector PRIVATE
    ${PROJECT_SOURCE_DIR}/src
)
target_link_libraries(terminal_injector PRIVATE
    terminjector_common
    terminjector_injector
    terminjector_mediator
    # MinHook 通过 terminjector_common PUBLIC 传递
)
