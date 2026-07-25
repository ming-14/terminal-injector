// Wait 句柄类 Console API Hook 实现（Phase 8）
// 详见 docs/phases/08-advanced-features.md 4.5
//
// 核心问题：
//   目标程序典型模式：GetConsoleInputWaitHandle → WaitForSingleObject → ReadConsoleInput
//   Hook 后输入来自中介管道，ConHost 的内核事件永远不会被置位，
//   程序会永远卡在 WaitForSingleObject。
//
// 解决方案：
//   Hook GetConsoleInputWaitHandle，返回 InputQueue 的手动重置事件句柄。
//   InputQueue 在 Enqueue 时 SetEvent，Dequeue 空时 ResetEvent，
//   程序等待此事件即可正确感知输入到达。
//
// GetConsoleInputWaitHandle 没有标准头文件声明，需 GetProcAddress 动态获取。
// 部分 Windows 版本可能不导出此函数，GetProcAddress 返回 nullptr 时跳过 Hook。
#include "WaitHooks.h"
#include "HookCommon.h"
#include "HookWhitelist.h"
#include "../HookManager.h"
#include "../state/InputQueue.h"
#include "../state/HandleRegistry.h"
#include "logging/Logger.h"

#include <windows.h>
#include <vector>

namespace terminjector::hooks {

// ============================================================
// 原函数指针定义
// ============================================================
// GetConsoleInputWaitHandle 无标准头文件声明，用 DEFINE_ORIG_PTR 定义函数指针类型
DEFINE_ORIG_PTR(GetConsoleInputWaitHandle, HANDLE WINAPI());

// ============================================================
// GetConsoleInputWaitHandle Hook
// ============================================================
// 返回 InputQueue 的手动重置事件句柄
//
// 程序典型用法：
//   HANDLE h = GetConsoleInputWaitHandle();
//   WaitForSingleObject(h, INFINITE);  // 等待输入到达
//   ReadConsoleInput(...);             // 读取输入
//
// Hook 后：
//   - GetConsoleInputWaitHandle 返回 InputQueue 事件
//   - WaitForSingleObject 等待此事件（真实内核事件，正常等待）
//   - DllRecvLoop 收到 VtInput → Enqueue → SetEvent → 唤醒等待
//   - ReadConsoleInput → Dequeue → 队列空时 ResetEvent
//
// 不调原 API：原 API 返回 ConHost 内核事件，永远不会被置位（输入走管道）
HANDLE WINAPI GetConsoleInputWaitHandle_Detour() {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (IsInLazyInit()) {
        return GetConsoleInputWaitHandle_orig();
    }

    HANDLE h = InputQueue::Instance().GetWaitHandle();
    // Phase 9：注册为 fake，防止程序调 CloseHandle 关闭它
    // 事件句柄是真实内核对象（高位非魔数），魔数快判断不命中，
    // 需 RegisterFake 加入 HandleRegistry 真实 fake 集合
    // std::set::insert 重复插入无害，多次调用安全
    HandleRegistry::Instance().RegisterFake(h);
    LOG_INFO("WaitHooks: GetConsoleInputWaitHandle -> InputQueue event %p", h);
    return h;
}

// ============================================================
// 注册 Wait 句柄类 Hook
// ============================================================
// GetConsoleInputWaitHandle 在 kernel32.dll 导出，部分版本可能在 kernelbase.dll
// GetProcAddress 判空：未导出时跳过（程序一般不用，无影响）
void RegisterWaitHooks() {
    HMODULE hKBase = GetModuleHandleW(L"kernelbase.dll");
    HMODULE hK32   = GetModuleHandleW(L"kernel32.dll");

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

    std::vector<HookEntry> entries;
    void* pWait = resolve("GetConsoleInputWaitHandle");
    if (pWait == nullptr) {
        // 未导出不是错误：程序一般不用此函数，跳过即可
        LOG_INFO("WaitHooks: GetConsoleInputWaitHandle not exported, skip");
        return;
    }

    entries.push_back({"GetConsoleInputWaitHandle",
        pWait,
        reinterpret_cast<void*>(&GetConsoleInputWaitHandle_Detour),
        reinterpret_cast<void**>(&GetConsoleInputWaitHandle_orig)});

    for (const auto& e : entries) {
        if (e.target == nullptr) {
            LOG_ERROR("RegisterWaitHooks: failed to resolve %s", e.name);
            return;
        }
    }

    HookManager::RegisterBatch(entries);
    LOG_INFO("WaitHooks registered (%zu hooks)", entries.size());
}

} // namespace terminjector::hooks
