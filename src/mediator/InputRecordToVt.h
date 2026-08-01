// INPUT_RECORD → UTF-8 VT 输入序列 翻译器
// 是 VtToInputRecord 的逆过程（mediator 侧使用）
//
// 用途：
//   mediator 改用 ReadConsoleInputW 读取 WT 的原始输入事件（绕过 conhost 的
//   VT 转换层），再用本类把 INPUT_RECORD 转为 UTF-8 VT 序列发给 DLL。
//   原因：conhost 在 ENABLE_VIRTUAL_TERMINAL_INPUT 模式下把 BMP 外字符（如
//   emoji）的代理对转成 U+FFFD，ReadConsoleInputW 直接读输入队列能拿到完整
//   代理对 wchar_t。
//
// 翻译规则（与 VtToInputRecord 互逆）：
//   - 普通字符：UnicodeChar → UTF-8（BMP 外字符需缓存高代理等待低代理配对）
//   - Alt+字符：\x1b 前缀 + UTF-8
//   - 控制字符：Ctrl+C → \x03, Enter → \r, Tab → \t, Backspace → \x7f
//   - 方向键：\x1b[A/B/C/D（带修饰键编码 1;{mod}）
//   - Home/End：\x1b[H/F
//   - 功能键：F1-F4 → SS3 \x1bOP/Q/R/S，F5-F12 → \x1b[15~/17~/...
//   - Insert/Delete/PgUp/PgDn：\x1b[2~/3~/5~/6~
//   - 鼠标 SGR 1006：\x1b[<btn;col;row M/m
//
// 状态：缓存高代理 wchar_t（跨 Convert 调用），处理代理对
// 只处理 bKeyDown=TRUE 的键盘事件（VT 序列不区分按下/释放）
#pragma once

#include <windows.h>
#include <string>

namespace terminjector {

class InputRecordToVt {
public:
    // 把单个 INPUT_RECORD 转为 VT 字节流，追加到 out
    // 内部缓存高代理 wchar_t（跨调用），处理代理对
    void Convert(const INPUT_RECORD& rec, std::string& out);

private:
    // 转换 KEY_EVENT_RECORD（只处理 bKeyDown=TRUE）
    void ConvertKey(const KEY_EVENT_RECORD& ke, std::string& out);

    // 转换 MOUSE_EVENT_RECORD → SGR 1006
    void ConvertMouse(const MOUSE_EVENT_RECORD& me, std::string& out);

    // ---- UTF-8 编码 ----
    // codepoint（或 wchar_t 隐式提升）→ UTF-8 字节，追加到 out
    static void AppendUtf8(std::string& out, uint32_t cp);

    // ---- VT 序列辅助 ----
    // 修饰键编码（1=无, 2=Shift, 3=Alt, 4=Shift+Alt, 5=Ctrl, 6=Ctrl+Shift,
    //              7=Ctrl+Alt, 8=Ctrl+Shift+Alt）
    static int ModifierCode(DWORD ctrlState);

    // 是否为单独修饰键按下（VK_SHIFT/CONTROL/MENU 等，UnicodeChar=0）
    static bool IsModifierOnlyKey(WORD vk, wchar_t ch);

    // ---- 代理对缓存 ----
    // 收到高代理后缓存，等低代理到来组合为完整 codepoint 转 4 字节 UTF-8
    bool m_hasHighSurrogate = false;
    wchar_t m_highSurrogate = 0;
    DWORD m_highSurrogateCtrl = 0;  // 高代理事件的修饰键状态（Alt 影响 UTF-8 前缀）

    // ---- 鼠标按键状态（Phase 16） ----
    // 跨 ConvertMouse 调用跟踪上一次按键状态，用于检测按下/释放转换
    // 替代静态变量 s_prevButtonState，避免多实例状态污染
    DWORD m_prevButtonState = 0;
};

} // namespace terminjector
