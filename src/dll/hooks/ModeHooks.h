// 模式类 Console API Hook（Phase 7）
// 详见 docs/phases/07-mode-signal.md 4.1/4.2
//
// 职责：
//   - GetConsoleMode：欺骗程序认为 VT 输出已开启，让它主动发 VT 序列
//   - SetConsoleMode：维护模式状态机，同步给中介，模式切换时清空输入队列
#pragma once

namespace terminjector::hooks {

// 注册 GetConsoleMode + SetConsoleMode Hook
void RegisterModeHooks();

} // namespace terminjector::hooks
