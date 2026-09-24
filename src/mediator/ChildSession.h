// 子进程会话管理（mediator 侧）
// 详见 docs/phases/12-child-process-injection.md 4.4.3-4.5
//
// Phase 12：每个被注入的子进程对应一个 ChildSession
//   - 创建命名管道实例 \\.\pipe\terminjector_<child_pid>_<随机>
//     （管道名由父 DLL 生成并经 ChildProcessNotify 上报，安全加固）
//   - 等待子进程 DLL 连接
//   - Hello 握手（收 Hello，回 HelloAck，不 ApplySnapshot）
//   - 接收线程：收 VtOutput → 回调 mediator 写 stdout；
//                收 ChildProcessNotify → 回调 mediator 创建孙进程会话
//
// Phase 23：会话分两种对端
//   - 普通会话（peerIsRelay=false）：对端是 injected.dll，握手走 Hello/HelloAck，
//     有 VtOutput 与输入路由
//   - 中继会话（peerIsRelay=true）：对端是 32 位 relay32.dll（如 py.exe 里的），
//     它没有 Console 会话 —— 握手走 RelayHello（不回 HelloAck）、不参与输入路由、
//     不产生 VtOutput；它只会发 RelayChildNotify 请 mediator 替它注入 64 位子进程，
//     并等 RelayChildAck
//
// 生命周期：
//   Start()  → 线程启动（Create + WaitClient + Handshake + RecvLoop）
//   RecvLoop 退出 → 线程结束（HasExited() 返回 true）
//   ~ChildSession → Disconnect + join
//
// 线程安全：
//   - ChildSession 由 Mediator 的 m_childSessions（shared_ptr vector）管理
//   - 回调在 ChildSession 线程中执行，访问 Mediator 成员
//   - Mediator 析构时 ChildSession 析构（Disconnect 中断线程 + join）
//   - 不需要 ExitCallback：已退出的 ChildSession 留在列表中，析构时统一清理
#pragma once

#include "transport/NamedPipeTransport.h"

#include <cstdint>
#include <functional>
#include <memory>
#include <thread>
#include <atomic>

namespace terminjector {

class ChildSession {
public:
    // 收到子进程 VtOutput 时调用（mediator 据此写 WT stdout）
    using VtOutputCallback    = std::function<void(const uint8_t*, size_t)>;
    // 收到 ChildProcessNotify 时调用（mediator 据此创建孙进程会话）
    // 第三个参数 pipeName：子/孙 DLL 生成的随机管道名
    // 第四个参数 peerIsRelay：孙会话对端是否为 32 位中继（决定其握手方式）
    using ChildNotifyCallback = std::function<void(uint32_t childPid, uint32_t parentPid,
                                                   const std::wstring& pipeName,
                                                   bool peerIsRelay)>;
    // 收到中继的 RelayChildNotify 时调用（mediator 据此建孙会话 + 注入 + 返回成败）
    // 返回值即回给中继的 RelayChildAck.ok —— 中继据此决定是否记录注入失败
    using RelayChildNotifyCallback =
        std::function<bool(uint32_t childPid, uint32_t parentPid,
                           const std::wstring& pipeName)>;
    // 子进程退出时调用（RecvLoop 结束后触发，mediator 据此同步 ConPTY 光标给父进程 DLL）
    using ExitCallback        = std::function<void(uint32_t childPid)>;
    // 收到 ModeChange 时调用（mediator 据此发 VT 鼠标报告启用/禁用序列给 WT）
    // 子进程 SetConsoleMode(ENABLE_MOUSE_INPUT) → DLL 发 ModeChange → 本回调
    // fromChild=true：子进程的 ModeChange 不应影响鼠标报告状态
    using ModeChangeCallback  = std::function<void(uint32_t inputMode, uint32_t outputMode, bool fromChild)>;
    // 收到 ModeSwitchNotify 时调用（mediator 据此记录 VT 输入模式状态）
    // 子进程 SetConsoleMode 检测到 ENABLE_VIRTUAL_TERMINAL_INPUT 标志变化 → 本回调
    using ModeSwitchNotifyCallback = std::function<void(uint32_t vtInputMode, uint32_t vtOutputMode)>;

    ChildSession(uint32_t childPid,
                 const std::wstring& pipeName,
                 bool peerIsRelay,
                 VtOutputCallback    onVtOutput,
                 ChildNotifyCallback onChildNotify,
                 RelayChildNotifyCallback onRelayChildNotify,
                 ExitCallback        onExit,
                 ModeChangeCallback  onModeChange,
                 ModeSwitchNotifyCallback onModeSwitchNotify);
    ~ChildSession();

    ChildSession(const ChildSession&) = delete;
    ChildSession& operator=(const ChildSession&) = delete;

    // 启动会话线程（非阻塞）
    // 线程内执行：Create pipe → WaitClient → Handshake → RecvLoop
    void Start();

    // 会话对端是否为 32 位中继（无 Console 语义）
    bool IsRelay() const { return m_isRelay; }

    // 转发 VtInput 给子进程 DLL（Phase 6+ 使用）
    void SendVtInput(const uint8_t* data, size_t len);

    // 转发 ResizeNotify 给子进程 DLL（Phase 19）
    // WT 窗口尺寸变化时 mediator 调用此方法通知子进程 DLL，
    // 子进程 DLL 更新 ConsoleState 的缓冲区/窗口尺寸。
    // TUI 程序（如 Textual）在子进程中运行，需要 resize 通知来调整布局。
    void SendResize(uint16_t cols, uint16_t rows,
                    uint16_t bufferCols, uint16_t bufferRows);

    // 接收线程是否已退出（RecvLoop 结束）
    bool HasExited() const { return m_exited.load(); }

    // 会话是否活跃（未退出 + 管道已建立 + 仍连接）
    // mediator 的 RouteInput 据此判断是否向此 ChildSession 转发输入
    // 中继会话恒为 false：中继进程没有 Console，输入不该发给它
    bool IsActive() const {
        return !m_isRelay && !m_exited.load() && m_running.load() &&
               m_transport && m_transport->IsConnected();
    }

    uint32_t Pid() const { return m_childPid; }

private:
    uint32_t m_childPid;
    std::wstring m_pipeName;  // 随机管道名（父 DLL 生成上报）
    bool m_isRelay;           // 对端是否 32 位中继（决定握手与是否参与输入路由）
    std::unique_ptr<NamedPipeTransport> m_transport;
    std::thread m_thread;
    std::atomic<bool> m_running{false};
    std::atomic<bool> m_exited{false};

    VtOutputCallback         m_onVtOutput;
    ChildNotifyCallback      m_onChildNotify;
    RelayChildNotifyCallback m_onRelayChildNotify;
    ExitCallback             m_onExit;
    ModeChangeCallback       m_onModeChange;
    ModeSwitchNotifyCallback m_onModeSwitchNotify;

    // 线程主函数：Create + WaitClient + Handshake + RecvLoop
    void Run();

    // 握手：普通会话收 Hello 回 HelloAck；中继会话收 RelayHello（不回 Ack，
    // 中继没有 Console 状态快照要接收）
    bool DoHandshake();

    // 接收循环：处理 VtOutput / ChildProcessNotify / RelayChildNotify / ByeAck
    void RecvLoop();

    // 给中继回 RelayChildAck（成败都要回：中继靠它 ResumeThread）
    void SendRelayChildAck(uint32_t childPid, uint32_t ok);
};

} // namespace terminjector
