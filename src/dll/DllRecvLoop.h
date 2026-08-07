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

// 请求停止接收线程：置 g_recvRunning=false
// 线程在下次 while 检查退出（Sleep 轮询，最多 10ms；若阻塞在 ReadFile，
// 需先断管道让 I/O 失败返回）。不处理线程对象，由调用方负责
// JoinDllRecvLoop（主动卸载）或 DetachDllRecvLoop（DLL_PROCESS_DETACH）。
// 幂等：重复调用安全
void StopDllRecvLoop();

// 等待接收线程退出并回收 std::thread 对象
// 仅在线程已停止（或即将退出）时调用，避免无限等待；
// 主动卸载路径在 ReleaseMediatorTransport 之后调用（管道已断，线程必然退出）
void JoinDllRecvLoop();

// 分离接收线程对象（DLL_PROCESS_DETACH 使用）
// DllMain 上下文持有 Loader Lock，不能 join（可能死锁），只能 detach
void DetachDllRecvLoop();

} // namespace terminjector
