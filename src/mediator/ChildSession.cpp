// 子进程会话管理实现（mediator 侧）
// 详见 docs/phases/12-child-process-injection.md 4.4.3-4.5
//
// 线程模型：
//   Start() → 线程执行 Run()
//   Run()   → Create pipe → WaitClient → DoHandshake → RecvLoop
//   RecvLoop 退出 → m_exited=true → 线程结束
//   ~ChildSession → Disconnect + join
//
// 生命周期管理：
//   - ChildSession 由 Mediator 的 m_childSessions 管理
//   - 线程退出后 m_exited=true，但对象仍在列表中（不主动移除）
//   - Mediator 析构时统一清理所有 ChildSession
#include "ChildSession.h"
#include "transport/ITransport.h"
#include "protocol/MessageSerializer.h"
#include "protocol/Message.h"
#include "logging/Logger.h"

#include <windows.h>
#include <cstring>
#include <vector>

namespace terminjector {

namespace {

// 获取 WT 侧当前窗口尺寸（与 Mediator.cpp 中的实现一致）
// 用于回填 HelloAck，子进程 DLL 据此校正 ConsoleState
bool GetWtWindowSize(uint16_t& cols, uint16_t& rows) {
    HANDLE hOut = GetStdHandle(STD_OUTPUT_HANDLE);
    CONSOLE_SCREEN_BUFFER_INFO csbi{};
    if (!GetConsoleScreenBufferInfo(hOut, &csbi)) return false;
    cols = static_cast<uint16_t>(csbi.srWindow.Right - csbi.srWindow.Left + 1);
    rows = static_cast<uint16_t>(csbi.srWindow.Bottom - csbi.srWindow.Top + 1);
    return true;
}

// 获取 WT/ConPTY 当前光标位置（与 Mediator.cpp 中的实现一致）
// 子进程 DLL 用它对齐 ConsoleState 光标缓存，使子进程输出接在 WT 当前位置之后
bool GetWtCursorPos(uint16_t& cursorX, uint16_t& cursorY) {
    HANDLE hOut = GetStdHandle(STD_OUTPUT_HANDLE);
    CONSOLE_SCREEN_BUFFER_INFO csbi{};
    if (!GetConsoleScreenBufferInfo(hOut, &csbi)) return false;
    cursorX = static_cast<uint16_t>(csbi.dwCursorPosition.X);
    cursorY = static_cast<uint16_t>(csbi.dwCursorPosition.Y);
    return true;
}

} // namespace

ChildSession::ChildSession(uint32_t childPid,
                           VtOutputCallback         onVtOutput,
                           ChildNotifyCallback      onChildNotify,
                           ExitCallback             onExit,
                           ModeChangeCallback       onModeChange,
                           ModeSwitchNotifyCallback onModeSwitchNotify)
    : m_childPid(childPid)
    , m_onVtOutput(std::move(onVtOutput))
    , m_onChildNotify(std::move(onChildNotify))
    , m_onExit(std::move(onExit))
    , m_onModeChange(std::move(onModeChange))
    , m_onModeSwitchNotify(std::move(onModeSwitchNotify)) {
}

ChildSession::~ChildSession() {
    m_running = false;
    // 断开管道，中断阻塞的 RecvPacket / WaitClient
    if (m_transport) {
        m_transport->Disconnect();
    }
    if (m_thread.joinable()) {
        m_thread.join();
    }
}

void ChildSession::Start() {
    m_running = true;
    m_thread = std::thread(&ChildSession::Run, this);
}

void ChildSession::Run() {
    // 确保线程退出时设置 m_exited 标志
    struct ExitGuard {
        std::atomic<bool>& flag;
        explicit ExitGuard(std::atomic<bool>& f) : flag(f) {}
        ~ExitGuard() { flag.store(true); }
    } guard{m_exited};

    LOG_INFO("ChildSession: Run start, pid=%u", m_childPid);

    // 1. 创建管道实例 \\.\pipe\terminjector_<child_pid>
    m_transport = std::make_unique<NamedPipeTransport>(
        MakePipeName(m_childPid), NamedPipeTransport::Role::Server);
    if (!m_transport->Create()) {
        LOG_ERROR("ChildSession: Create pipe failed, pid=%u", m_childPid);
        return;
    }

    // 2. 等待子进程 DLL 连接（阻塞）
    //    子进程 ResumeThread 后首个 Console API 触发 LazyInit，连接管道
    //    NamedPipeTransport Client 端有 5s 重试，此处等待不会超时
    if (!m_transport->WaitClient()) {
        LOG_ERROR("ChildSession: WaitClient failed, pid=%u", m_childPid);
        return;
    }
    LOG_INFO("ChildSession: DLL connected, pid=%u", m_childPid);

    // 3. Hello 握手
    if (!DoHandshake()) {
        LOG_ERROR("ChildSession: Handshake failed, pid=%u", m_childPid);
        return;
    }

    // 4. 接收循环
    RecvLoop();

    // 子进程已退出（RecvLoop 结束）：通知 mediator 同步 ConPTY 光标给父进程 DLL
    // 必须在 RecvLoop 之后、线程退出之前触发，让 mediator 抢在父进程输出新 prompt
    // 前把 ConPTY 光标发给父进程 DLL 对齐 ConsoleState 缓存
    if (m_onExit) {
        m_onExit(m_childPid);
    }

    LOG_INFO("ChildSession: Run exit, pid=%u", m_childPid);
}

bool ChildSession::DoHandshake() {
    using namespace protocol;

    // 收 Hello
    MessageType type;
    std::vector<uint8_t> payload;
    if (!RecvPacket(m_transport.get(), type, payload)) {
        LOG_ERROR("ChildSession Handshake: RecvPacket(Hello) failed, pid=%u", m_childPid);
        return false;
    }
    if (type != MessageType::Hello) {
        LOG_ERROR("ChildSession Handshake: expected Hello, got type=0x%08X, pid=%u",
                  static_cast<uint32_t>(type), m_childPid);
        return false;
    }

    // 解析 Hello payload（仅记日志，不 ApplySnapshot）
    HelloPayload hello{};
    if (payload.size() >= sizeof(hello)) {
        std::memcpy(&hello, payload.data(), sizeof(hello));
    }
    LOG_INFO("ChildSession Handshake: Hello received, pid=%u cols=%u rows=%u cursor=(%u,%u)",
             hello.targetPid, hello.bufferCols, hello.bufferRows,
             hello.cursorX, hello.cursorY);

    // 回 HelloAck：携带 WT 侧当前窗口尺寸 + 当前光标位置
    // isTarget=0：本会话为子进程（父进程 CreateProcess 创建），Hook 已就位，
    //             不存在旧 ReadConsoleW 阻塞，DLL 禁止 KickStart
    // cursorX/Y：子进程 DLL 用它对齐 ConsoleState 光标缓存，使子进程输出
    //            接在 WT 当前位置（父进程输出之后）之后
    HelloAckPayload ack{};
    ack.isTarget = 0;
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
        LOG_ERROR("ChildSession Handshake: Send(HelloAck) failed, sent=%d expected=%zu",
                  sent, ackPkt.size());
        return false;
    }
    LOG_INFO("ChildSession Handshake: HelloAck sent, wtCols=%u wtRows=%u isTarget=0 cursor=(%u,%u) pid=%u",
             ack.wtCols, ack.wtRows, ack.cursorX, ack.cursorY, m_childPid);
    return true;
}

void ChildSession::RecvLoop() {
    using namespace protocol;

    // Phase 12+：用 Peek 轮询代替阻塞 RecvPacket
    // 原因：同步命名管道一次只能一个 I/O 操作（MSDN），
    //   阻塞的 ReadFile(RecvPacket) 会持有管道 I/O 锁，
    //   阻止 mediator RouteInput→SendVtInput 的 WriteFile(Send) → 死锁：
    //   RecvLoop 等 DLL VtOutput → DLL 等子进程输出 →
    //   子进程等 InputQueue → InputQueue 等 mediator VtInput →
    //   SendVtInput 等 ReadFile 释放锁 → 死锁
    // 方案：Peek 非阻塞探测，有数据才调 RecvPacket（此时 ReadFile 不会长阻塞）
    //       与 VtPassThrough::ForwardPipeToStdout 一致
    uint8_t peekBuf[1];
    while (m_running && m_transport && m_transport->IsConnected()) {
        int peeked = m_transport->Peek(peekBuf, 1);
        if (peeked < 0) {
            // 管道出错或断开
            LOG_INFO("ChildSession RecvLoop: pipe error/broken (Peek=%d), pid=%u",
                     peeked, m_childPid);
            break;
        }
        if (peeked == 0) {
            // 无数据，短暂休眠后重试
            Sleep(10);
            continue;
        }

        // 有数据可读，调 RecvPacket 读取完整包
        // 此时管道内有数据，ReadFile 不会长时间阻塞，不影响 SendVtInput 的 Send
        MessageType type;
        std::vector<uint8_t> payload;
        if (!RecvPacket(m_transport.get(), type, payload)) {
            LOG_INFO("ChildSession RecvLoop: pipe closed (RecvPacket failed), pid=%u",
                     m_childPid);
            break;
        }

        switch (type) {
            case MessageType::VtOutput:
                // 子进程输出 → 回调 mediator 写 WT stdout
                if (m_onVtOutput && !payload.empty()) {
                    m_onVtOutput(payload.data(), payload.size());
                }
                break;

            case MessageType::ChildProcessNotify:
                // 子进程创建了孙进程 → 回调 mediator 创建孙进程 ChildSession
                if (m_onChildNotify && payload.size() >= sizeof(ChildProcessNotifyPayload)) {
                    ChildProcessNotifyPayload notify{};
                    std::memcpy(&notify, payload.data(), sizeof(notify));
                    LOG_INFO("ChildSession RecvLoop: ChildProcessNotify "
                             "grandchild=%u parent=%u", notify.childPid, notify.parentPid);
                    m_onChildNotify(notify.childPid, notify.parentPid);
                }
                break;

            case MessageType::ByeAck:
                // 子进程 DLL 卸载（Phase 11），退出接收循环
                LOG_INFO("ChildSession RecvLoop: ByeAck received, pid=%u", m_childPid);
                m_running = false;
                break;

            case MessageType::ModeChange: {
                // 子进程 SetConsoleMode → DLL 发 ModeChange
                // 转发 mediator 的 OnModeChange：发 VT 鼠标报告启用/禁用序列给 WT
                // 不转发的后果：子进程启用 ENABLE_MOUSE_INPUT 时 WT 不开鼠标报告，
                // 鼠标事件不发 SGR 1006 序列，子进程 ReadConsoleInputW 永远阻塞
                if (m_onModeChange && payload.size() >= sizeof(ModeChangePayload)) {
                    ModeChangePayload mc{};
                    std::memcpy(&mc, payload.data(), sizeof(mc));
                    LOG_INFO("ChildSession RecvLoop: ModeChange inputMode=0x%lx outputMode=0x%lx, pid=%u",
                             mc.inputMode, mc.outputMode, m_childPid);
                    m_onModeChange(mc.inputMode, mc.outputMode);
                }
                break;
            }

            case MessageType::ModeSwitchNotify: {
                // 子进程启用 ENABLE_VIRTUAL_TERMINAL_INPUT → DLL 发 ModeSwitchNotify
                // 转发 mediator 的 OnModeSwitchNotify：记录 VT 输入模式状态
                if (m_onModeSwitchNotify && payload.size() >= sizeof(ModeSwitchNotifyPayload)) {
                    ModeSwitchNotifyPayload ms{};
                    std::memcpy(&ms, payload.data(), sizeof(ms));
                    LOG_INFO("ChildSession RecvLoop: ModeSwitchNotify vtInput=%d vtOutput=%d, pid=%u",
                             ms.vtInputMode, ms.vtOutputMode, m_childPid);
                    m_onModeSwitchNotify(ms.vtInputMode, ms.vtOutputMode);
                }
                break;
            }

            default:
                // 其他消息（CpChange/Ping/Pong 等）暂不处理
                LOG_INFO("ChildSession RecvLoop: unhandled msg type=0x%08X len=%zu, pid=%u",
                         static_cast<uint32_t>(type), payload.size(), m_childPid);
                break;
        }
    }
}

void ChildSession::SendVtInput(const uint8_t* data, size_t len) {
    if (!m_transport || !m_transport->IsConnected() || data == nullptr || len == 0) {
        return;
    }
    auto pkt = protocol::Serialize(protocol::MessageType::VtInput, data,
                                   static_cast<uint32_t>(len));
    m_transport->Send(pkt.data(), pkt.size());
}

} // namespace terminjector
