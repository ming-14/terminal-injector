// VT 转义序列生成实现
// 详见 docs/phases/04-output-chain.md 4.2
//
// 本文件实现除颜色映射（SgrFromAttribute 在 Color.cpp）外的所有 VT 序列生成。
// 每个函数返回一个 std::string，拼接后发给 mediator 透传给 WT 渲染。
//
// VT 序列参考：ECMA-48 / xterm spec
//   CSI = \x1b[  (0x1b 0x5b)
//   常用结尾字母：H(光标定位) A/B/C/D(光标移动) J/K(清屏/清行)
//                 S/T(滚屏) L/M(插入/删除行) @/P(插入/删除字符)
//                 b(重复字符) r(滚动区域) t(窗口尺寸)
#include "VtEscape.h"
#include <cstdio>

namespace terminjector::vt {

namespace {

// 安全的 snprintf 封装：格式化单个 VT 序列到 std::string
// fmt 必须以 CSI(\x1b[) 开头，以终结字母结尾
std::string MakeSeq(const char* fmt, ...) {
    char buf[64];
    va_list args;
    va_start(args, fmt);
    int n = std::vsnprintf(buf, sizeof(buf), fmt, args);
    va_end(args);
    if (n <= 0) return {};
    return std::string(buf, static_cast<size_t>(n));
}

} // namespace

// 光标定位（1-based）：CSI row;col H
std::string CursorPosition(int row, int col) {
    return MakeSeq("\x1b[%d;%dH", row, col);
}

// 光标上移 N 行：CSI N A
std::string CursorUp(int n) {
    if (n <= 0) return {};
    return MakeSeq("\x1b[%dA", n);
}

// 光标下移 N 行：CSI N B
std::string CursorDown(int n) {
    if (n <= 0) return {};
    return MakeSeq("\x1b[%dB", n);
}

// 光标前移（右）N 列：CSI N C
std::string CursorForward(int n) {
    if (n <= 0) return {};
    return MakeSeq("\x1b[%dC", n);
}

// 光标后移（左）N 列：CSI N D
std::string CursorBack(int n) {
    if (n <= 0) return {};
    return MakeSeq("\x1b[%dD", n);
}

// 清屏：CSI n J
//   0=光标到屏末, 1=屏首到光标, 2=全屏, 3=全屏+回滚
std::string EraseDisplay(int mode) {
    return MakeSeq("\x1b[%dJ", mode);
}

// 清行：CSI n K
//   0=光标到行末, 1=行首到光标, 2=整行
std::string EraseLine(int mode) {
    return MakeSeq("\x1b[%dK", mode);
}

// 上滚 N 行（内容上移，底部空出）：CSI N S
std::string ScrollUp(int n) {
    if (n <= 0) return {};
    return MakeSeq("\x1b[%dS", n);
}

// 下滚 N 行（内容下移，顶部空出）：CSI N T
std::string ScrollDown(int n) {
    if (n <= 0) return {};
    return MakeSeq("\x1b[%dT", n);
}

// 插入 N 行（光标位置起，下方内容下移）：CSI N L
std::string InsertLine(int n) {
    if (n <= 0) return {};
    return MakeSeq("\x1b[%dL", n);
}

// 删除 N 行（光标位置起，下方内容上移）：CSI N M
std::string DeleteLine(int n) {
    if (n <= 0) return {};
    return MakeSeq("\x1b[%dM", n);
}

// 插入 N 个空白字符（光标位置起，右侧右移）：CSI N @
std::string InsertChar(int n) {
    if (n <= 0) return {};
    return MakeSeq("\x1b[%d@", n);
}

// 删除 N 个字符（光标位置起，右侧左移）：CSI N P
std::string DeleteChar(int n) {
    if (n <= 0) return {};
    return MakeSeq("\x1b[%dP", n);
}

// 重复上一个字符 N 次：CSI N b
// 用于 FillConsoleOutputCharacter：输出一个字符后重复 N-1 次
std::string RepeatChar(int n) {
    if (n <= 0) return {};
    return MakeSeq("\x1b[%db", n);
}

// 设置滚动区域（DECSTBM）：CSI top;bottom r（1-based 行号）
std::string SetScrollRegion(int top, int bottom) {
    return MakeSeq("\x1b[%d;%dr", top, bottom);
}

// 重置滚动区域（全屏）：CSI r
std::string ResetScrollRegion() {
    return MakeSeq("\x1b[r");
}

// 设置窗口尺寸：\x1b[8;<rows>;<cols>t
std::string ResizeWindow(int rows, int cols) {
    return MakeSeq("\x1b[8;%d;%dt", rows, cols);
}

// OSC 标题：\x1b]0;<title>\x07
// 转义 title 中的 BEL(0x07) 与 ESC(0x1b) 字符，避免提前终止 OSC 序列
//   实际程序几乎不会在标题里传这俩字符，但兜底处理保证健壮性
//   xterm 实践：BEL 直接过滤，ESC 后接 Backslash 用 ST(\x1b\\) 表示
//   此处采用最简策略：把 BEL/ESC 替换为空格
std::string SetTitleOsc(const std::string& utf8Title) {
    std::string out;
    out.reserve(utf8Title.size() + 6);
    out.append("\x1b]0;");
    for (char c : utf8Title) {
        unsigned char uc = static_cast<unsigned char>(c);
        if (uc == 0x07 || uc == 0x1b) {
            out.push_back(' ');
        } else {
            out.push_back(c);
        }
    }
    out.push_back('\x07');
    return out;
}

} // namespace terminjector::vt
