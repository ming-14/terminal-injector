// VT 转义序列常量与生成函数
// 详见 docs/phases/03-dll-framework.md 4.6.1 与 docs/phases/04-output-chain.md 4.2
//
// WT 原生支持 VT 渲染，DLL 侧把 Console 输出翻译为 VT 字节流发给 mediator，
// mediator 透传给 WT 的 stdout，WT 即可渲染。
//
// 本头文件只声明 VT 序列生成函数，不含颜色映射逻辑（Color.cpp 单独负责）。
// 颜色 SGR 由 Color.cpp 的 SgrFromAttribute 生成，声明在此处供外部调用。
#pragma once

#include <windows.h>  // WORD
#include <string>

namespace terminjector::vt {

// ===== 常用 VT 转义序列常量 =====
constexpr const char* kCsi   = "\x1b[";   // Control Sequence Introducer
constexpr const char* kOsc   = "\x1b]";   // Operating System Command
constexpr const char* kReset = "\x1b[0m"; // 重置所有属性

// ===== 颜色（实现在 Color.cpp） =====
// Console 16 色属性 → VT SGR 序列
// attr 是 Windows WORD（低 4 位前景，4-7 位背景，8 位高强度，15 位闪烁等）
// 仅当与上次不同时才输出 SGR（thread_local 缓存，线程安全）
std::string SgrFromAttribute(WORD attr);

// ===== 光标控制 =====
// 光标定位（1-based，VT 规范）：CSI row;col H
std::string CursorPosition(int row, int col);
// 光标上/下/前/后移动 N：CSI N A/B/C/D
std::string CursorUp(int n);
std::string CursorDown(int n);
std::string CursorForward(int n);
std::string CursorBack(int n);

// ===== 清屏/清行 =====
// 清屏：CSI n J  (0=光标到屏末, 1=屏首到光标, 2=全屏, 3=全屏+回滚)
std::string EraseDisplay(int mode);
// 清行：CSI n K  (0=光标到行末, 1=行首到光标, 2=整行)
std::string EraseLine(int mode);

// ===== 滚屏 =====
// 上滚 N 行（内容上移，底部空出）：CSI N S
std::string ScrollUp(int n);
// 下滚 N 行（内容下移，顶部空出）：CSI N T
std::string ScrollDown(int n);

// ===== 插入/删除行 =====
// 在光标位置插入 N 行（下方内容下移）：CSI N L
std::string InsertLine(int n);
// 删除光标位置起 N 行（下方内容上移）：CSI N M
std::string DeleteLine(int n);

// ===== 插入/删除字符 =====
// 在光标位置插入 N 个空白字符：CSI N @
std::string InsertChar(int n);
// 删除光标位置起 N 个字符（右侧左移）：CSI N P
std::string DeleteChar(int n);

// ===== 重复字符 =====
// 重复上一个字符 N 次：CSI N b
std::string RepeatChar(int n);

// ===== 滚动区域（DECSTBM） =====
// 设置滚动区域：CSI top;bottom r（1-based 行号）
std::string SetScrollRegion(int top, int bottom);
// 重置滚动区域（全屏）：CSI r
std::string ResetScrollRegion();

// ===== 窗口尺寸 =====
// 设置窗口尺寸（\x1b[8;<rows>;<cols>t）
std::string ResizeWindow(int rows, int cols);

// ===== Alt Buffer（Phase 8） =====
// DECSET/DECRST 1049：进入/退出 Alt Buffer
//   1049 = 1047（用备用屏）+ 1048（保存光标）+ 清屏，组合语义最稳
//   vim/less 等全屏 TUI 进入时 h，退出时 l，WT 自动恢复主屏内容
// 注意：用 char 数组而非 const char* 指针——调用方用 sizeof()-1 求长度，
//       指针的 sizeof 恒为 8（64 位），会截断尾字节（BUG-002 根因）
constexpr char kEnterAltBuffer[] = "\x1b[?1049h";
constexpr char kExitAltBuffer[]  = "\x1b[?1049l";

// ===== 光标显隐（Phase 8） =====
// DECSET/DECRST 25：显示/隐藏光标
//   vim 进入 normal 模式常隐藏光标，退出时恢复
constexpr const char* kShowCursor = "\x1b[?25h";
constexpr const char* kHideCursor = "\x1b[?25l";

// ===== 终端查询（Phase 15） =====
// DSR CPR 查询：CSI 6 n（请求光标位置报告）
// WT 响应：CSI row ; col R
constexpr const char* kDsrCprQuery = "\x1b[6n";
// Primary DA 查询：CSI c（请求终端属性）
// WT 响应：CSI ? 1 ; Ps c（Ps 标识特性集）
constexpr const char* kDaPrimaryQuery = "\x1b[c";

// ===== OSC 标题（Phase 8） =====
// OSC 0 ; <title> BEL：设置窗口/标签页标题
//   cmd 的 title 命令、程序的 SetConsoleTitle 调用都走此序列
//   title 内的 BEL(0x07) 与 ST(\x1b\\) 需转义，但实际程序几乎不会传
//   返回完整 OSC 字节流（含起始 \x1b] 与结束 \x07）
std::string SetTitleOsc(const std::string& utf8Title);

} // namespace terminjector::vt
