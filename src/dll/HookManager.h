// Hook 管理器：MinHook 生命周期统一管理
// 详见 docs/phases/03-dll-framework.md 4.2
//
// 设计要点：
//   - 进程级单例（静态成员），所有 Hook 集中注册/启用/卸载
//   - Register 仅登记，InstallAll 才真正 CreateHook + EnableHook
//   - InstallAll 失败时回滚已创建的 Hook，避免半安装状态
//   - 一次性 MH_EnableHook(MH_ALL_HOOKS) 启用，减少窗口期
//
// 使用流程：
//   1. 各 Hook 模块调用 HookManager::RegisterBatch 登记自身
//   2. DllMain(ATTACH) 中 Register 全部模块后调用 InstallAll（一次性启用）
//   3. LazyInit 由首个被 Hook 的 API（或 DllMain worker 线程）触发
//   4. DLL_PROCESS_DETACH 调用 HookManager::UninstallAll
//
// 自触发陷阱（关键决策点）：
//   InstallAll 的 MH_EnableHook 启用 Hook 的过程中，MinHook 内部若调用被
//   Hook 的 API（实测 WaitForSingleObjectEx），会立即进入 Detour → 同步
//   EnsureLazyInitialized → ConnectToMediator 轮询最长 5s，把 LoadLibraryW
//   线程（注入器侧）堵死 6s+。实测命中点不止 EnableHook：DllMain 调用
//   CreateThread(worker) 的线程创建过程（TLS 初始化）也会调用被 Hook 的
//   WaitForSingleObjectEx。因此标志由 DllMain(ATTACH) 全程管理：ATTACH
//   开头置位，CreateThread(worker) 之后清除；期间任何 Detour 命中都跳过
//   懒加载初始化，改由 worker 线程（Sleep 100ms）异步完成。
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

    // InstallAll 是否进行中（DllMain 安装钩子期间，懒加载初始化须跳过）
    static bool IsInstalling() { return s_installing != 0; }

    // DllMain(ATTACH) 全程置位/清除：ATTACH 开头置 true，CreateThread(worker)
    // 之后置 false。期间任何被 Hook 的 API 命中 Detour 都跳过懒加载初始化
    //（详见文件头"自触发陷阱"），由 worker 线程异步完成
    static void SetInstalling(bool v) { InterlockedExchange(&s_installing, v ? 1 : 0); }

private:
    static std::vector<HookEntry> s_entries;  // 已注册的 Hook 列表
    static bool s_installed;                  // 是否已 InstallAll
    static volatile LONG s_installing;        // 1=InstallAll 进行中
};

} // namespace terminjector
