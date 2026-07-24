// HookManager 实现：MinHook 生命周期管理
// 详见 docs/phases/03-dll-framework.md 4.2.2
//
// 关键点：
//   - InstallAll 先全部 CreateHook，再一次性 EnableHook(MH_ALL)
//   - CreateHook 阶段失败则回滚已创建的，保持"全有或全无"
//   - EnableHook 阶段失败则全部 RemoveHook
//   - UninstallAll 幂等，未安装时直接返回
#include "HookManager.h"
#include "logging/Logger.h"
#include <MinHook.h>

namespace terminjector {

// 静态成员定义
std::vector<HookEntry> HookManager::s_entries;
bool HookManager::s_installed = false;

void HookManager::Register(const HookEntry& entry) {
    s_entries.push_back(entry);
    LOG_DEBUG("Hook registered: %s target=%p detour=%p",
              entry.name, entry.target, entry.detour);
}

void HookManager::RegisterBatch(const std::vector<HookEntry>& entries) {
    for (const auto& e : entries) Register(e);
}

bool HookManager::InstallAll() {
    if (s_installed) {
        LOG_WARN("InstallAll called twice, skip");
        return true;
    }
    if (s_entries.empty()) {
        LOG_WARN("InstallAll: no hooks registered");
        return true;
    }

    // 1. 逐个 CreateHook，失败则回滚已创建的
    std::vector<void*> created;  // 已成功 CreateHook 的 target，用于回滚
    for (auto& e : s_entries) {
        MH_STATUS st = MH_CreateHook(e.target, e.detour, e.original);
        if (st != MH_OK) {
            LOG_ERROR("MH_CreateHook(%s) failed: %d", e.name, st);
            for (void* t : created) MH_RemoveHook(t);
            return false;
        }
        created.push_back(e.target);
    }
    LOG_INFO("Created %zu hooks", s_entries.size());

    // 2. 一次性 EnableHook(MH_ALL_HOOKS)，减少窗口期
    MH_STATUS st = MH_EnableHook(MH_ALL_HOOKS);
    if (st != MH_OK) {
        LOG_ERROR("MH_EnableHook(MH_ALL) failed: %d", st);
        for (auto& e : s_entries) MH_RemoveHook(e.target);
        return false;
    }

    s_installed = true;
    LOG_INFO("All hooks enabled (%zu)", s_entries.size());
    return true;
}

void HookManager::UninstallAll() {
    if (!s_installed) return;
    MH_DisableHook(MH_ALL_HOOKS);
    for (auto& e : s_entries) MH_RemoveHook(e.target);
    s_entries.clear();
    s_installed = false;
    LOG_INFO("All hooks uninstalled");
}

} // namespace terminjector
