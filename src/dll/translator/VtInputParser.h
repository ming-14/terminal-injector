// VT 输入流分帧状态机
// 详见 docs/phases/06-input-chain.md 4.5.5
//
// 职责：把可能不完整的字节流切分为完整 VT 序列，再交给 VtToInputRecord 翻译
//   - 一次 IPC Recv 可能拿到半个 CSI 序列或多条序列拼接
//   - 本类维护内部缓冲 m_buf，Feed 喂入字节后返回已完整的 INPUT_RECORD 数组
//   - 不完整部分留在 m_buf 等下次 Feed 补全
//
// 分帧规则：
//   - ESC (\x1b) + '[' → CSI：找 0x40-0x7E 结束字节
//   - ESC + 'O' → SS3：3 字节（ESC O final）
//   - ESC + 其他 → Alt+char：UTF-8 解码 ch
//   - 非 ESC → 控制字符（1 字节）或 UTF-8 多字节字符
#pragma once

#include "VtToInputRecord.h"
#include <string>
#include <vector>
#include <cstdint>
#include <windows.h>

namespace terminjector {

class VtInputParser {
public:
    // 喂入字节流，返回当前已完整的序列翻译后的 INPUT_RECORD 数组
    // 不完整的序列留在内部缓冲，等下次 Feed 补全
    std::vector<INPUT_RECORD> Feed(const uint8_t* data, size_t len) {
        // 追加到缓冲
        m_buf.append(reinterpret_cast<const char*>(data), len);

        std::vector<INPUT_RECORD> result;
        size_t i = 0;
        size_t bufLen = m_buf.size();

        while (i < bufLen) {
            size_t seqLen = CalcSequenceLength(m_buf, i, bufLen);
            if (seqLen == 0) {
                // 不完整序列，停止解析
                break;
            }
            // 翻译完整序列
            auto records = VtToInputRecord::Parse(
                reinterpret_cast<const uint8_t*>(m_buf.data() + i), seqLen);
            for (auto& r : records) {
                result.push_back(std::move(r));
            }
            i += seqLen;
        }

        // 移除已消费部分，保留未完整部分
        if (i > 0) {
            m_buf.erase(0, i);
        }

        return result;
    }

    // 清空缓冲（模式切换时调用，避免残留半序列）
    void Reset() {
        m_buf.clear();
    }

    // 仅分帧：返回缓冲中所有已完整的 VT 序列原始字节（不做翻译）
    // 供 VT 透传分支使用：鼠标 SGR 序列需保留原始字节逐字节展开为
    // KEY_EVENT（mimo/libuv 等字节流消费者只认 KEY_EVENT，需还原原始字节，
    // 见 DllRecvLoop），键盘序列则由调用方调 VtToInputRecord::Parse 翻译
    // 不完整部分继续留在 m_buf 等待后续数据（与 Feed 相同的分帧规则）
    std::vector<std::string> FrameRaw(const uint8_t* data, size_t len) {
        m_buf.append(reinterpret_cast<const char*>(data), len);

        std::vector<std::string> result;
        size_t i = 0;
        size_t bufLen = m_buf.size();

        while (i < bufLen) {
            size_t seqLen = CalcSequenceLength(m_buf, i, bufLen);
            if (seqLen == 0) {
                // 不完整序列，停止解析
                break;
            }
            result.emplace_back(m_buf, i, seqLen);
            i += seqLen;
        }

        // 移除已消费部分，保留未完整部分
        if (i > 0) {
            m_buf.erase(0, i);
        }

        return result;
    }

    // 检查是否有悬挂的不完整序列（如单独 ESC 等待超时交付）
    bool HasPending() const {
        return !m_buf.empty();
    }

    // 刷新悬挂的不完整序列
    // 场景：用户按 Esc，WT 发送单独 0x1b，Feed() 因可能是 CSI/SS3/Alt 序列开头
    //   而不交付。DllRecvLoop 等 50ms 无新数据后调此方法，将 ESC 作为 VK_ESCAPE
    //   交付，解决 Esc vs Alt+key 歧义（终端标准做法）
    // 其他不完整序列（如半截 CSI）不处理，继续等更多数据
    std::vector<INPUT_RECORD> FlushPending() {
        std::vector<INPUT_RECORD> result;
        if (m_buf.size() == 1 && static_cast<uint8_t>(m_buf[0]) == 0x1b) {
            // 单独 ESC：构造 VK_ESCAPE 按键事件（按下 + 释放）
            INPUT_RECORD rec{};
            rec.EventType = KEY_EVENT;
            rec.Event.KeyEvent.wRepeatCount      = 1;
            rec.Event.KeyEvent.wVirtualKeyCode   = VK_ESCAPE;
            rec.Event.KeyEvent.wVirtualScanCode  = static_cast<WORD>(
                MapVirtualKeyW(VK_ESCAPE, MAPVK_VK_TO_VSC));
            rec.Event.KeyEvent.uChar.UnicodeChar = 0x1b;
            rec.Event.KeyEvent.dwControlKeyState = 0;

            rec.Event.KeyEvent.bKeyDown = TRUE;
            result.push_back(rec);
            rec.Event.KeyEvent.bKeyDown = FALSE;
            result.push_back(rec);

            m_buf.clear();
        }
        return result;
    }

private:
    // 计算从 buf[pos] 开始的序列完整长度
    // 返回 0 表示不完整（需要更多字节）
    static size_t CalcSequenceLength(const std::string& buf, size_t pos, size_t len) {
        if (pos >= len) return 0;

        uint8_t b = static_cast<uint8_t>(buf[pos]);

        if (b == 0x1b) {
            // ESC 开头
            if (pos + 1 >= len) return 0;  // 只有 ESC，不完整

            uint8_t next = static_cast<uint8_t>(buf[pos + 1]);
            if (next == '[') {
                // CSI：\x1b[ ... final(0x40-0x7E)
                // 参数中可能含 0x30-0x39(数字), ';'(0x3B), 中间字节 0x20-0x2F
                // 鼠标以 '<' 开头也在 CSI 内
                size_t j = pos + 2;
                while (j < len) {
                    uint8_t c = static_cast<uint8_t>(buf[j]);
                    if (c >= 0x40 && c <= 0x7e) {
                        return j - pos + 1;  // 找到 final，完整
                    }
                    // 参数字节：0x30-0x3F（数字和 ; < = > ?）
                    // 中间字节：0x20-0x2F
                    if (c < 0x20 || c > 0x3f) {
                        if (c >= 0x20 && c <= 0x2f) {
                            // 中间字节，继续
                            ++j;
                            continue;
                        }
                        // 非法字节，序列损坏，返回 1 跳过 ESC
                        return 1;
                    }
                    ++j;
                }
                return 0;  // 未找到 final，不完整
            } else if (next == 'O') {
                // SS3：\x1bO final(0x40-0x7E)，共 3 字节
                if (pos + 2 >= len) return 0;  // 不完整
                return 3;
            } else {
                // Alt+char：\x1b + UTF-8 字符
                // 判断 UTF-8 字符长度
                int utf8Len = CalcUtf8Length(next);
                if (utf8Len <= 0) {
                    // 非法字节，跳过 ESC
                    return 1;
                }
                if (pos + 1 + static_cast<size_t>(utf8Len) > len) {
                    return 0;  // UTF-8 字符不完整
                }
                return 1 + static_cast<size_t>(utf8Len);
            }
        } else if (b < 0x80) {
            // ASCII / 控制字符：1 字节
            return 1;
        } else {
            // UTF-8 多字节字符
            int utf8Len = CalcUtf8Length(b);
            if (utf8Len <= 0) {
                // 非法起始字节，跳过
                return 1;
            }
            if (pos + static_cast<size_t>(utf8Len) > len) {
                return 0;  // 不完整
            }
            return static_cast<size_t>(utf8Len);
        }
    }

    // 根据 UTF-8 首字节计算期望的字节数
    // 返回 0 表示非法首字节
    static int CalcUtf8Length(uint8_t b) {
        if (b < 0x80) return 1;
        if ((b & 0xE0) == 0xC0) return 2;
        if ((b & 0xF0) == 0xE0) return 3;
        if ((b & 0xF8) == 0xF0) return 4;
        return 0;  // 0x80-0xBF 是续字节，不应作为首字节
    }

private:
    std::string m_buf;  // 未解析的缓冲
};

} // namespace terminjector
