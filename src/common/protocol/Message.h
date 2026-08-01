// 消息类型与 payload 结构定义
// 详见 docs/phases/01-scaffold.md 4.5.3
//
// 设计原则：
//   - 二进制紧凑结构，#pragma pack(1) 保证跨进程内存布局一致
//   - MessageType 枚举值固定 32 位，与 PacketHeader.type 对齐
//   - 每个 payload 结构对应一种业务消息，字段含义见注释
//
// 消息流向：
//   DLL  -> 中介：Hello / VtOutput / ModeChange / CpChange / Pong / ByeAck
//                  UnloadComplete / ChildProcessNotify / ChildExitNotify
//   中介 -> DLL：HelloAck / VtInput / ResizeNotify / Ping / Shutdown
#pragma once

#include <cstdint>

namespace terminjector::protocol {

// 消息类型枚举
// 数值固定为 uint32_t，写入 PacketHeader.type 字段
enum class MessageType : uint32_t {
    // 握手（Phase 2）
    Hello        = 0x0001,  // DLL 注入成功后上报：含目标 PID、初始状态快照、dllBase
    HelloAck     = 0x0002,  // 中介确认握手，回传中介侧 WT 的初始尺寸

    // 输出数据（Phase 4）
    VtOutput     = 0x0010,  // 已翻译为 VT 的输出字节流（变长 payload）

    // 输入数据（Phase 6）
    VtInput      = 0x0020,  // WT 发来的 VT 输入字节流（变长 payload）

    // 控制流（Phase 5/7）
    ResizeNotify = 0x0030,  // 中介->DLL：WT 窗口尺寸变化
    ModeChange   = 0x0031,  // DLL->中介：目标程序切换 ConsoleMode
    CpChange     = 0x0032,  // DLL->中介：目标程序切换 ConsoleCP/OutputCP

    // 心跳/保活（Phase 10）
    Ping         = 0x0040,
    Pong         = 0x0041,

    // 卸载（Phase 11）
    Shutdown       = 0x0050,  // 中介->DLL：要求卸载 Hook
    ByeAck         = 0x0051,  // DLL->中介：已卸载完毕（保留枚举，未使用）
    UnloadComplete = 0x0052,  // DLL->中介：DoUnload 已完成，请求远程 FreeLibrary
                              // 详见 docs/phases/11-unload-testing.md
                              // DLL 内部无法让 LoadCount 归零（cmd 主线程 LdrpThreadBlob
                              // 持引用），需 mediator 用 CreateRemoteThread 远程调
                              // FreeLibrary(dllBase) 才能触发 DETACH

    // 子进程注入（Phase 12）
    // DLL->中介：父进程 CreateProcess Hook 捕获到子进程创建，
    // 中介据此建立子进程管道实例，准备接收子进程 DLL 的连接
    ChildProcessNotify = 0x0060,
    // DLL->中介：子进程退出，中介清理对应子进程管道实例
    ChildExitNotify     = 0x0061,

    // 中介->DLL：子进程退出后同步 ConPTY 光标到父进程 DLL（Phase 12 扩展）
    // 父子进程各有独立 ConsoleState，子进程输出推进了 ConPTY 光标，但父进程缓存
    // 仍停留在子进程启动前的位置。子进程退出后父进程输出新 prompt 时会发错误的
    // 光标定位序列，把 ConPTY 光标拉回旧位置，覆盖子进程输出。
    // 中介在子进程退出时查询 ConPTY 当前光标，通过此消息发给父进程 DLL 对齐缓存。
    ChildExitSync       = 0x0062,

    // 模式切换通知（Phase 13）
    // DLL->中介：VT 直通模式与行编辑模式切换
    ModeSwitchNotify    = 0x0070,

    // WT 状态报告（Phase 14）
    // 中介->DLL：WT 窗口尺寸变化或 DSR CPR 响应，用于虚拟 Console 状态反向同步
    WtStateReport       = 0x0080,
};

// 各消息的 payload 结构
// 1 字节对齐，确保跨进程（DLL 与中介可能用不同 CRT）内存布局一致
#pragma pack(push, 1)

// Hello 消息 payload（DLL -> 中介）
// 注入瞬间由 DLL 读取目标进程 Console 状态填入
// #pragma pack(1) 紧凑布局，无需手动对齐填充
struct HelloPayload {
    uint32_t targetPid;       // 目标进程 PID
    uint32_t targetBitness;   // 32 / 64，目前固定 64
    uint16_t consoleMode;     // 初始 GetConsoleMode（输入句柄）
    uint16_t consoleCp;       // 初始 GetConsoleCP
    uint16_t consoleOutputCp; // 初始 GetConsoleOutputCP
    uint16_t bufferCols;      // 初始缓冲区宽（dwSize.X）
    uint16_t bufferRows;      // 初始缓冲区高（dwSize.Y）
    uint16_t cursorX;         // 初始光标 X
    uint16_t cursorY;         // 初始光标 Y
    uint16_t windowRows;      // 可见窗口高（srWindow.Bottom - Top + 1）
    uint64_t dllBase;         // injected.dll 的 HMODULE（基址），mediator 据此
                              // 远程调 FreeLibrary(dllBase) 触发 DETACH（Phase 11）
};
static_assert(sizeof(HelloPayload) == 32, "HelloPayload 大小应为 32 字节");

// HelloAck 消息 payload（中介 -> DLL）
// 中介回传自己（WT 侧）的当前尺寸与光标，DLL 据此校正目标缓冲区与光标缓存
// isTarget 标识本 DLL 实例所在进程是否为注入目标进程：
//   - 注入目标进程（mediator 主会话）：注入前已卡在 ReadConsoleW，需 KickStart 唤醒
//   - 子进程（ChildSession）：注入后由父进程 CreateProcess 创建，Hook 已就位，
//     不存在旧 ReadConsoleW 阻塞，禁止 KickStart（否则 ENTER 残留队列被误读）
//
// cursorX/cursorY：mediator 侧 WT/ConPTY 的当前光标位置（0-based）
//   DLL 用它覆盖 ConsoleState 的初始光标缓存，使 DLL 缓存坐标系与 ConPTY 对齐。
//   原因：注入瞬间 DLL 从目标进程私有 ConHost 读取的光标（如 cmd 已显示 prompt 后
//   的位置）与 WT/ConPTY 的光标（PowerShell 等前置输出之后的位置）不在同一坐标系，
//   若 DLL 用 ConHost 光标发 VT 定位序列，会把 ConPTY 光标拉到错误位置，导致
//   注入后头几行输出偏右。改用 ConPTY 光标后，cmd 后续输出接在 WT 当前位置之后。
struct HelloAckPayload {
    uint16_t wtCols;       // WT 当前列数
    uint16_t wtRows;       // WT 当前行数
    uint16_t isTarget;     // 1=注入目标进程，0=子进程（控制 KickStart 行为）
    uint16_t cursorX;      // WT/ConPTY 当前光标 X（0-based），DLL 据此对齐缓存
    uint16_t cursorY;      // WT/ConPTY 当前光标 Y（0-based），DLL 据此对齐缓存
    uint16_t reserved2;
};
static_assert(sizeof(HelloAckPayload) == 12, "HelloAckPayload 大小应为 12 字节");

// Resize 消息 payload（中介 -> DLL）
// WT 窗口尺寸变化时通知 DLL，DLL 再调整目标进程屏幕缓冲区
struct ResizePayload {
    uint16_t cols;         // 新窗口列数
    uint16_t rows;         // 新窗口行数
    uint16_t bufferCols;   // 屏幕缓冲区尺寸（可能 >= 窗口）
    uint16_t bufferRows;
};
static_assert(sizeof(ResizePayload) == 8, "ResizePayload 大小应为 8 字节");

// ModeChange 消息 payload（DLL -> 中介）
// 目标程序调用 SetConsoleMode 时上报
struct ModeChangePayload {
    uint32_t inputMode;    // CONIN$ 模式
    uint32_t outputMode;   // CONOUT$ 模式
};
static_assert(sizeof(ModeChangePayload) == 8, "ModeChangePayload 大小应为 8 字节");

// CpChange 消息 payload（DLL -> 中介）
// 目标程序调用 SetConsoleCP / SetConsoleOutputCP 时上报
struct CpChangePayload {
    uint32_t inputCp;      // SetConsoleCP 的新值
    uint32_t outputCp;     // SetConsoleOutputCP 的新值
};
static_assert(sizeof(CpChangePayload) == 8, "CpChangePayload 大小应为 8 字节");

// ChildProcessNotify 消息 payload（DLL -> 中介，Phase 12）
// 父进程的 CreateProcess Hook 捕获到子进程创建后上报
// 中介据此为子进程创建命名管道实例，等待子进程 DLL 连接
// 注：ChildExitNotify 复用此 payload（仅需 childPid 即可定位会话）
struct ChildProcessNotifyPayload {
    uint32_t childPid;     // 新创建的子进程 PID
    uint32_t parentPid;    // 父进程 PID（即上报方）
};
static_assert(sizeof(ChildProcessNotifyPayload) == 8,
              "ChildProcessNotifyPayload 大小应为 8 字节");

// ChildExitSync 消息 payload（中介 -> 父进程 DLL）
// 子进程退出后，中介查询 ConPTY 当前光标，发给父进程 DLL 对齐 ConsoleState 缓存
struct ChildExitSyncPayload {
    uint16_t cursorX;      // ConPTY 当前光标 X（0-based）
    uint16_t cursorY;      // ConPTY 当前光标 Y（0-based）
    uint16_t reserved;
};
static_assert(sizeof(ChildExitSyncPayload) == 6,
              "ChildExitSyncPayload 大小应为 6 字节");

// ModeSwitchNotify 消息 payload（DLL -> 中介，Phase 13）
// DLL 检测到目标程序切换 ENABLE_VIRTUAL_TERMINAL_INPUT 标志时通知 mediator，
// 中介据此切换输入翻译策略（行编辑模式走 VtToInputRecord，VT 直通模式原样转发字节）
struct ModeSwitchNotifyPayload {
    uint32_t vtInputMode;   // 1=VT 直通, 0=行编辑
    uint32_t vtOutputMode;  // 1=VT 处理, 0=老式
};
static_assert(sizeof(ModeSwitchNotifyPayload) == 8,
              "ModeSwitchNotifyPayload 大小应为 8 字节");

// WtStateReport 消息 payload（中介 -> DLL，Phase 14/15）
// 中介向 DLL 报告 WT 侧真实状态，用于虚拟 Console 状态反向同步
// type=0: resize（cols=new cols, rows=new rows）
// type=1: cursor_report 响应 DSR CPR（cols=col, rows=row，1-based VT 坐标）
// type=2: da_report 响应 Primary DA 查询（cols=caps, rows=0，终端能力标识）
struct WtStateReportPayload {
    uint32_t type;       // 0=resize, 1=cursor_report, 2=da_report
    int32_t cols;        // resize: 新列数；cursor: 列；da: 终端能力标识
    int32_t rows;        // resize: 新行数；cursor: 行；da: 0
};
static_assert(sizeof(WtStateReportPayload) == 12,
              "WtStateReportPayload 大小应为 12 字节");

#pragma pack(pop)

} // namespace terminjector::protocol
