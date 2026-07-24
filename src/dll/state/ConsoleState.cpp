// ConsoleState 实现：运行期状态缓存
// 详见 docs/phases/03-dll-framework.md 4.4
//
// 关键点：
//   - 所有 getter/setter 用 SRWLOCK 保护（共享读/独占写）
//   - AdvanceCursor 处理行末换行与滚屏（滚屏 Phase 5 补全）
//   - 单例用 Meyers's Singleton（C++11 起线程安全）
#include "ConsoleState.h"
#include "logging/Logger.h"

namespace terminjector {

ConsoleState& ConsoleState::Instance() {
    static ConsoleState inst;
    return inst;
}

void ConsoleState::InitFromSnapshot(const StateSnapshot& snap) {
    AcquireSRWLockExclusive(&m_lock);
    m_screenInfo = snap.screenBufferInfo;
    m_cursorInfo = snap.cursorInfo;
    m_inputMode  = snap.inputMode;
    m_outputMode = snap.outputMode;
    m_inputCp    = snap.inputCp;
    m_outputCp   = snap.outputCp;
    m_title      = snap.title;
    m_fontInfo   = snap.fontInfo;
    m_mainBufferHandle = GetStdHandle(STD_OUTPUT_HANDLE);
    ReleaseSRWLockExclusive(&m_lock);
    LOG_INFO("ConsoleState init from snapshot: size=%dx%d cursor=(%d,%d) attr=0x%04x",
             m_screenInfo.dwSize.X, m_screenInfo.dwSize.Y,
             m_screenInfo.dwCursorPosition.X, m_screenInfo.dwCursorPosition.Y,
             m_screenInfo.wAttributes);
}

COORD ConsoleState::GetBufferSize() const {
    AcquireSRWLockShared(&m_lock);
    COORD r = m_screenInfo.dwSize;
    ReleaseSRWLockShared(&m_lock);
    return r;
}

void ConsoleState::SetBufferSize(COORD size) {
    AcquireSRWLockExclusive(&m_lock);
    m_screenInfo.dwSize = size;
    ReleaseSRWLockExclusive(&m_lock);
}

SMALL_RECT ConsoleState::GetWindow() const {
    AcquireSRWLockShared(&m_lock);
    SMALL_RECT r = m_screenInfo.srWindow;
    ReleaseSRWLockShared(&m_lock);
    return r;
}

void ConsoleState::SetWindow(SMALL_RECT w) {
    AcquireSRWLockExclusive(&m_lock);
    m_screenInfo.srWindow = w;
    ReleaseSRWLockExclusive(&m_lock);
}

COORD ConsoleState::GetCursorPosition() const {
    AcquireSRWLockShared(&m_lock);
    COORD r = m_screenInfo.dwCursorPosition;
    ReleaseSRWLockShared(&m_lock);
    return r;
}

void ConsoleState::SetCursorPosition(COORD pos) {
    AcquireSRWLockExclusive(&m_lock);
    m_screenInfo.dwCursorPosition = pos;
    ReleaseSRWLockExclusive(&m_lock);
}

// Phase 5：一次填充 CONSOLE_SCREEN_BUFFER_INFO 全部字段
// 用于 GetConsoleScreenBufferInfo Hook，避免目标程序拿到 ConHost 的旧状态
// dwMaximumWindowSize 用当前 srWindow 大小近似（实际值依赖字体/显示器，
// 目标程序一般不用此字段做关键决策）
void ConsoleState::FillScreenBufferInfo(CONSOLE_SCREEN_BUFFER_INFO& info) const {
    AcquireSRWLockShared(&m_lock);
    info = m_screenInfo;
    // dwMaximumWindowSize：用 srWindow 尺寸近似
    // Right/Bottom 是 inclusive，故 +1
    info.dwMaximumWindowSize.X = static_cast<SHORT>(
        m_screenInfo.srWindow.Right - m_screenInfo.srWindow.Left + 1);
    info.dwMaximumWindowSize.Y = static_cast<SHORT>(
        m_screenInfo.srWindow.Bottom - m_screenInfo.srWindow.Top + 1);
    ReleaseSRWLockShared(&m_lock);
}

// 输出后推进光标：解析 buf 中的控制字符按 ConHost 语义更新光标位置
// 修复 Phase 6 换行异常：
//   旧实现只按 charsWritten 盲目累加 X，把 \r \n 当可见字符，
//   导致 Python 写 "123\r\n" 时 cursor 跑到 (X+5, Y) 而非 (0, Y+1)
//
// ConHost 控制字符语义（与 Windows console 行为一致）：
//   \r (0x0D) → 光标回到行首（X=0），Y 不变
//   \n (0x0A) → 光标下移一行（Y++），X 不变（CRLF 由调用方显式写 \r\n）
//   \b (0x08) → 光标左移一格，不跨行
//   \t (0x09) → 移到下一个 8 列 tab stop，行末 wrap
//   其他字符 → 光标右移一格，行末按 wrapAtEol 决定 wrap 或停留
//
// 滚屏：Y 超出 buffer 底部时钳制（Phase 5 留 TODO 实现真实滚屏）
void ConsoleState::AdvanceCursor(const wchar_t* buf, int len, bool wrapAtEol) {
    if (buf == nullptr || len <= 0) return;

    AcquireSRWLockExclusive(&m_lock);
    COORD& c = m_screenInfo.dwCursorPosition;
    SHORT cols = m_screenInfo.dwSize.X;
    if (cols <= 0) cols = 80;  // 兜底
    SHORT rows = m_screenInfo.dwSize.Y;
    if (rows <= 0) rows = 25;

    // 行末换行辅助：Y++ 并处理底部钳制
    auto wrapLine = [&]() {
        c.X = 0;
        c.Y++;
        if (c.Y >= rows) {
            c.Y = rows - 1;
            // TODO Phase 5: 触发 ScrollConsoleScreenBuffer 等价逻辑
        }
    };

    for (int i = 0; i < len; ++i) {
        wchar_t ch = buf[i];
        switch (ch) {
            case L'\r':
                // CR：光标回到行首
                c.X = 0;
                break;
            case L'\n':
                // LF：匹配 ConPTY/WT 实际行为（\n = CR+LF，移动到下一行行首）
                //
                // 原 ConHost 语义 \n 只 Y++ 不改 X，但 DLL 的 VT 经 mediator 写入 ConPTY，
                // ConPTY/WT 把 \n 当作 CR+LF（X=0 Y++）。若 AdvanceCursor 不改 X，
                // DLL 缓存光标 X 会偏大，导致输出前光标定位把 ConPTY 光标拉到错误列，
                // 表现为输出整体偏右（Python banner 后 >>> 偏右 89 列）。
                c.X = 0;
                c.Y++;
                if (c.Y >= rows) {
                    c.Y = rows - 1;
                    // TODO Phase 5: 触发 ScrollConsoleScreenBuffer 等价逻辑
                }
                break;
            case L'\b':
                // Backspace：光标左移一格，不跨行
                if (c.X > 0) c.X--;
                break;
            case L'\t':
                // Tab：移到下一个 8 列 tab stop
                {
                    int next = ((c.X + 8) / 8) * 8;
                    if (next >= cols) {
                        if (wrapAtEol) {
                            wrapLine();
                        } else {
                            c.X = cols - 1;
                        }
                    } else {
                        c.X = static_cast<SHORT>(next);
                    }
                }
                break;
            default:
                // 可见字符（含宽字符：简化处理为单格，ConHost 对宽字符实际占 2 格
                // 但目标程序通过 GetConsoleScreenBufferInfo 查 cursor 时一般不依赖宽字符精确列）
                c.X++;
                if (c.X >= cols) {
                    if (wrapAtEol) {
                        wrapLine();
                    } else {
                        c.X = cols - 1;
                        // 已到行末且不 wrap，后续字符都停在这里
                        // 但仍需处理后续 \r \n 等控制字符，故不 break 出循环
                    }
                }
                break;
        }
    }
    ReleaseSRWLockExclusive(&m_lock);
}

CONSOLE_CURSOR_INFO ConsoleState::GetCursorInfo() const {
    AcquireSRWLockShared(&m_lock);
    CONSOLE_CURSOR_INFO r = m_cursorInfo;
    ReleaseSRWLockShared(&m_lock);
    return r;
}

void ConsoleState::SetCursorInfo(const CONSOLE_CURSOR_INFO& info) {
    AcquireSRWLockExclusive(&m_lock);
    m_cursorInfo = info;
    ReleaseSRWLockExclusive(&m_lock);
}

DWORD ConsoleState::GetInputMode() const {
    AcquireSRWLockShared(&m_lock);
    DWORD r = m_inputMode;
    ReleaseSRWLockShared(&m_lock);
    return r;
}

void ConsoleState::SetInputMode(DWORD m) {
    AcquireSRWLockExclusive(&m_lock);
    m_inputMode = m;
    ReleaseSRWLockExclusive(&m_lock);
}

DWORD ConsoleState::GetOutputMode() const {
    AcquireSRWLockShared(&m_lock);
    DWORD r = m_outputMode;
    ReleaseSRWLockShared(&m_lock);
    return r;
}

void ConsoleState::SetOutputMode(DWORD m) {
    AcquireSRWLockExclusive(&m_lock);
    m_outputMode = m;
    ReleaseSRWLockExclusive(&m_lock);
}

UINT ConsoleState::GetInputCp() const {
    AcquireSRWLockShared(&m_lock);
    UINT r = m_inputCp;
    ReleaseSRWLockShared(&m_lock);
    return r;
}

void ConsoleState::SetInputCp(UINT cp) {
    AcquireSRWLockExclusive(&m_lock);
    m_inputCp = cp;
    ReleaseSRWLockExclusive(&m_lock);
}

UINT ConsoleState::GetOutputCp() const {
    AcquireSRWLockShared(&m_lock);
    UINT r = m_outputCp;
    ReleaseSRWLockShared(&m_lock);
    return r;
}

void ConsoleState::SetOutputCp(UINT cp) {
    AcquireSRWLockExclusive(&m_lock);
    m_outputCp = cp;
    ReleaseSRWLockExclusive(&m_lock);
}

WORD ConsoleState::GetTextAttribute() const {
    AcquireSRWLockShared(&m_lock);
    WORD r = m_screenInfo.wAttributes;
    ReleaseSRWLockShared(&m_lock);
    return r;
}

void ConsoleState::SetTextAttribute(WORD attr) {
    AcquireSRWLockExclusive(&m_lock);
    m_screenInfo.wAttributes = attr;
    ReleaseSRWLockExclusive(&m_lock);
}

std::wstring ConsoleState::GetTitle() const {
    AcquireSRWLockShared(&m_lock);
    std::wstring r = m_title;
    ReleaseSRWLockShared(&m_lock);
    return r;
}

void ConsoleState::SetTitle(const std::wstring& t) {
    AcquireSRWLockExclusive(&m_lock);
    m_title = t;
    ReleaseSRWLockExclusive(&m_lock);
}

bool ConsoleState::IsAltBufferActive() const {
    return m_altBuffer.load(std::memory_order_acquire);
}

void ConsoleState::SetAltBufferActive(bool b) {
    m_altBuffer.store(b, std::memory_order_release);
}

// 主缓冲区句柄缓存（InitFromSnapshot 时赋值）
// 用于 SetConsoleActiveScreenBuffer Hook 判断切换方向
HANDLE ConsoleState::GetMainBufferHandle() const {
    AcquireSRWLockShared(&m_lock);
    HANDLE h = m_mainBufferHandle;
    ReleaseSRWLockShared(&m_lock);
    return h;
}

HANDLE ConsoleState::GetAltBufferHandle() const {
    AcquireSRWLockShared(&m_lock);
    HANDLE h = m_altBufferHandle;
    ReleaseSRWLockShared(&m_lock);
    return h;
}

void ConsoleState::SetAltBufferHandle(HANDLE h) {
    AcquireSRWLockExclusive(&m_lock);
    m_altBufferHandle = h;
    ReleaseSRWLockExclusive(&m_lock);
}

// 字体信息缓存：注入瞬间从 StateSnapshot 拷贝
// GetCurrentConsoleFontEx Hook 返回此值，避免目标程序因字体尺寸不一致布局错乱
// SetCurrentConsoleFontEx Hook 仅记录不真改（WT 字体由用户配置控制）
CONSOLE_FONT_INFOEX ConsoleState::GetFontInfo() const {
    AcquireSRWLockShared(&m_lock);
    CONSOLE_FONT_INFOEX r = m_fontInfo;
    ReleaseSRWLockShared(&m_lock);
    return r;
}

void ConsoleState::SetFontInfo(const CONSOLE_FONT_INFOEX& info) {
    AcquireSRWLockExclusive(&m_lock);
    m_fontInfo = info;
    ReleaseSRWLockExclusive(&m_lock);
}

} // namespace terminjector
