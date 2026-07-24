// HookCommon 实现：SendToMediator
// 详见 docs/phases/03-dll-framework.md 4.5.4
//
// SendToMediator 把 payload 封装为指定类型消息发送
// 多线程并发调用 WriteConsoleW 时，用 SRWLOCK 保证数据包不交错
// Phase 12 扩展：支持发送非 VtOutput 消息（如 ChildProcessNotify）
#include "HookCommon.h"
#include "logging/Logger.h"
#include "transport/ITransport.h"
#include "protocol/MessageSerializer.h"
#include "protocol/Message.h"

#include <cstdio>

namespace terminjector::hooks {

namespace {
// 保护 SendToMediator 的串行锁（多线程 WriteConsole 并发）
SRWLOCK g_sendLock = SRWLOCK_INIT;
}

bool SendToMediator(const void* data, size_t len, protocol::MessageType type) {
    if (data == nullptr || len == 0) return true;

    // 获取懒加载建立的传输通道
    ITransport* transport = GetMediatorTransport();
    bool connected = (transport != nullptr && transport->IsConnected());

    // Phase 5 诊断日志：确认 SendToMediator 入口与 transport 状态
    // 此处 LOG_INFO 写日志文件走 WriteFile_Detour，但日志文件非 console
    // 句柄，IsConsoleHandle 返回 false 直接 pass-through，无递归风险
    LOG_INFO("SendToMediator: len=%zu type=%u transport=%p connected=%d",
             len, static_cast<uint32_t>(type), (void*)transport, connected ? 1 : 0);

    // 临时诊断：VtOutput 消息 hex dump（排查 Python 双 >>> 的 VT 内容，验证后移除）
    // 仅对 VtOutput(type=16) 记录，前 32 字节，避免日志爆炸
    // VT 序列含 ESC(0x1b)，hex 表示最清晰，便于确认光标定位/SGR 等序列实际内容
    if (type == protocol::MessageType::VtOutput) {
        const uint8_t* p = static_cast<const uint8_t*>(data);
        size_t dumpLen = len < 32 ? len : 32;
        char hex[32 * 3 + 1];
        for (size_t i = 0; i < dumpLen; ++i) {
            std::snprintf(hex + i * 3, 4, "%02X ", p[i]);
        }
        hex[dumpLen * 3] = '\0';
        LOG_INFO("SendToMediator VT hex(%zu/%zu): %s", dumpLen, len, hex);
    }

    if (!connected) {
        // mediator 未连接，Hook 走 pass-through（调用方会调原 API）
        return false;
    }

    // 封装为指定类型消息（len 截断为 uint32_t，单包不会超 4GB）
    auto pkt = protocol::Serialize(type, data, static_cast<uint32_t>(len));

    // 串行发送，避免多线程数据包交错
    AcquireSRWLockExclusive(&g_sendLock);
    const int sent = transport->Send(pkt.data(), pkt.size());
    ReleaseSRWLockExclusive(&g_sendLock);

    // Phase 5 诊断日志：Send 返回值
    LOG_INFO("SendToMediator: sent=%d/%zu", sent, pkt.size());

    if (sent != static_cast<int>(pkt.size())) {
        LOG_WARN("SendToMediator: partial send %d/%zu", sent, pkt.size());
        return false;
    }
    return true;
}

} // namespace terminjector::hooks
