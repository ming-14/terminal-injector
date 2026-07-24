// Phase 10 任务1：后台状态轮询线程
// 详见 docs/phases/10-state-sync-stability.md 4.1
//
// 背景：LazyInit 的 StateSnapshot::Capture 只在主线程采样一次，
//   LazyInit 期间其他线程的 Console 输出（ENSURE_INITIALIZED 在 t_inLazyInit
//   时 pass-through 调 _orig）会改变 ConHost 真实状态，Capture 遗漏这些并发更新。
//   Phase 9 静默模式后 Hook 完全接管，ConHost 不再更新，StatePoller 在
//   LazyInit 完成后立即轮询捕获这些遗漏的差异并同步到 ConsoleState + mediator。
//
// 生命周期：
//   - LazyInit 末尾 Start()，CreateThread 启动 PollLoop
//   - 3 秒后 PollLoop 自动退出
//   - DLL_PROCESS_DETACH 调 Stop() 确保 thread join（避免线程访问已释放资源）
//
// 轮询参数：100ms 间隔，3 秒总时长（约 30 次）
#pragma once

#include <windows.h>
#include <thread>
#include <atomic>

namespace terminjector {

class StatePoller {
public:
    static StatePoller& Instance();

    // 在 LazyInit 末尾调用，启动后台轮询线程
    // 幂等：重复调用不会启动多个线程
    void Start();

    // 在 DLL_PROCESS_DETACH 调用，确保线程退出后再释放 transport/Hook
    // 幂等：未启动或已停止时安全
    void Stop();

private:
    StatePoller() = default;
    ~StatePoller() = default;
    StatePoller(const StatePoller&) = delete;
    StatePoller& operator=(const StatePoller&) = delete;

    // 轮询线程主循环
    void PollLoop();

    std::thread       m_thread;
    std::atomic<bool> m_running{false};

    static constexpr int kPollIntervalMs = 100;   // 100ms 轮询间隔
    static constexpr int kPollDurationMs  = 3000;  // 总轮询 3 秒
};

} // namespace terminjector
