// Wait 句柄类 Console API Hook 声明（Phase 8 + Textual 退出卡死修复）
// 详见 docs/phases/08-advanced-features.md 4.5
//
// 职责：
//   - GetConsoleInputWaitHandle → 返回 InputQueue 的手动重置事件句柄
//     （覆盖 "GetConsoleInputWaitHandle → WaitForSingleObject → ReadConsoleInput" 模式）
//   - WaitForMultipleObjects / WaitForMultipleObjectsEx → 句柄组中的输入句柄
//     替换为 InputQueue 事件
//     （覆盖 "直接等 stdin 句柄" 模式，如 Textual win32 driver 的
//       wait_for_handles([hIn], 100) 轮询；无 GetConsoleInputWaitHandle 依赖）
//
// 设计理由（不 Hook WaitForSingleObject）：
//   InputQueue 事件是真实内核事件，WaitForSingleObject 可直接等待，无需替换。
//   InputQueue 在 Enqueue 时 SetEvent，Dequeue 空时 ResetEvent，
//   事件管理策略（mutex 内 Set/Reset）保证不丢信号。
//   WaitForMultipleObjects* 需要替换是因为调用方直接持有输入句柄等待，
//   该句柄（ConHost）的 signal 状态与实际输入来源（InputQueue）脱节。
#pragma once

namespace terminjector::hooks {

// 注册 Wait 句柄类 Hook（由 DllMain 调用）
void RegisterWaitHooks();

} // namespace terminjector::hooks
