// 假句柄注册表（Phase 9 自保护）
// 详见 docs/phases/09-self-protection.md 4.1 与 4.5
//
// 职责：
//   - 统一管理所有"假"句柄，供 CloseHandle Hook 查询是否静默返回
//   - 统一管理"受保护"句柄（如日志文件句柄），让相关 Hook 放行而非拦截
//
// 假句柄分类：
//   1. 魔数 fake（位 16-31 为 0xABCD）：BufferHooks 的 Alt Buffer sentinel 0xABCDE123
//      CloseHandle 快路径用 IsFakeHandleFast O(1) 判断，无需查表
//   2. 真实 fake：InputQueue 的事件句柄等真实内核句柄，需 RegisterFake 注册
//      CloseHandle 慢路径查 m_fakes 集合判断
//
// 性能：CloseHandle 是高频调用，IsFake 内部先 IsFakeHandleFast（O(1)），
//       未命中再查 std::set（n 通常 1-3，可忽略）
//
// 线程安全：SRWLOCK 保护（与其他 state 模块一致，避免 CRT 锁）
#pragma once

#include <windows.h>
#include <set>

namespace terminjector {

// 假句柄魔数：位 16-31 为 0xABCD（如 sentinel 0xABCDE123）
// 真实内核句柄高位不会是 0xABCD，魔数判断无冲突
constexpr uintptr_t kFakeHandleMagicMask = 0xFFFF0000ULL;
constexpr uintptr_t kFakeHandleMagicBits = 0xABCD0000ULL;

class HandleRegistry {
public:
    static HandleRegistry& Instance();

    // 注册/注销假句柄（魔数 fake 与 真实 fake 都需注册）
    // 魔数 fake 注册是为统一接口（实际 CloseHandle 走快路径不查表）
    void RegisterFake(HANDLE h);
    void UnregisterFake(HANDLE h);

    // 是否为假句柄：先魔数快判断 O(1)，未命中再查 m_fakes
    bool IsFake(HANDLE h) const;

    // 注册/注销/查询受保护句柄（如日志文件句柄）
    void RegisterProtected(HANDLE h);
    void UnregisterProtected(HANDLE h);
    bool IsProtected(HANDLE h) const;

private:
    HandleRegistry() = default;

    mutable SRWLOCK m_lock = SRWLOCK_INIT;
    std::set<HANDLE> m_fakes;      // 真实假句柄集合（魔数 fake 不必加入，但加入也无害）
    std::set<HANDLE> m_protected;  // 受保护句柄集合
};

// 魔数快速判断：O(1)，无锁
// 用于 CloseHandle 快路径：魔数 fake 直接静默返回，无需查 HandleRegistry
inline bool IsFakeHandleFast(HANDLE h) {
    if (h == nullptr || h == INVALID_HANDLE_VALUE) return false;
    uintptr_t v = reinterpret_cast<uintptr_t>(h);
    return (v & kFakeHandleMagicMask) == kFakeHandleMagicBits;
}

} // namespace terminjector
