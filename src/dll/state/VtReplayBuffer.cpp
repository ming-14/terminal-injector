// VtReplayBuffer 实现
// 详见 docs/phases/22-conhost-replay.md
#include "VtReplayBuffer.h"
#include "logging/Logger.h"

#include <cstring>

namespace terminjector {

VtReplayBuffer& VtReplayBuffer::Instance() {
    static VtReplayBuffer instance;
    return instance;
}

void VtReplayBuffer::Append(const void* data, size_t len) {
    if (data == nullptr || len == 0) return;
    if (m_truncated.load()) return;  // 已截断，不再追加（保持前缀语义）

    AcquireSRWLockExclusive(&m_lock);

    // 追加后超限：只保留超限前的部分，标记截断
    if (m_data.size() + len > kMaxBytes) {
        size_t room = kMaxBytes - m_data.size();
        if (room > 0) {
            m_data.append(static_cast<const char*>(data), room);
        }
        m_truncated.store(true);
        if (!m_warned) {
            m_warned = true;
            LOG_WARN("VtReplayBuffer: reached %zu bytes cap, "
                     "replay will end at truncation point", kMaxBytes);
        }
        ReleaseSRWLockExclusive(&m_lock);
        return;
    }

    m_data.append(static_cast<const char*>(data), len);
    ReleaseSRWLockExclusive(&m_lock);
}

bool VtReplayBuffer::IsTruncated() const {
    return m_truncated.load();
}

const std::string& VtReplayBuffer::Data() const {
    return m_data;
}

void VtReplayBuffer::Clear() {
    AcquireSRWLockExclusive(&m_lock);
    m_data.clear();
    m_truncated.store(false);
    m_warned = false;
    ReleaseSRWLockExclusive(&m_lock);
}

} // namespace terminjector
