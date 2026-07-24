// LineEditor 实现：conhost 风格的行编辑
// 详见 docs/phases/06-input-chain.md（行编辑扩展）
//
// 实现要点：
//   - 字符插入：在光标处插入，若非行末则全行重绘
//   - 退格：删除光标前字符，全行重绘
//   - Delete：删除光标处字符，全行重绘
//   - 左右箭头：移动光标（VT CSI D/C）
//   - Home/End：移动到行首/行末
//   - Esc：清空当前输入（保留 prompt）
//   - Enter：输出 \r\n，返回行内容
//   - 上/下箭头：历史导航，替换当前行
//   - Tab：补全（委托 TabCompleter）
//
// VT 策略：
//   - 简单追加（光标在行末）：直接输出字符
//   - 编辑操作（插入/删除/历史/补全）：全行重绘
//   - 全行重绘：CSI <cursor>D（移到行首）→ 输出行 → CSI K（擦行末）→ CSI <delta>D（移回光标）
//   - 假设行不跨屏幕换行（CSI D 不跨行）
//
// 字符显示宽度：
//   - 使用 wcwidth 计算每个 wchar_t 占用的列数（中文等 CJK 字符为 2 列，
//     组合符为 0 列，控制字符按 0 列处理）
//   - 所有 VT 光标移动距离（CSI C/D）必须按"显示列数"而非字符数计算，
//     否则中文退格/方向键会出现光标错位（典型现象：中文需按两次退格键）
#include "LineEditor.h"
#include "TabCompleter.h"
#include "../state/ConsoleState.h"
#include "logging/Logger.h"

#include <windows.h>
#include <cstdio>
#include <algorithm>
#include <wcwidth.h>

namespace terminjector {

namespace {

// ============================================================
// 字符显示宽度计算（基于 wcwidth / wcwidth32）
// ============================================================
// 返回值：0（零宽/控制字符）/ 1 / 2
// wcwidth 返回 -1 表示非打印控制字符，按 0 处理避免负偏移
//
// 代理对处理：
//   ReadConsoleInputW 把 BMP 外字符（如 emoji U+1F600）拆成高代理+
//   低代理两个 wchar_t。DisplayWidth 遍历 wstring 时必须把代理对组合
//   为 32 位 codepoint 调用 wcwidth32，否则每个代理 wchar_t 单独调用
//   wcwidth 返回 -1（按 0 处理），导致 emoji 光标偏移计算为 0，回显
//   与光标移动全部错位
int GetCharWidth(wchar_t ch) {
    int w = wcwidth(ch);
    return w < 0 ? 0 : w;
}

// 32 位 codepoint 的显示宽度（用于代理对组合后的完整字符）
int GetCharWidth32(uint32_t cp) {
    int w = wcwidth32(cp);
    return w < 0 ? 0 : w;
}

bool IsHighSurrogate(wchar_t ch) { return ch >= 0xD800 && ch <= 0xDBFF; }
bool IsLowSurrogate(wchar_t ch)  { return ch >= 0xDC00 && ch <= 0xDFFF; }

// 组合高代理+低代理为 32 位 codepoint
uint32_t CombineSurrogate(wchar_t high, wchar_t low) {
    return 0x10000 + ((static_cast<uint32_t>(high) - 0xD800) << 10)
                   + (static_cast<uint32_t>(low)  - 0xDC00);
}

// 计算 s[from, from+count) 子串的显示宽度（按字符显示列宽累加）
// count=npos 时计算到字符串末尾
// 遍历识别代理对：高代理+低代理组合调用 wcwidth32，否则单 wchar_t 调 wcwidth
int DisplayWidth(const std::wstring& s, size_t from,
                 size_t count = std::wstring::npos) {
    if (from > s.size()) from = s.size();
    size_t end = s.size();
    if (count != std::wstring::npos) {
        end = from + count;
        if (end > s.size()) end = s.size();
    }
    int w = 0;
    size_t i = from;
    while (i < end) {
        wchar_t ch = s[i];
        if (IsHighSurrogate(ch) && i + 1 < end && IsLowSurrogate(s[i + 1])) {
            w += GetCharWidth32(CombineSurrogate(ch, s[i + 1]));
            i += 2;
        } else {
            w += GetCharWidth(ch);
            ++i;
        }
    }
    return w;
}

// 向 vtOut 追加 CSI <n>C（光标右移 n 列）
void AppendC(std::string& vtOut, int n) {
    if (n <= 0) return;
    char buf[32];
    std::snprintf(buf, sizeof(buf), "\x1b[%dC", n);
    vtOut += buf;
}

// 向 vtOut 追加 CSI <n>D（光标左移 n 列）
void AppendD(std::string& vtOut, int n) {
    if (n <= 0) return;
    char buf[32];
    std::snprintf(buf, sizeof(buf), "\x1b[%dD", n);
    vtOut += buf;
}

} // namespace

// ============================================================
// 单例
// ============================================================
LineEditor& LineEditor::Instance() {
    static LineEditor inst;
    return inst;
}

LineEditor::LineEditor()
    : m_cursor(0)
    , m_historyIdx(-1) {
    m_tabCompleter = std::make_unique<TabCompleter>();
}

// ============================================================
// 开始新的行编辑会话
// ============================================================
// 记录行首绝对光标 m_startCursor，用于操作后同步 ConsoleState 光标
void LineEditor::BeginSession() {
    m_line.clear();
    m_cursor = 0;
    m_historyIdx = -1;
    m_savedLine.clear();
    m_tabCompleter->Cancel();
    m_startCursor = ConsoleState::Instance().GetCursorPosition();
}

// ============================================================
// 同步 ConsoleState 光标到 LineEditor 当前状态
// ============================================================
// LineEditor 输出的 VT 序列直接发给 mediator → WT 渲染，但 DLL 的
// ConsoleState 光标缓存不会自动更新。若不同步，后续 cmd WriteConsoleW
// 时 OutputHooks 用旧光标定位，会覆盖 LineEditor 已输出的内容。
//
// 同步规则：
//   toLineStart=true: X=0（Enter/Ctrl+C 换行后）
//   toLineStart=false: X = m_startCursor.X + 行内显示偏移
//   deltaY: Y 偏移（Enter/Ctrl+C 为 1，其他为 0）
void LineEditor::SyncCursor(int deltaY, bool toLineStart) const {
    COORD c;
    if (toLineStart) {
        c.X = 0;
    } else {
        int offsetX = DisplayWidth(m_line, 0, m_cursor);
        c.X = static_cast<SHORT>(m_startCursor.X + offsetX);
    }
    c.Y = static_cast<SHORT>(m_startCursor.Y + deltaY);
    ConsoleState::Instance().SetCursorPosition(c);
}

// ============================================================
// wchar_t → UTF-8
// ============================================================
std::string LineEditor::WToUtf8(const std::wstring& w) {
    if (w.empty()) return {};
    int len = WideCharToMultiByte(CP_UTF8, 0, w.data(),
                                  static_cast<int>(w.size()),
                                  nullptr, 0, nullptr, nullptr);
    std::string out(static_cast<size_t>(len), '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.data(),
                        static_cast<int>(w.size()),
                        out.data(), len, nullptr, nullptr);
    return out;
}

// ============================================================
// 全行重绘
// ============================================================
// 移到行首 → 输出整行 → 擦除行末 → 移回光标位置
//
// 用 \r 移到行首而非 CSI D（后退 N）：
//   CSI D 需要知道当前 WT 光标位置才能计算后退距离，
//   但调用方（如字符插入）在修改 m_cursor 后 WT 光标未必同步，
//   用 \r 直接回到行首避免依赖 WT 光标当前位置
//
// 光标后退距离按显示宽度计算（CJK 字符占 2 列）：
//   行末显示宽度 - 光标位置显示宽度
void LineEditor::FullRedraw(std::string& vtOut) const {
    // \r 移到行首（不依赖当前光标位置）
    vtOut += '\r';

    // 输出整行（UTF-8）
    vtOut += WToUtf8(m_line);

    // 擦除从光标到行末（防止旧行内容残留）
    vtOut += "\x1b[0K";

    // 后退到光标位置：行末 -> m_cursor，按显示列宽
    AppendD(vtOut, DisplayWidth(m_line, m_cursor));
}

// ============================================================
// 替换当前行（历史导航/Tab 补全用）
// ============================================================
// 只更新差异部分，不重绘整行：
//   1. 计算旧行 m_line 与新行 newLine 的公共前缀长度
//   2. 移到公共前缀末尾（WT 光标从 m_cursor 前进/后退到 commonPrefix）
//   3. 输出 newLine 从 commonPrefix 开始的部分
//   4. 若新行比旧行短，擦除行末残留
//   5. 后退到 newCursor 位置
//
// 所有光标移动距离按显示宽度计算（CJK 字符占 2 列）
void LineEditor::ReplaceLine(const std::wstring& newLine, size_t newCursor,
                              std::string& vtOut) {
    size_t oldLen = m_line.size();
    size_t newLen = newLine.size();

    // 计算公共前缀长度
    size_t commonPrefix = 0;
    while (commonPrefix < oldLen && commonPrefix < newLen &&
           m_line[commonPrefix] == newLine[commonPrefix]) {
        ++commonPrefix;
    }

    // WT 光标在 m_cursor 位置，移到 commonPrefix 位置（按显示列宽计算偏移）
    // 注意：此时 m_line 仍是旧行，用 m_line 计算显示宽度
    if (m_cursor > commonPrefix) {
        AppendD(vtOut, DisplayWidth(m_line, commonPrefix, m_cursor - commonPrefix));
    } else if (m_cursor < commonPrefix) {
        AppendC(vtOut, DisplayWidth(m_line, m_cursor, commonPrefix - m_cursor));
    }

    // 更新 m_line 和 m_cursor（在输出差异之前更新，供后续计算用）
    m_line = newLine;
    m_cursor = newCursor;

    // 输出 newLine 从 commonPrefix 开始的部分
    if (newLen > commonPrefix) {
        std::wstring diff = newLine.substr(commonPrefix);
        vtOut += WToUtf8(diff);
    }

    // 若新行比旧行短，擦除行末残留
    if (newLen < oldLen) {
        vtOut += "\x1b[0K";
    }

    // 后退到 newCursor 位置（WT 光标在 newLen 位置 -> newCursor）
    AppendD(vtOut, DisplayWidth(newLine, newCursor));

    // 同步 ConsoleState 光标（历史导航/Tab 补全后光标位置变化）
    SyncCursor();
}

// ============================================================
// 添加到历史
// ============================================================
void LineEditor::AddToHistory(const std::wstring& line) {
    if (line.empty()) return;
    // 避免连续重复
    if (!m_history.empty() && m_history.back() == line) return;
    m_history.push_back(line);
    // 限制历史长度（与 conhost 默认 50 一致）
    if (m_history.size() > 50) {
        m_history.erase(m_history.begin());
    }
}

// ============================================================
// 历史导航
// ============================================================
void LineEditor::NavigateHistory(int direction, std::string& vtOut) {
    if (m_history.empty()) return;

    // 首次进入历史导航：保存当前行
    if (m_historyIdx == -1) {
        if (direction < 0) {
            // Up：从最后一条开始
            m_historyIdx = static_cast<int>(m_history.size()) - 1;
            m_savedLine = m_line;
        } else {
            // Down 但未在历史中：无操作
            return;
        }
    } else {
        m_historyIdx += direction;
        if (m_historyIdx < 0) {
            // 超出最旧：回到保存的行
            m_historyIdx = -1;
            ReplaceLine(m_savedLine, m_savedLine.size(), vtOut);
            return;
        }
        if (m_historyIdx >= static_cast<int>(m_history.size())) {
            // 超出最新：回到保存的行
            m_historyIdx = -1;
            ReplaceLine(m_savedLine, m_savedLine.size(), vtOut);
            return;
        }
    }

    // 替换为历史条目，光标放在行末
    const std::wstring& entry = m_history[static_cast<size_t>(m_historyIdx)];
    ReplaceLine(entry, entry.size(), vtOut);
}

// ============================================================
// Tab 补全
// ============================================================
void LineEditor::HandleTab(bool shift, std::string& vtOut) {
    std::wstring newLine;
    size_t newCursor;

    if (m_tabCompleter->IsActive()) {
        // 已在补全模式：循环下一个
        if (shift) {
            m_tabCompleter->Prev(newLine, newCursor);
        } else {
            m_tabCompleter->Next(newLine, newCursor);
        }
        ReplaceLine(newLine, newCursor, vtOut);
    } else {
        // 首次补全
        if (m_tabCompleter->Complete(m_line, m_cursor, newLine, newCursor)) {
            ReplaceLine(newLine, newCursor, vtOut);
        }
        // 无匹配：不做任何操作
    }
}

// ============================================================
// 处理按键事件
// ============================================================
bool LineEditor::ProcessKey(const KEY_EVENT_RECORD& ker, bool echoEnabled,
                             std::wstring& lineOut, std::string& vtOut) {
    vtOut.clear();
    lineOut.clear();

    WORD vk = ker.wVirtualKeyCode;
    wchar_t ch = ker.uChar.UnicodeChar;
    DWORD ctrlState = ker.dwControlKeyState;

    // ---- Ctrl+C：清空当前行并返回空行 ----
    // Phase 7 会完整实现信号链路，此处仅清行
    if (ch == 0x03 && (ctrlState & LEFT_CTRL_PRESSED)) {
        // 输出 ^C + 换行（conhost 行为）
        if (echoEnabled) {
            vtOut = "^C\r\n";
        }
        m_line.clear();
        m_cursor = 0;
        lineOut.clear();
        // 光标移到下一行行首（^C\r\n 后）
        SyncCursor(1, true);
        return true;
    }

    // ---- Enter：行完成 ----
    if (vk == VK_RETURN || ch == L'\r') {
        // 取消 Tab 补全模式
        m_tabCompleter->Cancel();

        // 移动光标到行末（视觉对齐），按显示列宽前进
        if (echoEnabled) {
            AppendC(vtOut, DisplayWidth(m_line, m_cursor));
            // 输出换行
            vtOut += "\r\n";
        }

        // 添加到历史
        AddToHistory(m_line);

        // 光标移到下一行行首（\r\n 后），同步 ConsoleState
        // 注意：m_line 仍保留行内容，SyncCursor 用 m_cursor 计算偏移，
        //       但 Enter 后光标应在 (0, Y+1)，故用 toLineStart=true
        SyncCursor(1, true);

        // 返回行内容（不含 \r\n）
        lineOut = m_line;
        return true;
    }

    // ---- Tab：补全 ----
    if (vk == VK_TAB) {
        bool shift = (ctrlState & SHIFT_PRESSED) != 0;
        // Ctrl+Tab 或 Alt+Tab 不处理（避免冲突）
        if (ctrlState & (LEFT_CTRL_PRESSED | LEFT_ALT_PRESSED)) {
            return false;
        }
        HandleTab(shift, vtOut);
        return false;
    }

    // 以下按键取消 Tab 补全模式
    m_tabCompleter->Cancel();

    // ---- Backspace（VK_BACK 或 \b）----
    // 退格光标后退距离按被删字符显示宽度计算（CJK 字符占 2 列）
    // 代理对处理：若光标前为低代理且前前为高代理，删除整个代理对（2 个
    // wchar_t），后退距离按 wcwidth32(组合cp) 计算（emoji 占 2 列）
    if (vk == VK_BACK || ch == L'\b') {
        if (m_cursor == 0) return false;

        // 检测光标前是否为代理对尾部（低代理 + 前一个高代理）
        bool isSurrogatePair = (m_cursor >= 2 &&
                                IsLowSurrogate(m_line[m_cursor - 1]) &&
                                IsHighSurrogate(m_line[m_cursor - 2]));
        int erasedWidth;
        size_t eraseCount;
        if (isSurrogatePair) {
            uint32_t cp = CombineSurrogate(m_line[m_cursor - 2], m_line[m_cursor - 1]);
            erasedWidth = GetCharWidth32(cp);
            eraseCount = 2;
        } else {
            erasedWidth = GetCharWidth(m_line[m_cursor - 1]);
            eraseCount = 1;
        }

        // 删除光标前字符（或代理对）
        m_line.erase(m_cursor - eraseCount, eraseCount);
        m_cursor -= eraseCount;

        if (echoEnabled) {
            // WT 光标在 oldCursor 位置（= m_cursor + erasedWidth）
            // 后退 erasedWidth 到 m_cursor，输出后续字符，擦行末，后退到光标
            AppendD(vtOut, erasedWidth);
            std::wstring tail = m_line.substr(m_cursor);
            vtOut += WToUtf8(tail);
            vtOut += "\x1b[0K";  // 行变短，擦除残留
            AppendD(vtOut, DisplayWidth(m_line, m_cursor));
        }
        SyncCursor();
        return false;
    }

    // ---- Delete（VK_DELETE）----
    // 代理对处理：若光标处为高代理且下一个为低代理，删除整个代理对
    if (vk == VK_DELETE) {
        if (m_cursor >= m_line.size()) return false;

        // 检测光标处是否为代理对（高代理 + 下一个低代理）
        bool isSurrogatePair = (m_cursor + 1 < m_line.size() &&
                                IsHighSurrogate(m_line[m_cursor]) &&
                                IsLowSurrogate(m_line[m_cursor + 1]));
        size_t eraseCount = isSurrogatePair ? 2 : 1;

        // 删除光标处字符（或代理对）
        m_line.erase(m_cursor, eraseCount);

        if (echoEnabled) {
            // WT 光标在 m_cursor 位置（不变）
            // 输出后续字符，擦行末，后退到光标（按显示列宽）
            std::wstring tail = m_line.substr(m_cursor);
            vtOut += WToUtf8(tail);
            vtOut += "\x1b[0K";  // 行变短，擦除残留
            AppendD(vtOut, DisplayWidth(m_line, m_cursor));
        }
        SyncCursor();
        return false;
    }

    // ---- 左箭头 ----
    // 单步后退距离 = 跨过字符的显示宽度（CJK 字符占 2 列）
    // 代理对处理：若光标前为低代理且前前为高代理，单步跨过整个代理对，
    // 后退距离按 wcwidth32(组合cp) 计算（emoji 占 2 列）
    if (vk == VK_LEFT) {
        if (m_cursor == 0) return false;

        int moveWidth = 0;
        // Ctrl+Left：按 word 移动（跳过空格和非空格），累加显示宽度
        // Ctrl+Left 按 wchar_t 遍历，代理对会被拆开；word 移动场景下
        // emoji 出现在 word 边界的情况极少，暂不特殊处理代理对
        if (ctrlState & (LEFT_CTRL_PRESSED | RIGHT_CTRL_PRESSED)) {
            while (m_cursor > 0 && m_line[m_cursor - 1] == L' ') {
                moveWidth += GetCharWidth(m_line[m_cursor - 1]);
                --m_cursor;
            }
            while (m_cursor > 0 && m_line[m_cursor - 1] != L' ') {
                moveWidth += GetCharWidth(m_line[m_cursor - 1]);
                --m_cursor;
            }
        } else {
            // 单步左移：跨过代理对整体
            if (m_cursor >= 2 &&
                IsLowSurrogate(m_line[m_cursor - 1]) &&
                IsHighSurrogate(m_line[m_cursor - 2])) {
                uint32_t cp = CombineSurrogate(m_line[m_cursor - 2], m_line[m_cursor - 1]);
                moveWidth = GetCharWidth32(cp);
                m_cursor -= 2;
            } else {
                --m_cursor;
                moveWidth = GetCharWidth(m_line[m_cursor]);
            }
        }

        if (echoEnabled) {
            AppendD(vtOut, moveWidth);
        }
        SyncCursor();
        return false;
    }

    // ---- 右箭头 ----
    // 单步前进距离 = 跨过字符的显示宽度（CJK 字符占 2 列）
    // 代理对处理：若光标处为高代理且下一个为低代理，单步跨过整个代理对
    if (vk == VK_RIGHT) {
        if (m_cursor >= m_line.size()) return false;

        int moveWidth = 0;
        // Ctrl+Right：按 word 移动，累加显示宽度
        if (ctrlState & (LEFT_CTRL_PRESSED | RIGHT_CTRL_PRESSED)) {
            while (m_cursor < m_line.size() && m_line[m_cursor] == L' ') {
                moveWidth += GetCharWidth(m_line[m_cursor]);
                ++m_cursor;
            }
            while (m_cursor < m_line.size() && m_line[m_cursor] != L' ') {
                moveWidth += GetCharWidth(m_line[m_cursor]);
                ++m_cursor;
            }
        } else {
            // 单步右移：跨过代理对整体
            if (m_cursor + 1 < m_line.size() &&
                IsHighSurrogate(m_line[m_cursor]) &&
                IsLowSurrogate(m_line[m_cursor + 1])) {
                uint32_t cp = CombineSurrogate(m_line[m_cursor], m_line[m_cursor + 1]);
                moveWidth = GetCharWidth32(cp);
                m_cursor += 2;
            } else {
                moveWidth = GetCharWidth(m_line[m_cursor]);
                ++m_cursor;
            }
        }

        if (echoEnabled) {
            AppendC(vtOut, moveWidth);
        }
        SyncCursor();
        return false;
    }

    // ---- Home ----
    // 后退到行首，距离 = 光标之前所有字符的显示宽度
    if (vk == VK_HOME) {
        if (m_cursor == 0) return false;

        size_t oldCursor = m_cursor;
        m_cursor = 0;

        if (echoEnabled) {
            AppendD(vtOut, DisplayWidth(m_line, 0, oldCursor));
        }
        SyncCursor();
        return false;
    }

    // ---- End ----
    // 前进到行末，距离 = 光标之后所有字符的显示宽度
    if (vk == VK_END) {
        size_t lineLen = m_line.size();
        if (m_cursor >= lineLen) return false;

        size_t oldCursor = m_cursor;
        m_cursor = lineLen;

        if (echoEnabled) {
            AppendC(vtOut, DisplayWidth(m_line, oldCursor));
        }
        SyncCursor();
        return false;
    }

    // ---- Esc：清空当前输入（保留 prompt）----
    // conhost 行为：Esc 只清除用户输入的命令，prompt（如 C:\>）保留不动
    // 光标移回输入起点（prompt 之后），擦除从光标到行末
    // 后退距离按显示宽度计算（CJK 字符占 2 列）
    if (vk == VK_ESCAPE) {
        // 在 clear 之前先计算光标到行首的显示宽度（m_line 清空后无法再算）
        int backWidth = DisplayWidth(m_line, 0, m_cursor);
        m_line.clear();
        m_cursor = 0;

        if (echoEnabled) {
            // 后退 backWidth 列到输入起点（prompt 之后）
            // 不用 \r：\r 会回到行首（prompt 之前），导致 prompt 被擦除
            AppendD(vtOut, backWidth);
            // \x1b[0K 擦除从光标到行末，保留 prompt
            // 不用 \x1b[2K：那会擦除整行包括 prompt
            vtOut += "\x1b[0K";
        }
        SyncCursor();
        return false;
    }

    // ---- 上箭头：历史导航（前一个）----
    if (vk == VK_UP) {
        NavigateHistory(-1, vtOut);
        return false;
    }

    // ---- 下箭头：历史导航（下一个）----
    if (vk == VK_DOWN) {
        NavigateHistory(1, vtOut);
        return false;
    }

    // ---- 普通字符（可打印）----
    // UnicodeChar != 0 且不是上述特殊键
    //
    // 代理对处理：
    //   ReadConsoleInputW 把 BMP 外字符（如 emoji U+1F600）拆成高代理+
    //   低代理两个 KEY_EVENT 依次送达。LineEditor 必须缓存高代理，等低
    //   代理到来后组合为完整 32 位 codepoint，作为 UTF-16 代理对（2 个
    //   wchar_t）插入 m_line，并输出组合字符的 UTF-8（4 字节）回显。
    //   - 若只收到高代理后接非低代理：丢弃缓存（孤立高代理不插入）
    //   - 若未缓存高代理却收到低代理：按普通字符插入（让 UTF-8 编码器
    //     自行处理，通常输出 U+FFFD）
    if (ch != 0 && ch != L'\n') {
        // ---- 高代理：缓存，等待低代理 ----
        if (IsHighSurrogate(ch)) {
            // 连续两个高代理：旧的丢弃（孤立高代理不插入）
            m_hasHighSurrogate = true;
            m_highSurrogate = ch;
            return false;
        }

        // ---- 低代理 + 已缓存高代理：组合为完整 codepoint ----
        if (IsLowSurrogate(ch) && m_hasHighSurrogate) {
            wchar_t high = m_highSurrogate;
            m_hasHighSurrogate = false;

            // 作为 UTF-16 代理对插入（2 个 wchar_t）
            size_t insertPos = m_cursor;
            m_line.insert(m_cursor, 1, high);
            m_line.insert(m_cursor + 1, 1, ch);
            m_cursor += 2;

            if (echoEnabled) {
                if (m_cursor == m_line.size()) {
                    // 光标在行末：直接输出组合字符的 UTF-8
                    // WideCharToMultiByte(CP_UTF8) 把代理对编码为 4 字节 UTF-8
                    std::wstring pair(1, high);
                    pair += ch;
                    vtOut += WToUtf8(pair);
                } else {
                    // 光标在中间：输出从插入点开始的后续字符，后退到光标
                    // 后退距离按显示列宽（emoji 占 2 列，由 wcwidth32 计算）
                    std::wstring tail = m_line.substr(insertPos);
                    vtOut += WToUtf8(tail);
                    AppendD(vtOut, DisplayWidth(m_line, m_cursor));
                }
            }
            SyncCursor();
            return false;
        }

        // ---- 普通字符（非代理，或孤立低代理）----
        // 若之前缓存了高代理但当前不是低代理，丢弃缓存
        m_hasHighSurrogate = false;

        size_t insertPos = m_cursor;  // 插入前的光标位置
        m_line.insert(m_cursor, 1, ch);
        ++m_cursor;

        if (echoEnabled) {
            if (m_cursor == m_line.size()) {
                // 光标在行末：直接输出字符
                vtOut += WToUtf8(std::wstring(1, ch));
            } else {
                // 光标在中间：输出新字符 + 后续字符，后退到光标位置
                // WT 光标在 insertPos，直接输出 m_line[insertPos:] 即可
                std::wstring tail = m_line.substr(insertPos);
                vtOut += WToUtf8(tail);
                // 后退到 m_cursor（按显示列宽，CJK 字符占 2 列）
                AppendD(vtOut, DisplayWidth(m_line, m_cursor));
            }
        }
        SyncCursor();
        return false;
    }

    // 其他按键（如纯 Shift/Ctrl/Alt 按下）：忽略
    return false;
}

} // namespace terminjector
