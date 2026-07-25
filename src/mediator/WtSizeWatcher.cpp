// WtSizeWatcher 实现：WT 窗口尺寸监听线程
// 详见 docs/phases/05-cursor-buffer.md 4.5 与 docs/phases/10-state-sync-stability.md 4.3
//
// 流程：
//   - Start 启动后台线程，每 50ms 读取自身 stdout 的 CONSOLE_SCREEN_BUFFER_INFO
//   - srWindow 的 cols/rows 变化时（用户拖动 WT 边框）触发回调
//   - Stop 设置 m_running=false 并 join
//
// 注意：mediator 的 stdout 由 WT 的 ConPTY 提供，WT 窗口 resize 会同步更新
//       此 stdout 的 srWindow，故轮询它即可感知 WT 尺寸变化
//
// Phase 10 优化：
//   - 轮询间隔 100ms → 50ms（20fps，更跟手）
//   - 仅当 srWindow 的 cols/rows 变化时才通知（原有逻辑，确认无回归）
//   - 不检测 dwSize 单独变化：ConPTY 下 dwSize 与 srWindow 通常同步变化，
//     且 DLL 侧 ResizeNotify 处理会同时更新 dwSize 和 srWindow（见 DllRecvLoop.cpp）
#include "WtSizeWatcher.h"
#include "logging/Logger.h"

#include <windows.h>

namespace terminjector {

WtSizeWatcher::WtSizeWatcher() = default;

WtSizeWatcher::~WtSizeWatcher() {
    Stop();
}

void WtSizeWatcher::Start(OnResize callback) {
    if (m_running.load()) return;  // 已在运行
    m_callback = std::move(callback);
    m_running.store(true);
    m_thread = std::thread([this]() { WatchLoop(); });
    LOG_INFO("WtSizeWatcher started, interval=%dms", kPollIntervalMs);
}

void WtSizeWatcher::Stop() {
    if (!m_running.load()) return;
    m_running.store(false);
    if (m_thread.joinable()) m_thread.join();
    LOG_INFO("WtSizeWatcher stopped");
}

void WtSizeWatcher::WatchLoop() {
    HANDLE hOut = GetStdHandle(STD_OUTPUT_HANDLE);
    if (hOut == nullptr || hOut == INVALID_HANDLE_VALUE) {
        LOG_WARN("WtSizeWatcher: stdout handle invalid, exit loop");
        return;
    }

    // 首次读取初始化 m_lastCols/m_lastRows（避免启动即误触发回调）
    CONSOLE_SCREEN_BUFFER_INFO info{};
    if (GetConsoleScreenBufferInfo(hOut, &info)) {
        m_lastCols = info.srWindow.Right - info.srWindow.Left + 1;
        m_lastRows = info.srWindow.Bottom - info.srWindow.Top + 1;
        LOG_INFO("WtSizeWatcher: initial size %dx%d (buf %dx%d)",
                 m_lastCols, m_lastRows, info.dwSize.X, info.dwSize.Y);
    } else {
        LOG_WARN("WtSizeWatcher: initial GetConsoleScreenBufferInfo failed: %lu",
                 GetLastError());
    }

    while (m_running.load()) {
        if (GetConsoleScreenBufferInfo(hOut, &info)) {
            int cols = info.srWindow.Right  - info.srWindow.Left + 1;
            int rows = info.srWindow.Bottom - info.srWindow.Top  + 1;
            // 仅当视口尺寸变化时才通知（cols/rows）
            // 不比较 Left/Top：用户滚动 WT 时 Left/Top 变但 cols/rows 不变，
            //                 滚动对 DLL 无意义，避免噪音
            if (cols != m_lastCols || rows != m_lastRows) {
                int bufCols = info.dwSize.X;
                int bufRows = info.dwSize.Y;
                LOG_INFO("WtSizeWatcher: srWindow changed %dx%d -> %dx%d (buf %dx%d)",
                         m_lastCols, m_lastRows, cols, rows, bufCols, bufRows);
                m_lastCols = cols;
                m_lastRows = rows;
                if (m_callback) {
                    m_callback(cols, rows, bufCols, bufRows);
                }
            }
        }
        Sleep(kPollIntervalMs);  // Phase 10: 50ms（20fps）
    }
}

} // namespace terminjector
