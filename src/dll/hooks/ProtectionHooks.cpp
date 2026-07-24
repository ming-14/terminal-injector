// 自保护类 Console API Hook 实现（Phase 9）
// 详见 docs/phases/09-self-protection.md 4.4 与 4.5
//
// 防越狱策略：
//   - AllocConsole：拒绝（ERROR_NOT_ENOUGH_MEMORY），防止目标程序分配新控制台
//   - AttachConsole：拒绝（ERROR_ACCESS_DENIED），防止附加到其他进程 Console
//   - FreeConsole：返回 TRUE 但不真断，保持 ConHost 控制台存在
//     （ConHost 仍负责 GenerateConsoleCtrlEvent 等控制信号传递）
//   - CloseHandle：假句柄静默 TRUE，其他调真实 API
//
// CloseHandle 性能注意：
//   高频调用（每次 CreateFile/CloseHandle 配对），Detour 必须极快
//   快路径：IsFakeHandleFast O(1) 位比较，命中即返回
//   慢路径：HandleRegistry::IsFake 查表（n 通常 1-3）
//   非假句柄直接调 orig，性能与无 Hook 接近
//
// 懒加载安全：
//   - Logger::Initialize 内调 CloseHandle 关旧日志句柄，
//     此时 HandleRegistry 未就绪，需 IsInLazyInit 跳过拦截走真实 API
//
// 不调用 ENSURE_INITIALIZED 的原因：
//   ProtectionHooks 的职责（防越狱 + 假句柄静默）不依赖 Logger/mediator/
//   ConsoleState。HandleRegistry 是 Meyers's Singleton（C++11 线程安全初始化），
//   IsFake/IsProtected 用 SRWLOCK 自保护，可在 DllMain 期间安全调用。
//   若 Detour 内调 ENSURE_INITIALIZED，DllMain 期间被触发时会启动 Logger
//   worker 线程（std::thread），worker 需要 Loader Lock 初始化 CRT，而
//   Loader Lock 被 DllMain 持有 → worker 卡在 LdrInitializeThunk，
//   后续 LOG 调 OutputDebugStringW 在 Loader Lock 下死锁。
//   懒加载由 DllMain 末尾的 worker 线程主动触发，无需 ProtectionHooks 触发。
#include "ProtectionHooks.h"
#include "HookCommon.h"
#include "../HookManager.h"
#include "../LazyInit.h"
#include "../state/HandleRegistry.h"
#include "logging/Logger.h"

#include <windows.h>
#include <vector>

namespace terminjector::hooks {

// ============================================================
// 原函数指针定义
// ============================================================
DEFINE_ORIG_PTR(AllocConsole, BOOL WINAPI());
DEFINE_ORIG_PTR(AttachConsole, BOOL WINAPI(DWORD));
DEFINE_ORIG_PTR(FreeConsole, BOOL WINAPI());
DEFINE_ORIG_PTR(CloseHandle, BOOL WINAPI(HANDLE));

// ============================================================
// AllocConsole Hook
// ============================================================
// 拒绝：假装分配失败，阻止目标程序脱离中介管道分配新控制台
// 程序典型用法：FreeConsole() → AllocConsole() 弹新黑框
// FreeConsole 已被拦（返回 TRUE 不真断），AllocConsole 再被拦，
// 程序进入错误处理路径（不会得到新 Console）
BOOL WINAPI AllocConsole_Detour() {
    if (IsInLazyInit()) {
        return AllocConsole_orig();
    }

    SetLastError(ERROR_NOT_ENOUGH_MEMORY);
    LOG_WARN("ProtectionHooks: AllocConsole blocked");
    return FALSE;
}

// ============================================================
// AttachConsole Hook
// ============================================================
// 拒绝：不允许附加到其他进程的 Console（防止劫持其他进程控制台）
// 程序典型用法：AttachConsole(ATTACH_PARENT_PROCESS) 附加父进程 Console
BOOL WINAPI AttachConsole_Detour(DWORD pid) {
    if (IsInLazyInit()) {
        return AttachConsole_orig(pid);
    }

    SetLastError(ERROR_ACCESS_DENIED);
    LOG_WARN("ProtectionHooks: AttachConsole(%u) blocked", pid);
    return FALSE;
}

// ============================================================
// FreeConsole Hook
// ============================================================
// 假装成功但不真断：保持 ConHost 控制台存在
//
// 关键：不能让程序真的 Free，否则后续 Console API 失效
//   - GenerateConsoleCtrlEvent 需要 ConHost 控制台存在
//   - GetConsoleWindow 等返回真实 HWND
//   - 程序内部 Console 句柄仍有效（ConHost 仍存活）
//
// 程序后续调 AllocConsole（被拦返回 FALSE），可能进入错误处理路径
// 此风险已在文档 4.4 评估，常见程序行为可接受
//
// 注意：不调 ENSURE_INITIALIZED（详见文件头注释）
BOOL WINAPI FreeConsole_Detour() {
    if (IsInLazyInit()) {
        return FreeConsole_orig();
    }

    LOG_WARN("ProtectionHooks: FreeConsole blocked (returning TRUE)");
    return TRUE;  // 不调原 API
}

// ============================================================
// CloseHandle Hook
// ============================================================
// 假句柄：静默返回 TRUE，不真关
//   - 魔数 fake（Alt Buffer sentinel 0xABCDE123）：IsFakeHandleFast O(1) 命中
//   - 真实 fake（InputQueue 事件）：HandleRegistry::IsFake 查表命中
// 其他句柄：调真实 CloseHandle_orig
//
// 受保护句柄（日志文件）：理论上程序不该关，若尝试关则放行让其成功
//   （避免程序行为异常，日志写入后续失败可接受）
// 注意：不调 ENSURE_INITIALIZED（详见文件头注释）
// 不调 IsInLazyInit()：thread_local 变量访问在 DllMain 期间可能卡在
//   __tls_get_addr（DLL TLS 初始化未完成）
//
// 判断顺序（关键）：
//   1. IsFakeHandleFast（魔数判断，纯位运算无依赖）必须最先
//      原因：LazyInit 在 worker 线程异步执行（DllMain 返回后 Sleep 100ms 才
//      开始），目标程序主线程可能在 LazyInit 完成前就调用 CloseHandle。
//      若魔数判断放在 IsLazyInitialized() 之后，LazyInit 完成前的假句柄
//      关闭会走真实 API，测试 CloseHandle(0xABCDE123) 在 LazyInit 完成
//      前调用会失败（err=6 ERROR_INVALID_HANDLE）。
//      IsFakeHandleFast 是 inline 纯位运算，不访问 HandleRegistry 实例，
//      DllMain 期间也安全。
//   2. IsLazyInitialized() 检查（保护 HandleRegistry::Instance()）
//      原因：HandleRegistry::Instance() 是 Meyers's Singleton，DllMain 期间
//      首次调用触发静态局部变量初始化（std::set 构造），与 Loader Lock
//      冲突死锁。IsLazyInitialized() 在 LazyInit 完成后才返回 true，
//      确保 HandleRegistry 调用时 Loader Lock 已释放。
//   3. HandleRegistry::IsFake（查真实 fake 集合）
//      真实 fake（InputQueue 事件句柄）由 BufferHooks/InputQueue 在 LazyInit
//      后注册，LazyInit 完成前不会有真实 fake 需要查询。
BOOL WINAPI CloseHandle_Detour(HANDLE h) {
    // 1. 魔数假句柄快路径：纯位运算，无依赖，任何时候安全
    //    必须在 IsLazyInitialized() 之前，覆盖 LazyInit 完成前的假句柄关闭
    if (IsFakeHandleFast(h)) {
        LOG_INFO("ProtectionHooks: CloseHandle(magic fake %p) silently ignored", h);
        return TRUE;
    }

    // 2. 懒加载未完成时直接走真实 API
    //    此时不会有真实 fake（InputQueue 事件等）需要拦截
    //    DllMain 期间的 CloseHandle（如 worker 线程句柄）走此路径，安全
    if (!IsLazyInitialized()) {
        return CloseHandle_orig(h);
    }

    // 3. 真实假句柄：查 HandleRegistry（LazyInit 已完成，安全调用 Instance）
    if (HandleRegistry::Instance().IsFake(h)) {
        LOG_INFO("ProtectionHooks: CloseHandle(reg fake %p) silently ignored", h);
        return TRUE;
    }

    // 4. 其他句柄（含受保护句柄）：调真实 CloseHandle
    LOG_DEBUG("ProtectionHooks: CloseHandle(real %p) forwarding", h);
    return CloseHandle_orig(h);
}

// ============================================================
// 注册自保护类 Hook
// ============================================================
// 只 Hook 一个版本：优先 kernelbase，回退 kernel32
//
// 原因：kernel32!CloseHandle 等是 import thunk（jmp qword ptr [rip+disp32]），
//   import 表项指向 kernelbase!CloseHandle（经 Python 验证）。
//   只 Hook kernelbase!CloseHandle 即可拦截所有调用路径：
//     调用 kernel32!CloseHandle → jmp [import] → kernelbase!CloseHandle 入口
//     → 已被 Hook（jmp detour）→ detour 拦截
//
// 历史教训（切勿同时 Hook kernel32 和 kernelbase 同名 API）：
//   曾经同时 Hook kernel32!CloseHandle 和 kernelbase!CloseHandle，第二次
//   CreateHook 把 CloseHandle_orig 覆盖为 kernel32 trampoline。detour 调
//   CloseHandle_orig → kernel32 trampoline → 执行 import thunk → kernelbase!
//   CloseHandle（被 Hook）→ 跳回 detour → 死循环，DllMain 期间 CloseHandle
//   永不返回，LoadLibrary 超时，握手失败。
//
//   另：MinHook 对 rip-relative 指令（FF 25 disp32）的 trampoline 重定位
//   可能不可靠，Hook import thunk 有风险。直接 Hook 真实实现（kernelbase）
//   更稳妥。
void RegisterProtectionHooks() {
    HMODULE hKBase = GetModuleHandleW(L"kernelbase.dll");
    HMODULE hK32   = GetModuleHandleW(L"kernel32.dll");

    // 解析 API 地址：优先 kernelbase，回退 kernel32
    auto resolve = [hKBase, hK32](const char* name) -> void* {
        if (hKBase != nullptr) {
            void* p = GetProcAddress(hKBase, name);
            if (p != nullptr) return p;
        }
        if (hK32 != nullptr) {
            return GetProcAddress(hK32, name);
        }
        return nullptr;
    };

    struct Entry {
        const char* name;
        void* target;
        void* detour;
        void** orig;
    };

    auto make = [&](const char* name, void* detour, void** orig) -> Entry {
        return Entry{name, resolve(name), detour, orig};
    };

    std::vector<Entry> entries;
    entries.push_back(make("AllocConsole",
        reinterpret_cast<void*>(&AllocConsole_Detour),
        reinterpret_cast<void**>(&AllocConsole_orig)));
    entries.push_back(make("AttachConsole",
        reinterpret_cast<void*>(&AttachConsole_Detour),
        reinterpret_cast<void**>(&AttachConsole_orig)));
    entries.push_back(make("FreeConsole",
        reinterpret_cast<void*>(&FreeConsole_Detour),
        reinterpret_cast<void**>(&FreeConsole_orig)));
    entries.push_back(make("CloseHandle",
        reinterpret_cast<void*>(&CloseHandle_Detour),
        reinterpret_cast<void**>(&CloseHandle_orig)));

    // 收集 HookEntry：每个 API 仅 Hook 一个 target（kernelbase 优先）
    std::vector<HookEntry> hooks;
    for (const auto& e : entries) {
        if (e.target == nullptr) {
            LOG_ERROR("RegisterProtectionHooks: failed to resolve %s", e.name);
            return;
        }
        hooks.push_back({e.name, e.target, e.detour, e.orig});
    }

    HookManager::RegisterBatch(hooks);
    LOG_INFO("ProtectionHooks registered (%zu hooks)", hooks.size());
}

} // namespace terminjector::hooks
