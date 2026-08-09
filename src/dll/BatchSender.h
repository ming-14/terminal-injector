// BatchSender：VtOutput 小包合并发送器
// 详见 docs/phases/10-state-sync-stability.md 4.8 IPC 小包合并
//
// 目标：
//   高频小包（如 WriteConsoleW 单字符输出）逐条 Send 系统调用开销大，
//   攒批后合并为单个 VtOutput 消息发送，减少 IPC 次数。
//
// 设计：
//   - VtOutput 走 EnqueueVtOutput：追加到 m_buffer，立即返回（非阻塞）
//   - 后台 flush 线程每 16ms 或被事件唤醒时取出整个 m_buffer，封装为
//     单个 VtOutput 消息发送
//   - 缓冲区达 16KB 立即触发 flush（避免大输出延迟过高）
//   - 控制消息（ChildProcessNotify/ModeChange/CpChange）不走本路径，
//     仍由 SendToMediator 直接发送，保证即时性
//
// 顺序保证：
//   - EnqueueVtOutput 用 SRWLOCK 串行追加，保证入队顺序
//   - FlushLocked 用 swap 取出整个缓冲区，保证发送顺序
//   - 合并后的 VtOutput 包仍走 g_sendLock（与控制消息串行）
//
// 生命周期：
//   - LazyInit 末尾 Init()，启动 flush 线程
//   - DLL_PROCESS_DETACH 调 Shutdown()，flush 线程做最终 flush 后退出
//
// 风险：
//   - 16ms 延迟对交互式输入响应有影响（vim 等 TUI）
//     实测总延迟 16-32ms，< 50ms 标准，可接受
//   - 攒批期间程序崩溃导致数据丢失
//     DLL_PROCESS_DETACH 会调 Shutdown 强制 flush，崩溃场景可接受
#pragma once

#include <windows.h>
#include <atomic>
#include <string>
#include <thread>

namespace terminjector {

class BatchSender {
public:
    static BatchSender& Instance();

    // 在 LazyInit 末尾调用，启动 flush 线程
    // 幂等：重复调用不会启动多个线程
    void Init();

    // 在 DLL_PROCESS_DETACH 调用，确保缓冲区数据发出后再退出
    // 幂等：未启动或已停止时安全
    void Shutdown();

    // 入队 VtOutput 字节，立即返回（线程安全）
    // data/len: VT 字节流（不含协议头，由 flush 时统一封装）
    // recordReplay: 是否计入卸载时 ConHost 重放缓冲。
    //   协议查询（如 LazyInit 的 DSR/DA 校准探针）传 false，只发不收，
    //   避免卸载重放时 ConHost 对查询自答出字面 VT 文本（Phase 22 修复）
    // 返回 true 入队成功；transport 未连接返回 false（调用方走 pass-through）
    bool EnqueueVtOutput(const void* data, size_t len, bool recordReplay = true);

private:
    BatchSender() = default;
    ~BatchSender() = default;
    BatchSender(const BatchSender&) = delete;
    BatchSender& operator=(const BatchSender&) = delete;

    // flush 线程主循环：等待事件或超时，触发 FlushLocked
    void FlushLoop();

    // 取出 m_buffer 全部内容，封装为 VtOutput 消息发送
    // 空缓冲区直接返回，无副作用
    void FlushLocked();

    SRWLOCK        m_bufLock = SRWLOCK_INIT;
    std::string    m_buffer;          // 攒批缓冲（VT 字节流）
    HANDLE         m_flushEvent = nullptr;  // 唤醒 flush 线程的事件
    std::thread    m_flushThread;
    std::atomic<bool> m_running{false};
    std::atomic<bool> m_initialized{false};

    // 16ms flush 间隔（~60fps），平衡延迟与吞吐
    static constexpr int kFlushIntervalMs = 16;
    // 16KB 触发立即 flush，避免大输出（如 tree /f）延迟过高
    static constexpr size_t kFlushMaxBytes = 16 * 1024;
};

} // namespace terminjector
