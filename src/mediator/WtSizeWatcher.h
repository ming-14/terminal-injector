// WT 窗口尺寸监听器
// 详见 docs/phases/05-cursor-buffer.md 4.5 与 docs/phases/10-state-sync-stability.md 4.3
//
// 监听 mediator 自身 stdout 的 CONSOLE_SCREEN_BUFFER_INFO 变化
// （WT resize 会导致 stdout 缓冲区/视口尺寸变化）
// 通过回调通知 Mediator，由 Mediator 封装 ResizeNotify 发给 DLL
//
// 策略（Phase 10 优化）：
//   - 50ms 轮询（20fps），足够响应人手拖动 WT 边框
//   - 仅当 srWindow 的 cols/rows 变化时才触发回调（避免无意义通知）
//   - 不用 ReadConsoleInput(WINDOW_BUFFER_SIZE_EVENT)：mediator stdin 是 ConPTY
//     提供的 VT 字节流，结构化事件不会出现在其中（详见 Phase 10 文档 4.3 分析）
#pragma once

#include <windows.h>
#include <thread>
#include <atomic>
#include <functional>

namespace terminjector {

// WT 窗口尺寸监听线程
class WtSizeWatcher {
public:
    // 尺寸变化回调：cols/rows=可视窗口尺寸，bufCols/bufRows=缓冲区尺寸
    using OnResize = std::function<void(int cols, int rows, int bufCols, int bufRows)>;

    WtSizeWatcher();
    ~WtSizeWatcher();

    WtSizeWatcher(const WtSizeWatcher&) = delete;
    WtSizeWatcher& operator=(const WtSizeWatcher&) = delete;

    // 启动监听线程（不可重复启动）
    void Start(OnResize callback);

    // 停止监听线程（阻塞等待线程退出）
    void Stop();

private:
    // 监听循环主体
    void WatchLoop();

    std::thread       m_thread;
    std::atomic<bool> m_running{false};
    int               m_lastCols = 0;
    int               m_lastRows = 0;
    OnResize          m_callback;

    // 轮询间隔（Phase 10：100ms → 50ms）
    // 50ms = 20fps，人手拖动窗口的响应阈值约 100-200ms，50ms 足够流畅
    static constexpr int kPollIntervalMs = 50;
};

} // namespace terminjector
