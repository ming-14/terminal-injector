// DLL 侧后台接收线程
// 详见 docs/phases/05-cursor-buffer.md 4.6
//
// 职责：阻塞接收 mediator 发来的控制流消息
//   - Phase 5：ResizeNotify（WT 窗口 resize 通知）→ 更新 ConsoleState
//   - Phase 6：VtInput（WT 按键）→ 写入目标进程 CONIN$
//   - Phase 10：Ping（心跳）→ 回 Pong
//   - Phase 11：Shutdown → 卸载 Hook
//
// 生命周期：
//   - LazyInit 握手成功后 StartDllRecvLoop
//   - DLL_PROCESS_DETACH 时 StopDllRecvLoop
#pragma once

namespace terminjector {

// 启动 DLL 侧后台接收线程（懒加载握手完成后调用）
// 幂等：重复调用不会启动多个线程
void StartDllRecvLoop();

// 停止 DLL 侧后台接收线程（DLL_PROCESS_DETACH 调用）
// 阻塞等待线程退出（实际依赖 pipe 断开唤醒）
void StopDllRecvLoop();

} // namespace terminjector
