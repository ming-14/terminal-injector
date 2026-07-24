// Ctrl 信号触发（Phase 7）
// 详见 docs/phases/07-mode-signal.md 4.4
//
// 信号链路：
//   WT 按 Ctrl+C → VT \x03 → mediator → DllRecvLoop → TriggerCtrlC
//   → GenerateConsoleCtrlEvent(CTRL_C_EVENT, 0) 让 ConHost 触发 CtrlHandler
//
// 设计说明：
//   DLL 后注入，程序的 SetConsoleCtrlHandler 调用发生在 Hook 安装前，
//   无法通过 Hook 拦截获取回调。改为调用 GenerateConsoleCtrlEvent 让
//   ConHost 触发已注册的 CtrlHandler（ConHost 仍存在，仅 I/O 被 Hook 拦截）
#pragma once
namespace terminjector::hooks {
// 触发 Ctrl+C 信号（供 DllRecvLoop 收到 \x03 时调用）
void TriggerCtrlC();
}
