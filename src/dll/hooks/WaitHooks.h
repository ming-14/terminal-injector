// Wait 句柄类 Console API Hook 声明（Phase 8）
// 详见 docs/phases/08-advanced-features.md 4.5
//
// 职责：
//   - GetConsoleInputWaitHandle → 返回 InputQueue 的手动重置事件句柄
//
// 设计理由（不 Hook WaitForSingleObject / WaitForMultipleObjects）：
//   InputQueue 事件是真实内核事件，WaitForSingleObject 可直接等待，无需替换。
//   InputQueue 在 Enqueue 时 SetEvent，Dequeue 空时 ResetEvent，
//   事件管理策略（mutex 内 Set/Reset）保证不丢信号。
//
//   文档原方案 Hook WaitFor* 检测"伪句柄"并替换，是因为假设 GetConsoleInputWaitHandle
//   返回魔数伪句柄。本实现直接返回 InputQueue 真实事件，省去中间层，更简洁可靠。
//
//   风险：若程序在 Hook 安装前已缓存旧句柄（ConHost 内核事件），Hook 后仍用旧句柄
//   等待会卡死。实际场景中程序一般在每次等待前重新调用 GetConsoleInputWaitHandle，
//   此风险可接受。如测试发现卡死，再补 WaitFor* Hook 检测旧句柄。
#pragma once

namespace terminjector::hooks {

// 注册 Wait 句柄类 Hook（由 DllMain 调用）
void RegisterWaitHooks();

} // namespace terminjector::hooks
