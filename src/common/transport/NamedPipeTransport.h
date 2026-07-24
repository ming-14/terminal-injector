// 命名管道传输实现
// 详见 docs/phases/01-scaffold.md 4.4.2 / 4.4.3 与 02-injector-modes.md 4.5.3
//
// 角色约定：
//   - Server（中介程序）：CreateNamedPipeW 创建管道 -> ConnectNamedPipe 等待 DLL
//   - Client（DLL 侧）  ：CreateFileW 打开 \\.\pipe\terminjector_<pid>
//
// 模式：PIPE_TYPE_BYTE | PIPE_READMODE_BYTE（字节流，由上层协议分帧）
// 缓冲：输入/输出各 64KB（鼠标攒批可能产生较大包）
//
// Server 端时序（Phase 2 调整）：
//   mediator 必须先 Create() 建立管道，再 SpawnInjector，
//   最后 WaitClient() 阻塞等待 DLL 连接，避免竞态：
//     1. Create()        — CreateNamedPipeW（不阻塞）
//     2. SpawnInjector   — fork 注入子进程
//     3. WaitClient()    — ConnectNamedPipe（阻塞等 DLL）
#pragma once

#include "ITransport.h"

#include <windows.h>
#include <string>

namespace terminjector {

class NamedPipeTransport : public ITransport {
public:
    // 角色枚举：Server=中介，Client=DLL
    enum class Role { Server, Client };

    // pipeName 形如 \\.\pipe\terminjector_<pid>
    // role 决定 Connect 行为
    NamedPipeTransport(std::wstring pipeName, Role role);
    ~NamedPipeTransport() override;

    NamedPipeTransport(const NamedPipeTransport&) = delete;
    NamedPipeTransport& operator=(const NamedPipeTransport&) = delete;

    // === Server 端专用：拆分为两步，避免与 injector 竞态 ===

    // 创建命名管道实例（不阻塞）
    // 仅 Server 角色有效；返回 true 表示管道已创建，等待 WaitClient
    bool Create();

    // 阻塞等待客户端连接（ConnectNamedPipe）
    // 仅 Server 角色有效；Create() 必须先调用
    // 返回 true 表示客户端已连上
    bool WaitClient();

    // === ITransport 实现 ===

    // Client 端：连接服务端（重试 5s）
    // Server 端：等价于 Create() + WaitClient()（向后兼容，但不推荐用于 mediator）
    bool Connect() override;
    void Disconnect() override;
    bool IsConnected() const override;
    int  Send(const void* data, size_t len) override;
    int  Recv(void* buf, size_t len) override;
    int  Peek(void* buf, size_t len) override;

private:
    // 关闭当前句柄并置为 INVALID_HANDLE_VALUE
    void Close();

    std::wstring m_pipeName;
    Role         m_role;
    HANDLE       m_pipeHandle = INVALID_HANDLE_VALUE;
    bool         m_created = false;  // Server 端：Create() 已调用

    // Phase 5：Send 串行锁
    // 多线程并发 Send（stdin 线程 + WtSizeWatcher 线程）会导致数据包交错，
    // 用 SRWLOCK 保证单次 Send 原子完成
    mutable SRWLOCK m_sendLock = SRWLOCK_INIT;
};

} // namespace terminjector
