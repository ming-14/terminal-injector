# MinHook 查找与集成
# 详见 docs/phases/01-scaffold.md 4.1.4 与 4.2
# MinHook 源码由镜像下载到 third_party/minhook/，作为静态库编译

set(TERMINJECTOR_MINHOOK_DIR "${CMAKE_SOURCE_DIR}/third_party/minhook")

find_path(MINHOOK_INCLUDE_DIR
    NAMES MinHook.h
    PATHS "${TERMINJECTOR_MINHOOK_DIR}/include"
    NO_DEFAULT_PATH)

if(NOT MINHOOK_INCLUDE_DIR)
    message(FATAL_ERROR
        "MinHook 未找到于 ${TERMINJECTOR_MINHOOK_DIR}\n"
        "请按 docs/phases/01-scaffold.md 4.2 节镜像下载：\n"
        "  $env:GIT_CONFIG_COUNT='1'\n"
        "  $env:GIT_CONFIG_KEY_0='url.https://v4.gh-proxy.org/https://github.com/.insteadOf'\n"
        "  $env:GIT_CONFIG_VALUE_0='https://github.com/'\n"
        "  git clone --depth 1 https://github.com/TsudaKageyu/minhook.git third_party/minhook")
endif()

message(STATUS "MinHook include: ${MINHOOK_INCLUDE_DIR}")

# MinHook 静态库目标（在首次引用时创建，避免重复）
if(NOT TARGET minhook)
    add_library(minhook STATIC
        "${TERMINJECTOR_MINHOOK_DIR}/src/buffer.c"
        "${TERMINJECTOR_MINHOOK_DIR}/src/hook.c"
        "${TERMINJECTOR_MINHOOK_DIR}/src/trampoline.c"
        "${TERMINJECTOR_MINHOOK_DIR}/src/hde/hde64.c"
    )
    target_include_directories(minhook PUBLIC "${MINHOOK_INCLUDE_DIR}")
    # MinHook 是 C 代码，但被 C++ 工程引用，需正确设置
    set_target_properties(minhook PROPERTIES
        C_STANDARD 11
        C_STANDARD_REQUIRED ON
        POSITION_INDEPENDENT_CODE ON)
    # 抑制 MinHook 自身的警告（非本项目代码）
    if(MSVC)
        target_compile_options(minhook PRIVATE /W0)
    endif()
    message(STATUS "MinHook static library target created")
endif()
