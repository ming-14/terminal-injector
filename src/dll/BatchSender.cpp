// BatchSender 实现：VtOutput 小包合并发送
// 详见 docs/phases/10-state-sync-stability.md 4.8
//
// 流程：
//   - Hook 调 EnqueueVtOutput 追加 VT 字节到 m_buffer，立即返回
//   - flush 线程每 16ms 或被事件唤醒，取出整个 m_buffer 封装为
//     单个 VtOutput 消息发送
//   - Shutdown 时强制最终 flush，确保数据不丢
//
// 线程模型：
//   - 多个 Hook 线程并发 EnqueueVtOutput（m_bufLock 保护追加）
//   - 单个 flush 线程消费 m_buffer（swap 取出，无锁发送）
//   - 发送时用 m_sendLock 串行，与控制消息的 Send 互不干扰
//     （NamedPipeTransport::Send 内部已有 m_sendLock 保证 WriteFile 原子性，
//      BatchSender 的 m_sendLock 是协议包级别串行，确保一个 VtOutput 包
//      不被另一个 VtOutput 包分割）
#include "BatchSender.h"
#include "LazyInit.h"
#include "state/VtReplayBuffer.h"
#include "state/PromptTracker.h"
#include "logging/Logger.h"
#include "transport/ITransport.h"
#include "protocol/MessageSerializer.h"
#include "protocol/Message.h"

#include <windows.h>
#include <cstring>

namespace terminjector {

namespace {
// BatchSender 发送锁：串行化协议包发送，避免合并的 VtOutput 包交错
// 与 HookCommon.cpp 的 g_sendLock 独立（控制消息走 g_sendLock，VtOutput 走此锁）
// NamedPipeTransport::Send 内部 m_sendLock 保证 WriteFile 原子性，
// 两层锁不冲突，协议包不会交错
SRWLOCK g_batchSendLock = SRWLOCK_INIT;
} // namespace

BatchSender& BatchSender::Instance() {
    static BatchSender instance;
    return instance;
}

void BatchSender::Init() {
    if (m_initialized.exchange(true)) return;  // 已初始化

    m_flushEvent = CreateEventW(nullptr, FALSE, FALSE, nullptr);  // auto-reset
    if (m_flushEvent == nullptr) {
        LOG_ERROR("BatchSender::Init: CreateEventW failed, err=%lu", GetLastError());
        m_initialized.store(false);
        return;
    }

    m_running.store(true);
    m_flushThread = std::thread(&BatchSender::FlushLoop, this);
    LOG_INFO("BatchSender initialized, flushInterval=%dms maxBytes=%zu",
             kFlushIntervalMs, kFlushMaxBytes);
}

void BatchSender::Shutdown() {
    if (!m_initialized.exchange(false)) return;  // 未初始化或已 shutdown

    m_running.store(false);
    if (m_flushEvent) SetEvent(m_flushEvent);  // 唤醒 flush 线程做最终 flush

    if (m_flushThread.joinable()) {
        m_flushThread.join();
    }

    if (m_flushEvent) {
        CloseHandle(m_flushEvent);
        m_flushEvent = nullptr;
    }

    // join 后再做一次最终 flush（防止 flush 线程退出后又有新数据入队）
    // 注意：此时 m_initialized=false，EnqueueVtOutput 会直接走 fallback Send
    FlushLocked();

    LOG_INFO("BatchSender shutdown");
}

bool BatchSender::EnqueueVtOutput(const void* data, size_t len, bool recordReplay) {
    if (data == nullptr || len == 0) return true;

    // Phase 22：会话 VT 重放缓冲 —— 记录所有发往 WT 的 VT 字节
    // 卸载时重放到 ConHost 恢复画面（详见 VtReplayBuffer.h）
    // 放在最前，保证正常攒批与 fallback 直发两条路径都被记录
    // 协议查询（recordReplay=false，如 LazyInit 的 DSR/DA 校准探针）只发不收，
    // 避免卸载重放时 ConHost 把查询当请求自答出字面 VT 文本（Phase 22 修复）
    if (recordReplay) {
        std::int64_t start = VtReplayBuffer::Instance().Append(data, len);
        // 记录"本次内容写入起点"（线程局部）：行编辑读入口处作为该读的
        // prompt 候选（写-读序列语义，见 PromptTracker.h）。
        // 卸载重放据此截断终点，让 shell 自己重绘 prompt。
        if (start >= 0) {
            PromptTracker::Instance().OnOutputWrite(start);
        }
    }

    // 未初始化时（LazyInit 前 / Shutdown 后）走 fallback：直接 Send
    // 保证功能正确性，只是失去合并优化
    if (!m_initialized.load() || !m_running.load()) {
        ITransport* transport = GetMediatorTransport();
        if (!transport || !transport->IsConnected()) return false;
        auto pkt = protocol::Serialize(protocol::MessageType::VtOutput,
                                       data, static_cast<uint32_t>(len));
        AcquireSRWLockExclusive(&g_batchSendLock);
        int sent = transport->Send(pkt.data(), pkt.size());
        ReleaseSRWLockExclusive(&g_batchSendLock);
        return sent == static_cast<int>(pkt.size());
    }

    // 检查 transport 连接状态（不入队的提前返回）
    ITransport* transport = GetMediatorTransport();
    if (!transport || !transport->IsConnected()) return false;

    bool needFlush = false;
    {
        AcquireSRWLockExclusive(&m_bufLock);
        m_buffer.append(static_cast<const char*>(data), len);
        needFlush = (m_buffer.size() >= kFlushMaxBytes);
        ReleaseSRWLockExclusive(&m_bufLock);
    }

    if (needFlush && m_flushEvent) {
        SetEvent(m_flushEvent);  // 唤醒 flush 线程立即发送
    }
    return true;
}

void BatchSender::FlushLoop() {
    LOG_INFO("BatchSender flush thread started");

    while (m_running.load()) {
        // 等待事件唤醒（缓冲区满）或 16ms 超时（周期性 flush）
        if (m_flushEvent) {
            WaitForSingleObject(m_flushEvent, kFlushIntervalMs);
        } else {
            Sleep(kFlushIntervalMs);
        }
        if (!m_running.load()) break;
        FlushLocked();
    }

    // 退出前最终 flush（确保 Shutdown 时缓冲区数据发出）
    FlushLocked();
    LOG_INFO("BatchSender flush thread exit");
}

void BatchSender::FlushLocked() {
    std::string pkt;
    {
        AcquireSRWLockExclusive(&m_bufLock);
        if (m_buffer.empty()) {
            ReleaseSRWLockExclusive(&m_bufLock);
            return;
        }
        pkt.swap(m_buffer);  // O(1) 取出整个缓冲区
        ReleaseSRWLockExclusive(&m_bufLock);
    }

    ITransport* transport = GetMediatorTransport();
    if (!transport || !transport->IsConnected()) {
        LOG_WARN("BatchSender::FlushLocked: transport disconnected, dropped %zu bytes",
                 pkt.size());
        return;
    }

    // 封装为 VtOutput 消息发送
    auto msg = protocol::Serialize(protocol::MessageType::VtOutput,
                                   pkt.data(), static_cast<uint32_t>(pkt.size()));

    AcquireSRWLockExclusive(&g_batchSendLock);
    int sent = transport->Send(msg.data(), msg.size());
    ReleaseSRWLockExclusive(&g_batchSendLock);

    if (sent != static_cast<int>(msg.size())) {
        LOG_WARN("BatchSender::FlushLocked: partial send %d/%zu", sent, msg.size());
    } else {
        LOG_INFO("BatchSender::FlushLocked: flushed %zu bytes (merged)", pkt.size());
    }
}

} // namespace terminjector
