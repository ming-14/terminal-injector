// VirtualConsoleState 实现：虚拟 Console 状态维护
// 详见 docs/phases/14-virtual-console-state.md
//
// 关键实现细节：
//   - InitializeFromConHost 在 LazyInit 时调用，用 orig 绕过 Hook 读真实 ConHost 状态
//   - AdvanceCursor 解析 \r \n \b \t 等控制字符，按 Windows Console 语义推进光标
//   - ApplyWtResize / ApplyWtCursorReport 由 DLL 接收线程在收到 WtStateReport 时调用
#include "VirtualConsoleState.h"
#include "logging/Logger.h"

#include <windows.h>
#include <cstring>

namespace terminjector {

VirtualConsoleState& VirtualConsoleState::Instance() {
    static VirtualConsoleState inst;
    return inst;
}

void VirtualConsoleState::InitializeFromConHost() {
    // 用 orig 绕过 Hook（通过 GetProcAddress 获取原始 API 地址）
    // 注意：LazyInit 时 GetConsoleScreenBufferInfo 的 Hook 已安装，
    //       但 LazyInit 内 IsInLazyInit() 返回 true，Hook 走 pass-through
    HANDLE hOut = GetStdHandle(STD_OUTPUT_HANDLE);
    CONSOLE_SCREEN_BUFFER_INFO info{};
    if (!GetConsoleScreenBufferInfo(hOut, &info)) {
        LOG_WARN("VirtualConsoleState::InitializeFromConHost: "
                 "GetConsoleScreenBufferInfo failed, err=%lu", GetLastError());
        return;
    }

    std::lock_guard<std::mutex> lock(m_lock);
    m_bufferSize = info.dwSize;
    m_cursorPos = info.dwCursorPosition;
    m_attributes.store(info.wAttributes);
    m_windowRect = info.srWindow;
    m_initialized.store(true);
    LOG_INFO("VirtualConsoleState initialized: size=%dx%d cursor=(%d,%d) attr=0x%04x win=(%d,%d)-(%d,%d)",
             m_bufferSize.X, m_bufferSize.Y,
             m_cursorPos.X, m_cursorPos.Y,
             m_attributes.load(),
             m_windowRect.Left, m_windowRect.Top,
             m_windowRect.Right, m_windowRect.Bottom);
}

COORD VirtualConsoleState::GetCursorPos() const {
    std::lock_guard<std::mutex> lock(m_lock);
    return m_cursorPos;
}

void VirtualConsoleState::SetCursorPos(COORD pos) {
    std::lock_guard<std::mutex> lock(m_lock);
    m_cursorPos = pos;
}

// 输出后推进光标：解析 buf 中的控制字符按 Windows Console 语义更新光标位置
// 与 ConsoleState::AdvanceCursor 逻辑一致，但更新 VirtualConsoleState
//
// 控制字符语义：
//   \r (0x0D) → X=0（回行首）
//   \n (0x0A) → Y++（下移一行），X 不变（Windows \n 语义不带回车）
//   \b (0x08) → X--（左移一格），不跨行
//   \t (0x09) → 下一个 8 列 tab stop
//   其他字符 → X++，行末回绕到下一行行首
void VirtualConsoleState::AdvanceCursor(const wchar_t* buf, int len) {
    if (buf == nullptr || len <= 0) return;

    std::lock_guard<std::mutex> lock(m_lock);
    SHORT cols = m_bufferSize.X;
    if (cols <= 0) cols = 80;
    SHORT rows = m_bufferSize.Y;
    if (rows <= 0) rows = 25;

    auto wrapLine = [&]() {
        m_cursorPos.X = 0;
        m_cursorPos.Y++;
        if (m_cursorPos.Y >= rows) {
            m_cursorPos.Y = rows - 1;
        }
    };

    for (int i = 0; i < len; ++i) {
        wchar_t ch = buf[i];
        switch (ch) {
            case L'\r':
                m_cursorPos.X = 0;
                break;
            case L'\n':
                m_cursorPos.X = 0;  // ConPTY/WT 把 \n 当作 CR+LF
                m_cursorPos.Y++;
                if (m_cursorPos.Y >= rows) {
                    m_cursorPos.Y = rows - 1;
                }
                break;
            case L'\b':
                if (m_cursorPos.X > 0) m_cursorPos.X--;
                break;
            case L'\t':
                {
                    int next = ((m_cursorPos.X + 8) / 8) * 8;
                    if (next >= cols) {
                        wrapLine();
                    } else {
                        m_cursorPos.X = static_cast<SHORT>(next);
                    }
                }
                break;
            default:
                m_cursorPos.X++;
                if (m_cursorPos.X >= cols) {
                    wrapLine();
                }
                break;
        }
    }
}

COORD VirtualConsoleState::GetBufferSize() const {
    std::lock_guard<std::mutex> lock(m_lock);
    return m_bufferSize;
}

void VirtualConsoleState::SetBufferSize(COORD size) {
    std::lock_guard<std::mutex> lock(m_lock);
    m_bufferSize = size;
}

SMALL_RECT VirtualConsoleState::GetWindowRect() const {
    std::lock_guard<std::mutex> lock(m_lock);
    return m_windowRect;
}

void VirtualConsoleState::SetWindowRect(SMALL_RECT rect) {
    std::lock_guard<std::mutex> lock(m_lock);
    m_windowRect = rect;
}

WORD VirtualConsoleState::GetAttributes() const {
    return m_attributes.load();
}

void VirtualConsoleState::SetAttributes(WORD attr) {
    m_attributes.store(attr);
}

COORD VirtualConsoleState::GetLargestWindowSize() const {
    std::lock_guard<std::mutex> lock(m_lock);
    COORD r;
    r.X = static_cast<SHORT>(m_windowRect.Right - m_windowRect.Left + 1);
    r.Y = static_cast<SHORT>(m_windowRect.Bottom - m_windowRect.Top + 1);
    return r;
}

void VirtualConsoleState::FillScreenBufferInfo(CONSOLE_SCREEN_BUFFER_INFO& info) const {
    std::lock_guard<std::mutex> lock(m_lock);
    info.dwSize = m_bufferSize;
    info.dwCursorPosition = m_cursorPos;
    info.wAttributes = m_attributes.load();
    info.srWindow = m_windowRect;
    info.dwMaximumWindowSize.X = static_cast<SHORT>(
        m_windowRect.Right - m_windowRect.Left + 1);
    info.dwMaximumWindowSize.Y = static_cast<SHORT>(
        m_windowRect.Bottom - m_windowRect.Top + 1);
}

// WT resize 反向同步：更新 bufferSize 和 srWindow
void VirtualConsoleState::ApplyWtResize(int32_t cols, int32_t rows) {
    std::lock_guard<std::mutex> lock(m_lock);
    m_bufferSize.X = static_cast<SHORT>(cols);
    m_bufferSize.Y = static_cast<SHORT>(rows);
    m_windowRect.Left = 0;
    m_windowRect.Top = 0;
    m_windowRect.Right = static_cast<SHORT>(cols - 1);
    m_windowRect.Bottom = static_cast<SHORT>(rows - 1);
    // 光标位置裁剪到新缓冲区内
    if (m_cursorPos.X >= m_bufferSize.X) {
        m_cursorPos.X = static_cast<SHORT>(m_bufferSize.X - 1);
    }
    if (m_cursorPos.Y >= m_bufferSize.Y) {
        m_cursorPos.Y = static_cast<SHORT>(m_bufferSize.Y - 1);
    }
    LOG_INFO("VirtualConsoleState::ApplyWtResize: size=%dx%d win=(%d,%d)-(%d,%d) cursor=(%d,%d)",
             cols, rows,
             m_windowRect.Left, m_windowRect.Top,
             m_windowRect.Right, m_windowRect.Bottom,
             m_cursorPos.X, m_cursorPos.Y);
}

// WT DSR CPR 响应反向同步：更新光标位置
// col/row 是 VT 1-based 坐标，需转为 0-based
void VirtualConsoleState::ApplyWtCursorReport(int32_t col, int32_t row) {
    std::lock_guard<std::mutex> lock(m_lock);
    m_cursorPos.X = static_cast<SHORT>(col - 1);
    m_cursorPos.Y = static_cast<SHORT>(row - 1);
    LOG_INFO("VirtualConsoleState::ApplyWtCursorReport: cursor=(%d,%d) (from VT %d,%d)",
             m_cursorPos.X, m_cursorPos.Y, col, row);
}

} // namespace terminjector