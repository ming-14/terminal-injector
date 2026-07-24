// WtSizeWatcher 实现：WT 窗口尺寸监听线程
// 详见 docs/phases/05-cursor-buffer.md 4.5
//
// 流程：
//   - Start 启动后台线程，每 100ms 读取自身 stdout 的 CONSOLE_SCREEN_BUFFER_INFO
//   - srWindow 变化时（用户拖动 WT 边框）触发回调
//   - Stop 设置 m_running=false 并 join
//
// 注意：mediator 的 stdout 由 WT 的 ConPTY 提供，WT 窗口 resize 会同步更新
//       此 stdout 的 srWindow，故轮询它即可感知 WT 尺寸变化
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
    LOG_INFO("WtSizeWatcher started");
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
    }

    while (m_running.load()) {
        if (GetConsoleScreenBufferInfo(hOut, &info)) {
            int cols = info.srWindow.Right  - info.srWindow.Left + 1;
            int rows = info.srWindow.Bottom - info.srWindow.Top  + 1;
            if (cols != m_lastCols || rows != m_lastRows) {
                int bufCols = info.dwSize.X;
                int bufRows = info.dwSize.Y;
                m_lastCols = cols;
                m_lastRows = rows;
                if (m_callback) {
                    m_callback(cols, rows, bufCols, bufRows);
                }
            }
        }
        Sleep(100);  // 10fps，足够响应人手拖动
    }
}

} // namespace terminjector
