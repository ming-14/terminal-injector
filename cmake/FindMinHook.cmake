# MinHook 查找与集成
# 详见 docs/phases/01-scaffold.md 4.1.4 与 4.2
# MinHook 源码由镜像下载到 third_party/minhook/，作为静态库编译
#
# 32 位构建支持：
#   MinHook 的反汇编器后端与目标位数绑定（hde64.c / hde32.c），
#   原先硬编码 hde64.c，32 位构建必然链接失败。现按 CMAKE_SIZEOF_VOID_P 选择。
#   third_party/minhook 自带 CMakeLists 也是同样选择方式。
#
# 允许调用方覆盖：
#   TERMINJECTOR_ROOT            仓库根目录（默认 CMAKE_SOURCE_DIR）
#   TERMINJECTOR_MINHOOK_TARGET  目标名（默认 minhook）

if(NOT DEFINED TERMINJECTOR_ROOT)
    set(TERMINJECTOR_ROOT "${CMAKE_SOURCE_DIR}")
endif()
if(NOT DEFINED TERMINJECTOR_MINHOOK_TARGET)
    set(TERMINJECTOR_MINHOOK_TARGET "minhook")
endif()

set(TERMINJECTOR_MINHOOK_DIR "${TERMINJECTOR_ROOT}/third_party/minhook")

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

# 按目标位数选择反汇编器后端
if(CMAKE_SIZEOF_VOID_P EQUAL 8)
    set(TERMINJECTOR_MINHOOK_HDE "${TERMINJECTOR_MINHOOK_DIR}/src/hde/hde64.c")
else()
    set(TERMINJECTOR_MINHOOK_HDE "${TERMINJECTOR_MINHOOK_DIR}/src/hde/hde32.c")
endif()
message(STATUS "MinHook arch backend: ${TERMINJECTOR_MINHOOK_HDE}")

# MinHook 静态库目标（在首次引用时创建，避免重复）
if(NOT TARGET ${TERMINJECTOR_MINHOOK_TARGET})
    add_library(${TERMINJECTOR_MINHOOK_TARGET} STATIC
        "${TERMINJECTOR_MINHOOK_DIR}/src/buffer.c"
        "${TERMINJECTOR_MINHOOK_DIR}/src/hook.c"
        "${TERMINJECTOR_MINHOOK_DIR}/src/trampoline.c"
        "${TERMINJECTOR_MINHOOK_HDE}"
    )
    target_include_directories(${TERMINJECTOR_MINHOOK_TARGET} PUBLIC "${MINHOOK_INCLUDE_DIR}")
    # MinHook 是 C 代码，但被 C++ 工程引用，需正确设置
    set_target_properties(${TERMINJECTOR_MINHOOK_TARGET} PROPERTIES
        C_STANDARD 11
        C_STANDARD_REQUIRED ON
        POSITION_INDEPENDENT_CODE ON)
    # 抑制 MinHook 自身的警告（非本项目代码）
    if(MSVC)
        target_compile_options(${TERMINJECTOR_MINHOOK_TARGET} PRIVATE /W0)
    endif()
    message(STATUS "MinHook static library target created (${TERMINJECTOR_MINHOOK_TARGET})")
endif()
