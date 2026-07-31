// 中介程序：被 WT 启动，桥接 DLL 与 WT 的 ConPTY
// 详见 docs/phases/02-injector-modes.md 4.5
//
// 时序（关键）：
//   1. Create()    创建命名管道（不阻塞）
//   2. SpawnInjector  fork 自身 --inject 子命令，注入 DLL 到目标
//   3. WaitClient() 阻塞等待 DLL 连接（DLL 在 DllMain 中作为 Client 连接）
//   4. Handshake()  收 Hello，回 HelloAck
//   5. BridgeLoop() 双向转发（Phase 3+ 实现）
//
// Phase 12 扩展：多客户端管理
//   - 主管道：父进程 DLL 连接
//   - 子进程管道：父进程 DLL 发 ChildProcessNotify 时，创建 ChildSession
//   - VtOutput 合并：所有子进程的 VtOutput 写到同一 WT stdout
#pragma once

#include "transport/ITransport.h"
#include "WtSizeWatcher.h"
#include "ChildSession.h"
#include "VtParser.h"

#include <memory>
#include <mutex>
#include <vector>
#include <cstdint>
#include <string>
#include <atomic>

namespace terminjector {

class Mediator {
public:
    Mediator();
    ~Mediator();

    Mediator(const Mediator&) = delete;
    Mediator& operator=(const Mediator&) = delete;

    // 主循环
    // targetPid 目标进程 PID（构造管道名、触发注入）
    // pipeName  命名管道名
    // dllPath   injected.dll 路径（fork inject 子命令时用）
    // 返回退出码
    int Run(uint32_t targetPid, const std::wstring& pipeName,
            const std::wstring& dllPath);

private:
    // 触发注入：fork 自身以 --inject 模式运行
    // 返回 true 表示注入子进程已启动（不等其完成）
    bool SpawnInjector(uint32_t targetPid, const std::wstring& dllPath);

    // 执行 Hello 握手
    // 收 DLL 发来的 Hello，回 HelloAck
    bool Handshake();

    // 桥接循环：stdin(WT) ↔ pipe(DLL)
    // Phase 3+ 实现真实桥接，Phase 12 处理 ChildProcessNotify
    void BridgeLoop();

    // === Phase 12：子进程会话管理 ===

    // 收到 ChildProcessNotify 时创建子进程会话
    // childPid 新创建的子进程 PID
    // parentPid 父进程 PID
    void OnChildProcessNotify(uint32_t childPid, uint32_t parentPid);

    // 子进程退出时同步 ConPTY 光标给父进程 DLL
    // childPid 退出的子进程 PID
    // 由 ChildSession 的 ExitCallback 触发，查询 ConPTY 当前光标，
    // 通过 ChildExitSync 消息发给父进程 DLL 对齐 ConsoleState 缓存
    void OnChildExit(uint32_t childPid);

    // 将子进程的 VtOutput 写到 WT stdout
    // data/len VT 字节流
    void WriteChildVtOutput(const uint8_t* data, size_t len);

    // === Phase 12+：输入路由 ===

    // 将 WT stdin 输入路由到活跃子进程（无活跃子进程时发到父进程）
    // 由 VtPassThrough::ForwardStdinToPipe 的 InputRouter 回调调用，在 stdin 线程执行
    // 路由策略：优先发送到最后一个活跃 ChildSession（最深前台子进程）；
    //           无活跃子进程时回退到父进程 transport（保持 cmd 自身输入）
    // 线程安全：通过 m_childMutex 保护 m_childSessions 的遍历与清理
    void RouteInput(const uint8_t* data, size_t len);

    // === Phase 6+：鼠标报告模式管理 ===

    // 收到 DLL 的 ModeChange 时，根据 inputMode 的 ENABLE_MOUSE_INPUT 标志
    // 向 WT stdout 发送 VT 鼠标报告启用/禁用序列。
    //   含 ENABLE_MOUSE_INPUT → \x1b[?1002h\x1b[?1006h（按钮事件 + SGR1006）
    //   不含                  → \x1b[?1002l\x1b[?1006l（禁用）
    // 链路：目标 SetConsoleMode(ENABLE_MOUSE_INPUT) → DLL Hook 发 ModeChange
    //   → mediator 发 VT 序列 → WT 启用鼠标报告 → 鼠标事件转 SGR1006 发回 stdin
    //   → mediator ReadConsoleInputW 读到 MOUSE_EVENT_RECORD → InputRecordToVt
    //   → DLL VtToInputRecord → 目标 ReadConsoleInputW
    void OnModeChange(uint32_t inputMode, uint32_t outputMode);

    // === Phase 13：VT 模式切换通知 ===

    // 收到 DLL 的 ModeSwitchNotify 时，记录 VT 输入模式状态
    // DLL 在 SetConsoleMode 检测到 ENABLE_VIRTUAL_TERMINAL_INPUT 标志变化时触发
    void OnModeSwitchNotify(uint32_t vtInputMode, uint32_t vtOutputMode);

    // 向 WT stdout 写 VT 序列（发鼠标报告请求用）
    void WriteStdoutVt(const char* data, size_t len);

    // === Phase 11：DLL 远程卸载 ===

    // 收到 DLL 的 UnloadComplete 时调用
    // DLL 已完成 DoUnload（Hook 卸载、Logger 关闭、卸载线程 ExitThread），
    // 但 LoadCount 仍为 1（cmd 主线程 LdrpThreadBlob 持引用，DLL 内部无法释放）。
    // 本方法在目标进程中创建远程线程调用 FreeLibrary(dllBase)，把 LoadCount
    // 从 1 减到 0，触发 DLL_PROCESS_DETACH 完成真正卸载。
    // 远程线程从未进入过 injected.dll 代码，LDR 不会为其持有 ThreadBlob。
    void OnUnloadComplete();

    std::unique_ptr<ITransport> m_transport;
    uint32_t m_targetPid = 0;

    // Phase 11：injected.dll 在目标进程中的基址（Hello 上报）
    // OnUnloadComplete 据此远程调 FreeLibrary(m_dllBase)
    uint64_t m_dllBase = 0;

    // Phase 5：WT 尺寸监听器（监听 WT resize 通知 DLL）
    WtSizeWatcher m_sizeWatcher;

    // Phase 12：子进程会话列表（线程安全）
    // ChildSession 用 shared_ptr 管理（线程持有自身引用，m_onExit 可安全移除）
    std::mutex m_childMutex;
    std::vector<std::shared_ptr<ChildSession>> m_childSessions;

    // Phase 6+：上次收到的 inputMode，用于检测 ENABLE_MOUSE_INPUT 变化
    // 初始值 0 表示未收到过 ModeChange
    uint32_t m_lastInputMode = 0;
    bool m_mouseReportEnabled = false;  // WT 鼠标报告当前是否已启用

    // Phase 13：VT 输入模式状态（DLL 通过 ModeSwitchNotify 通知）
    // true=DLL 处于 VT 直通模式，false=行编辑模式
    std::atomic<bool> m_vtInputMode{false};

    // Phase 14：轻量 VT 解析器（识别 DSR CPR 等 WT 响应）
    VtParser m_vtParser;
};

} // namespace terminjector
