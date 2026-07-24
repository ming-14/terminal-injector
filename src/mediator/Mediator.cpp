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
#include "logging/Logger.h"

#include <windows.h>
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
void ApplySnapshotToWt(const protocol::HelloPayload& hello) {
    std::string vt;
    char buf[64];

    // 设置窗口尺寸（\x1b[8;<rows>;<cols>t）
    // 用可见窗口高度（windowRows）而非缓冲区高度（bufferRows=9001），
    // 否则会把 WT 窗口撑到 9001 行，后续 GetWtWindowSize 读到错误的 9001
    int n = std::snprintf(buf, sizeof(buf),
        "\x1b[8;%u;%ut", hello.windowRows, hello.bufferCols);
    vt.append(buf, n);

    // 写到 stdout（WT 收到并渲染）
    HANDLE hOut = GetStdHandle(STD_OUTPUT_HANDLE);
    DWORD written = 0;
    WriteFile(hOut, vt.data(), static_cast<DWORD>(vt.size()), &written, nullptr);
    LOG_INFO("Applied snapshot to WT: %s", vt.c_str());
}

} // namespace

Mediator::Mediator() = default;
Mediator::~Mediator() = default;

int Mediator::Run(uint32_t targetPid, const std::wstring& pipeName,
                  const std::wstring& dllPath) {
    m_targetPid = targetPid;
    LOG_INFO("Mediator starting, targetPid=%u pipe=%ls dll=%ls",
             targetPid, pipeName.c_str(), dllPath.c_str());

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
                       L" --dll \"" + dllPath + L"\"";
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
             "winRows=%u mode=0x%04x cp=%u/%u cursor=(%u,%u)",
             hello.targetPid, hello.targetBitness,
             hello.bufferCols, hello.bufferRows, hello.windowRows,
             hello.consoleMode, hello.consoleCp, hello.consoleOutputCp,
             hello.cursorX, hello.cursorY);

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

    // Phase 5：启动 WT 尺寸监听
    // WT 窗口 resize 时封装 ResizeNotify 发给 DLL，DLL 更新 ConsoleState
    m_sizeWatcher.Start([this](int cols, int rows, int bufCols, int bufRows) {
        protocol::ResizePayload p{};
        p.cols       = static_cast<uint16_t>(cols);
        p.rows       = static_cast<uint16_t>(rows);
        p.bufferCols = static_cast<uint16_t>(bufCols);
        p.bufferRows = static_cast<uint16_t>(bufRows);
        auto pkt = protocol::Serialize(protocol::MessageType::ResizeNotify, &p, sizeof(p));
        const int sent = m_transport->Send(pkt.data(), pkt.size());
        LOG_INFO("ResizeNotify sent: win=%dx%d buf=%dx%d (sent=%d/%zu)",
                 cols, rows, bufCols, bufRows, sent, pkt.size());
    });

    // stdin→pipe 独立线程（ReadFile 阻塞，pipe 断开时主线程退出，
    // 进程结束会强制终止此线程；Phase 10 用 CancelIoEx 优雅唤醒）
    // Phase 12+：通过 RouteInput 路由回调，把输入分发到活跃子进程或父进程
    std::thread stdinThread([this]() {
        VtPassThrough::ForwardStdinToPipe(
            [this](const uint8_t* data, size_t len) { RouteInput(data, len); });
    });

    // 主线程：pipe→stdout（阻塞循环，pipe 断开时返回）
    // Phase 12：非 VtOutput 消息交给 handler 处理（如 ChildProcessNotify）
    // Phase 6+：处理 ModeChange，发 VT 鼠标报告请求给 WT
    VtPassThrough::ForwardPipeToStdout(*m_transport,
        [this](protocol::MessageType type, const std::vector<uint8_t>& payload) {
            if (type == protocol::MessageType::ChildProcessNotify) {
                // 父进程 DLL 通知子进程创建，创建 ChildSession 等待子进程 DLL 连接
                if (payload.size() >= sizeof(protocol::ChildProcessNotifyPayload)) {
                    protocol::ChildProcessNotifyPayload notify{};
                    std::memcpy(&notify, payload.data(), sizeof(notify));
                    OnChildProcessNotify(notify.childPid, notify.parentPid);
                }
            } else if (type == protocol::MessageType::ModeChange) {
                // DLL 上报目标 SetConsoleMode 模式变更
                // 检测 ENABLE_MOUSE_INPUT 变化，发 VT 鼠标报告请求给 WT
                if (payload.size() >= sizeof(protocol::ModeChangePayload)) {
                    protocol::ModeChangePayload mc{};
                    std::memcpy(&mc, payload.data(), sizeof(mc));
                    OnModeChange(mc.inputMode, mc.outputMode);
                }
            } else {
                LOG_INFO("BridgeLoop: unhandled msg type=0x%08X len=%zu",
                         static_cast<uint32_t>(type), payload.size());
            }
        });

    // 停止 size watcher（pipe 断开后停止轮询）
    m_sizeWatcher.Stop();

    // 等待 stdin 线程（实际场景下进程退出会强制终止，这里 join 兜底）
    stdinThread.join();
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
void Mediator::OnModeChange(uint32_t inputMode, uint32_t outputMode) {
    (void)outputMode;  // 输出模式不影响鼠标报告

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
// Phase 12：子进程会话管理
// ============================================================

void Mediator::OnChildProcessNotify(uint32_t childPid, uint32_t parentPid) {
    LOG_INFO("OnChildProcessNotify: childPid=%u parentPid=%u", childPid, parentPid);

    // 创建子进程会话：管道实例 + Handshake + 接收线程
    // VtOutput 回调：子进程输出写到 WT stdout（与父进程输出合并）
    // ChildNotify 回调：子进程创建孙进程时递归创建 ChildSession
    // Exit 回调：子进程退出时同步 ConPTY 光标给父进程 DLL（OnChildExit）
    auto session = std::make_shared<ChildSession>(
        childPid,
        [this](const uint8_t* data, size_t len) { WriteChildVtOutput(data, len); },
        [this](uint32_t cp, uint32_t pp) { OnChildProcessNotify(cp, pp); },
        [this](uint32_t cp) { OnChildExit(cp); });
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
    LOG_INFO("ChildVtOutput: len=%zu written=%lu ok=%d err=%lu",
             len, written, ok, err);
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

} // namespace terminjector
