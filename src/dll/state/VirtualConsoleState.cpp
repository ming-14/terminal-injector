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
#include <wcwidth.h>

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
//   其他字符 → 按 wcwidth 计算显示宽度（0/1/2）推进光标，行末回绕
//
// Phase 17 字符宽度审计：
//   使用 wcwidth/wcwidth32 计算 CJK 字符（宽度 2）和零宽字符（宽度 0），
//   代理对（emoji 等 BMP 外字符）组合为 32 位 codepoint 后调用 wcwidth32。
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
            m_scrollbackLines++;  // Phase 18：递增滚动计数
        }
    };

    // 代理对辅助函数
    auto isHighSurrogate = [](wchar_t ch) -> bool {
        return ch >= 0xD800 && ch <= 0xDBFF;
    };
    auto isLowSurrogate = [](wchar_t ch) -> bool {
        return ch >= 0xDC00 && ch <= 0xDFFF;
    };
    auto combineSurrogate = [](wchar_t high, wchar_t low) -> uint32_t {
        return 0x10000 + ((static_cast<uint32_t>(high) - 0xD800) << 10)
                       + (static_cast<uint32_t>(low) - 0xDC00);
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
                    // Phase 18：递增滚动计数（与 ConsoleState::AdvanceCursor
                    // 的 \n 分支语义保持一致，修复两状态不一致）
                    m_scrollbackLines++;
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
                {
                    // 按字符显示宽度推进光标（Phase 17）
                    int w;
                    if (isHighSurrogate(ch) && i + 1 < len && isLowSurrogate(buf[i + 1])) {
                        // 代理对：组合为 32 位 codepoint 后计算宽度
                        uint32_t cp = combineSurrogate(ch, buf[i + 1]);
                        w = wcwidth32(cp);
                        ++i;  // 跳过低代理
                    } else {
                        w = wcwidth(ch);
                    }
                    if (w < 0) w = 0;  // 控制字符按 0 宽度处理
                    m_cursorPos.X = static_cast<SHORT>(m_cursorPos.X + w);
                    if (m_cursorPos.X >= cols) {
                        wrapLine();
                    }
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
// Phase 18：保留用户设置的缓冲区高度 + 滚动计数，不因 WT resize 丢失 scrollback
void VirtualConsoleState::ApplyWtResize(int32_t cols, int32_t rows) {
    std::lock_guard<std::mutex> lock(m_lock);
    m_bufferSize.X = static_cast<SHORT>(cols);
    // Phase 18：保留用户设置的缓冲区高度 + 滚动计数
    int32_t minHeight = (std::max)(m_userBufferHeight,
                                  rows + m_scrollbackLines);
    m_bufferSize.Y = static_cast<SHORT>((std::max)(static_cast<int32_t>(rows), minHeight));
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
    LOG_INFO("VirtualConsoleState::ApplyWtResize: size=%dx%d win=(%d,%d)-(%d,%d) cursor=(%d,%d) scrollback=%d userBufH=%d",
             cols, rows,
             m_windowRect.Left, m_windowRect.Top,
             m_windowRect.Right, m_windowRect.Bottom,
             m_cursorPos.X, m_cursorPos.Y,
             m_scrollbackLines, m_userBufferHeight);
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

// Phase 15：WT DA 报告反向同步——存储终端能力标识
// WT 响应 Primary DA 查询（\x1b[c）时，返回 \x1b[?1;Psc，
// 其中 Ps 标识终端类型（如 61=VT320, 67=VT525）。
// 此信息记录在 VirtualConsoleState 中，供后续查询使用。
void VirtualConsoleState::ApplyWtDaReport(int32_t caps) {
    m_terminalCaps.store(caps);
    LOG_INFO("VirtualConsoleState::ApplyWtDaReport: terminal caps=%d", caps);
}

// ============================================================
// 滚动缓冲区接口（Phase 18）
// ============================================================

int32_t VirtualConsoleState::GetScrollbackLines() const {
    std::lock_guard<std::mutex> lock(m_lock);
    return m_scrollbackLines;
}

void VirtualConsoleState::SetUserBufferHeight(int32_t height) {
    std::lock_guard<std::mutex> lock(m_lock);
    m_userBufferHeight = height;
    // 同步更新 bufferSize：确保 bufferSize.Y >= max(视口高度, 用户请求高度)
    SHORT rows = static_cast<SHORT>(m_windowRect.Bottom - m_windowRect.Top + 1);
    int32_t minHeight = (std::max)(height, rows + m_scrollbackLines);
    m_bufferSize.Y = static_cast<SHORT>(rows > minHeight ? rows : minHeight);
    LOG_INFO("VirtualConsoleState::SetUserBufferHeight: height=%d bufferSize.Y=%d scrollback=%d",
             height, m_bufferSize.Y, m_scrollbackLines);
}

void VirtualConsoleState::ResetScrollback() {
    std::lock_guard<std::mutex> lock(m_lock);
    m_scrollbackLines = 0;
    m_userBufferHeight = 0;
    // �ָ� bufferSize Ϊ�ӿڳߴ�
    SHORT rows = static_cast<SHORT>(m_windowRect.Bottom - m_windowRect.Top + 1);
    SHORT cols = static_cast<SHORT>(m_windowRect.Right - m_windowRect.Left + 1);
    m_bufferSize.X = cols;
    m_bufferSize.Y = rows;
    LOG_INFO("VirtualConsoleState::ResetScrollback: bufferSize reset to %dx%d", cols, rows);
}

// Phase 19：VT 直通追踪器内容滚出视口顶部一行时回调，与
// AdvanceCursor 内部 wrapLine 的滚动计数保持一致（Phase 18 语义）。
// 高频调用，不记日志。
void VirtualConsoleState::NotifyScrollLine() {
    std::lock_guard<std::mutex> lock(m_lock);
    m_scrollbackLines++;
}

} // namespace terminjector