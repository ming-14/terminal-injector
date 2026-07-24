// 输入类 Console API Hook 声明
// 详见 docs/phases/06-input-chain.md 4.3/4.4/4.6/4.7
//
// Phase 6：拦截输入类 API，从 InputQueue 返回数据（而非真实 ConHost 输入缓冲区）
//   - ReadConsoleInputW/A：阻塞读按键/鼠标事件（核心）
//   - PeekConsoleInputW/A：偷窥队列（不消费）
//   - GetNumberOfConsoleInputEvents：查询队列长度
//   - WriteConsoleInputW/A：程序主动塞事件（入队到我们的队列）
//   - FlushConsoleInputBuffer：清空队列
//   - ReadFile（stdin）：透传模式（ENABLE_VIRTUAL_TERMINAL_INPUT）
#pragma once

namespace terminjector::hooks {

// 注册所有输入类 Hook
void RegisterInputHooks();

// Phase 6：唤醒阻塞在原 ReadConsoleW 的目标线程
// 原因：cmd 在 Hook 安装前已进入 ReadConsoleW 阻塞（等 ConHost 输入），
//       MinHook inline hook 修改函数入口字节，但对已在函数内部阻塞的线程无效。
//       此函数向 ConHost 写一个回车键，让原 ReadConsoleW 返回（空行），
//       目标程序下次调用 ReadConsoleW 时走 Detour。
// 必须在 LazyInit 完成、Hook 已安装后调用
void KickStartBlockedReaders();

} // namespace terminjector::hooks
