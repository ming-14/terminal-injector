// StatePoller 实现：注入后轮询真实 ConHost 状态补全快照
// 详见 docs/phases/10-state-sync-stability.md 4.1
//
// 流程：
//   1. LazyInit 末尾 Start() → CreateThread 启动 PollLoop
//   2. PollLoop 每 100ms 调 CallRealGetConsoleScreenBufferInfo（orig trampoline）
//      拿 ConHost 真实状态，绕过自己的 GetConsoleScreenBufferInfo Hook
//   3. 比较 cursor 与 ConsoleState 缓存，若有差异：
//      a. 更新 ConsoleState 缓存（SetCursorPosition）
//      b. 发 VT CursorPosition 给 mediator，让 WT 光标同步
//   4. 3 秒后退出循环，线程结束
//
// 关键决策：
//   - 只同步 cursor：Phase 9 隐藏原 cmd 窗口后 srWindow/dwSize 不会变
//     （用户拖不动隐藏窗口），无需比较。cursor 是唯一可能因并发输出变化的字段
//   - 用 orig trampoline 而非直接调 API：Hook 已装，直接调会进入 Detour
//     返回缓存，拿不到 ConHost 真实值
//   - SendToMediator 线程安全，可在轮询线程直接调用（Phase 10 任务5 将重构其锁）
//
// BUG-013 扩展（2026-08-19）：ConPTY 场景（目标跑在 WT 内）。
//   目标 ConPTY 尺寸由 WT 掌控（缓冲=窗口），且与注入窗口（mediator 所在
//   WT 窗口）相互独立——mediator 的 ResizeNotify 携带的是注入窗口尺寸，对
//   ConPTY 目标无效（DllRecvLoop 已跳过其尺寸应用）。此处改为 ConPTY 场景
//   持续轮询真实 ConPTY 尺寸，检测变化时：
//     a. ApplyWtResize 更新 VirtualConsoleState（GCSBI Hook 返回新尺寸）
//     b. EnqueueResizeEvent 注入 WINDOW_BUFFER_SIZE_EVENT（目标 TUI 重排）
//   ConPTY 场景不设时长上限（直到 Stop），轮询间隔放宽到 500ms 足够
//   （WT 窗口拖动是秒级操作）。
#include "StatePoller.h"
#include "ConsoleState.h"
#include "VirtualConsoleState.h"
#include "InputQueue.h"
#include "../hooks/HookCommon.h"
#include "../hooks/CursorHooks.h"
#include "../translator/VtEscape.h"
#include "logging/Logger.h"

#include <windows.h>

namespace terminjector {

// Meyers's Singleton：C++11 起局部 static 初始化线程安全
StatePoller& StatePoller::Instance() {
    static StatePoller instance;
    return instance;
}

void StatePoller::Start() {
    // exchange 返回旧值：若原为 true 表示已在运行，直接返回
    // 若原为 false，将其置 true 表示开始运行
    if (m_running.exchange(true)) {
        LOG_WARN("StatePoller::Start: already running, skip");
        return;
    }
    m_thread = std::thread(&StatePoller::PollLoop, this);
    LOG_INFO("StatePoller started, interval=%dms duration=%dms conpty=%d",
             kPollIntervalMs, kPollDurationMs,
             hooks::IsConPtyConsole() ? 1 : 0);
}

void StatePoller::Stop() {
    // 无条件清运行标志：PollLoop 3 秒后自然退出时已把 m_running 置 false，
    // 若用 m_running 判断是否 join，会因"已停止"而跳过 join → 线程对象残留
    // joinable → DLL_PROCESS_DETACH 时 atexit 析构单例的 std::thread 触发
    // std::terminate → abort (c0000409)。必须只按 joinable() 判断。
    m_running = false;
    if (m_thread.joinable()) {
        m_thread.join();
    }
    LOG_INFO("StatePoller stopped");
}

void StatePoller::PollLoop() {
    const auto startTick = GetTickCount64();
    // GetStdHandle 不被 Hook（Phase 9 决定），返回真实 Console 输出句柄
    // 窗口隐藏不影响句柄有效性，ConHost 仍存在
    const HANDLE hOut = GetStdHandle(STD_OUTPUT_HANDLE);
    // ConPTY 场景：持续轮询真实 ConPTY 尺寸（由 WT 掌控，非本 DLL 可改）
    const bool isConPty = hooks::IsConPtyConsole();

    // ConPTY 尺寸缓存：上次轮询看到的真实 ConPTY 窗口尺寸（0 = 未初始化）
    SHORT lastCols = 0, lastRows = 0;

    int pollCount = 0;
    int diffCount = 0;

    while (m_running) {
        ++pollCount;

        CONSOLE_SCREEN_BUFFER_INFO info{};
        if (hooks::CallRealGetConsoleScreenBufferInfo(hOut, &info)) {
            auto& state = ConsoleState::Instance();
            COORD cached = state.GetCursorPosition();
            // ConHost 真实 cursor 与缓存不一致：记录差异，但不同步
            //
            // Phase 10 修正：原实现同步 ConHost 光标到 ConsoleState + 发 VT，
            // 但这会破坏 LazyInit 的 prompt 覆盖策略：
            //   - LazyInit 补发屏幕内容后，ConsoleState 光标设为行首 (0, Y)
            //   - cmd 被 KickStart 唤醒后从行首写 prompt，覆盖补发的 prompt
            //   - 若 StatePoller 在 cmd 输出前把光标同步到 ConHost (41, Y)，
            //     cmd 会从 (41, Y) 开始写 prompt，导致 prompt 叠加重复
            //   - cmd 输出后 AdvanceCursor 自然更新 ConsoleState，无需 StatePoller 干预
            //
            // StatePoller 现仅观察记录，用于诊断 ConHost 与缓存的光标偏差
            if (info.dwCursorPosition.X != cached.X ||
                info.dwCursorPosition.Y != cached.Y) {
                ++diffCount;
                LOG_INFO("StatePoller: cursor diff ConHost(%d,%d) cache(%d,%d), not syncing (let AdvanceCursor handle)",
                         info.dwCursorPosition.X, info.dwCursorPosition.Y,
                         cached.X, cached.Y);
            }

            // ConPTY 场景：检测真实 ConPTY 尺寸变化（用户拖动 WT 窗口）
            if (isConPty) {
                const SHORT cols = static_cast<SHORT>(
                    info.srWindow.Right - info.srWindow.Left + 1);
                const SHORT rows = static_cast<SHORT>(
                    info.srWindow.Bottom - info.srWindow.Top + 1);
                if (lastCols == 0 && lastRows == 0) {
                    // 首次采样：仅记录基线（VirtualConsoleState 在 LazyInit
                    // 已按真实 ConPTY 尺寸初始化，无需再 Apply）
                    lastCols = cols;
                    lastRows = rows;
                } else if (cols != lastCols || rows != lastRows) {
                    lastCols = cols;
                    lastRows = rows;
                    LOG_INFO("StatePoller(ConPTY): real ConPTY size changed to %dx%d, "
                             "applying to virtual state + resize event",
                             cols, rows);
                    // 同步 VirtualConsoleState：GCSBI Hook 返回新尺寸，目标 TUI
                    // 的 _poll_window_size 轮询也能看到（双重感知）
                    VirtualConsoleState::Instance().ApplyWtResize(cols, rows);
                    // 注入 WINDOW_BUFFER_SIZE_EVENT：目标被阻塞在 ReadConsoleInput
                    // （如 winui 的事件循环）时必须靠事件唤醒重排
                    InputQueue::Instance().EnqueueResizeEvent(cols, rows);
                    InputQueue::Instance().SignalDataReady();
                }
            }
        }

        // ConPTY 场景不退出（直到 Stop，由 Unloader 步骤 1 调用）
        if (!isConPty && GetTickCount64() - startTick >= kPollDurationMs) {
            break;
        }
        Sleep(kPollIntervalMs);
    }

    m_running = false;
    LOG_INFO("StatePoller loop done, polls=%d diffs=%d duration=%llums conpty=%d",
             pollCount, diffCount, GetTickCount64() - startTick, isConPty ? 1 : 0);
}

} // namespace terminjector