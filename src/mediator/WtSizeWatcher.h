// WT 窗口尺寸监听器
// 详见 docs/phases/05-cursor-buffer.md 4.5
//
// 监听 mediator 自身 stdout 的 CONSOLE_SCREEN_BUFFER_INFO 变化
// （WT resize 会导致 stdout 缓冲区/视口尺寸变化）
// 通过回调通知 Mediator，由 Mediator 封装 ResizeNotify 发给 DLL
//
// 策略：100ms 轮询（10fps），简单可靠
// 优化（Phase 10）：改用 ReadConsoleInput 监听 WINDOW_BUFFER_SIZE_EVENT
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
};

} // namespace terminjector
