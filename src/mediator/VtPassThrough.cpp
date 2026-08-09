// VtPassThrough 实现：WT stdin ↔ DLL pipe 双向桥接
// 详见 docs/phases/03-dll-framework.md 4.7.2
//
// 关键点：
//   - stdin→pipe：ReadConsoleInputW(WT stdin) 阻塞，收到 INPUT_RECORD 转为
//     UTF-8 VT 序列后封装 VtInput 发 DLL
//   - pipe→stdout：RecvPacket 阻塞，收 VtOutput 写 WT stdout（WT 渲染）
//   - pipe 断开时主线程退出，进程结束，stdin 线程被强制终止（Phase 3 简化）
//   - Phase 10 用 CancelIoEx 优雅唤醒 stdin 线程
//
// 输入读取方式（Phase 6 改造）：
//   原方案用 ReadFile + ENABLE_VIRTUAL_TERMINAL_INPUT，让 conhost 把按键转为
//   VT 字节流。但 conhost 的 VT 转换层把 BMP 外字符（emoji 等代理对）转成
//   U+FFFD，导致 emoji 乱码。
//   现方案用 ReadConsoleInputW 直接读取 INPUT_RECORD 队列（绕过 VT 转换），
//   再用 InputRecordToVt 转为 UTF-8 VT 序列发给 DLL。这样代理对能完整保留。
#include "VtPassThrough.h"
#include "InputRecordToVt.h"
#include "transport/ITransport.h"
#include "protocol/MessageSerializer.h"
#include "protocol/Message.h"
#include "logging/Logger.h"

#include <windows.h>
#include <cstdio>
#include <vector>
#include <string>

namespace terminjector {

void VtPassThrough::ForwardStdinToPipe(InputRouter router,
                                       const std::atomic<bool>& stop,
                                       std::atomic<bool>& done) {
    HANDLE hStdin = GetStdHandle(STD_INPUT_HANDLE);
    // 诊断：打印 stdin 句柄类型与 console mode，便于判断读取行为
    DWORD stdinMode = 0;
    GetConsoleMode(hStdin, &stdinMode);
    LOG_INFO("stdin→router: loop start, hStdin=%p consoleMode=0x%x", hStdin, stdinMode);

    // 禁用 VT 输入模式，改用原始事件模式
    // 原因：conhost 在 ENABLE_VIRTUAL_TERMINAL_INPUT 模式下把 BMP 外字符（emoji）
    //   的代理对转成 U+FFFD。ReadConsoleInputW 直接读输入队列能拿到完整代理对 wchar_t，
    //   再由 InputRecordToVt 转为 UTF-8 VT 序列发给 DLL。
    // 不设 ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT | ENABLE_PROCESSED_INPUT：
    //   - LINE_INPUT/ECHO_INPUT：conhost 行编辑，由 DLL 内 LineEditor 负责而非 conhost
    //   - PROCESSED_INPUT：让 Ctrl+C 作为按键发给程序（不生成信号），由 LineEditor 处理
    DWORD newMode = ENABLE_WINDOW_INPUT | ENABLE_MOUSE_INPUT;
    if (SetConsoleMode(hStdin, newMode)) {
        LOG_INFO("stdin→router: consoleMode changed 0x%x → 0x%x (ReadConsoleInputW mode)",
                 stdinMode, newMode);
    } else {
        LOG_WARN("stdin→router: SetConsoleMode failed, err=%lu (fallback to original mode)",
                 GetLastError());
    }

    // InputRecordToVt 转换器（有状态：缓存高代理 wchar_t 处理代理对）
    InputRecordToVt converter;

    INPUT_RECORD recs[64];
    // 循环靠 ReadConsoleInputW 失败退出；router 由 mediator 提供，在 BridgeLoop 退出前一直有效
    // stop 置位（断管清理）后，BridgeLoop 从其他线程 CancelIoEx 唤醒阻塞的
    // ReadConsoleInputW（返回 FALSE），或本线程回到循环顶检查 stop 退出
    while (true) {
        if (stop.load()) {
            LOG_INFO("stdin→router: stop requested, exit loop");
            break;
        }
        DWORD read = 0;
        // ReadConsoleInputW 阻塞等 WT 输入，返回 INPUT_RECORD 数组（按下/释放分开）
        if (!ReadConsoleInputW(hStdin, recs, 64, &read) || read == 0) {
            LOG_INFO("stdin→router: ReadConsoleInputW EOF or failed, err=%lu stop=%d",
                     GetLastError(), stop.load() ? 1 : 0);
            break;
        }

        // 把 INPUT_RECORD 数组转为 UTF-8 VT 字节流
        std::string vtOut;
        for (DWORD i = 0; i < read; ++i) {
            converter.Convert(recs[i], vtOut);
        }

        if (vtOut.empty()) continue;

        // 诊断：打印转换后的 VT 字节（十六进制，前 60 字节）
        char hex[128] = {0};
        int n = 0;
        for (size_t i = 0; i < vtOut.size() && n < 60; ++i) {
            n += std::snprintf(hex + n, sizeof(hex) - n, "%02X ",
                               static_cast<unsigned char>(vtOut[i]));
        }
        LOG_INFO("stdin→router: converted %zu bytes: %s", vtOut.size(), hex);

        // 通过路由回调分发，mediator 的 RouteInput 决定发送目标
        router(reinterpret_cast<const uint8_t*>(vtOut.data()), vtOut.size());
        LOG_INFO("stdin→router: routed, len=%zu", vtOut.size());
    }
    done.store(true);
    LOG_INFO("stdin→router thread exit");
}

void VtPassThrough::ForwardPipeToStdout(ITransport& transport,
                                        NonVtMessageHandler handler) {
    HANDLE hStdout = GetStdHandle(STD_OUTPUT_HANDLE);
    LOG_INFO("pipe→stdout: loop start, hStdout=%p", hStdout);
    // Phase 6 修正：用 Peek 轮询代替阻塞 RecvPacket
    // 原因：同步命名管道句柄一次只能一个 I/O 操作（MSDN），
    //   阻塞的 ReadFile(RecvPacket) 会持有管道 I/O 锁，
    //   阻止 stdin→pipe 线程的 WriteFile(Send) → 死锁：
    //   主线程 ReadFile 等 DLL VtOutput → DLL 等 cmd 输出 →
    //   cmd 等 InputQueue → InputQueue 等 mediator VtInput →
    //   VtInput Send 等 ReadFile 释放锁 → 死锁
    // 方案：Peek 非阻塞探测，有数据才调 RecvPacket（此时 ReadFile 不会长阻塞）
    uint8_t peekBuf[1];
    while (transport.IsConnected()) {
        int peeked = transport.Peek(peekBuf, 1);
        if (peeked < 0) {
            // 管道出错或断开
            LOG_INFO("pipe→stdout: pipe error/broken (Peek=%d)", peeked);
            break;
        }
        if (peeked == 0) {
            // 无数据，短暂休眠后重试
            Sleep(10);
            continue;
        }

        // 有数据可读，调 RecvPacket 读取完整包
        // 此时管道内有数据，ReadFile 不会长时间阻塞，不影响 stdin→pipe 的 Send
        protocol::MessageType type;
        std::vector<uint8_t> payload;
        if (!RecvPacket(&transport, type, payload)) {
            LOG_INFO("pipe→stdout: pipe closed (RecvPacket failed)");
            break;
        }
        if (type == protocol::MessageType::VtOutput) {
            // 直接写 stdout（WT 收到 VT 字节流并渲染）
            DWORD written = 0;
            BOOL ok = WriteFile(hStdout, payload.data(), static_cast<DWORD>(payload.size()),
                                &written, nullptr);
            // 只在失败时取 GetLastError：成功时 GetLastError 返回的是
            // 上一次失败遗留的错误码（可能是 ERROR_INVALID_HANDLE=6），造成误导
            DWORD err = ok ? 0 : GetLastError();
            // Phase 8：记录前 N 字节十六进制，便于测试验证 OSC/Alt Buffer 等序列
            // Phase 10 任务5：BatchSender 合并后单包可能包含多个语义单元
            // （回显 + OSC + prompt），32 字节不足以覆盖 OSC 序列位置，提到 256
            // 256 字节覆盖绝大多数控制序列（OSC/CSI/SGR），日志开销可控（~768 字符）
            constexpr size_t kHexDumpMax = 256;
            char hexBuf[kHexDumpMax * 3 + 8] = {0};
            size_t hexLen = payload.size() < kHexDumpMax ? payload.size() : kHexDumpMax;
            size_t pos = 0;
            for (size_t i = 0; i < hexLen; ++i) {
                pos += std::snprintf(hexBuf + pos, sizeof(hexBuf) - pos,
                                     "%02X ", payload[i]);
            }
            // 每次 VT 写入后自读 ConPTY 光标+窗口（合并进本日志行，不另起一行）。
            // 背景（2026-08-08 TUI-CURSOR-BUG）：外部 AttachConsole 读 ConPTY
            // 必然失败(ERROR_ACCESS_DENIED)，ConPTY 状态只能由 mediator 进程内部
            // 自读；e2e 回归解析本行的 cursor=(X,Y) 与 ConHost 光标比对，
            // 验证 TUI 劫持后 WT 光标落点。
            CONSOLE_SCREEN_BUFFER_INFO csbi{};
            char cursorInfo[96] = "cursor=NA";
            if (GetConsoleScreenBufferInfo(hStdout, &csbi)) {
                std::snprintf(cursorInfo, sizeof(cursorInfo),
                              "cursor=(%d,%d) buf=%dx%d win=(%d,%d)-(%d,%d)",
                              csbi.dwCursorPosition.X, csbi.dwCursorPosition.Y,
                              csbi.dwSize.X, csbi.dwSize.Y,
                              csbi.srWindow.Left, csbi.srWindow.Top,
                              csbi.srWindow.Right, csbi.srWindow.Bottom);
            }
            // 注意：cursorInfo 放在 hex 之后（行尾），保证既有测试正则
            // err=\d+ hex[\d+]= 仍可匹配（tests/e2e/line_editor 与 modes 依赖）
            LOG_INFO("pipe→stdout: VtOutput len=%zu written=%lu ok=%d err=%lu "
                     "hex[%zu]=%s%s %s",
                     payload.size(), written, ok, err, hexLen, hexBuf,
                     payload.size() > kHexDumpMax ? "..." : "", cursorInfo);
        } else {
            // 非 VtOutput 消息：交给 handler 处理（如 ChildProcessNotify）
            if (handler) {
                handler(type, payload);
            } else {
                LOG_INFO("pipe→stdout: got msg type=0x%08X len=%zu",
                         static_cast<uint32_t>(type), payload.size());
            }
        }
    }
    LOG_INFO("pipe→stdout thread exit");
}

} // namespace terminjector
