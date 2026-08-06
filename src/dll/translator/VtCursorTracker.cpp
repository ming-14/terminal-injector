// VtCursorTracker 实现:见头文件设计说明
#include "VtCursorTracker.h"
#include "../state/VirtualConsoleState.h"
#include "logging/Logger.h"

#include <wcwidth.h>

#include <cstring>
#include <cwchar>

namespace terminjector {

VtCursorTracker& VtCursorTracker::Instance() {
    static VtCursorTracker inst;
    return inst;
}

void VtCursorTracker::Feed(const char* data, size_t len) {
    if (data == nullptr || len == 0) return;
    std::lock_guard<std::mutex> lock(m_lock);
    LoadDimensions();
    SyncFromVcs();
    // Phase 19 debug:采样记录 feed 前后光标,定位写后查询偏差
    static thread_local int s_feedLog = 0;
    bool logThis = s_feedLog < 40;
    if (logThis) LOG_DEBUG("VtCursorTracker::Feed: start len=%zu cursor=(%d,%d)", len, m_pos.X, m_pos.Y);
    for (size_t i = 0; i < len; ++i) {
        ProcessByte(static_cast<unsigned char>(data[i]));
    }
    // 最后一帧可能有未中断的纯文本段,结束时统一推进
    FlushText();
    CommitCursor();
    if (logThis) {
        s_feedLog++;
        LOG_DEBUG("VtCursorTracker::Feed: end cursor=(%d,%d) pending=%d", m_pos.X, m_pos.Y, m_wrapPending);
    }
}

void VtCursorTracker::Feed(const wchar_t* data, size_t len) {
    if (data == nullptr || len == 0) return;
    // 统一转 UTF-8 走字节级解析(WriteConsoleW_Detour 是 UTF-16 缓冲)
    int n = WideCharToMultiByte(CP_UTF8, 0, data, static_cast<int>(len),
                                nullptr, 0, nullptr, nullptr);
    if (n <= 0) return;
    std::string utf8(static_cast<size_t>(n), '\0');
    WideCharToMultiByte(CP_UTF8, 0, data, static_cast<int>(len),
                        utf8.data(), n, nullptr, nullptr);
    Feed(utf8.data(), utf8.size());
}

COORD VtCursorTracker::GetCursorPosition() const {
    // 直通写前的"期望位置"：追踪器最后一次 Commit 与 VCS 一致；
    // 若从未 Feed 过(如 LazyInit 对齐后首写),以 VCS 缓存为基准
    // (LazyInit 已把 VCS 对齐到 HelloAck 的 ConPTY 光标)。
    std::lock_guard<std::mutex> lock(m_lock);
    COORD pos = VirtualConsoleState::Instance().GetCursorPos();
    if (pos.X < 0) pos.X = 0;
    if (pos.Y < 0) pos.Y = 0;
    return pos;
}

// ---- 尺寸与基准 ----

void VtCursorTracker::LoadDimensions() {
    COORD size = VirtualConsoleState::Instance().GetBufferSize();
    if (size.X > 0) m_cols = size.X;
    if (size.Y > 0) m_rows = size.Y;
}

void VtCursorTracker::SyncFromVcs() {
    m_pos = VirtualConsoleState::Instance().GetCursorPos();
    if (m_pos.X < 0) m_pos.X = 0;
    if (m_pos.Y < 0) m_pos.Y = 0;
}

void VtCursorTracker::CommitCursor() {
    if (m_pos.X < 0) m_pos.X = 0;
    if (m_pos.Y < 0) m_pos.Y = 0;
    ClampCursor();
    VirtualConsoleState::Instance().SetCursorPos(m_pos);
}

void VtCursorTracker::ClampCursor() {
    if (m_pos.X >= m_cols) m_pos.X = static_cast<SHORT>(m_cols - 1);
    if (m_pos.Y >= m_rows) m_pos.Y = static_cast<SHORT>(m_rows - 1);
    if (m_pos.X < 0) m_pos.X = 0;
    if (m_pos.Y < 0) m_pos.Y = 0;
}

// ---- 滚动 ----

void VtCursorTracker::NotifyScrollOut() {
    // 仅整屏滚动(未设 DECSTBM)时滚出视口顶部才算 scrollback
    VirtualConsoleState::Instance().NotifyScrollLine();
}

void VtCursorTracker::ScrollUpInRegion(int n) {
    if (n <= 0) return;
    // 区域/全屏内容上滚 n 行,顶部滚出;光标位置不变
    if (!m_regionSet) {
        for (int i = 0; i < n; ++i) NotifyScrollOut();
    }
}

void VtCursorTracker::ScrollDownInRegion(int n) {
    (void)n; // 内容下移,顶部空;不产生 scrollback,光标不动
}

void VtCursorTracker::Linefeed() {
    int bottom = m_regionSet ? m_regionBottom : (m_rows - 1);
    if (m_pos.Y + 1 > bottom) {
        ScrollUpInRegion(1);
        m_pos.Y = static_cast<SHORT>(bottom);
    } else {
        m_pos.Y++;
    }
}

void VtCursorTracker::ReverseIndex() {
    int top = m_regionSet ? m_regionTop : 0;
    if (m_pos.Y <= top) {
        ScrollDownInRegion(1);
        m_pos.Y = static_cast<SHORT>(top);
    } else {
        m_pos.Y--;
    }
}

// ---- 文本推进 ----

void VtCursorTracker::FlushText() {
    if (m_textBuf.empty()) return;
    for (uint32_t cp : m_textBuf) {
        PutCodepoint(cp);
    }
    m_textBuf.clear();
}

void VtCursorTracker::PutCodepoint(uint32_t cp) {
    switch (cp) {
        case 0x0D:  // CR:回行首,清除 wrap pending
            m_pos.X = 0;
            m_wrapPending = false;
            return;
        case 0x0A:  // LF:ConPTY 视为 CR+LF(与 WriteConsoleW 翻译路径一致)
            m_pos.X = 0;
            m_wrapPending = false;
            Linefeed();
            return;
        case 0x08:  // BS
            m_wrapPending = false;
            if (m_pos.X > 0) m_pos.X--;
            return;
        case 0x09: { // TAB:下一 8 列 tab stop
            int next = ((m_pos.X / 8) + 1) * 8;
            m_pos.X = static_cast<SHORT>(next < m_cols ? next : m_cols - 1);
            return;
        }
        case 0x07:
            return; // BEL 忽略
        default:
            break;
    }
    if (cp < 0x20 || cp == 0x7F) return; // 其余 C0/DEL 忽略

    int w = wcwidth32(cp);
    if (w < 0) w = 0;

    int lastCol = m_cols - 1;
    if (m_wrapPending) {
        // 上一字符在末列:本字符到达时先换行
        m_wrapPending = false;
        m_pos.X = 0;
        Linefeed();
    }
    if (w == 0) return; // 组合/零宽:不推进

    if (m_pos.X + w > lastCol) {
        if (m_autoWrap) {
            if (w == 1 && m_pos.X == lastCol) {
                // 末列写宽度 1 字符:光标停在末列,标记 wrap pending
                m_wrapPending = true;
                return;
            }
            // 双宽放不下或坐标靠后:换行后写入
            m_pos.X = 0;
            Linefeed();
            m_pos.X = static_cast<SHORT>(m_pos.X + w);
        } else {
            m_pos.X = static_cast<SHORT>(lastCol);
        }
        return;
    }
    m_pos.X = static_cast<SHORT>(m_pos.X + w);
}

// ---- DECSC / 备用屏 ----

void VtCursorTracker::SaveCursor() {
    m_savedValid = true;
    m_savedPos = m_pos;
    m_savedWrap = m_wrapPending;
}

void VtCursorTracker::RestoreCursor() {
    if (!m_savedValid) return;
    m_pos = m_savedPos;
    m_wrapPending = m_savedWrap;
}

void VtCursorTracker::EnterAltScreen(bool /*restore*/) {
    if (m_altActive) return;
    m_altActive = true;
    m_altSavedPos = m_pos; // 保存主屏光标基准(内容不跟踪)
}

void VtCursorTracker::ExitAltScreen(bool restore) {
    if (!m_altActive) return;
    m_altActive = false;
    if (restore) {
        m_pos = m_altSavedPos;
        m_wrapPending = false;
    }
}

void VtCursorTracker::ResetState() { // RIS:复位全部状态
    m_autoWrap = true;
    if (m_altActive) {
        m_altActive = false;
        m_pos = m_altSavedPos;
    }
    m_regionSet = false;
    m_pos.X = 0;
    m_pos.Y = 0;
    m_wrapPending = false;
    m_savedValid = false;
    VirtualConsoleState::Instance().ResetScrollback();
}

// ---- 字节状态机 ----

void VtCursorTracker::ResetCsi() {
    m_params.clear();
    m_curParamVal = 0;
    m_hasParams = false;
    m_question = false;
    m_inter.clear();
    m_final = 0;
}

int VtCursorTracker::CsiParam(size_t i, int dft) const {
    if (m_hasParams && i < m_params.size()) {
        int v = m_params[i];
        return v <= 0 ? 1 : v; // xterm:参数 0 解释为 1
    }
    return dft;
}

void VtCursorTracker::DispatchCsi() {
    switch (m_final) {
        case 'H': case 'f': {  // CUP / HVP:row;col(1-based)
            int row = CsiParam(0, 1) - 1;
            int col = CsiParam(1, 1) - 1;
            m_pos.Y = static_cast<SHORT>(row);
            m_pos.X = static_cast<SHORT>(col);
            m_wrapPending = false;
            break;
        }
        case 'A': {  // CUU
            m_pos.Y = static_cast<SHORT>(m_pos.Y - CsiParam(0, 1));
            break;
        }
        case 'B': case 'e': {  // CUD / VPR
            m_pos.Y = static_cast<SHORT>(m_pos.Y + CsiParam(0, 1));
            break;
        }
        case 'C': case 'a': {  // CUF / HPR
            m_pos.X = static_cast<SHORT>(m_pos.X + CsiParam(0, 1));
            m_wrapPending = false;
            break;
        }
        case 'D': {  // CUB
            m_pos.X = static_cast<SHORT>(m_pos.X - CsiParam(0, 1));
            m_wrapPending = false;
            break;
        }
        case 'E': {  // CNL:行首下移 n
            m_pos.X = 0;
            m_pos.Y = static_cast<SHORT>(m_pos.Y + CsiParam(0, 1));
            break;
        }
        case 'F': {  // CPL:行首上移 n
            m_pos.X = 0;
            m_pos.Y = static_cast<SHORT>(m_pos.Y - CsiParam(0, 1));
            break;
        }
        case 'G': case '`': {  // CHA / HPA:绝对列
            m_pos.X = static_cast<SHORT>(CsiParam(0, 1) - 1);
            m_wrapPending = false;
            break;
        }
        case 'd': {  // VPA:绝对行
            m_pos.Y = static_cast<SHORT>(CsiParam(0, 1) - 1);
            break;
        }
        case 'I': case 'Z': {  // CHT / CBT:tab stop
            int n = CsiParam(0, 1);
            if (m_final == 'I') {
                for (int i = 0; i < n; ++i) {
                    int next = ((m_pos.X / 8) + 1) * 8;
                    if (next >= m_cols) { m_pos.X = static_cast<SHORT>(m_cols - 1); break; }
                    m_pos.X = static_cast<SHORT>(next);
                }
            } else {
                for (int i = 0; i < n; ++i) {
                    if (m_pos.X <= 0) break;
                    m_pos.X = static_cast<SHORT>(((m_pos.X - 1) / 8) * 8);
                }
            }
            m_wrapPending = false;
            break;
        }
        case 'S':  // SU:区域上滚 n,光标不动
            ScrollUpInRegion(CsiParam(0, 1));
            break;
        case 'T':  // SD:区域下滚 n,光标不动
            ScrollDownInRegion(CsiParam(0, 1));
            break;
        case 'r': {  // DECSTBM:滚动区
            int p1 = m_params.size() >= 2 ? m_params[0] : 0;
            int p2 = m_params.size() >= 2 ? m_params[1] : 0;
            if (m_params.size() < 2 || (p1 == 0 && p2 == 0)) {
                // 无参数:恢复全屏滚动区
                m_regionSet = false;
                m_pos.X = 0;
                m_pos.Y = 0;
            } else if (p1 >= 1 && p2 >= 1 && p1 < p2) {
                m_regionSet = true;
                m_regionTop = p1 - 1;
                m_regionBottom = p2 - 1;
                // 光标移到区域左上角
                m_pos.X = 0;
                m_pos.Y = static_cast<SHORT>(m_regionTop);
            }
            m_wrapPending = false;
            break;
        }
        case 'J': {  // ED:清屏(光标不动);ED3 清滚存
            int ps = m_hasParams ? m_params[0] : 0;
            if (ps == 3) {
                VirtualConsoleState::Instance().ResetScrollback();
            }
            break;
        }
        case 's':  // ANSI.SYS 保存光标
            SaveCursor();
            break;
        case 'u':  // ANSI.SYS 恢复光标
            RestoreCursor();
            break;
        default:
            // EL/ED/IL/DL/ICH/DCH/ECH/SGR/DSR/DA/DECSM 等:不影响光标,忽略
            break;
    }
    ClampCursor();
}

void VtCursorTracker::DispatchDecSet(bool enable) {
    for (int ps : m_params) {
        switch (ps) {
            case 7:   // DECAWM:自动换行
                if (enable) { m_autoWrap = true; }
                else { m_autoWrap = false; if (m_wrapPending) m_wrapPending = false; }
                break;
            case 47:  // 备用屏(与 1047 相同),只切屏不恢复光标
                if (enable) EnterAltScreen(false);
                else ExitAltScreen(false);
                break;
            case 1047:
                if (enable) EnterAltScreen(false);
                else ExitAltScreen(false);
                break;
            case 1048:  // 仅保存/恢复光标
                if (enable) SaveCursor();
                else RestoreCursor();
                break;
            case 1049:  // 备用屏 + 光标保存/恢复
                if (enable) {
                    if (!m_altActive) { SaveCursor(); EnterAltScreen(true); }
                } else {
                    ExitAltScreen(true);
                }
                break;
            default:
                // 鼠标模式/光标可见性/键盘等:不影响位置,忽略
                break;
        }
    }
    ClampCursor();
}

void VtCursorTracker::ProcessByte(unsigned char b) {
    switch (m_state) {
        case State::Ground:
            if (b == 0x1B) {
                FlushText();
                m_state = State::Esc;
            } else if (b == 0x0D || b == 0x0A || b == 0x08 || b == 0x09 || b == 0x07) {
                m_textBuf.push_back(b); // 控制字符随文本段一起推进
            } else if (b >= 0x20 && b <= 0x7E) {
                m_textBuf.push_back(b);
            } else if (b >= 0x80) {
                // UTF-8 多字节:仅当无累积时由 lead byte 决定字节数;
                // 续字节禁止重算(否则被误判为 need=1 提前解码出垃圾 codepoint)
                if (m_utf8Carry.empty()) {
                    if ((b & 0xE0) == 0xC0) m_utf8Need = 2;
                    else if ((b & 0xF0) == 0xE0) m_utf8Need = 3;
                    else if ((b & 0xF8) == 0xF0) m_utf8Need = 4;
                    else { m_utf8Carry.clear(); break; } // 非法起始,丢弃
                }
                m_utf8Carry.push_back(static_cast<char>(b));
                if (static_cast<int>(m_utf8Carry.size()) >= m_utf8Need) {
                    uint32_t cp = 0;
                    const unsigned char* u =
                        reinterpret_cast<const unsigned char*>(m_utf8Carry.data());
                    size_t n = m_utf8Carry.size();
                    if (n == 2) cp = ((u[0] & 0x1F) << 6) | (u[1] & 0x3F);
                    else if (n == 3) cp = ((u[0] & 0x0F) << 12) | ((u[1] & 0x3F) << 6) | (u[2] & 0x3F);
                    else if (n == 4) cp = ((u[0] & 0x07) << 18) | ((u[1] & 0x3F) << 12) |
                                           ((u[2] & 0x3F) << 6) | (u[3] & 0x3F);
                    m_textBuf.push_back(cp);
                    m_utf8Carry.clear();
                }
            }
            break;

        case State::Esc:
            if (b == '[') {
                ResetCsi();
                m_state = State::Csi;
            } else if (b == ']') {
                m_escSeen = false;
                m_state = State::Osc;
            } else if (b == '(' || b == ')' || b == '*' || b == '+') {
                m_state = State::CharsetSel;
            } else if (b == 'P' || b == '_' || b == '^') {
                // DCS / APC / PM:忽略直到 ST
                m_escSeen = false;
                m_state = State::Dcs;
            } else if (b == '7') {
                SaveCursor();
                m_state = State::Ground;
            } else if (b == '8') {
                RestoreCursor();
                m_state = State::Ground;
            } else if (b == 'D') {   // IND
                m_pos.X = 0;
                Linefeed();
                m_state = State::Ground;
            } else if (b == 'M') {   // RI
                ReverseIndex();
                m_state = State::Ground;
            } else if (b == 'E') {   // NEL:CR+LF
                m_pos.X = 0;
                Linefeed();
                m_state = State::Ground;
            } else if (b == 'c') {   // RIS
                ResetState();
                m_state = State::Ground;
            } else {
                // ESC 单字符(7/8 已处理):其余(HTS 'H'、字符集、索引进给等)
                m_state = State::Ground;
            }
            break;

        case State::Csi:
            if (b >= '0' && b <= '9') {
                m_curParamVal = m_curParamVal * 10 + (b - '0');
                m_hasParams = true;
            } else if (b == ';' || b == ':') {
                m_params.push_back(m_curParamVal);
                m_curParamVal = 0;
                m_hasParams = true;
            } else if (b == '?') {
                m_question = true;
            } else if (b >= 0x20 && b <= 0x2F) {
                m_inter.push_back(static_cast<char>(b));
            } else if (b >= 0x40 && b <= 0x7E) {
                m_params.push_back(m_curParamVal);
                m_final = b;
                if (m_question && (b == 'h' || b == 'l')) {
                    DispatchDecSet(b == 'h');
                } else if (b == 'h' || b == 'l') {
                    // 非 DECSET 的 h/l(光标可见性、键盘锁定):不影响位置,忽略
                } else {
                    DispatchCsi();
                }
                ResetCsi();
                m_state = State::Ground;
            }
            break;

        case State::Osc:
            if (m_escSeen) {
                if (b == '\\') {
                    m_state = State::Ground;  // ST
                } else {
                    m_escSeen = false;        // 非 ST,继续 OSC
                }
            } else if (b == 0x07) {
                m_state = State::Ground;      // BEL 终止
            } else if (b == 0x1B) {
                m_escSeen = true;
            }
            break;

        case State::Dcs:
            if (m_escSeen) {
                if (b == '\\') {
                    m_state = State::Ground;  // ST
                } else {
                    m_escSeen = false;
                }
            } else if (b == 0x1B) {
                m_escSeen = true;
            } else if (b == 0x07) {
                m_state = State::Ground;      // 兜底 BEL 终止
            }
            break;

        case State::CharsetSel:
            m_state = State::Ground;
            break;
    }
}

} // namespace terminjector