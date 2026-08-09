// PromptTracker 实现
// 详见 docs/phases/22-conhost-replay.md
#include "PromptTracker.h"

namespace terminjector {

thread_local std::int64_t PromptTracker::tls_lastWriteOffset = -1;

PromptTracker& PromptTracker::Instance() {
    static PromptTracker instance;
    return instance;
}

void PromptTracker::OnOutputWrite(std::int64_t replayOffset) {
    tls_lastWriteOffset = replayOffset;
}

void PromptTracker::OnLineReadBegin() {
    m_lastPromptOffset.store(tls_lastWriteOffset, std::memory_order_relaxed);
    m_lineReadCount.fetch_add(1, std::memory_order_acq_rel);
}

void PromptTracker::OnLineReadEnd() {
    m_lineReadCount.fetch_sub(1, std::memory_order_acq_rel);
}

std::optional<std::size_t> PromptTracker::TruncateOffset() const {
    if (m_lineReadCount.load(std::memory_order_acquire) <= 0) {
        return std::nullopt;
    }
    std::int64_t off = m_lastPromptOffset.load(std::memory_order_relaxed);
    if (off < 0) return std::nullopt;
    return static_cast<std::size_t>(off);
}

PromptTracker::LineReadScope::LineReadScope(bool echoEnabled)
    : m_active(echoEnabled) {
    if (m_active) {
        PromptTracker::Instance().OnLineReadBegin();
    }
}

PromptTracker::LineReadScope::~LineReadScope() {
    if (m_active) {
        PromptTracker::Instance().OnLineReadEnd();
    }
}

} // namespace terminjector
