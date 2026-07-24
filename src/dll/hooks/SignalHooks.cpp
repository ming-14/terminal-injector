// Ctrl 信号触发实现（Phase 7）
// 详见 docs/phases/07-mode-signal.md 4.4
//
// TriggerCtrlC 调用 GenerateConsoleCtrlEvent 让 ConHost 触发 CtrlHandler：
//   - ConHost 仍存在（cmd 有控制台），仅 I/O 被 Hook 拦截
//   - 程序注册的 CtrlHandler 在 ConHost 中（不 Hook SetConsoleCtrlHandler）
//   - ConHost 在目标进程中创建新线程调用 CtrlHandler
//   - 不需要本地回调表，不需要独立线程（ConHost 自己创建线程）
//
// 不 Hook SetConsoleCtrlHandler/GenerateConsoleCtrlEvent 的原因：
//   DLL 后注入，程序在 Hook 安装前已调用 SetConsoleCtrlHandler 注册回调，
//   Hook 无法拦截到这些早期注册。改用 GenerateConsoleCtrlEvent 让 ConHost
//   直接触发已注册的回调，无需本地维护回调表
#include "SignalHooks.h"
#include "logging/Logger.h"

#include <windows.h>

namespace terminjector::hooks {

void TriggerCtrlC() {
    LOG_INFO("SignalHooks: TriggerCtrlC via GenerateConsoleCtrlEvent");

    // 调用 GenerateConsoleCtrlEvent 让 ConHost 触发 CtrlHandler
    // CTRL_C_EVENT: 发送 Ctrl+C 信号
    // 0: 发送给调用进程所在进程组的所有进程（含子进程如 ping）
    BOOL ok = GenerateConsoleCtrlEvent(CTRL_C_EVENT, 0);
    if (!ok) {
        LOG_WARN("SignalHooks: GenerateConsoleCtrlEvent failed err=%lu",
                 GetLastError());
    }
}

} // namespace terminjector::hooks
