// Mediator 实现
// 详见 docs/phases/02-injector-modes.md 4.5.2
//
// 关键时序（避免竞态）：
//   1. Create()        — CreateNamedPipeW（不阻塞）
//   2. SpawnInjector   — fork --inject 子命令（注入 DLL）
//   3. WaitClient()    — ConnectNamedPipe（阻塞等 DLL 连接）
//   4. Handshake()     — 收 Hello，回 HelloAck
//   5. BridgeLoop()    — Phase 3+ 实现
#include "Mediator.h"
#include "transport/NamedPipeTransport.h"
#include "transport/TransportFactory.h"
#include "protocol/Message.h"
#include "protocol/MessageSerializer.h"
#include "VtPassThrough.h"
#include "VtParser.h"
#include "logging/Logger.h"

#include <windows.h>
#include <atomic>
#include <cstdio>
#include <cstring>
#include <thread>
#include <vector>
#include <algorithm>

namespace terminjector {

namespace {

// 获取 WT 侧（mediator 的 stdout）当前窗口尺寸，用于回填 HelloAck
// 返回 false 表示无 Console 或读取失败
bool GetWtWindowSize(uint16_t& cols, uint16_t& rows) {
    HANDLE hOut = GetStdHandle(STD_OUTPUT_HANDLE);
    CONSOLE_SCREEN_BUFFER_INFO csbi{};
    if (!GetConsoleScreenBufferInfo(hOut, &csbi)) return false;
    cols = static_cast<uint16_t>(csbi.srWindow.Right - csbi.srWindow.Left + 1);
    rows = static_cast<uint16_t>(csbi.srWindow.Bottom - csbi.srWindow.Top + 1);
    return true;
}

// 获取 mediator 侧 WT/ConPTY 的当前光标位置（0-based）
// DLL 用它对齐 ConsoleState 光标缓存，避免注入后头几行输出偏右
// 详见 HelloAckPayload.cursorX/cursorY 注释
bool GetWtCursorPos(uint16_t& cursorX, uint16_t& cursorY) {
    HANDLE hOut = GetStdHandle(STD_OUTPUT_HANDLE);
    CONSOLE_SCREEN_BUFFER_INFO csbi{};
    if (!GetConsoleScreenBufferInfo(hOut, &csbi)) return false;
    cursorX = static_cast<uint16_t>(csbi.dwCursorPosition.X);
    cursorY = static_cast<uint16_t>(csbi.dwCursorPosition.Y);
    return true;
}

// 把 Hello 携带的目标快照状态翻译为 VT 序列写到 stdout，
// 让 WT 立即"跳到"目标程序的当前状态（仅窗口尺寸）
// 详见 docs/phases/03-dll-framework.md 4.3.3
//
// 注意：不移动 WT 光标到目标 ConHost 光标位置
//   原因：目标进程私有 ConHost 的光标坐标系与 WT/ConPTY 独立（WT 从未显示过
//   目标进程之前的内容）。把 WT 光标移到 ConHost 坐标会让后续输出偏右。
//   DLL 侧改为用 ConPTY 当前光标对齐缓存（见 HelloAckPayload.cursorX/Y），
//   cmd 后续输出自然接在 WT 当前位置之后。
//
// 将 ConPTY 缓冲区 resize 到 srWindow 尺寸（即 WT 窗口当前字符尺寸）。
// 原因：ConPTY 缓冲区初始化时可能小于 WT 窗口（如 120x30 vs 160x40），
// 导致 TUI 程序（如 Textual）查询到的缓冲区尺寸为 120x30，
// 渲染只占左半窗口，右半显示旧缓冲区内容。
// 通过 Win32 API SetConsoleScreenBufferSize + SetConsoleWindowInfo
// 将缓冲区扩展为 WT 窗口大小，使 TUI 程序可以使用全窗口宽度。
//
// 注意：使用 srWindow（实际可见窗口）而非 dwMaximumWindowSize（ConPTY 默认最大值）
// 因为 dwMaximumWindowSize 在 ConPTY 中可能固定为 120x30，不反映实际 WT 窗口尺寸。
// \x1b[8;rows;colst VT 序列在 ConPTY 中只改窗口尺寸不改缓冲区尺寸，
// 因此使用 Win32 API 确保缓冲区尺寸也同步更新。
void ApplySnapshotToWt(const protocol::HelloPayload& hello) {
    (void)hello;
    HANDLE hOut = GetStdHandle(STD_OUTPUT_HANDLE);
    CONSOLE_SCREEN_BUFFER_INFO csbi{};
    if (GetConsoleScreenBufferInfo(hOut, &csbi)) {
        // 使用 srWindow 反映 WT 窗口当前实际可见字符尺寸
        SHORT cols = static_cast<SHORT>(csbi.srWindow.Right - csbi.srWindow.Left + 1);
        SHORT rows = static_cast<SHORT>(csbi.srWindow.Bottom - csbi.srWindow.Top + 1);
        if (cols > 0 && rows > 0) {
            // 第一步：先设置窗口大小为当前尺寸
            SMALL_RECT win = {0, 0, static_cast<SHORT>(cols - 1), static_cast<SHORT>(rows - 1)};
            SetConsoleWindowInfo(hOut, TRUE, &win);
            // 第二步：设置缓冲区尺寸（窗口先设好，避免缓冲区缩小被拒绝）
            COORD bufSize = {cols, rows};
            SetConsoleScreenBufferSize(hOut, bufSize);
            LOG_INFO("ApplySnapshotToWt: buffer resized to %dx%d (srWindow) via Win32 API",
                     cols, rows);
        }
    }
}

} // namespace

Mediator::Mediator() = default;
Mediator::~Mediator() = default;

int Mediator::Run(uint32_t targetPid, const std::wstring& pipeName,
                  const std::wstring& dllPath, uint32_t selfPid) {
    m_targetPid = targetPid;
    m_selfPid = selfPid;
    m_pipeName = pipeName;
    LOG_INFO("Mediator starting, targetPid=%u pipe=%ls dll=%ls selfPid=%u",
             targetPid, pipeName.c_str(), dllPath.c_str(), selfPid);

    if (targetPid == 0) {
        LOG_ERROR("Mediator: targetPid is 0");
        return 1;
    }
    if (pipeName.empty()) {
        LOG_ERROR("Mediator: pipeName is empty");
        return 1;
    }

    // 设置 mediator 控制台代码页为 UTF-8（必须在任何 stdout 写入前完成）
    //
    // 原因：mediator 把 DLL 翻译后的 UTF-8 VT 字节流写到 stdout（ConPTY），
    //       若不设置 UTF-8 代码页，ConPTY 按系统默认代码页（zh-CN 为 GBK/936）
    //       解码 UTF-8 字节，导致中文显示为乱码（mojibake）。
    //       例：UTF-8 "驱"=E9 A9 BE 被 GBK 两字节分组为 E9A9="椹" BEE5="卞"...
    // 同步设置输入代码页，保证 WT 发来的 UTF-8 VT 输入（如粘贴的中文）正确解释。
    if (!SetConsoleOutputCP(CP_UTF8)) {
        LOG_WARN("SetConsoleOutputCP(CP_UTF8) failed, err=%lu", GetLastError());
    }
    if (!SetConsoleCP(CP_UTF8)) {
        LOG_WARN("SetConsoleCP(CP_UTF8) failed, err=%lu", GetLastError());
    }
    LOG_INFO("Mediator: console CP set to UTF-8 (out=%lu in=%lu)",
             GetConsoleOutputCP(), GetConsoleCP());

    // 1. 创建命名管道服务端（不阻塞，等 SpawnInjector 后再 WaitClient）
    m_transport = CreateTransport(TransportType::NamedPipe, pipeName,
                                  NamedPipeTransport::Role::Server);
    auto* namedPipe = dynamic_cast<NamedPipeTransport*>(m_transport.get());
    if (namedPipe == nullptr) {
        LOG_ERROR("Mediator: transport is not NamedPipeTransport");
        return 1;
    }
    if (!namedPipe->Create()) {
        LOG_ERROR("Mediator: pipe Create failed");
        return 1;
    }
    LOG_INFO("Mediator: pipe created, will spawn injector then wait client");

    // 2. 触发注入（fork 自身 --inject 模式）
    if (!SpawnInjector(targetPid, dllPath)) {
        LOG_ERROR("Mediator: SpawnInjector failed");
        return 1;
    }

    // 3. 等待 DLL 连接（阻塞）
    if (!namedPipe->WaitClient()) {
        LOG_ERROR("Mediator: WaitClient failed");
        return 1;
    }
    LOG_INFO("Mediator: DLL connected");

    // 4. Hello 握手
    if (!Handshake()) {
        LOG_ERROR("Mediator: Handshake failed");
        return 1;
    }
    LOG_INFO("Mediator: Handshake OK, entering bridge loop");

    // 5. 桥接循环（Phase 2 占位）
    BridgeLoop();

    return 0;
}

bool Mediator::SpawnInjector(uint32_t targetPid, const std::wstring& dllPath) {
    // fork 自身：terminal-injector.exe --inject <pid> --dll <path>
    // 注意：CreateProcessW 的 lpCommandLine 需可写，且首 token 是 exe 路径
    // 用 GetModuleFileNameW 获取自身路径作为首 token
    wchar_t exePath[MAX_PATH] = {0};
    if (GetModuleFileNameW(nullptr, exePath, MAX_PATH) == 0) {
        LOG_ERROR("GetModuleFileNameW failed: %lu", GetLastError());
        return false;
    }

    std::wstring cmd = std::wstring(exePath) +
                       L" --inject " + std::to_wstring(targetPid) +
                       L" --dll \"" + dllPath + L"\"" +
                       L" --pipe \"" + m_pipeName + L"\"" +
                       L" --mediator-pid " + std::to_wstring(m_selfPid);
    LOG_INFO("SpawnInjector cmd: %ls", cmd.c_str());

    STARTUPINFOW si{};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi{};
    // CreateProcessW 的 lpCommandLine 必须是可写缓冲
    std::vector<wchar_t> cmdBuf(cmd.begin(), cmd.end());
    cmdBuf.push_back(L'\0');

    if (!CreateProcessW(nullptr, cmdBuf.data(),
                        nullptr, nullptr, FALSE,
                        CREATE_NO_WINDOW,  // 注入器无需窗口
                        nullptr, nullptr, &si, &pi)) {
        LOG_ERROR("CreateProcessW(injector) failed: %lu", GetLastError());
        return false;
    }

    // 不等待注入器完成（注入器会阻塞至 LoadLibraryW 远程线程返回）
    // 但需关闭句柄避免泄漏
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    LOG_INFO("Injector spawned, pid=%lu", pi.dwProcessId);
    return true;
}

bool Mediator::Handshake() {
    using namespace protocol;

    // 收 Hello
    MessageType type;
    std::vector<uint8_t> payload;
    if (!RecvPacket(m_transport.get(), type, payload)) {
        LOG_ERROR("Handshake: RecvPacket(Hello) failed");
        return false;
    }
    if (type != MessageType::Hello) {
        LOG_ERROR("Handshake: expected Hello, got type=0x%08X",
                  static_cast<uint32_t>(type));
        return false;
    }

    // 解析 Hello payload
    HelloPayload hello{};
    if (payload.size() >= sizeof(hello)) {
        std::memcpy(&hello, payload.data(), sizeof(hello));
    } else {
        LOG_WARN("Handshake: Hello payload too small %zu < %zu",
                 payload.size(), sizeof(hello));
    }
    LOG_INFO("Handshake: Hello received, pid=%u bitness=%u cols=%u rows=%u "
             "winRows=%u mode=0x%04x cp=%u/%u cursor=(%u,%u) dllBase=0x%llx "
             "protocolVersion=%u",
             hello.targetPid, hello.targetBitness,
             hello.bufferCols, hello.bufferRows, hello.windowRows,
             hello.consoleMode, hello.consoleCp, hello.consoleOutputCp,
             hello.cursorX, hello.cursorY,
             static_cast<unsigned long long>(hello.dllBase),
             kVersion);

    // Phase 11：保存 injected.dll 基址
    // 收到 UnloadComplete 时据此远程调 FreeLibrary(dllBase) 触发 DETACH
    m_dllBase = hello.dllBase;

    // 把目标快照状态应用到 WT（窗口尺寸）
    ApplySnapshotToWt(hello);

    // 回 HelloAck：携带 WT 侧当前窗口尺寸 + 当前光标位置
    // isTarget=1：本会话为注入目标进程（mediator 主会话），DLL 据此启用 KickStart
    // cursorX/Y：ApplySnapshotToWt 之后获取，DLL 用它对齐 ConsoleState 光标缓存，
    //            避免注入后头几行输出偏右（详见 HelloAckPayload 注释）
    HelloAckPayload ack{};
    ack.isTarget = 1;
    uint16_t wtCols = 0, wtRows = 0;
    if (GetWtWindowSize(wtCols, wtRows)) {
        ack.wtCols = wtCols;
        ack.wtRows = wtRows;
    }
    uint16_t curX = 0, curY = 0;
    if (GetWtCursorPos(curX, curY)) {
        ack.cursorX = curX;
        ack.cursorY = curY;
    }
    auto ackPkt = Serialize(MessageType::HelloAck, &ack, sizeof(ack));
    const int sent = m_transport->Send(ackPkt.data(), ackPkt.size());
    if (sent != static_cast<int>(ackPkt.size())) {
        LOG_ERROR("Handshake: Send(HelloAck) failed, sent=%d expected=%zu",
                  sent, ackPkt.size());
        return false;
    }
    LOG_INFO("Handshake: HelloAck sent, wtCols=%u wtRows=%u isTarget=1 cursor=(%u,%u)",
             ack.wtCols, ack.wtRows, ack.cursorX, ack.cursorY);
    return true;
}

void Mediator::BridgeLoop() {
    // Phase 3：真实双向桥接
    //   - 独立线程：WT stdin → 封装 VtInput → DLL pipe
    //   - 主线程：  DLL pipe → 收 VtOutput → WT stdout（WT 渲染）
    // Phase 5：增加 WtSizeWatcher 线程，监听 WT resize 发 ResizeNotify 给 DLL
    // Phase 12：pipe→stdout 增加 ChildProcessNotify 处理（创建子进程会话）
    //
    // 退出条件：pipe 断开（DLL 卸载或目标进程退出）
    // Phase 10+ 处理 Ping / Shutdown 等控制流
    LOG_INFO("BridgeLoop starting (stdin<->pipe, pipe<->stdout, sizeWatcher, childSession)");

    // Phase 14：设置 VtParser DSR CPR 回调
    // 当 WT 响应 DSR 查询时，发送 WtStateReport(type=1) 给 DLL 更新 VirtualConsoleState
    m_vtParser.SetCursorReportCallback([this](int col, int row) {
        protocol::WtStateReportPayload wt{};
        wt.type = 1;  // cursor_report
        wt.cols = col;
        wt.rows = row;
        auto pkt = protocol::Serialize(protocol::MessageType::WtStateReport, &wt, sizeof(wt));
        const int sent = m_transport->Send(pkt.data(), pkt.size());
        LOG_INFO("WtStateReport cursor sent: VT col=%d row=%d (sent=%d/%zu)",
                 col, row, sent, pkt.size());
    });

    // Phase 15：设置 VtParser DA 报告回调
    // 当 WT 响应 DA 查询时，发送 WtStateReport(type=2) 给 DLL 存储终端能力
    m_vtParser.SetDaReportCallback([this](int caps) {
        protocol::WtStateReportPayload wt{};
        wt.type = 2;  // da_report
        wt.cols = caps;
        wt.rows = 0;
        auto pkt = protocol::Serialize(protocol::MessageType::WtStateReport, &wt, sizeof(wt));
        const int sent = m_transport->Send(pkt.data(), pkt.size());
        LOG_INFO("WtStateReport DA sent: caps=%d (sent=%d/%zu)",
                 caps, sent, pkt.size());
    });

    // Phase 5：启动 WT 尺寸监听
    // WT 窗口 resize 时：
    //   1. 用 Win32 API 实际 resize ConPTY 缓冲区（使 VT 输出填满整个 WT 窗口）
    //   2. 封装 ResizeNotify 发给 DLL，DLL 更新 ConsoleState
    //   3. 发送 WtStateReport 给 VirtualConsoleState
    //   4. 转发 ResizeNotify 到所有活跃子进程
    m_sizeWatcher.Start([this](int cols, int rows, int bufCols, int bufRows) {
        // 第一步：用 Win32 API 实际 resize ConPTY 缓冲区
        // 若未 resize，ConPTY 缓冲区保持旧尺寸，VT 输出只填充左半窗口，
        // 右半显示旧缓冲区内容（"刷屏"）。
        //
        // 顺序：先 SetConsoleScreenBufferSize 增大缓冲区，再 SetConsoleWindowInfo 设窗口。
        // 若反过来，窗口可能设得比缓冲区大（如 156x42 窗口 vs 120x30 缓冲区），
        // SetConsoleWindowInfo 会失败，导致 resize 不生效。
        HANDLE hOut = GetStdHandle(STD_OUTPUT_HANDLE);
        if (hOut != INVALID_HANDLE_VALUE && hOut != nullptr) {
            // 先设缓冲区（确保缓冲区至少和窗口一样大）
            // 注意：windows.h 的 max 宏会与 std::max 冲突，手动比较
            SHORT minBufCols = static_cast<SHORT>((bufCols > cols) ? bufCols : cols);
            SHORT minBufRows = static_cast<SHORT>((bufRows > rows) ? bufRows : rows);
            COORD bufSize = {minBufCols, minBufRows};
            SetConsoleScreenBufferSize(hOut, bufSize);
            // 再设窗口
            SMALL_RECT win = {0, 0, static_cast<SHORT>(cols - 1), static_cast<SHORT>(rows - 1)};
            SetConsoleWindowInfo(hOut, TRUE, &win);
            LOG_INFO("WtSizeWatcher callback: ConPTY buffer resized to %dx%d win=%dx%d via Win32 API",
                     minBufCols, minBufRows, cols, rows);
        }

        protocol::ResizePayload p{};
        p.cols       = static_cast<uint16_t>(cols);
        p.rows       = static_cast<uint16_t>(rows);
        p.bufferCols = static_cast<uint16_t>(bufCols);
        p.bufferRows = static_cast<uint16_t>(bufRows);
        auto pkt = protocol::Serialize(protocol::MessageType::ResizeNotify, &p, sizeof(p));
        const int sent = m_transport->Send(pkt.data(), pkt.size());
        LOG_INFO("ResizeNotify sent: win=%dx%d buf=%dx%d (sent=%d/%zu)",
                 cols, rows, bufCols, bufRows, sent, pkt.size());

        // Phase 14：同时发送 WtStateReport 给 VirtualConsoleState
        protocol::WtStateReportPayload wt{};
        wt.type = 0;  // resize
        wt.cols = cols;
        wt.rows = rows;
        auto wtPkt = protocol::Serialize(protocol::MessageType::WtStateReport, &wt, sizeof(wt));
        const int wtSent = m_transport->Send(wtPkt.data(), wtPkt.size());
        LOG_INFO("WtStateReport resize sent: cols=%d rows=%d (sent=%d/%zu)",
                 cols, rows, wtSent, wtPkt.size());

        // Phase 19：转发 ResizeNotify 到所有活跃子进程
        // TUI 程序（如 Textual）在子进程中运行，需要 WT 尺寸变化通知
        // 才能调整布局。若不转发，子进程 TUI 在 WT resize 时不会刷新。
        {
            std::lock_guard<std::mutex> lock(m_childMutex);
            for (auto& session : m_childSessions) {
                if (session->IsActive()) {
                    session->SendResize(
                        static_cast<uint16_t>(cols),
                        static_cast<uint16_t>(rows),
                        static_cast<uint16_t>(bufCols),
                        static_cast<uint16_t>(bufRows));
                }
            }
        }
    });

    // stdin→pipe 独立线程（ReadConsoleInputW 阻塞）
    // 断管清理时：置 stop 位 → 从主线程 CancelIoEx(hStdin) 唤醒阻塞的读 → join
    // （Phase 3 曾用"进程结束强制终止"简化，但进程要等 join 才能退，会永久挂死）
    // Phase 12+：通过 RouteInput 路由回调，把输入分发到活跃子进程或父进程
    // Phase 11：stdin EOF（WT tab 关闭）时主动发 Shutdown 给 DLL，触发 DLL 自卸载
    //           否则 mediator 进程退出后管道断开，DLL 才被动感知（延迟且依赖进程退出）
    std::atomic<bool> stdinStop = false;
    std::atomic<bool> stdinDone = false;
    std::thread stdinThread([this, &stdinStop, &stdinDone]() {
        VtPassThrough::ForwardStdinToPipe(
            [this](const uint8_t* data, size_t len) { RouteInput(data, len); },
            stdinStop, stdinDone);
        // stdin EOF：WT 已关闭，通知 DLL 主动卸载
        // DLL 收到 Shutdown → Unloader::RequestUnload → FreeLibraryAndExitThread
        // → DLL 卸载 → pipe 断开 → 主线程 ForwardPipeToStdout 退出
        if (m_transport && m_transport->IsConnected()) {
            auto pkt = protocol::Serialize(protocol::MessageType::Shutdown, nullptr, 0);
            const int sent = m_transport->Send(pkt.data(), pkt.size());
            LOG_INFO("Shutdown sent to DLL on stdin EOF, sent=%d/%zu",
                     sent, pkt.size());
        }
    });

    // 主线程：pipe→stdout（阻塞循环，pipe 断开时返回）
    // Phase 12：非 VtOutput 消息交给 handler 处理（如 ChildProcessNotify）
    // Phase 6+：处理 ModeChange，发 VT 鼠标报告请求给 WT
    // Phase 11：处理 UnloadComplete，远程调 FreeLibrary 触发 DLL DETACH
    VtPassThrough::ForwardPipeToStdout(*m_transport,
        [this](protocol::MessageType type, const std::vector<uint8_t>& payload) {
            if (type == protocol::MessageType::ChildProcessNotify) {
                // 父进程 DLL 通知子进程创建，创建 ChildSession 等待子进程 DLL 连接
                if (payload.size() >= sizeof(protocol::ChildProcessNotifyPayload)) {
                    protocol::ChildProcessNotifyPayload notify{};
                    std::memcpy(&notify, payload.data(), sizeof(notify));
                    // 子会话随机管道名由父 DLL 生成上报（安全加固），
                    // 与父 DLL 注入子 DLL 时下发的参数必须一致
                    std::wstring childPipe(notify.pipeName);
                    OnChildProcessNotify(notify.childPid, notify.parentPid, childPipe);
                }
            } else if (type == protocol::MessageType::ModeChange) {
                // DLL 上报目标 SetConsoleMode 模式变更
                // 检测 ENABLE_MOUSE_INPUT 变化，发 VT 鼠标报告请求给 WT
                if (payload.size() >= sizeof(protocol::ModeChangePayload)) {
                    protocol::ModeChangePayload mc{};
                    std::memcpy(&mc, payload.data(), sizeof(mc));
                    OnModeChange(mc.inputMode, mc.outputMode);
                }
            } else if (type == protocol::MessageType::UnloadComplete) {
                // Phase 11：DLL 已完成 DoUnload，请求远程 FreeLibrary 触发 DETACH
                // 无 payload，直接调 OnUnloadComplete（用 m_dllBase 远程调 FreeLibrary）
                OnUnloadComplete();
            } else if (type == protocol::MessageType::ModeSwitchNotify) {
                // Phase 13：DLL 通知 VT 输入模式切换
                if (payload.size() >= sizeof(protocol::ModeSwitchNotifyPayload)) {
                    protocol::ModeSwitchNotifyPayload ms{};
                    std::memcpy(&ms, payload.data(), sizeof(ms));
                    OnModeSwitchNotify(ms.vtInputMode, ms.vtOutputMode);
                }
            } else {
                LOG_INFO("BridgeLoop: unhandled msg type=0x%08X len=%zu",
                         static_cast<uint32_t>(type), payload.size());
            }
        });

    // 停止 size watcher（pipe 断开后停止轮询）
    m_sizeWatcher.Stop();

    // 等待 stdin 线程（pipe 断开后的清理步骤）
    // 不能直接 join：ReadConsoleInputW 阻塞时 WT 窗口若仍开着就没有 EOF，
    // 线程永不返回 → 进程挂死（observed: 目标退出/DLL 卸载后残留窗口按键全部失败）
    // 唤醒方式：置 stop 位后从本线程 CancelIoEx(hStdin)，使阻塞的
    // ReadConsoleInputW 返回失败退出。竞态：线程可能尚未进入 ReadConsoleInputW
    // （刚检查完 stop），单次 CancelIoEx 会错过，需重试直到线程置位 done。
    stdinStop.store(true);
    HANDLE hStdin = GetStdHandle(STD_INPUT_HANDLE);
    for (int i = 0; i < 200 && !stdinDone.load(); ++i) {
        CancelIoEx(hStdin, nullptr);
        Sleep(5);
    }
    if (stdinDone.load()) {
        stdinThread.join();
    } else {
        // 兜底：句柄类型不支持 CancelIoEx 等极端情况，线程仍阻塞。
        // 分离后进程退出强制终止线程，避免 join 永久挂死。
        stdinThread.detach();
        LOG_ERROR("stdin thread did not exit after CancelIoEx retries, detached");
    }
    LOG_INFO("BridgeLoop exit");
}

// ============================================================
// Phase 6+：鼠标报告模式管理
// ============================================================
// 收到 DLL 的 ModeChange 时，根据 inputMode 的 ENABLE_MOUSE_INPUT 标志
// 向 WT stdout 发 VT 鼠标报告启用/禁用序列。
//
// WT 只在目标程序请求 VT 鼠标报告（DECSET 1000h/1002h/1003h + 1006h）时
// 才把鼠标事件转为 SGR 1006 序列发给 stdin。mediator 代理这个请求：
//   目标 SetConsoleMode(ENABLE_MOUSE_INPUT) → DLL Hook 发 ModeChange
//   → mediator 发 \x1b[?1002h\x1b[?1006h → WT 启用鼠标报告
//
// 选择 1002h（按钮事件报告）而非 1000h（普通）：1002h 覆盖点击+释放+拖拽，
// 与 Windows ENABLE_MOUSE_INPUT 语义更接近。1006h 为 SGR 格式（坐标从 0 开始）。
//
// 幂等性：只在标志变化时发送，避免重复发 VT 序列
void Mediator::OnModeChange(uint32_t inputMode, uint32_t outputMode, bool fromChild) {
    (void)outputMode;  // 输出模式不影响鼠标报告

    // 子进程的 ModeChange 不应影响鼠标报告状态。
    // 子进程（如 Textual/python）使用 VT 模式，通过 DECSET 序列控制鼠标报告，
    // 若子进程 ModeChange 关闭鼠标报告，会覆盖父进程已开启的鼠标报告，
    // 导致 WT 不再发送 SGR 1006 鼠标事件，子进程无法接收鼠标。
    // 父进程（cmd.exe）开启鼠标报告后应保持，子进程通过 DECSET 自行控制。
    if (fromChild) {
        LOG_INFO("OnModeChange: from child, inputMode=0x%lx, skipping mouse report change", inputMode);
        m_lastInputMode = inputMode;
        return;
    }

    const uint32_t ENABLE_MOUSE_INPUT_FLAG = 0x0010;
    bool wantMouse = (inputMode & ENABLE_MOUSE_INPUT_FLAG) != 0;
    bool changed = (wantMouse != m_mouseReportEnabled);

    LOG_INFO("OnModeChange: inputMode=0x%lx wantMouse=%d wasEnabled=%d changed=%d",
             inputMode, wantMouse ? 1 : 0, m_mouseReportEnabled ? 1 : 0, changed ? 1 : 0);

    if (!changed) {
        m_lastInputMode = inputMode;
        return;
    }

    if (wantMouse) {
        // 启用：按钮事件报告 + SGR 1006 格式
        // 1002h = 按钮事件鼠标报告（点击/释放/拖拽）
        // 1006h = SGR 鼠标格式（坐标从 0 开始，btn;col;rowM/m）
        const char seq[] = "\x1b[?1002h\x1b[?1006h";
        WriteStdoutVt(seq, sizeof(seq) - 1);
        m_mouseReportEnabled = true;
        LOG_INFO("OnModeChange: sent \\x1b[?1002h\\x1b[?1006h (enable mouse report)");
    } else {
        // 禁用：取消按钮事件报告 + SGR 格式
        const char seq[] = "\x1b[?1002l\x1b[?1006l";
        WriteStdoutVt(seq, sizeof(seq) - 1);
        m_mouseReportEnabled = false;
        LOG_INFO("OnModeChange: sent \\x1b[?1002l\\x1b[?1006l (disable mouse report)");
    }

    m_lastInputMode = inputMode;
}

// 向 WT stdout 写 VT 序列
// mediator 的 stdout 连到 WT 的 ConPTY，写的 VT 序列由 WT 渲染端处理
// 用于发鼠标报告请求等控制序列（不经过 DLL 管道）
void Mediator::WriteStdoutVt(const char* data, size_t len) {
    HANDLE hStdout = GetStdHandle(STD_OUTPUT_HANDLE);
    if (hStdout == INVALID_HANDLE_VALUE) {
        LOG_ERROR("WriteStdoutVt: GetStdHandle failed, err=%lu", GetLastError());
        return;
    }
    DWORD written = 0;
    BOOL ok = WriteFile(hStdout, data, static_cast<DWORD>(len), &written, nullptr);
    if (!ok || written != len) {
        LOG_ERROR("WriteStdoutVt: WriteFile failed ok=%d written=%lu len=%zu err=%lu",
                  ok ? 1 : 0, written, len, GetLastError());
    }
}

// ============================================================
// Phase 13：VT 模式切换通知
// ============================================================

void Mediator::OnModeSwitchNotify(uint32_t vtInputMode, uint32_t vtOutputMode) {
    // LIM-004 清理：VT 输入直通/翻译的决策在 DLL 侧（DllRecvLoop VtInput 分支
    // 按 ENABLE_VIRTUAL_TERMINAL_INPUT 选择 raw 直通或 INPUT_RECORD 翻译），
    // mediator 只转发，此处仅记录日志
    LOG_INFO("OnModeSwitchNotify: VT input mode=%u (vtOutput=%u)",
             vtInputMode, vtOutputMode);
}

// ============================================================
// Phase 12：子进程会话管理
// ============================================================

void Mediator::OnChildProcessNotify(uint32_t childPid, uint32_t parentPid,
                                    const std::wstring& pipeName) {
    LOG_INFO("OnChildProcessNotify: childPid=%u parentPid=%u pipe=%ls",
             childPid, parentPid, pipeName.c_str());

    // 创建子进程会话：管道实例 + Handshake + 接收线程
    // VtOutput 回调：子进程输出写到 WT stdout（与父进程输出合并）
    // ChildNotify 回调：子进程创建孙进程时递归创建 ChildSession
    // Exit 回调：子进程退出时同步 ConPTY 光标给父进程 DLL（OnChildExit）
    // ModeChange 回调：子进程 SetConsoleMode 时发 VT 鼠标报告启用/禁用序列给 WT
    // pipeName：父 DLL 生成的随机管道名（必须与父 DLL 传给子 DLL 的一致）
    auto session = std::make_shared<ChildSession>(
        childPid, pipeName,
        [this](const uint8_t* data, size_t len) { WriteChildVtOutput(data, len); },
        [this](uint32_t cp, uint32_t pp, const std::wstring& grandPipe) {
            OnChildProcessNotify(cp, pp, grandPipe); },
        [this](uint32_t cp) { OnChildExit(cp); },
        [this](uint32_t in, uint32_t out, bool fromChild) { OnModeChange(in, out, fromChild); },
        [this](uint32_t vtIn, uint32_t vtOut) { OnModeSwitchNotify(vtIn, vtOut); });
    session->Start();

    // 加入会话列表（线程安全）
    std::lock_guard<std::mutex> lock(m_childMutex);
    m_childSessions.push_back(session);
    LOG_INFO("OnChildProcessNotify: ChildSession started, pid=%u, total=%zu",
             childPid, m_childSessions.size());
}

void Mediator::WriteChildVtOutput(const uint8_t* data, size_t len) {
    // 子进程的 VtOutput 写到 WT stdout（与父进程的 VtOutput 合并）
    // 子进程通常在前台运行（cmd.exe WaitForSingleObject 等待），输出不会交错
    HANDLE hStdout = GetStdHandle(STD_OUTPUT_HANDLE);
    DWORD written = 0;
    BOOL ok = WriteFile(hStdout, data, static_cast<DWORD>(len), &written, nullptr);
    DWORD err = ok ? 0 : GetLastError();
    // 日志含 hex 前缀，供 e2e 测试验证输出内容
    // Phase 13 e2e 测试通过此日志验证 VT 输出直通字节
    size_t hexLen = (len > 256) ? 256 : len;  // 最多记 256 字节
    std::string hex;
    hex.reserve(hexLen * 3);
    for (size_t i = 0; i < hexLen; ++i) {
        char buf[4];
        std::snprintf(buf, sizeof(buf), "%02X ", data[i]);
        hex += buf;
    }
    LOG_INFO("ChildVtOutput: len=%zu written=%lu ok=%d err=%lu hex[%zu]=%s",
             len, written, ok, err, hexLen, hex.c_str());
}

void Mediator::OnChildExit(uint32_t childPid) {
    LOG_INFO("OnChildExit: childPid=%u", childPid);

    // 子进程退出后，查询 ConPTY 当前光标，发给父进程 DLL 对齐 ConsoleState 缓存
    // 原因：父子进程各有独立 ConsoleState，子进程输出推进了 ConPTY 光标，但父进程
    //       缓存仍停留在子进程启动前的位置。父进程输出新 prompt 时会发错误的光标
    //       定位序列，把 ConPTY 光标拉回旧位置，覆盖子进程输出。
    //       此处抢在父进程输出前同步光标，避免覆盖。
    uint16_t curX = 0, curY = 0;
    if (!GetWtCursorPos(curX, curY)) {
        LOG_WARN("OnChildExit: GetWtCursorPos failed, cannot sync cursor, pid=%u",
                 childPid);
        return;
    }

    // 通过父进程管道发 ChildExitSync 给父进程 DLL
    // m_transport->Send 线程安全（NamedPipeTransport::Send 内部加锁），
    // 可在 ChildSession 线程安全调用
    if (!m_transport || !m_transport->IsConnected()) {
        LOG_WARN("OnChildExit: parent transport unavailable, pid=%u", childPid);
        return;
    }

    protocol::ChildExitSyncPayload sync{};
    sync.cursorX = curX;
    sync.cursorY = curY;
    auto pkt = protocol::Serialize(protocol::MessageType::ChildExitSync,
                                   &sync, sizeof(sync));
    const int sent = m_transport->Send(pkt.data(), pkt.size());
    LOG_INFO("OnChildExit: ChildExitSync sent cursor=(%u,%u) sent=%d/%zu pid=%u",
             curX, curY, sent, pkt.size(), childPid);
}

// ============================================================
// Phase 12+：输入路由
// ============================================================

void Mediator::RouteInput(const uint8_t* data, size_t len) {
    // Phase 14：先通过 VtParser 解析，检测 DSR CPR 等 WT 响应
    // 若检测到 DSR CPR，VtParser 回调发送 WtStateReport 给 DLL
    m_vtParser.Feed(data, len);

    // 路由策略：优先发送到最后一个活跃 ChildSession（最深前台子进程）；
    //           无活跃子进程时回退到父进程 transport（保持 cmd 自身输入）
    //
    // 线程安全：m_childMutex 保护 m_childSessions 的遍历与清理
    //   - OnChildProcessNotify（主线程）push_back 时持锁
    //   - RouteInput（stdin 线程）遍历时持锁
    //   - 两者互斥，不会出现迭代器失效
    //
    // 注意：在持锁状态下调用 SendVtInput / m_transport->Send 不会死锁：
    //   - ChildSession::SendVtInput 调子进程 transport 的 Send（内部自带锁，不回调 RouteInput）
    //   - 父进程 m_transport->Send 同理
    //   - m_childMutex 只保护 m_childSessions 列表本身，不嵌套 transport 锁
    std::lock_guard<std::mutex> lock(m_childMutex);

    // 清理已退出的 ChildSession（避免列表无限增长）
    m_childSessions.erase(
        std::remove_if(m_childSessions.begin(), m_childSessions.end(),
            [](const std::shared_ptr<ChildSession>& s) { return !s->IsActive(); }),
        m_childSessions.end());

    // 逆序遍历：最后加入的 ChildSession 是最深前台子进程（如 cmd → python 的 python）
    for (auto it = m_childSessions.rbegin(); it != m_childSessions.rend(); ++it) {
        if ((*it)->IsActive()) {
            (*it)->SendVtInput(data, len);
            LOG_INFO("RouteInput: routed to child pid=%u, len=%zu", (*it)->Pid(), len);
            return;
        }
    }

    // 没有活跃子进程：发送到父进程 transport（cmd 自身输入）
    auto pkt = protocol::Serialize(protocol::MessageType::VtInput, data,
                                    static_cast<uint32_t>(len));
    const int sent = m_transport->Send(pkt.data(), pkt.size());
    LOG_INFO("RouteInput: routed to parent, len=%zu sent=%d/%zu",
             len, sent, pkt.size());
}

// ============================================================
// Phase 11：DLL 远程卸载
// ============================================================

// 收到 DLL 的 UnloadComplete 后，在目标进程中创建远程线程调用
// FreeLibrary(dllBase)，把 LoadCount 从 1 减到 0 触发 DLL_PROCESS_DETACH。
//
// 为什么需要远程线程（不能让 DLL 自己 FreeLibrary）：
//   DLL 的 DoUnload 线程曾在 injected.dll 代码中执行，LDR 为其持有
//   LdrpThreadBlob 引用，即使线程退出 LoadCount 也不会归零（实测减到 1）。
//   远程线程由 LDR 全新创建，从未进入 injected.dll 代码，不持有 ThreadBlob，
//   其调用 FreeLibrary 会让 LoadCount 真正归零，触发 DETACH。
//
// FreeLibrary 地址解析：
//   kernel32.dll 是系统 DLL，Windows 在所有 64 位进程中共享同一基址
//   （ASLR 仅在启动时随机一次，系统 DLL 在所有进程中相同）。
//   因此 mediator 进程的 kernel32!FreeLibrary 地址等于目标进程中的地址。
void Mediator::OnUnloadComplete() {
    LOG_INFO("OnUnloadComplete: received, targetPid=%u dllBase=0x%llx",
             m_targetPid, static_cast<unsigned long long>(m_dllBase));

    if (m_dllBase == 0) {
        LOG_ERROR("OnUnloadComplete: dllBase is 0 (Hello 未上报?), skip remote FreeLibrary");
        return;
    }

    // 1. 打开目标进程（需 VM_OPERATION 才能创建远程线程）
    //    权限：
    //      PROCESS_CREATE_THREAD  - 创建远程线程
    //      PROCESS_QUERY_INFORMATION - 后续 GetExitCodeThread 等
    //      PROCESS_VM_OPERATION  - WriteProcessMemory（CreateRemoteThread 隐式需要）
    //      PROCESS_VM_WRITE      - 写入远程线程参数（HMODULE）
    //      PROCESS_VM_READ       - 读取远程内存（备用）
    DWORD access = PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION |
                   PROCESS_VM_OPERATION | PROCESS_VM_WRITE | PROCESS_VM_READ;
    HANDLE hProc = OpenProcess(access, FALSE, m_targetPid);
    if (!hProc) {
        LOG_ERROR("OnUnloadComplete: OpenProcess failed pid=%u err=%lu",
                  m_targetPid, GetLastError());
        return;
    }

    // 2. 取 kernel32!FreeLibrary 地址
    //    系统共享基址保证此地址在目标进程中也是 FreeLibrary
    HMODULE hKernel32 = GetModuleHandleW(L"kernel32.dll");
    if (!hKernel32) {
        LOG_ERROR("OnUnloadComplete: GetModuleHandleW(kernel32) failed err=%lu",
                  GetLastError());
        CloseHandle(hProc);
        return;
    }
    auto pFreeLibrary = reinterpret_cast<LPTHREAD_START_ROUTINE>(
        GetProcAddress(hKernel32, "FreeLibrary"));
    if (!pFreeLibrary) {
        LOG_ERROR("OnUnloadComplete: GetProcAddress(FreeLibrary) failed err=%lu",
                  GetLastError());
        CloseHandle(hProc);
        return;
    }

    // 3. 创建远程线程调 FreeLibrary(dllBase)
    //    FreeLibrary 签名：BOOL WINAPI FreeLibrary(HMODULE hLibModule)
    //    与 LPTHREAD_START_ROUTINE 的单参数 stdcall 签名兼容
    //    HMODULE 在 64 位是 8 字节，与 LPVOID 等宽，可直接传
    HANDLE hThread = CreateRemoteThread(
        hProc, nullptr, 0,
        pFreeLibrary,
        reinterpret_cast<LPVOID>(static_cast<uintptr_t>(m_dllBase)),
        0, nullptr);
    if (!hThread) {
        LOG_ERROR("OnUnloadComplete: CreateRemoteThread failed pid=%u dllBase=0x%llx err=%lu",
                  m_targetPid,
                  static_cast<unsigned long long>(m_dllBase),
                  GetLastError());
        CloseHandle(hProc);
        return;
    }

    // 4. 等待远程 FreeLibrary 返回（最多 5 秒）
    //    FreeLibrary 会触发 DLL_PROCESS_DETACH（DllMain 中 MH_Uninitialize 等）
    //    正常应在毫秒级完成；超时视为异常（DETACH 卡死或 Loader Lock）
    DWORD waitRet = WaitForSingleObject(hThread, 5000);
    DWORD exitCode = 0;
    GetExitCodeThread(hThread, &exitCode);
    // FreeLibrary 返回非零表示成功（BOOL TRUE）
    LOG_INFO("OnUnloadComplete: remote FreeLibrary returned, wait=%lu exitCode=%lu "
             "(nonzero=success) dllBase=0x%llx",
             waitRet, exitCode,
             static_cast<unsigned long long>(m_dllBase));

    CloseHandle(hThread);
    CloseHandle(hProc);
}

} // namespace terminjector
