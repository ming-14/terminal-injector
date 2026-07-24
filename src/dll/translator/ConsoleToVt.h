// Console → VT 翻译器接口
// 详见 docs/phases/03-dll-framework.md 4.6.3 与 docs/phases/04-output-chain.md
//
// Phase 3：WriteConsoleW 分支（文本透传 + SGR 颜色）
// Phase 4：扩展 FillConsoleOutputCharacter/Attribute、WriteConsoleOutput/Character、
//          ScrollConsoleScreenBuffer 翻译函数
//
// 设计要点：
//   - 所有方法为静态（无需实例，状态由 ConsoleState 单例管理）
//   - 仅实现 W 版本（A 版本在 Hook 层转 W 后复用）
//   - WriteConsoleOutput 跳过空格+默认属性 cell 以减少 VT 字节量
//   - ScrollConsoleScreenBuffer 本 Phase 实现简化整体滚屏，Phase 10 补区域裁剪
#pragma once

#include <windows.h>
#include <string>

namespace terminjector {

class ConsoleToVt {
public:
    // ===== Phase 3 =====
    // WriteConsoleW 分支：wchar_t 文本 → UTF-8 + SGR 颜色
    // buf: 目标程序传入的 UTF-16 文本
    // len: 字符数（非字节数）
    // attr: 当前 Console 颜色属性（WORD）
    // 返回：VT 字节流（SGR + UTF-8 文本）
    static std::string WriteConsoleW(const wchar_t* buf, DWORD len, WORD attr);

    // ===== Phase 4 =====

    // FillConsoleOutputCharacter：在指定坐标填充 N 个相同字符
    // 用于 cls 清屏（填充空格）
    // VT 策略：光标定位 + 输出一个字符 + RepeatChar(N-1)
    static std::string FillConsoleOutputCharacter(
        wchar_t character, DWORD count, COORD writeCoord);

    // FillConsoleOutputAttribute：在指定坐标填充 N 个 cell 的颜色属性
    // 用于 color 命令改变整屏颜色
    // VT 策略：光标定位 + SGR（不输出字符，仅改变颜色状态）
    static std::string FillConsoleOutputAttribute(
        WORD attribute, DWORD count, COORD writeCoord);

    // WriteConsoleOutput：写字符矩阵（每个 cell 带字符+属性）
    // 用于全屏重绘（如 vim/curses 程序）
    // VT 策略：遍历矩阵，每个非空 cell 输出 光标定位 + SGR + 字符
    // 优化：跳过空格+默认属性 cell；同行连续相同属性合并为一次定位+多字符
    // bufferSize: 矩阵尺寸（X=列数, Y=行数）
    // bufferCoord: 源缓冲区起始坐标（通常为 {0,0}）
    // writeRegion: 目标屏幕区域（会被更新为实际写入区域）
    static std::string WriteConsoleOutput(
        const CHAR_INFO* buffer, COORD bufferSize, COORD bufferCoord,
        SMALL_RECT writeRegion);

    // WriteConsoleOutputCharacter：在指定坐标写一串字符（不改颜色）
    // 用于 prompt $P$G 等局部文本输出
    // VT 策略：光标定位 + UTF-8 字符串
    static std::string WriteConsoleOutputCharacter(
        const wchar_t* buffer, DWORD length, COORD writeCoord);

    // ScrollConsoleScreenBuffer：滚动屏幕缓冲区区域
    // 用于滚屏（如输出超出缓冲区底部时 cmd 自动滚屏）
    // VT 策略（简化版）：根据 destOrigin 与 scrollRect 的偏移判断方向，
    //   上滚用 SU，下滚用 SD。Phase 10 补 DECSTBM 区域滚动。
    // scrollRect: 要滚动的区域
    // clipRect: 裁剪区域（可为 nullptr）
    // destOrigin: 目标左上角坐标
    // fillChar: 空出位置填充字符
    // fillAttr: 空出位置填充属性
    static std::string ScrollConsoleScreenBuffer(
        SMALL_RECT scrollRect, const SMALL_RECT* clipRect,
        COORD destOrigin, wchar_t fillChar, WORD fillAttr);
};

} // namespace terminjector
