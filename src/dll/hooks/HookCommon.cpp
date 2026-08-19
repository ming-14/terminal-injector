// HookCommon 实现：SendToMediator
// 详见 docs/phases/03-dll-framework.md 4.5.4
//
// SendToMediator 把 payload 封装为指定类型消息发送
// 多线程并发调用 WriteConsoleW 时，用 SRWLOCK 保证数据包不交错
// Phase 12 扩展：支持发送非 VtOutput 消息（如 ChildProcessNotify）
// Phase 10 任务5：VtOutput 走 BatchSender 攒批合并，减少高频小包的 IPC 次数
//                 控制消息仍走本函数直接发送，保证即时性
#include "HookCommon.h"
#include "../BatchSender.h"
#include "logging/Logger.h"
#include "transport/ITransport.h"
#include "protocol/MessageSerializer.h"
#include "protocol/Message.h"

#include <cstdio>

namespace terminjector::hooks {

namespace {
// 保护 SendToMediator 的串行锁（多线程 WriteConsole 并发）
// 仅用于控制消息（ChildProcessNotify/ModeChange/CpChange 等）
// VtOutput 走 BatchSender 内部的 g_batchSendLock
SRWLOCK g_sendLock = SRWLOCK_INIT;
}

bool SendToMediator(const void* data, size_t len, protocol::MessageType type,
                    bool recordReplay) {
    if (data == nullptr || len == 0) return true;

    // Phase 10 任务5：VtOutput 走 BatchSender 攒批合并路径
    // EnqueueVtOutput 内部检查 transport 连接状态，未连接返回 false
    // BatchSender 未初始化时（LazyInit 前）走 fallback 直接 Send
    if (type == protocol::MessageType::VtOutput) {
        return BatchSender::Instance().EnqueueVtOutput(data, len, recordReplay);
    }

    // 控制消息走原路径（即时发送，不攒批）
    ITransport* transport = GetMediatorTransport();
    bool connected = (transport != nullptr && transport->IsConnected());

    LOG_INFO("SendToMediator: len=%zu type=%u transport=%p connected=%d",
             len, static_cast<uint32_t>(type), (void*)transport, connected ? 1 : 0);

    if (!connected) {
        return false;
    }

    auto pkt = protocol::Serialize(type, data, static_cast<uint32_t>(len));

    AcquireSRWLockExclusive(&g_sendLock);
    const int sent = transport->Send(pkt.data(), pkt.size());
    ReleaseSRWLockExclusive(&g_sendLock);

    LOG_INFO("SendToMediator: sent=%d/%zu", sent, pkt.size());

    if (sent != static_cast<int>(pkt.size())) {
        LOG_WARN("SendToMediator: partial send %d/%zu", sent, pkt.size());
        return false;
    }
    return true;
}

} // namespace terminjector::hooks
