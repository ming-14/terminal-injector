# common 静态库
# 包含 logging / transport / protocol / console 四个子模块
# 详见 docs/phases/01-scaffold.md 4.7
add_library(terminjector_common STATIC
    logging/Logger.cpp
    transport/NamedPipeTransport.cpp
    protocol/MessageSerializer.cpp
)

# include 路径：自身目录对内可见（用 #include "logging/Logger.h" 形式）
# 对外可见（其他模块用 #include "logging/Logger.h" 时需 common 目录在路径中）
target_include_directories(terminjector_common PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}
)

# 依赖 MinHook（仅 main.cpp 验证用，common 本身不依赖，但保持链接链）
# PUBLIC 让依赖 terminjector_common 的目标也继承 MinHook include
target_link_libraries(terminjector_common PUBLIC minhook)
