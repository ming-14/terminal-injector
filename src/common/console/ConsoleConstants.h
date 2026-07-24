// Console 常量与模式标志
// 详见 docs/phases/01-scaffold.md 4.6.2
#pragma once

#include <windows.h>

namespace terminjector::console {

// 默认输入模式（cmd/powershell 启动时的典型值）
constexpr DWORD kInputModeDefault =
    ENABLE_ECHO_INPUT | ENABLE_LINE_INPUT | ENABLE_PROCESSED_INPUT |
    ENABLE_EXTENDED_FLAGS | ENABLE_INSERT_MODE | ENABLE_QUICK_EDIT_MODE;

// 启用 VT 处理的输出模式（本项目目标状态）
constexpr DWORD kOutputModeVt =
    ENABLE_PROCESSED_OUTPUT | ENABLE_WRAP_AT_EOL_OUTPUT |
    ENABLE_VIRTUAL_TERMINAL_PROCESSING;

// 代码页常量
constexpr UINT kCpUtf8   = 65001;
constexpr UINT kCpGbk    = 936;
constexpr UINT kCpDefault = kCpUtf8;

// 默认文本属性（白字黑底）
constexpr WORD kDefaultAttribute = 0x07;

} // namespace terminjector::console
