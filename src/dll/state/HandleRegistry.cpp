// HandleRegistry 实现：假/受保护句柄注册表（Phase 9 自保护）
// 详见 docs/phases/09-self-protection.md 4.1 与 4.5
//
// 单例：Meyers's Singleton（C++11 线程安全初始化）
// 锁：SRWLOCK（共享读 IsFake/IsProtected，独占写 Register/Unregister）
//     与 ConsoleState/InputQueue 同策略，避免 CRT mutex 依赖
#include "HandleRegistry.h"
#include "logging/Logger.h"

namespace terminjector {

HandleRegistry& HandleRegistry::Instance() {
    static HandleRegistry inst;
    return inst;
}

void HandleRegistry::RegisterFake(HANDLE h) {
    if (h == nullptr || h == INVALID_HANDLE_VALUE) return;
    AcquireSRWLockExclusive(&m_lock);
    m_fakes.insert(h);
    ReleaseSRWLockExclusive(&m_lock);
    LOG_INFO("HandleRegistry: RegisterFake %p", h);
}

void HandleRegistry::UnregisterFake(HANDLE h) {
    AcquireSRWLockExclusive(&m_lock);
    m_fakes.erase(h);
    ReleaseSRWLockExclusive(&m_lock);
}

bool HandleRegistry::IsFake(HANDLE h) const {
    if (h == nullptr || h == INVALID_HANDLE_VALUE) return false;
    // 快路径：魔数判断 O(1)
    if (IsFakeHandleFast(h)) return true;
    // 慢路径：查真实 fake 集合（如 InputQueue 事件）
    AcquireSRWLockShared(&m_lock);
    bool found = m_fakes.find(h) != m_fakes.end();
    ReleaseSRWLockShared(&m_lock);
    return found;
}

void HandleRegistry::RegisterProtected(HANDLE h) {
    if (h == nullptr || h == INVALID_HANDLE_VALUE) return;
    AcquireSRWLockExclusive(&m_lock);
    m_protected.insert(h);
    ReleaseSRWLockExclusive(&m_lock);
    LOG_INFO("HandleRegistry: RegisterProtected %p", h);
}

void HandleRegistry::UnregisterProtected(HANDLE h) {
    AcquireSRWLockExclusive(&m_lock);
    m_protected.erase(h);
    ReleaseSRWLockExclusive(&m_lock);
}

bool HandleRegistry::IsProtected(HANDLE h) const {
    if (h == nullptr || h == INVALID_HANDLE_VALUE) return false;
    AcquireSRWLockShared(&m_lock);
    bool found = m_protected.find(h) != m_protected.end();
    ReleaseSRWLockShared(&m_lock);
    return found;
}

} // namespace terminjector
