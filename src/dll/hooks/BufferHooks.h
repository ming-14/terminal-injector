// 缓冲区类 Console API Hook 声明
// 详见 docs/phases/05-cursor-buffer.md 4.7
//
// Phase 5：拦截屏幕缓冲区尺寸/窗口位置 API
//   - SetConsoleScreenBufferSize → 更新缓存 + 调原 API
//   - SetConsoleWindowInfo       → 更新 srWindow 缓存
//   - GetLargestConsoleWindowSize → 返回缓存值
#pragma once

#include <windows.h>

namespace terminjector::hooks {

// 注册缓冲区类 Hook（由 DllMain 调用）
void RegisterBufferHooks();

// 暴露 orig trampoline 给 LazyInit 等模块（绕过 Detour 拿真实行为）
// 用途：LazyInit 注入尺寸对齐需真实修改 ConHost 缓冲/窗口；
//       若直接调 SetConsoleScreenBufferSize/SetConsoleWindowInfo 会进入
//       Detour（只更新缓存不调原 API），真实 ConHost 尺寸不会变化。
BOOL CallRealSetConsoleScreenBufferSize(HANDLE hConsoleOutput, COORD dwSize);
BOOL CallRealSetConsoleWindowInfo(HANDLE hConsoleOutput, BOOL bAbsolute,
                                  const SMALL_RECT* lpConsoleWindow);

} // namespace terminjector::hooks
