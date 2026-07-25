// 缓冲区类 Console API Hook 实现
// 详见 docs/phases/05-cursor-buffer.md 4.7
//
// 流程：
//   1. ENSURE_INITIALIZED() 触发懒加载
//   2. 非真实 Console 句柄 pass-through
//   3. Set 类：更新缓存 + 调原 API
//   4. Get 类：返回缓存
//
// 注意：SetConsoleScreenBufferSize 和 SetConsoleWindowInfo 改变目标程序的
// 内部缓冲区尺寸/视口。WT 侧的真实尺寸由 mediator 的 WtSizeWatcher 监听并
// 通过 ResizeNotify 通知 DLL（反向同步），此处仅维护缓存一致性
#include "BufferHooks.h"
#include "HookCommon.h"
#include "HookWhitelist.h"
#include "../HookManager.h"
#include "../state/ConsoleState.h"
#include "../state/HandleRegistry.h"
#include "../translator/VtEscape.h"
#include "logging/Logger.h"

#include <windows.h>
#include <vector>

namespace terminjector::hooks {

// ============================================================
// 原函数指针定义
// ============================================================
DEFINE_ORIG_PTR(SetConsoleScreenBufferSize, BOOL WINAPI(HANDLE, COORD));
DEFINE_ORIG_PTR(SetConsoleWindowInfo, BOOL WINAPI(HANDLE, BOOL, const SMALL_RECT*));
DEFINE_ORIG_PTR(GetLargestConsoleWindowSize, COORD WINAPI(HANDLE));
// Phase 8：Alt Buffer 相关
DEFINE_ORIG_PTR(SetConsoleActiveScreenBuffer, BOOL WINAPI(HANDLE));
DEFINE_ORIG_PTR(CreateConsoleScreenBuffer, HANDLE WINAPI(
    DWORD, DWORD, const SECURITY_ATTRIBUTES*, DWORD, LPVOID));

// ============================================================
// SetConsoleScreenBufferSize Hook
// ============================================================
// 目标程序改变缓冲区尺寸（如 mode con: cols=... lines=...）
// Phase 9：不调原 API，避免 ConHost 真改导致原 cmd 黑框闪烁
// WT 侧的真实尺寸由 mediator WtSizeWatcher 监听并反向同步
BOOL WINAPI SetConsoleScreenBufferSize_Detour(HANDLE hConsoleOutput, COORD dwSize) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (!IsConsoleHandle(hConsoleOutput)) {
        return SetConsoleScreenBufferSize_orig(hConsoleOutput, dwSize);
    }

    // 更新缓存
    ConsoleState::Instance().SetBufferSize(dwSize);
    LOG_DEBUG("SetConsoleScreenBufferSize: %dx%d", dwSize.X, dwSize.Y);

    // 不调原 API：ConHost 不再收到尺寸变更，消除原 cmd 黑框更新闪烁
    // 通知 mediator 由 WtSizeWatcher 反向同步处理
    return TRUE;
}

// ============================================================
// SetConsoleWindowInfo Hook
// ============================================================
// 目标程序改变视口位置/尺寸
// bAbsolute=true：lpConsoleWindow 为绝对坐标；false：相对当前窗口的偏移
// Phase 9：不调原 API，避免 ConHost 真改导致闪烁
BOOL WINAPI SetConsoleWindowInfo_Detour(HANDLE hConsoleOutput, BOOL bAbsolute,
                                        const SMALL_RECT* lpConsoleWindow) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (!IsConsoleHandle(hConsoleOutput) || lpConsoleWindow == nullptr) {
        return SetConsoleWindowInfo_orig(hConsoleOutput, bAbsolute, lpConsoleWindow);
    }

    // 更新缓存
    if (bAbsolute) {
        ConsoleState::Instance().SetWindow(*lpConsoleWindow);
    } else {
        // 相对偏移：在当前窗口基础上叠加
        SMALL_RECT cur = ConsoleState::Instance().GetWindow();
        cur.Left   += lpConsoleWindow->Left;
        cur.Top    += lpConsoleWindow->Top;
        cur.Right  += lpConsoleWindow->Right;
        cur.Bottom += lpConsoleWindow->Bottom;
        ConsoleState::Instance().SetWindow(cur);
    }

    // 不调原 API：ConHost 不再收到窗口变更
    return TRUE;
}

// ============================================================
// GetLargestConsoleWindowSize Hook
// ============================================================
// 返回缓存值（与 FillScreenBufferInfo 中 dwMaximumWindowSize 一致）
COORD WINAPI GetLargestConsoleWindowSize_Detour(HANDLE hConsoleOutput) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (!IsConsoleHandle(hConsoleOutput)) {
        return GetLargestConsoleWindowSize_orig(hConsoleOutput);
    }

    // 用 srWindow 尺寸近似（与 FillScreenBufferInfo 同策略）
    SMALL_RECT w = ConsoleState::Instance().GetWindow();
    COORD r;
    r.X = static_cast<SHORT>(w.Right - w.Left + 1);
    r.Y = static_cast<SHORT>(w.Bottom - w.Top + 1);
    return r;
}

// ============================================================
// SetConsoleActiveScreenBuffer Hook（Phase 8：Alt Buffer）
// ============================================================
// 目标程序切换主/备缓冲区（vim/less 进入时切备，退出时切主）
//
// 切换方向判断：
//   传入句柄 == ConsoleState 缓存的主缓冲区句柄 → 退出 Alt Buffer
//   传入句柄 != 主缓冲区句柄（通常 == Alt 伪句柄）→ 进入 Alt Buffer
//
// VT 序列：DECSET/DECRST 1049
//   1049 = 保存光标 + 切到备用屏 + 清屏，退出时恢复光标 + 切回主屏
//   WT 原生支持，无需 DLL 自己保存/恢复屏幕内容
//
// 不调原 API：避免 ConHost 真切缓冲区导致状态不一致
//   （DLL 已用 VT 序列让 WT 切，ConHost 切不切无关紧要）
BOOL WINAPI SetConsoleActiveScreenBuffer_Detour(HANDLE h) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (IsInLazyInit()) {
        return SetConsoleActiveScreenBuffer_orig(h);
    }

    auto& state = ConsoleState::Instance();
    HANDLE mainH = state.GetMainBufferHandle();
    bool toAlt = (h != mainH);

    state.SetAltBufferActive(toAlt);
    const char* seq = toAlt ? vt::kEnterAltBuffer : vt::kExitAltBuffer;
    SendToMediator(seq, toAlt ? sizeof(vt::kEnterAltBuffer) - 1
                              : sizeof(vt::kExitAltBuffer) - 1);
    LOG_INFO("BufferHooks: AltBuffer %s (h=%p main=%p)",
             toAlt ? "enter" : "exit", h, mainH);
    return TRUE;  // 不调原 API
}

// ============================================================
// CreateConsoleScreenBuffer Hook（Phase 8：Alt Buffer）
// ============================================================
// 目标程序创建新屏幕缓冲区（vim/less 进入全屏前的准备工作）
//
// 不真的创建（避免 ConHost 状态混乱），返回伪句柄标识 Alt Buffer
// 伪句柄用一个不可能为真实 HANDLE 的魔数，避免与系统句柄冲突
// 程序随后调 SetConsoleActiveScreenBuffer(伪句柄) 触发 Alt Buffer 进入
//
// 注意：伪句柄被 CloseHandle 时需 Phase 9 Hook 跳过（不真关）
HANDLE WINAPI CreateConsoleScreenBuffer_Detour(
    DWORD access, DWORD share, const SECURITY_ATTRIBUTES* sa,
    DWORD flags, LPVOID data) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (IsInLazyInit()) {
        return CreateConsoleScreenBuffer_orig(access, share, sa, flags, data);
    }

    // 伪句柄魔数：选一个不可能为真实内核句柄的值
    // 真实 HANDLE 通常是 4 字节对齐的内核指针，魔数用 0xABCDE123（奇数+非对齐）
    // 位 16-31 为 0xABCD，命中 IsFakeHandleFast 快路径判断，CloseHandle 静默返回 TRUE
    // 注意：值必须满足 (h & 0xFFFF0000) == 0xABCD0000，否则 IsFakeHandleFast 不命中
    static const HANDLE kAltBufferSentinel =
        reinterpret_cast<HANDLE>(static_cast<uintptr_t>(0xABCDE123));

    ConsoleState::Instance().SetAltBufferHandle(kAltBufferSentinel);
    // Phase 9：注册到 HandleRegistry（统一假句柄管理）
    // 实际 CloseHandle 快路径靠魔数判断，注册是语义清晰（"所有假句柄都在此"）
    HandleRegistry::Instance().RegisterFake(kAltBufferSentinel);
    LOG_INFO("BufferHooks: CreateConsoleScreenBuffer -> sentinel %p",
             kAltBufferSentinel);
    return kAltBufferSentinel;
}

// ============================================================
// 注册所有缓冲区类 Hook
// ============================================================
void RegisterBufferHooks() {
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
    entries.push_back({"SetConsoleScreenBufferSize",
        resolve("SetConsoleScreenBufferSize"),
        reinterpret_cast<void*>(&SetConsoleScreenBufferSize_Detour),
        reinterpret_cast<void**>(&SetConsoleScreenBufferSize_orig)});
    entries.push_back({"SetConsoleWindowInfo",
        resolve("SetConsoleWindowInfo"),
        reinterpret_cast<void*>(&SetConsoleWindowInfo_Detour),
        reinterpret_cast<void**>(&SetConsoleWindowInfo_orig)});
    entries.push_back({"GetLargestConsoleWindowSize",
        resolve("GetLargestConsoleWindowSize"),
        reinterpret_cast<void*>(&GetLargestConsoleWindowSize_Detour),
        reinterpret_cast<void**>(&GetLargestConsoleWindowSize_orig)});
    // Phase 8：Alt Buffer
    entries.push_back({"SetConsoleActiveScreenBuffer",
        resolve("SetConsoleActiveScreenBuffer"),
        reinterpret_cast<void*>(&SetConsoleActiveScreenBuffer_Detour),
        reinterpret_cast<void**>(&SetConsoleActiveScreenBuffer_orig)});
    entries.push_back({"CreateConsoleScreenBuffer",
        resolve("CreateConsoleScreenBuffer"),
        reinterpret_cast<void*>(&CreateConsoleScreenBuffer_Detour),
        reinterpret_cast<void**>(&CreateConsoleScreenBuffer_orig)});

    for (const auto& e : entries) {
        if (e.target == nullptr) {
            LOG_ERROR("RegisterBufferHooks: failed to resolve %s", e.name);
            return;
        }
    }

    HookManager::RegisterBatch(entries);
    LOG_INFO("BufferHooks registered (%zu hooks)", entries.size());
}

} // namespace terminjector::hooks
