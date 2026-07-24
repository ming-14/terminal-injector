// InputRecordToVt 实现：INPUT_RECORD → UTF-8 VT 输入序列
// 详见 InputRecordToVt.h
//
// 实现要点：
//   - 只处理 bKeyDown=TRUE 的键盘事件（VT 序列不区分按下/释放）
//   - 代理对跨事件缓存：高代理按下 → 缓存，低代理按下 → 组合输出 4 字节 UTF-8
//   - Alt 修饰：字符前加 \x1b 前缀
//   - 修饰键单独按下（VK_SHIFT/CONTROL/MENU, ch=0）忽略
//   - 鼠标事件转为 SGR 1006 格式，跨事件维护按键状态
#include "InputRecordToVt.h"
#include "logging/Logger.h"

#include <cstdio>
#include <cstring>

namespace terminjector {

// ============================================================
// 公开入口：转换单个 INPUT_RECORD
// ============================================================
void InputRecordToVt::Convert(const INPUT_RECORD& rec, std::string& out) {
    switch (rec.EventType) {
        case KEY_EVENT:
            ConvertKey(rec.Event.KeyEvent, out);
            break;
        case MOUSE_EVENT:
            ConvertMouse(rec.Event.MouseEvent, out);
            break;
        // WINDOW_BUFFER_SIZE_EVENT / FOCUS_EVENT / MENU_EVENT 忽略
        default:
            break;
    }
}

// ============================================================
// 键盘事件转换
// ============================================================
void InputRecordToVt::ConvertKey(const KEY_EVENT_RECORD& ke, std::string& out) {
    // 只处理按下事件（VT 序列不区分按下/释放）
    if (!ke.bKeyDown) return;

    WORD vk = ke.wVirtualKeyCode;
    wchar_t ch = ke.uChar.UnicodeChar;
    DWORD ctrl = ke.dwControlKeyState;
    bool altPressed = (ctrl & LEFT_ALT_PRESSED) != 0;

    // ---- 修饰键单独按下（ch=0）忽略 ----
    if (IsModifierOnlyKey(vk, ch)) return;

    // ---- 特殊键（方向键/功能键/编辑键）----
    // 这些键 UnicodeChar 可能为 0，靠 vk 识别
    // Alt+方向键等也需要转，放这里处理
    switch (vk) {
        case VK_UP:
        case VK_DOWN:
        case VK_RIGHT:
        case VK_LEFT:
        case VK_HOME:
        case VK_END: {
            // \x1b[1;{mod}{final} 或 \x1b[{final}（无修饰时省略 mod）
            char buf[32];
            int mod = ModifierCode(ctrl);
            const char* finalChar = nullptr;
            switch (vk) {
                case VK_UP:    finalChar = "A"; break;
                case VK_DOWN:  finalChar = "B"; break;
                case VK_RIGHT: finalChar = "C"; break;
                case VK_LEFT:  finalChar = "D"; break;
                case VK_HOME:  finalChar = "H"; break;
                case VK_END:   finalChar = "F"; break;
            }
            if (mod > 1) {
                std::snprintf(buf, sizeof(buf), "\x1b[1;%d%s", mod, finalChar);
            } else {
                std::snprintf(buf, sizeof(buf), "\x1b[%s", finalChar);
            }
            out += buf;
            return;
        }
        case VK_INSERT:
        case VK_DELETE:
        case VK_PRIOR:   // PageUp
        case VK_NEXT:    // PageDown
        case VK_F5:
        case VK_F6:
        case VK_F7:
        case VK_F8:
        case VK_F9:
        case VK_F10:
        case VK_F11:
        case VK_F12: {
            // \x1b[{code};{mod}~ 或 \x1b[{code}~（无修饰时省略 mod）
            char buf[32];
            int mod = ModifierCode(ctrl);
            int code = 0;
            switch (vk) {
                case VK_INSERT: code = 2;  break;
                case VK_DELETE: code = 3;  break;
                case VK_PRIOR:  code = 5;  break;
                case VK_NEXT:   code = 6;  break;
                case VK_F5:     code = 15; break;
                case VK_F6:     code = 17; break;
                case VK_F7:     code = 18; break;
                case VK_F8:     code = 19; break;
                case VK_F9:     code = 20; break;
                case VK_F10:    code = 21; break;
                case VK_F11:    code = 23; break;
                case VK_F12:    code = 24; break;
            }
            if (mod > 1) {
                std::snprintf(buf, sizeof(buf), "\x1b[%d;%d~", code, mod);
            } else {
                std::snprintf(buf, sizeof(buf), "\x1b[%d~", code);
            }
            out += buf;
            return;
        }
        case VK_F1:
        case VK_F2:
        case VK_F3:
        case VK_F4: {
            // SS3：\x1bOP/Q/R/S（SS3 不支持修饰键编码）
            char buf[4] = { '\x1b', 'O', '\0', '\0' };
            switch (vk) {
                case VK_F1: buf[2] = 'P'; break;
                case VK_F2: buf[2] = 'Q'; break;
                case VK_F3: buf[2] = 'R'; break;
                case VK_F4: buf[2] = 'S'; break;
            }
            out.append(buf, 3);
            return;
        }
        default:
            break;
    }

    // ---- 以下处理字符类按键（UnicodeChar != 0）----
    if (ch == 0) return;  // 纯修饰键或其他无字符按键，忽略

    // ---- Backspace：UnicodeChar=0x08 → VT 发 0x7f（与 WT/conhost 行为一致）----
    if (vk == VK_BACK) {
        if (altPressed) out += '\x1b';
        out += '\x7f';
        return;
    }

    // ---- 控制字符（< 0x20 或 0x7f）直接发送 ----
    // Enter(\r), Tab(\t), Ctrl+C(\x03), Ctrl+Z(\x1a) 等的 UnicodeChar 已是对应控制码
    if (ch < 0x20 || ch == 0x7f) {
        if (altPressed) out += '\x1b';
        out += static_cast<char>(ch & 0x7F);
        return;
    }

    // ---- 代理对处理 ----
    // 高代理（0xD800-0xDBFF）：缓存，等低代理配对
    if (ch >= 0xD800 && ch <= 0xDBFF) {
        // 如果已有缓存的高代理（连续两个高代理），前一个是孤立的，输出 U+FFFD
        if (m_hasHighSurrogate) {
            if (m_highSurrogateCtrl & LEFT_ALT_PRESSED) out += '\x1b';
            AppendUtf8(out, 0xFFFD);
        }
        m_hasHighSurrogate = true;
        m_highSurrogate = ch;
        m_highSurrogateCtrl = ctrl;
        return;
    }

    // 低代理（0xDC00-0xDFFF）：与缓存的高代理组合
    if (ch >= 0xDC00 && ch <= 0xDFFF) {
        if (m_hasHighSurrogate) {
            // 组合高代理+低代理 → 完整 codepoint → 4 字节 UTF-8
            uint32_t cp = 0x10000u
                        + ((static_cast<uint32_t>(m_highSurrogate) - 0xD800u) << 10)
                        + (static_cast<uint32_t>(ch) - 0xDC00u);
            if (altPressed || (m_highSurrogateCtrl & LEFT_ALT_PRESSED)) out += '\x1b';
            AppendUtf8(out, cp);
            m_hasHighSurrogate = false;
        } else {
            // 孤立低代理 → U+FFFD
            if (altPressed) out += '\x1b';
            AppendUtf8(out, 0xFFFD);
        }
        return;
    }

    // ---- 普通字符（BMP 内可打印字符）----
    // 如果有缓存的孤立高代理，先输出 U+FFFD
    if (m_hasHighSurrogate) {
        if (m_highSurrogateCtrl & LEFT_ALT_PRESSED) out += '\x1b';
        AppendUtf8(out, 0xFFFD);
        m_hasHighSurrogate = false;
    }

    if (altPressed) out += '\x1b';
    AppendUtf8(out, ch);
}

// ============================================================
// 鼠标事件转换 → SGR 1006
// ============================================================
// 格式：\x1b[<btn;col;row M/m
//   btn: bit0-1 按键(0=左 1=中 2=右), bit6 滚轮, bit3 Shift, bit4 Alt, bit5 Ctrl
//   col/row: 1-based（SGR 坐标）
//   M=按下, m=释放
//
// 跨事件维护按键状态：MOUSE_EVENT_RECORD 的 dwButtonState 只含当前按下的键，
// 需要对比前一次状态判断是按下还是释放
void InputRecordToVt::ConvertMouse(const MOUSE_EVENT_RECORD& me, std::string& out) {
    // 静态变量维护跨事件的按键状态（与 VtToInputRecord::ParseMouse 对应）
    static DWORD s_prevButtonState = 0;

    DWORD cur = me.dwButtonState;
    DWORD prev = s_prevButtonState;
    s_prevButtonState = cur;

    // 滚轮事件：MOUSE_WHEELED，dwButtonState 高字是增量
    if (me.dwEventFlags & MOUSE_WHEELED) {
        // 高字正=上滚(64), 负=下滚(65)
        int wheel = static_cast<int>(cur) >> 16;
        int btn = (wheel > 0) ? 64 : 65;
        // 修饰键
        if (me.dwControlKeyState & SHIFT_PRESSED) btn |= 4;
        if (me.dwControlKeyState & LEFT_ALT_PRESSED) btn |= 8;
        if (me.dwControlKeyState & LEFT_CTRL_PRESSED) btn |= 16;

        char buf[64];
        std::snprintf(buf, sizeof(buf), "\x1b[<%d;%d;%dM",
                      btn,
                      me.dwMousePosition.X + 1,
                      me.dwMousePosition.Y + 1);
        out += buf;
        return;
    }

    // 检测按键变化（按下/释放）
    // 左键: FROM_LEFT_1ST_BUTTON_PRESSED (0x1)
    // 右键: RIGHTMOST_BUTTON_PRESSED (0x2)
    // 中键: FROM_LEFT_2ND_BUTTON_PRESSED (0x4)
    struct BtnMap { DWORD mask; int sgrBtn; };
    static const BtnMap btns[] = {
        { FROM_LEFT_1ST_BUTTON_PRESSED, 0 },   // 左键
        { FROM_LEFT_2ND_BUTTON_PRESSED, 1 },   // 中键
        { RIGHTMOST_BUTTON_PRESSED,      2 },   // 右键
    };

    for (const auto& b : btns) {
        bool wasDown = (prev & b.mask) != 0;
        bool isDown = (cur & b.mask) != 0;

        if (!wasDown && isDown) {
            // 按下
            int btn = b.sgrBtn;
            if (me.dwControlKeyState & SHIFT_PRESSED) btn |= 4;
            if (me.dwControlKeyState & LEFT_ALT_PRESSED) btn |= 8;
            if (me.dwControlKeyState & LEFT_CTRL_PRESSED) btn |= 16;
            char buf[64];
            std::snprintf(buf, sizeof(buf), "\x1b[<%d;%d;%dM",
                          btn, me.dwMousePosition.X + 1, me.dwMousePosition.Y + 1);
            out += buf;
        } else if (wasDown && !isDown) {
            // 释放
            int btn = b.sgrBtn;
            if (me.dwControlKeyState & SHIFT_PRESSED) btn |= 4;
            if (me.dwControlKeyState & LEFT_ALT_PRESSED) btn |= 8;
            if (me.dwControlKeyState & LEFT_CTRL_PRESSED) btn |= 16;
            char buf[64];
            std::snprintf(buf, sizeof(buf), "\x1b[<%d;%d;%dm",
                          btn, me.dwMousePosition.X + 1, me.dwMousePosition.Y + 1);
            out += buf;
        }
    }
    // MOUSE_MOVED（无按键变化）和 DOUBLE_CLICK 不发 VT 序列
    // （WT VT 输入模式默认不报告移动事件，除非启用 mouse tracking）
}

// ============================================================
// UTF-8 编码
// ============================================================
// codepoint → UTF-8 字节，追加到 out
void InputRecordToVt::AppendUtf8(std::string& out, uint32_t cp) {
    if (cp < 0x80) {
        out += static_cast<char>(cp);
    } else if (cp < 0x800) {
        out += static_cast<char>(0xC0 | (cp >> 6));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    } else if (cp < 0x10000) {
        out += static_cast<char>(0xE0 | (cp >> 12));
        out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    } else if (cp < 0x110000) {
        out += static_cast<char>(0xF0 | (cp >> 18));
        out += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
        out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
        out += static_cast<char>(0x80 | (cp & 0x3F));
    }
    // 超出 Unicode 范围忽略
}

// ============================================================
// 修饰键编码
// ============================================================
// 返回 SGR 修饰键编码：1=无, 2=Shift, 3=Alt, 4=Shift+Alt,
//                       5=Ctrl, 6=Ctrl+Shift, 7=Ctrl+Alt, 8=Ctrl+Shift+Alt
int InputRecordToVt::ModifierCode(DWORD ctrlState) {
    int code = 1;
    if (ctrlState & SHIFT_PRESSED) code += 1;
    if (ctrlState & LEFT_ALT_PRESSED) code += 2;
    if (ctrlState & LEFT_CTRL_PRESSED) code += 4;
    return code;
}

// ============================================================
// 判断是否为单独修饰键按下
// ============================================================
bool InputRecordToVt::IsModifierOnlyKey(WORD vk, wchar_t ch) {
    if (ch != 0) return false;
    switch (vk) {
        case VK_SHIFT:
        case VK_LSHIFT:
        case VK_RSHIFT:
        case VK_CONTROL:
        case VK_LCONTROL:
        case VK_RCONTROL:
        case VK_MENU:      // Alt
        case VK_LMENU:
        case VK_RMENU:
            return true;
        default:
            return false;
    }
}

} // namespace terminjector
