// VT 输入序列 → INPUT_RECORD 翻译器
// 详见 docs/phases/06-input-chain.md 4.5
//
// 职责：把完整的 VT 输入序列翻译为 Windows INPUT_RECORD 结构体
//   - 键盘：\x1b[A → VK_UP（按下+释放两个事件）
//   - 鼠标：\x1b[<0;10;20M → MOUSE_EVENT_RECORD（SGR 1006 格式）
//   - 普通：'a' → VK code + UnicodeChar
//   - Alt：\x1b<ch> → Alt+ch
//
// 无状态：所有方法均为静态，纯函数式翻译
// 调用方（VtInputParser）负责流分帧，保证传入的字节只含完整序列
#pragma once

#include <windows.h>
#include <cstdint>
#include <string>
#include <vector>

namespace terminjector {

class VtToInputRecord {
public:
    // 解析完整的 VT 输入字节流（可含多个完整序列），返回 INPUT_RECORD 数组
    // 调用方需保证不传入半个 CSI/SS3 序列（由 VtInputParser 保证分帧）
    static std::vector<INPUT_RECORD> Parse(const uint8_t* data, size_t len);

private:
    // ---- 键盘翻译 ----

    // 解析 CSI 序列（\x1b[...final），返回 consumed 字节数
    // out: 翻译后的 INPUT_RECORD（按下+释放）
    // 返回 0 表示无法识别的序列
    static size_t ParseCsi(const uint8_t* data, size_t len,
                           std::vector<INPUT_RECORD>& out);

    // 解析 SS3 序列（\x1bOx），返回 consumed 字节数
    static size_t ParseSs3(const uint8_t* data, size_t len,
                           std::vector<INPUT_RECORD>& out);

    // 构造按键事件（按下 + 释放两个 INPUT_RECORD）
    static void MakeKeyPair(std::vector<INPUT_RECORD>& out,
                            WORD vk, wchar_t ch, DWORD ctrlState);

    // 构造单个按键事件
    static INPUT_RECORD MakeKeyRecord(bool down, WORD vk, wchar_t ch,
                                      DWORD ctrlState);

    // ---- 鼠标翻译 ----

    // 解析 SGR 1006 鼠标序列 \x1b[<btn;col;row M/m
    // 返回 consumed 字节数；0 表示不是鼠标序列
    static size_t ParseMouse(const uint8_t* data, size_t len,
                             std::vector<INPUT_RECORD>& out);

    // ---- UTF-8 解码 ----

    // 解码 1 个 UTF-8 字符，返回 consumed 字节数（1-4），0 表示无效
    // out 追加解码后的 wchar_t：BMP 内字符 1 个，BMP 外字符（如 emoji）
    // 追加 UTF-16 代理对（高代理 + 低代理）2 个
    // 调用方需为 out 中每个 wchar_t 生成独立的 INPUT_RECORD
    static int DecodeUtf8(const uint8_t* p, size_t len, std::wstring& out);
};

} // namespace terminjector
