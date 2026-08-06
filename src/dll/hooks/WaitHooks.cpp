// Wait 句柄类 Console API Hook 实现（Phase 8 + Textual 退出卡死修复）
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
//   Textual 退出卡死修复（WaitForMultipleObjects* Hook）：
//   部分程序不调用 GetConsoleInputWaitHandle，而是直接
//   WaitForMultipleObjects([stdin 句柄], ...) 等输入（Textual win32 driver 的
//   wait_for_handles([hIn], 100) 轮询）。这类句柄的 signal 状态与输入来源
//   （InputQueue）脱节：
//     - KickStart 注入时向 ConHost 写的回车永远残留在 ConHost 输入队列
//       （ReadConsoleInput Detour 只从 InputQueue 读，不消费 ConHost 队列）
//     - ConHost 输入句柄因此永远 signaled → WaitForMultipleObjects 永远立即返回
//     - 程序反复调 ReadConsoleInput → 进入 Detour 后 InputQueue 空 →
//       Detour 的 100ms 无限循环（只检查 transport/unloader）→ 程序主循环被霸占
//     - Textual 事件线程因此无法检查 exit_event → Ctrl+Q 后 join 卡死，进程永不退出
//   方案：把句柄组中的输入句柄替换为 InputQueue 事件（位置不变，返回索引天然
//   对应原句柄位置，调用方无感知）：
//     - 队列有数据 → 事件 signaled → WaitForMultipleObjects 返回 → ReadConsoleInput 出队 ✓
//     - 队列空 → 正常超时 → 调用方轮询并检查自身退出条件 ✓
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
#include <mutex>
#include <vector>

namespace terminjector::hooks {

// ============================================================
// 原函数指针定义
// ============================================================
// GetConsoleInputWaitHandle 无标准头文件声明，用 DEFINE_ORIG_PTR 定义函数指针类型
DEFINE_ORIG_PTR(GetConsoleInputWaitHandle, HANDLE WINAPI());

// WaitForMultipleObjects*（kernelbase 实现，kernel32 入口 stub 转发至此）
DEFINE_ORIG_PTR(WaitForMultipleObjectsEx, DWORD WINAPI(DWORD, const HANDLE*, BOOL, DWORD, BOOL));
DEFINE_ORIG_PTR(WaitForMultipleObjects, DWORD WINAPI(DWORD, const HANDLE*, BOOL, DWORD));

// WaitForSingleObject*（Phase 21：node/libuv 型 TUI 补漏）
// 背景：opencode（crossterm/node 运行时）不用 WaitForMultipleObjects，
//       而是用单个 stdin 句柄 WaitForSingleObject(GetConsoleInputWaitHandle-like)，
//       此前仅 hook 多句柄版本，mate 单句柄 wait 永远不会被唤醒 →
//       TUI 输入线程永久阻塞（cdb 栈证实线程阻塞在 WaitForSingleObjectEx），
//       鼠标/键盘/重绘全部失效。
DEFINE_ORIG_PTR(WaitForSingleObjectEx, DWORD WINAPI(HANDLE, DWORD, BOOL));
DEFINE_ORIG_PTR(WaitForSingleObject, DWORD WINAPI(HANDLE, DWORD));

namespace {

// 句柄组中是否含输入句柄（快检，避免高频调用时无谓分配）
bool ContainsInputHandle(DWORD nCount, const HANDLE* in) {
    for (DWORD i = 0; i < nCount; ++i) {
        if (IsInputHandle(in[i])) return true;
    }
    return false;
}

// 把句柄组中的输入句柄替换为 InputQueue 事件（位置不变）
// 调用方只在等待期间短暂持有替换句柄，不会 CloseHandle，无需 RegisterFake
void ReplaceInputHandles(DWORD nCount, const HANDLE* in, std::vector<HANDLE>& out) {
    out.assign(in, in + nCount);
    for (DWORD i = 0; i < nCount; ++i) {
        if (IsInputHandle(in[i])) {
            out[i] = InputQueue::Instance().GetWaitHandle();
        }
    }
}

} // namespace

// ============================================================
// WaitForMultipleObjectsEx Hook
// ============================================================
// 输入句柄替换为 InputQueue 事件，其余 pass-through（bWaitAll/bAlertable 语义保留）
DWORD WINAPI WaitForMultipleObjectsEx_Detour(DWORD nCount, const HANDLE* lpHandles,
                                             BOOL bWaitAll, DWORD dwMilliseconds,
                                             BOOL bAlertable) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (IsInLazyInit() || nCount == 0 || lpHandles == nullptr) {
        return WaitForMultipleObjectsEx_orig(nCount, lpHandles, bWaitAll,
                                             dwMilliseconds, bAlertable);
    }

    if (!ContainsInputHandle(nCount, lpHandles)) {
        return WaitForMultipleObjectsEx_orig(nCount, lpHandles, bWaitAll,
                                             dwMilliseconds, bAlertable);
    }

    std::vector<HANDLE> replaced;
    ReplaceInputHandles(nCount, lpHandles, replaced);
    static std::once_flag s_logged;
    std::call_once(s_logged, [] {
        LOG_INFO("WaitHooks: WaitForMultipleObjectsEx input handle replaced by InputQueue event");
    });
    return WaitForMultipleObjectsEx_orig(nCount, replaced.data(), bWaitAll,
                                         dwMilliseconds, bAlertable);
}

// ============================================================
// WaitForMultipleObjects Hook
// ============================================================
// 同 Ex 版（bAlertable=FALSE），覆盖不传 alertable 的调用
DWORD WINAPI WaitForMultipleObjects_Detour(DWORD nCount, const HANDLE* lpHandles,
                                           BOOL bWaitAll, DWORD dwMilliseconds) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (IsInLazyInit() || nCount == 0 || lpHandles == nullptr) {
        return WaitForMultipleObjects_orig(nCount, lpHandles, bWaitAll, dwMilliseconds);
    }

    if (!ContainsInputHandle(nCount, lpHandles)) {
        return WaitForMultipleObjects_orig(nCount, lpHandles, bWaitAll, dwMilliseconds);
    }

    std::vector<HANDLE> replaced;
    ReplaceInputHandles(nCount, lpHandles, replaced);
    static std::once_flag s_logged;
    std::call_once(s_logged, [] {
        LOG_INFO("WaitHooks: WaitForMultipleObjects input handle replaced by InputQueue event");
    });
    return WaitForMultipleObjects_orig(nCount, replaced.data(), bWaitAll, dwMilliseconds);
}

// ============================================================
// WaitForSingleObjectEx Hook（Phase 21）
// ============================================================
// node/libuv 型 TUI（opencode 等）对 stdin 用单句柄等待：
//   WaitForSingleObjectEx(GetStdHandle(STD_INPUT_HANDLE), 0/INFINITE, alertable)
// 替换 input handle 为 InputQueue 事件（与多句柄版同策略），
// 有数据 → signaled → 返回 → 调用方 ReadConsoleInputW 出队;
// 无数据 → 按超时返回（调用方轮询自身状态）。
// bAlertable 原样透传；非输入句柄直接 pass-through。
DWORD WINAPI WaitForSingleObjectEx_Detour(HANDLE hHandle, DWORD dwMilliseconds,
                                          BOOL bAlertable) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (IsInLazyInit() || hHandle == nullptr) {
        return WaitForSingleObjectEx_orig(hHandle, dwMilliseconds, bAlertable);
    }

    if (!IsInputHandle(hHandle)) {
        return WaitForSingleObjectEx_orig(hHandle, dwMilliseconds, bAlertable);
    }

    static std::once_flag s_loggedEx;
    std::call_once(s_loggedEx, [] {
        LOG_INFO("WaitHooks: WaitForSingleObjectEx input handle replaced by InputQueue event");
    });
    HANDLE h = InputQueue::Instance().GetWaitHandle();
    return WaitForSingleObjectEx_orig(h, dwMilliseconds, bAlertable);
}

// ============================================================
// WaitForSingleObject Hook（Phase 21）
// ============================================================
// 同 Ex 版（bAlertable=FALSE），覆盖不传 alertable 的调用
DWORD WINAPI WaitForSingleObject_Detour(HANDLE hHandle, DWORD dwMilliseconds) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (IsInLazyInit() || hHandle == nullptr) {
        return WaitForSingleObject_orig(hHandle, dwMilliseconds);
    }

    if (!IsInputHandle(hHandle)) {
        return WaitForSingleObject_orig(hHandle, dwMilliseconds);
    }

    static std::once_flag s_logged;
    std::call_once(s_logged, [] {
        LOG_INFO("WaitHooks: WaitForSingleObject input handle replaced by InputQueue event");
    });
    HANDLE h = InputQueue::Instance().GetWaitHandle();
    return WaitForSingleObject_orig(h, dwMilliseconds);
}

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
    } else {
        entries.push_back({"GetConsoleInputWaitHandle",
            pWait,
            reinterpret_cast<void*>(&GetConsoleInputWaitHandle_Detour),
            reinterpret_cast<void**>(&GetConsoleInputWaitHandle_orig)});
    }

    // Textual 退出卡死修复：直接等 stdin 句柄的程序（不依赖 GetConsoleInputWaitHandle）
    // kernelbase 导出两个实现（kernel32 的入口 stub 转发至此），都注册
    void* pWmEx = resolve("WaitForMultipleObjectsEx");
    if (pWmEx == nullptr) {
        LOG_ERROR("WaitHooks: WaitForMultipleObjectsEx not exported");
        return;
    }
    entries.push_back({"WaitForMultipleObjectsEx",
        pWmEx,
        reinterpret_cast<void*>(&WaitForMultipleObjectsEx_Detour),
        reinterpret_cast<void**>(&WaitForMultipleObjectsEx_orig)});

    void* pWm = resolve("WaitForMultipleObjects");
    if (pWm != nullptr) {
        entries.push_back({"WaitForMultipleObjects",
            pWm,
            reinterpret_cast<void*>(&WaitForMultipleObjects_Detour),
            reinterpret_cast<void**>(&WaitForMultipleObjects_orig)});
    } else {
        // 个别系统 kernelbase 可能不导出非 Ex 版；kernel32 的入口 stub 会
        // 转调 Ex 版（已 Hook），功能不受影响
        LOG_INFO("WaitHooks: WaitForMultipleObjects not exported, skip");
    }

    // Phase 21：单句柄等待版本（node/libuv 型 TUI）
    // kernel32 的 WaitForSingleObject 入口 stub 会转调 kernelbase 的 Ex 版，
    // 两个版本都注册，确保无论程序调用哪个都能命中替换逻辑
    void* pWsEx = resolve("WaitForSingleObjectEx");
    if (pWsEx == nullptr) {
        LOG_ERROR("WaitHooks: WaitForSingleObjectEx not exported");
        return;
    }
    entries.push_back({"WaitForSingleObjectEx",
        pWsEx,
        reinterpret_cast<void*>(&WaitForSingleObjectEx_Detour),
        reinterpret_cast<void**>(&WaitForSingleObjectEx_orig)});

    void* pWs = resolve("WaitForSingleObject");
    if (pWs != nullptr) {
        entries.push_back({"WaitForSingleObject",
            pWs,
            reinterpret_cast<void*>(&WaitForSingleObject_Detour),
            reinterpret_cast<void**>(&WaitForSingleObject_orig)});
    } else {
        LOG_INFO("WaitHooks: WaitForSingleObject not exported, skip");
    }

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
