// 光标类 Console API Hook 声明
// 详见 docs/phases/05-cursor-buffer.md 4.2-4.4
//
// Phase 5：拦截光标位置/显隐与屏幕缓冲区信息查询
//   - SetConsoleCursorPosition  → 输出 VT 光标定位 + 更新缓存
//   - GetConsoleScreenBufferInfo → 返回缓存（不调原 API）
//   - Set/GetConsoleCursorInfo  → VT 光标显隐 \x1b[?25h/l + 缓存
#pragma once

#include <windows.h>

namespace terminjector::hooks {

// 注册光标类 Hook（由 DllMain 调用）
void RegisterCursorHooks();

// Phase 10 StatePoller 用：绕过 Hook 调用真实 GetConsoleScreenBufferInfo
// 通过 MinHook orig trampoline 执行原 API，不进入 Detour，拿到 ConHost 真实状态
// 前提：Hook 已安装（orig 非 null），由调用方保证（StatePoller 在 LazyInit 完成后启动）
BOOL CallRealGetConsoleScreenBufferInfo(HANDLE hConsoleOutput,
                                        PCONSOLE_SCREEN_BUFFER_INFO lpInfo);

} // namespace terminjector::hooks
