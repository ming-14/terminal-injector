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
volatile LONG HookManager::s_installing = 0;

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

    // 注意：不在此处管理 s_installing。InstallAll 只是 DllMain(ATTACH) 的
    // 一个步骤，CreateThread(worker) 的线程创建过程同样会命中被 Hook 的
    // API，因此标志由 DllMain(ATTACH) 全程置位/清除（见 HookManager.h）

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

void HookManager::DisableAll() {
    if (!s_installed) {
        LOG_WARN("DisableAll: hooks not installed, skip");
        return;
    }
    MH_DisableHook(MH_ALL_HOOKS);
    // 不清除 s_entries，保留 trampoline 供 residual ReadDetour 线程安全退出
    // 不设 s_installed=false，DLL_PROCESS_DETACH 中 UninstallAll() 会做最终清理
    LOG_INFO("All hooks disabled (trampolines preserved)");
}

} // namespace terminjector
