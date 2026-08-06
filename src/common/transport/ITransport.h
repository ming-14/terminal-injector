// IPC 传输抽象接口
// 详见 docs/phases/01-scaffold.md 4.4.1
//
// 实现层级：
//   - NamedPipeTransport（Phase 1，默认实现）
//   - SharedMemoryTransport（Phase 10 扩展，性能更高）
//
// 使用场景：
//   - 中介程序作为 Server：CreateNamedPipe 等待 DLL 连接
//   - DLL 作为 Client：CreateFile 连接中介创建的管道
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

namespace terminjector {

class ITransport {
public:
    virtual ~ITransport() = default;

    // 连接到对端
    //   - Server 端：创建管道并等待客户端连接（阻塞）
    //   - Client 端：连接到已存在的服务端管道
    // 返回 true 成功
    virtual bool Connect() = 0;

    // 断开连接
    virtual void Disconnect() = 0;

    // 是否处于连接状态
    virtual bool IsConnected() const = 0;

    // 发送字节流（阻塞直到全部发出或失败）
    // 返回实际发送字节数；<0 表示错误
    virtual int Send(const void* data, size_t len) = 0;

    // 接收字节流（阻塞直到读到 len 字节或失败）
    // 返回实际接收字节数；0 表示对端关闭；<0 表示错误
    virtual int Recv(void* buf, size_t len) = 0;

    // 非阻塞 peek（可选实现，默认返回 -1 表示不支持）
    virtual int Peek(void* buf, size_t len) { (void)buf; (void)len; return -1; }
};

} // namespace terminjector
