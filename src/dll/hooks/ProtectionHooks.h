// 自保护类 Console API Hook 声明（Phase 9）
// 详见 docs/phases/09-self-protection.md 4.4 与 4.5
//
// 职责：
//   - AllocConsole → 拒绝（防止目标程序脱离中介管道重新绑定 ConHost）
//   - AttachConsole → 拒绝（同上）
//   - FreeConsole → 假装成功但不真断（保持 ConHost 控制台存在，
//     GenerateConsoleCtrlEvent 等仍能工作）
//   - GetConsoleWindow → 返回 NULL（隔离目标程序对 ConHost 原生窗口操作）
//   - CloseHandle → 对假句柄静默返回 TRUE，其他调真实 API
//
// 关键决策（Phase 9 文档 4.3）：
//   - 不 Hook GetStdHandle：让程序拿到真实 Console 句柄，
//     所有读写 API 已 Hook 拦截基于句柄类型而非具体值
//   - 假句柄靠魔数（0xABCD 高位）+ HandleRegistry 真实 fake 集合判断
#pragma once

#include <windows.h>

namespace terminjector::hooks {

// 注册自保护类 Hook（由 DllMain 调用）
void RegisterProtectionHooks();

// DLL 内部模块绕过 Hook 拿真实 ConHost 窗口句柄
// （LazyInit 隐藏窗口 / StateSnapshot 记录可见性 / Unloader 恢复显示）
// 前提：Hook 已安装（orig 非 null），由调用方保证
HWND CallRealGetConsoleWindow();

} // namespace terminjector::hooks
