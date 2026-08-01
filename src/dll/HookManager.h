// Hook 管理器：MinHook 生命周期统一管理
// 详见 docs/phases/03-dll-framework.md 4.2
//
// 设计要点：
//   - 进程级单例（静态成员），所有 Hook 集中注册/启用/卸载
//   - Register 仅登记，InstallAll 才真正 CreateHook + EnableHook
//   - InstallAll 失败时回滚已创建的 Hook，避免半安装状态
//   - 一次性 MH_EnableHook(MH_ALL_HOOKS) 启用，减少窗口期
//
// 使用流程（懒加载触发）：
//   1. 各 Hook 模块调用 HookManager::RegisterBatch 登记自身
//   2. LazyInit 完成握手后调用 HookManager::InstallAll
//   3. DLL_PROCESS_DETACH 调用 HookManager::UninstallAll
#pragma once

#include <windows.h>
#include <vector>

namespace terminjector {

// 单个 Hook 的注册信息
struct HookEntry {
    const char* name;     // 用于日志的可读名（静态字符串）
    void*       target;   // 被 Hook 的 API 地址（GetProcAddress 获取）
    void*       detour;   // 我们的替代函数指针
    void**      original; // 接收原函数指针的指针（供 Detour 内调用原 API）
};

// Hook 生命周期管理（进程级单例，静态成员）
class HookManager {
public:
    // 注册一个 Hook（不立即启用）
    static void Register(const HookEntry& entry);

    // 批量注册（每个 Hook 模块调用一次）
    static void RegisterBatch(const std::vector<HookEntry>& entries);

    // 安装全部已注册的 Hook（MH_CreateHook + MH_EnableHook）
    // 失败则回滚已创建的，返回 false
    static bool InstallAll();

    // 卸载全部 Hook（MH_DisableHook + MH_RemoveHook）
    static void UninstallAll();

    // 仅禁用 Hook（保留 trampoline），供 Unloader::DoUnload 使用
    // 避免 ReadDetour 线程仍在执行 trampoline 时 MH_RemoveHook 释放内存导致 AV
    static void DisableAll();

    // 状态查询
    static bool IsInstalled() { return s_installed; }
    static size_t RegisteredCount() { return s_entries.size(); }

private:
    static std::vector<HookEntry> s_entries;  // 已注册的 Hook 列表
    static bool s_installed;                  // 是否已 InstallAll
};

} // namespace terminjector
