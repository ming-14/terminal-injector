// Console 结构体与类型定义
// 详见 docs/phases/01-scaffold.md 4.6.1
//
// Windows SDK 的 Console 结构体定义已正确，这里主要是：
//   - 集中引用，避免散落
//   - 添加 #pragma pack 保险（虽然 SDK 定义本身已对齐）
//   - 提供便捷访问方法
#pragma once

#include <windows.h>

namespace terminjector::console {

// 便捷封装：在系统结构体上添加访问方法
// 注意：内存布局与系统结构体完全一致，可安全 reinterpret_cast
struct ConsoleScreenBufferInfo : public CONSOLE_SCREEN_BUFFER_INFO {
    SHORT Cols()      const { return dwSize.X; }
    SHORT Rows()      const { return dwSize.Y; }
    SHORT WinWidth()  const { return srWindow.Right  - srWindow.Left + 1; }
    SHORT WinHeight() const { return srWindow.Bottom - srWindow.Top  + 1; }
};

static_assert(sizeof(ConsoleScreenBufferInfo) == sizeof(CONSOLE_SCREEN_BUFFER_INFO),
              "ConsoleScreenBufferInfo 布局必须与系统结构体一致");

} // namespace terminjector::console
