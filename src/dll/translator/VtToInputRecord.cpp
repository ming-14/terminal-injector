// VtToInputRecord 实现：VT 输入序列 → INPUT_RECORD 翻译
// 详见 docs/phases/06-input-chain.md 4.5
//
// 翻译规则：
//   - 键盘 CSI：\x1b[A/B/C/D/F/H/~ 等 → VK_UP/DOWN/LEFT/RIGHT/END/HOME/INS/DEL/PgUp/PgDn
//   - 修饰键：\x1b[1;2A=Shift+Up, 1;5A=Ctrl+Up, 1;3A=Alt+Up
//   - SS3：\x1bOA/B/C/D/H/F（部分终端用此编码方向键）
//   - 鼠标 SGR 1006：\x1b[<btn;col;row M/m → MOUSE_EVENT_RECORD
//   - Alt+字符：\x1b<ch> → Alt 修饰 + 字符
//   - 普通字符：UTF-8 解码 → VkKeyScanW 获取 VK code
//   - 控制字符：\r=Enter, \t=Tab, \x7f=Backspace, \x03=Ctrl+C
//
// 每个按键产生两个 INPUT_RECORD（按下 bKeyDown=TRUE + 释放 bKeyDown=FALSE）
#include "VtToInputRecord.h"
#include "../state/ConsoleState.h"
#include "logging/Logger.h"

#include <windows.h>
#include <cstdio>
#include <cstring>

namespace terminjector {

// ============================================================
// 公开入口：解析完整 VT 字节流
// ============================================================
std::vector<INPUT_RECORD> VtToInputRecord::Parse(const uint8_t* data, size_t len) {
    std::vector<INPUT_RECORD> result;
    size_t i = 0;

    while (i < len) {
        uint8_t b = data[i];

        if (b == 0x1b && i + 1 < len) {
            // ESC 开头：CSI / SS3 / Alt+char
            uint8_t next = data[i + 1];
            if (next == '[') {
                // CSI 序列：先检查是否鼠标（\x1b[<）
                if (i + 2 < len && data[i + 2] == '<') {
                    // 鼠标 SGR 1006
                    size_t consumed = ParseMouse(data + i, len - i, result);
                    if (consumed > 0) {
                        i += consumed;
                    } else {
                        // 不完整或无效，跳过 ESC（VtInputParser 应保证完整，此处兜底）
                        LOG_WARN("VtToInputRecord: invalid mouse seq at %zu", i);
                        i += 1;
                    }
                } else {
                    // 键盘 CSI
                    size_t consumed = ParseCsi(data + i, len - i, result);
                    if (consumed > 0) {
                        i += consumed;
                    } else {
                        LOG_WARN("VtToInputRecord: invalid CSI seq at %zu", i);
                        i += 1;
                    }
                }
            } else if (next == 'O') {
                // SS3 序列
                size_t consumed = ParseSs3(data + i, len - i, result);
                if (consumed > 0) {
                    i += consumed;
                } else {
                    LOG_WARN("VtToInputRecord: invalid SS3 seq at %zu", i);
                    i += 1;
                }
            } else {
                // Alt+字符：\x1b<ch>
                // UTF-8 解码 ch（BMP 外字符会生成代理对，每个 wchar_t 都带 Alt 修饰）
                std::wstring chars;
                int decoded = DecodeUtf8(data + i + 1, len - i - 1, chars);
                if (decoded > 0) {
                    // VkKeyScanW 对代理项返回 -1（0xFFFF），& 0xFF = 0xFF，钳制为 0
                    for (wchar_t c : chars) {
                        WORD vk = c ? static_cast<WORD>(VkKeyScanW(c) & 0xFF) : 0;
                        if (vk == 0xFF) vk = 0;
                        MakeKeyPair(result, vk, c, LEFT_ALT_PRESSED);
                    }
                    i += 1 + static_cast<size_t>(decoded);
                } else {
                    // \x1b 后无有效字节，跳过
                    i += 1;
                }
            }
        } else if (b == 0x1b) {
            // 单独 ESC，无后续（不应发生，VtInputParser 保证完整）
            i += 1;
        } else {
            // 非 ESC：控制字符或普通字符
            if (b < 0x80) {
                // ASCII：单字节
                wchar_t ch = static_cast<wchar_t>(b);
                WORD vk = 0;
                DWORD ctrl = 0;

                switch (b) {
                    case '\r':  // Enter
                        vk = VK_RETURN;
                        ch = L'\r';
                        break;
                    case '\n':  // LF（某些终端用 LF 代替 Enter）
                        vk = VK_RETURN;
                        ch = L'\r';
                        break;
                    case '\t':  // Tab
                        vk = VK_TAB;
                        ch = L'\t';
                        break;
                    case 0x7f:  // DEL → Backspace
                        vk = VK_BACK;
                        ch = L'\b';
                        break;
                    case 0x08:  // BS → Backspace
                        vk = VK_BACK;
                        ch = L'\b';
                        break;
                    case 0x03:  // Ctrl+C
                        vk = 'C';
                        ch = L'\x03';
                        ctrl = LEFT_CTRL_PRESSED;
                        break;
                    case 0x04:  // Ctrl+D
                        vk = 'D';
                        ch = L'\x04';
                        ctrl = LEFT_CTRL_PRESSED;
                        break;
                    case 0x1a:  // Ctrl+Z
                        vk = 'Z';
                        ch = L'\x1a';
                        ctrl = LEFT_CTRL_PRESSED;
                        break;
                    case 0x15:  // Ctrl+U
                        vk = 'U';
                        ch = L'\x15';
                        ctrl = LEFT_CTRL_PRESSED;
                        break;
                    case 0x17:  // Ctrl+W
                        vk = 'W';
                        ch = L'\x17';
                        ctrl = LEFT_CTRL_PRESSED;
                        break;
                    default:
                        // 可打印 ASCII 或其他控制字符
                        if (b >= 0x20) {
                            vk = static_cast<WORD>(VkKeyScanW(ch) & 0xFF);
                        }
                        break;
                }

                // Ctrl 字母（0x01-0x1A）统一处理
                // 排除已在 switch 中设置了专用 VK 的控制字符：
                //   0x03=Ctrl+C, 0x04=Ctrl+D, 0x15=Ctrl+U, 0x17=Ctrl+W, 0x1a=Ctrl+Z
                //   0x08=BS(VK_BACK), 0x09=Tab(VK_TAB), 0x0a=LF(VK_RETURN), 0x0d=CR(VK_RETURN)
                // 不排除的话 Tab 会被覆盖为 Ctrl+I（vk='I'），导致 LineEditor 的
                // vk==VK_TAB 判断失败，Tab 补全失效；Enter 同理被覆盖为 Ctrl+M
                if (b >= 0x01 && b <= 0x1A && b != 0x03 && b != 0x04 &&
                    b != 0x1a && b != 0x15 && b != 0x17 &&
                    b != 0x08 && b != 0x09 && b != 0x0a && b != 0x0d) {
                    vk = static_cast<WORD>(b + 0x40);  // 0x01→'A', 0x02→'B'...
                    ch = static_cast<wchar_t>(b);
                    ctrl = LEFT_CTRL_PRESSED;
                }

                MakeKeyPair(result, vk, ch, ctrl);
                i += 1;
            } else {
                // UTF-8 多字节
                std::wstring chars;
                int decoded = DecodeUtf8(data + i, len - i, chars);
                if (decoded > 0) {
                    // 对每个 wchar_t 生成独立的按键事件
                    // BMP 外字符（代理对）：高代理和低代理各生成一对 INPUT_RECORD，
                    // LineEditor 会把它们依次追加到 m_line 形成完整 UTF-16 字符
                    // VkKeyScanW 对代理项返回 -1（0xFFFF），钳制为 0
                    for (wchar_t c : chars) {
                        WORD vk = static_cast<WORD>(VkKeyScanW(c) & 0xFF);
                        if (vk == 0xFF) vk = 0;
                        MakeKeyPair(result, vk, c, 0);
                    }
                    i += static_cast<size_t>(decoded);
                } else {
                    // 无效 UTF-8，跳过 1 字节
                    i += 1;
                }
            }
        }
    }

    return result;
}

// ============================================================
// CSI 序列解析（\x1b[...final）
// ============================================================
// 格式：\x1b[<param>;<param>...<final>
//   final ∈ 0x40-0x7E（@ ~ ~）
// 常见序列：
//   \x1b[A/B/C/D → 上/下/右/左
//   \x1b[H/F → Home/End
//   \x1b[2~/3~/5~/6~ → Insert/Delete/PageUp/PageDown
//   \x1b[1;2A → Shift+Up（修饰键编码：2=Shift 3=Alt 4=Shift+Alt 5=Ctrl 6=Ctrl+Shift 7=Ctrl+Alt 8=All）
size_t VtToInputRecord::ParseCsi(const uint8_t* data, size_t len,
                                  std::vector<INPUT_RECORD>& out) {
    // data[0]=ESC, data[1]='['
    if (len < 3) return 0;

    // 收集参数和 final 字符
    // 参数区间：data[2] 开始，到 final 字符（0x40-0x7E）结束
    size_t pos = 2;
    int params[4] = {0, 0, 0, 0};
    int paramCount = 0;
    bool hasParam = false;

    while (pos < len) {
        uint8_t b = data[pos];
        if (b >= 0x30 && b <= 0x39) {
            // 数字
            if (paramCount < 4) {
                params[paramCount] = params[paramCount] * 10 + (b - 0x30);
                hasParam = true;
            }
            ++pos;
        } else if (b == ';') {
            // 参数分隔符
            if (paramCount < 4) ++paramCount;
            hasParam = true;
            ++pos;
        } else if (b >= 0x40 && b <= 0x7e) {
            // final 字符
            if (hasParam) ++paramCount;  // 最后一个参数
            break;
        } else if (b == '<') {
            // 不应到这里（鼠标已在 Parse 中分流），但兜底返回 0
            return 0;
        } else {
            // 中间字节（0x20-0x2F）或非法，跳过
            ++pos;
        }
    }

    if (pos >= len) return 0;  // 未找到 final，不完整

    uint8_t finalChar = data[pos];
    size_t consumed = pos + 1;

    // 解析修饰键（最后一个参数如果是修饰键编码）
    // 修饰键编码在参数的最后一个数字中：1=无, 2=Shift, 3=Alt, 4=Shift+Alt,
    // 5=Ctrl, 6=Ctrl+Shift, 7=Ctrl+Alt, 8=Ctrl+Alt+Shift
    DWORD ctrlState = 0;
    int modifier = (paramCount >= 2) ? params[paramCount - 1] : 1;
    if (modifier == 0) modifier = 1;  // 0 等同于 1（无修饰）

    switch (modifier) {
        case 2: ctrlState = SHIFT_PRESSED; break;
        case 3: ctrlState = LEFT_ALT_PRESSED; break;
        case 4: ctrlState = SHIFT_PRESSED | LEFT_ALT_PRESSED; break;
        case 5: ctrlState = LEFT_CTRL_PRESSED; break;
        case 6: ctrlState = SHIFT_PRESSED | LEFT_CTRL_PRESSED; break;
        case 7: ctrlState = LEFT_CTRL_PRESSED | LEFT_ALT_PRESSED; break;
        case 8: ctrlState = SHIFT_PRESSED | LEFT_CTRL_PRESSED | LEFT_ALT_PRESSED; break;
        default: ctrlState = 0; break;
    }

    WORD vk = 0;
    wchar_t ch = 0;

    switch (finalChar) {
        case 'A': vk = VK_UP;    break;
        case 'B': vk = VK_DOWN;  break;
        case 'C': vk = VK_RIGHT; break;
        case 'D': vk = VK_LEFT;  break;
        case 'H': vk = VK_HOME;  break;
        case 'F': vk = VK_END;   break;
        case '~': {
            // 功能键：参数决定具体键
            int code = (paramCount >= 1) ? params[0] : 0;
            switch (code) {
                case 1:  vk = VK_HOME;    break;  // Home
                case 2:  vk = VK_INSERT;  break;  // Insert
                case 3:  vk = VK_DELETE;  break;  // Delete
                case 4:  vk = VK_END;     break;  // End
                case 5:  vk = VK_PRIOR;   break;  // PageUp
                case 6:  vk = VK_NEXT;    break;  // PageDown
                case 11: vk = VK_F1;      break;
                case 12: vk = VK_F2;      break;
                case 13: vk = VK_F3;      break;
                case 14: vk = VK_F4;      break;
                case 15: vk = VK_F5;      break;
                case 17: vk = VK_F6;      break;
                case 18: vk = VK_F7;      break;
                case 19: vk = VK_F8;      break;
                case 20: vk = VK_F9;      break;
                case 21: vk = VK_F10;     break;
                case 23: vk = VK_F11;     break;
                case 24: vk = VK_F12;     break;
                default:
                    LOG_DEBUG("VtToInputRecord: unknown ~ code=%d", code);
                    return consumed;  // 消费但不生成事件
            }
            break;
        }
        default:
            LOG_DEBUG("VtToInputRecord: unknown CSI final=0x%02X", finalChar);
            return consumed;  // 消费但不生成事件
    }

    MakeKeyPair(out, vk, ch, ctrlState);
    return consumed;
}

// ============================================================
// SS3 序列解析（\x1bOx）
// ============================================================
size_t VtToInputRecord::ParseSs3(const uint8_t* data, size_t len,
                                  std::vector<INPUT_RECORD>& out) {
    // data[0]=ESC, data[1]='O'
    if (len < 3) return 0;

    uint8_t finalChar = data[2];
    WORD vk = 0;

    switch (finalChar) {
        case 'A': vk = VK_UP;    break;
        case 'B': vk = VK_DOWN;  break;
        case 'C': vk = VK_RIGHT; break;
        case 'D': vk = VK_LEFT;  break;
        case 'H': vk = VK_HOME;  break;
        case 'F': vk = VK_END;   break;
        case 'P': vk = VK_F1;    break;
        case 'Q': vk = VK_F2;    break;
        case 'R': vk = VK_F3;    break;
        case 'S': vk = VK_F4;    break;
        default:
            return 0;  // 不识别
    }

    MakeKeyPair(out, vk, 0, 0);
    return 3;
}

// ============================================================
// 鼠标 SGR 1006 解析（\x1b[<btn;col;row M/m）
// ============================================================
size_t VtToInputRecord::ParseMouse(const uint8_t* data, size_t len,
                                    std::vector<INPUT_RECORD>& out) {
    // data[0]=ESC, data[1]='[', data[2]='<'
    if (len < 3 || data[2] != '<') return 0;

    // 手动解析 btn;col;row<type>
    int values[3] = {0, 0, 0};
    int valueIdx = 0;
    size_t pos = 3;
    bool hasValue = false;

    while (pos < len) {
        uint8_t b = data[pos];
        if (b >= 0x30 && b <= 0x39) {
            if (valueIdx < 3) {
                values[valueIdx] = values[valueIdx] * 10 + (b - 0x30);
                hasValue = true;
            }
            ++pos;
        } else if (b == ';') {
            if (valueIdx < 3) ++valueIdx;
            hasValue = true;
            ++pos;
        } else if (b == 'M' || b == 'm') {
            // 结束符
            if (hasValue) ++valueIdx;
            if (valueIdx < 3) return 0;  // 参数不足

            int btn = values[0];
            int col = values[1];
            int row = values[2];
            bool isRelease = (b == 'm');

            MOUSE_EVENT_RECORD mer{};
            // SGR 坐标是 1-based，转 0-based
            mer.dwMousePosition.X = static_cast<SHORT>(col - 1);
            mer.dwMousePosition.Y = static_cast<SHORT>(row - 1);

            // 修饰键（位组合）
            if (btn & 8)  mer.dwControlKeyState |= SHIFT_PRESSED;
            if (btn & 16) mer.dwControlKeyState |= LEFT_ALT_PRESSED;
            if (btn & 32) mer.dwControlKeyState |= LEFT_CTRL_PRESSED;

            int baseBtn = btn & 3;
            int wheel = btn & 64;

            if (wheel) {
                // 滚轮事件（SGR 编码：baseBtn 0=上滚 1=下滚 2=左横滚 3=右横滚）
                if (baseBtn == 2 || baseBtn == 3) {
                    // 横滚（LIM-006 修复）：66=64+2 左滚，67=64+3 右滚
                    // MOUSE_HWHEELED：dwButtonState 高字为正=右滚，负=左滚
                    mer.dwEventFlags = MOUSE_HWHEELED;
                    mer.dwButtonState = (baseBtn == 3) ? 0x00010000 : 0xFFFF0000;
                } else {
                    // 垂直滚轮
                    mer.dwEventFlags = MOUSE_WHEELED;
                    mer.dwButtonState = (baseBtn == 0) ? 0x00010000 : 0xFFFF0000;
                }
            } else {
                // 普通按键/移动
                // 使用 ConsoleState 维护跨事件的按键状态（Phase 16 修复：
                // 替代静态变量，消除多会话/模式切换时的状态污染）
                DWORD s_buttonState = ConsoleState::Instance().GetMouseButtonState();

                if (isRelease || baseBtn == 3) {
                    // 释放：清除所有按键位
                    s_buttonState &= ~(FROM_LEFT_1ST_BUTTON_PRESSED |
                                       FROM_LEFT_2ND_BUTTON_PRESSED |
                                       RIGHTMOST_BUTTON_PRESSED);
                    mer.dwEventFlags = 0;
                } else {
                    switch (baseBtn) {
                        case 0:
                            s_buttonState |= FROM_LEFT_1ST_BUTTON_PRESSED;
                            break;
                        case 1:
                            s_buttonState |= FROM_LEFT_2ND_BUTTON_PRESSED;
                            break;
                        case 2:
                            s_buttonState |= RIGHTMOST_BUTTON_PRESSED;
                            break;
                        default:
                            break;
                    }
                    mer.dwEventFlags = 0;  // 按下事件
                }
                mer.dwButtonState = s_buttonState;
                ConsoleState::Instance().SetMouseButtonState(s_buttonState);
            }

            INPUT_RECORD r{};
            r.EventType = MOUSE_EVENT;
            r.Event.MouseEvent = mer;
            out.push_back(r);

            return pos + 1;
        } else {
            // 非法字符
            return 0;
        }
    }

    return 0;  // 不完整
}

// ============================================================
// 构造按键事件
// ============================================================
void VtToInputRecord::MakeKeyPair(std::vector<INPUT_RECORD>& out,
                                   WORD vk, wchar_t ch, DWORD ctrlState) {
    out.push_back(MakeKeyRecord(true,  vk, ch, ctrlState));
    out.push_back(MakeKeyRecord(false, vk, ch, ctrlState));
}

INPUT_RECORD VtToInputRecord::MakeKeyRecord(bool down, WORD vk, wchar_t ch,
                                             DWORD ctrlState) {
    INPUT_RECORD r{};
    r.EventType = KEY_EVENT;
    r.Event.KeyEvent.bKeyDown          = down ? TRUE : FALSE;
    r.Event.KeyEvent.wRepeatCount      = 1;
    r.Event.KeyEvent.wVirtualKeyCode   = vk;
    // VirtualScanCode：通过 MapVirtualKeyW 获取，0 表示无对应扫描码
    r.Event.KeyEvent.wVirtualScanCode  = vk ? static_cast<WORD>(
        MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)) : 0;
    r.Event.KeyEvent.uChar.UnicodeChar = ch;
    r.Event.KeyEvent.dwControlKeyState = ctrlState;
    return r;
}

// ============================================================
// UTF-8 解码
// ============================================================
// 解码 1 个 UTF-8 字符，返回 consumed 字节数（1-4），0 表示无效
// out 追加解码后的 wchar_t：
//   - ASCII / 2字节 / 3字节（BMP 内）：追加 1 个 wchar_t
//   - 4字节（BMP 外，如 emoji U+1F600）：追加 UTF-16 代理对（高代理 + 低代理）
// 调用方需为 out 中每个 wchar_t 生成独立的 INPUT_RECORD，
// 这样 BMP 外字符会发送两个 KEY_EVENT，LineEditor 依次追加形成完整 UTF-16
int VtToInputRecord::DecodeUtf8(const uint8_t* p, size_t len, std::wstring& out) {
    if (len == 0) return 0;

    uint8_t b = p[0];
    if (b < 0x80) {
        // ASCII
        out.push_back(static_cast<wchar_t>(b));
        return 1;
    } else if ((b & 0xE0) == 0xC0) {
        // 2 字节
        if (len < 2 || (p[1] & 0xC0) != 0x80) return 0;
        out.push_back(static_cast<wchar_t>(((b & 0x1F) << 6) | (p[1] & 0x3F)));
        return 2;
    } else if ((b & 0xF0) == 0xE0) {
        // 3 字节
        if (len < 3 || (p[1] & 0xC0) != 0x80 || (p[2] & 0xC0) != 0x80) return 0;
        out.push_back(static_cast<wchar_t>(((b & 0x0F) << 12) |
                                            ((p[1] & 0x3F) << 6) |
                                            (p[2] & 0x3F)));
        return 3;
    } else if ((b & 0xF8) == 0xF0) {
        // 4 字节 UTF-8 → UTF-16 代理对
        // BMP 外字符（如 emoji）需要生成高代理 + 低代理两个 wchar_t，
        // 调用方为每个 wchar_t 生成独立的 INPUT_RECORD
        if (len < 4 || (p[1] & 0xC0) != 0x80 ||
            (p[2] & 0xC0) != 0x80 || (p[3] & 0xC0) != 0x80) return 0;
        uint32_t cp = ((b & 0x07) << 18) |
                      ((p[1] & 0x3F) << 12) |
                      ((p[2] & 0x3F) << 6) |
                      (p[3] & 0x3F);
        if (cp < 0x10000) {
            // 4 字节编码但落在 BMP 内（非最短编码，罕见，兜底直接输出）
            out.push_back(static_cast<wchar_t>(cp));
            return 4;
        }
        // BMP 外：生成 UTF-16 代理对
        // cp - 0x10000 拆成高 10 位（高代理）和低 10 位（低代理）
        cp -= 0x10000;
        out.push_back(static_cast<wchar_t>(0xD800 + (cp >> 10)));    // 高代理
        out.push_back(static_cast<wchar_t>(0xDC00 + (cp & 0x3FF)));  // 低代理
        return 4;
    }
    return 0;
}

} // namespace terminjector
