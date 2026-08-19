// Hook 共享宏与工具
// 详见 docs/phases/03-dll-framework.md 4.5.1 与 4.5.4
//
// 每个 Hook Detour 入口调用 ENSURE_INITIALIZED() 触发懒加载
// IsConsoleHandle 判断句柄是否为真实 Console（排除日志文件句柄等）
// SendToMediator 把 VT 字节流封装为 VtOutput 消息发送（线程安全）
//   Phase 12 扩展：支持指定 MessageType 发送控制消息（如 ChildProcessNotify）
#pragma once

#include <windows.h>
#include "../LazyInit.h"
#include "protocol/Message.h"

namespace terminjector::hooks {

// 懒加载触发宏：每个 Detour 第一行调用
#define ENSURE_INITIALIZED() ::terminjector::EnsureLazyInitialized()

// 原函数指针类型定义宏
// 用法：DEFINE_ORIG_PTR(WriteConsoleW, BOOL WINAPI(HANDLE, const VOID*, DWORD, LPDWORD, LPVOID))
//       展开为：using WriteConsoleW_t = BOOL WINAPI(HANDLE, ...);
//               static WriteConsoleW_t* WriteConsoleW_orig = nullptr
#define DEFINE_ORIG_PTR(name, sig) \
    using name##_t = sig;          \
    static name##_t* name##_orig = nullptr

// 判断句柄是否为 Console 句柄（CONOUT$/CONIN$）
// Console 是 char device，GetFileType 返回 FILE_TYPE_CHAR
// 日志文件句柄是 FILE_TYPE_DISK，会被排除
inline bool IsConsoleHandle(HANDLE h) {
    if (h == nullptr || h == INVALID_HANDLE_VALUE) return false;
    return GetFileType(h) == FILE_TYPE_CHAR;
}

// 判断句柄是否为 Console 输入句柄（CONIN$）
//
// 快路径：与 GetStdHandle 缓存的 stdin/stdout 比较（覆盖 99% 场景）
// 慢路径：调真实 GetNumberOfConsoleInputEvents（trampoline 绕过 Hook）
//   —— 返回 TRUE 即 input 句柄，FALSE 即 output 句柄
//   这与 CPython _get_console_type 使用同一判据，语义最可靠
//
// 用途：Input* Detour 区分 stdin/stdout，避免对 stdout 误返回 TRUE
//       （Python _get_console_type 用 GetNumberOfConsoleInputEvents 探测句柄类型，
//        若 Hook 对 stdout 返回 TRUE，stdout 被误识别为 input，导致启动崩溃）
//
// 历史教训（勿用模式位判断）：
//   前一版慢路径用 GetConsoleMode + 模式位区分 input/output，但 console 模式位
//   低 3 位完全重叠（PROCESSED_INPUT=PROCESSED_OUTPUT=0x1, LINE_INPUT=WRAP_AT_EOL=0x2,
//   ECHO_INPUT=VT_PROCESSING=0x4），output 句柄模式 0x7 也命中 inputOnlyBits，
//   导致 CPython 用 CreateFileW("CONOUT$") 打开的新句柄被误判为 input，启动崩溃。
//   故改用 GetNumberOfConsoleInputEvents_orig（与 CPython 同一判据）。
//
// 局限：Phase 9 HandleRegistry 将完整跟踪 CreateFileW 打开的 CONIN$/CONOUT$ 句柄
// 慢路径函数声明（实现在 InputHooks.cpp，访问 GetNumberOfConsoleInputEvents_orig）
bool IsInputHandleSlow(HANDLE h);

inline bool IsInputHandle(HANDLE h) {
    if (h == nullptr || h == INVALID_HANDLE_VALUE) return false;

    // 快路径：缓存的标准句柄比较（C++11 static 初始化线程安全）
    static HANDLE s_stdin  = GetStdHandle(STD_INPUT_HANDLE);
    static HANDLE s_stdout = GetStdHandle(STD_OUTPUT_HANDLE);
    if (h == s_stdin)  return true;
    if (h == s_stdout) return false;

    // 慢路径：未知句柄（如 CreateFileW("CONIN$")/CONOUT$ 直接打开）
    // 交给 IsInputHandleSlow（实现在 InputHooks.cpp）用 orig trampoline 判断
    return IsInputHandleSlow(h);
}

// 发送消息到 mediator（线程安全）
// data/len: payload 字节
// type: 消息类型，默认 VtOutput（VT 字节流）
// recordReplay: 该字节是否计入卸载时 ConHost 重放缓冲
//   VtOutput 类型下才会记录；控制类协议查询（如 DSR/DA 校准探针）传入 false，
//   避免卸载重放时 ConHost 把查询当请求自答出字面 VT 文本（Phase 22 修复）
// 内部封装为指定类型消息 + Serialize + ITransport::Send
// 返回 true 成功；mediator 未连接或发送失败返回 false
bool SendToMediator(const void* data, size_t len,
                    protocol::MessageType type = protocol::MessageType::VtOutput,
                    bool recordReplay = true);

// 判断本进程控制台是否为 ConPTY 场景（目标程序运行在 Windows Terminal /
// ConPTY 客户端里，如 wt 标签页内直接运行的 TUI）。
// 判定依据（2026-08-19 实测）：ConPTY 模式的 conhost 窗口类名为
// "PseudoConsoleWindow"；独立 ConHost 窗口类名为 "ConsoleWindowClass"。
// 场景差异（BUG-013 根因）：
//   - 独立 ConHost：目标进程独占控制台窗口，DLL 对齐/resize/卸载恢复几何有意义
//   - ConPTY：目标进程的控制台几何完全由 WT 掌控（缓冲=窗口），DLL 用外部
//     wtCols（注入时新开 WT 窗口的尺寸）做对齐会把目标 ConPTY 压缩/拉宽，
//     卸载时恢复几何又与 WT 实际渲染竞态 → 画面错乱 + 滚动条（用户报告）。
//   ConPTY 场景下所有几何操作应跳过，虚拟状态跟随真实 ConPTY 尺寸
//   （VirtualConsoleState 由 StatePoller 按真实 ConPTY 轮询更新）。
bool IsConPtyConsole();

} // namespace terminjector::hooks
