// 光标类 Console API Hook 实现
// 详见 docs/phases/05-cursor-buffer.md 4.2-4.4
//
// 流程：
//   1. ENSURE_INITIALIZED() 触发懒加载
//   2. 非真实 Console 句柄 pass-through
//   3. Set 类：更新缓存 + 输出 VT 序列 + 不调原 API（Phase 9 静默模式）
//   4. Get 类：直接返回缓存（不调原 API，避免 ConHost 旧值）
//
// 关键：GetConsoleScreenBufferInfo 不调原 API
//   原因：目标程序通过此 API 查询当前尺寸/光标，必须返回 DLL 缓存的"虚拟"状态，
//         否则 ConHost 仍保留目标程序注入瞬间的尺寸，cmd 提示符换行会按旧宽度
#include "CursorHooks.h"
#include "HookCommon.h"
#include "HookWhitelist.h"
#include "../HookManager.h"
#include "../state/ConsoleState.h"
#include "../translator/VtEscape.h"
#include "logging/Logger.h"

#include <windows.h>
#include <string>
#include <vector>

namespace terminjector::hooks {

// 引入 VT 光标定位函数
using terminjector::vt::CursorPosition;

// ============================================================
// 原函数指针定义
// ============================================================
DEFINE_ORIG_PTR(SetConsoleCursorPosition, BOOL WINAPI(HANDLE, COORD));
DEFINE_ORIG_PTR(GetConsoleScreenBufferInfo, BOOL WINAPI(HANDLE, PCONSOLE_SCREEN_BUFFER_INFO));
DEFINE_ORIG_PTR(SetConsoleCursorInfo, BOOL WINAPI(HANDLE, const CONSOLE_CURSOR_INFO*));
DEFINE_ORIG_PTR(GetConsoleCursorInfo, BOOL WINAPI(HANDLE, PCONSOLE_CURSOR_INFO));

// ============================================================
// SetConsoleCursorPosition Hook
// ============================================================
// 目标程序移动光标时：输出 VT 光标定位 + 更新缓存
// 解决 color/cls 后光标不同步问题（Phase 4 遗留）
//
// 关键：不调 _orig（Phase 9 自保护策略）
//   原因：调 _orig 会让 ConHost 处理 SetConsoleCursorPosition，ConPTY 拦截后
//   可能发送额外的 VT 序列与我们的光标定位冲突，导致 WT 光标位置不一致。
//   典型现象：Python REPL 输出 prompt 后调 SetConsoleCursorPosition 回退光标，
//   ConHost 处理导致 ConPTY 二次发送光标定位，WT 光标未移动，第二个 prompt
//   输出在第一个之后，视觉上出现双 >>>。
//   解决：只发 VT，让 ConPTY 单一来源处理光标定位。
BOOL WINAPI SetConsoleCursorPosition_Detour(HANDLE hConsoleOutput, COORD pos) {
    ENSURE_INITIALIZED();
    ASSERT_IN_HOOK();          // 关键 Detour：光标同步高频，检测非预期重入
    HookReentryGuard guard;

    if (!IsConsoleHandle(hConsoleOutput)) {
        return SetConsoleCursorPosition_orig(hConsoleOutput, pos);
    }

    // Phase 5 诊断日志
    LOG_INFO("SetConsoleCursorPosition_Detour: pos=(%d,%d)", pos.X, pos.Y);

    // 更新光标缓存
    ConsoleState::Instance().SetCursorPosition(pos);

    // 输出 VT 光标定位（VT 是 1-based，pos 是 0-based）
    std::string s = CursorPosition(pos.Y + 1, pos.X + 1);
    SendToMediator(s.data(), s.size());

    // Phase 9：不调 _orig，避免 ConHost/ConPTY 二次处理光标定位冲突
    return TRUE;
}

// ============================================================
// GetConsoleScreenBufferInfo Hook（关键）
// ============================================================
// 返回 ConsoleState 缓存，不调原 API
// 原因：ConHost 保留注入瞬间状态，cmd.exe 用此 API 判断换行宽度，
//       若返回 ConHost 旧值会导致换行按旧宽度，WT 显示错乱
BOOL WINAPI GetConsoleScreenBufferInfo_Detour(HANDLE hConsoleOutput,
                                              PCONSOLE_SCREEN_BUFFER_INFO lpInfo) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    // LazyInit 期间调原始 API：StateSnapshot::Capture 需要真实 console 值，
    // 不能返回未初始化的 ConsoleState 缓存（会是 0x0）
    if (IsInLazyInit()) {
        return GetConsoleScreenBufferInfo_orig(hConsoleOutput, lpInfo);
    }

    if (!IsConsoleHandle(hConsoleOutput) || lpInfo == nullptr) {
        return GetConsoleScreenBufferInfo_orig(hConsoleOutput, lpInfo);
    }

    // 直接返回缓存（不调原 API）
    ConsoleState::Instance().FillScreenBufferInfo(*lpInfo);

    // Phase 5 诊断日志（采样，避免日志爆炸）
    static thread_local int s_callCount = 0;
    if (s_callCount++ < 20) {
        LOG_INFO("GetConsoleScreenBufferInfo_Detour: size=%dx%d win=(%d,%d)-(%d,%d) cursor=(%d,%d)",
                 lpInfo->dwSize.X, lpInfo->dwSize.Y,
                 lpInfo->srWindow.Left, lpInfo->srWindow.Top,
                 lpInfo->srWindow.Right, lpInfo->srWindow.Bottom,
                 lpInfo->dwCursorPosition.X, lpInfo->dwCursorPosition.Y);
    }

    return TRUE;
}

// ============================================================
// SetConsoleCursorInfo Hook
// ============================================================
// 光标显隐：\x1b[?25h 显示 / \x1b[?25l 隐藏
// 光标大小：WT 不支持精确控制大小，忽略（仅缓存）
// Phase 9：不调原 API，避免 ConHost 真改光标导致闪烁
BOOL WINAPI SetConsoleCursorInfo_Detour(HANDLE hConsoleOutput,
                                        const CONSOLE_CURSOR_INFO* lpInfo) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (!IsConsoleHandle(hConsoleOutput) || lpInfo == nullptr) {
        return SetConsoleCursorInfo_orig(hConsoleOutput, lpInfo);
    }

    // 更新缓存
    ConsoleState::Instance().SetCursorInfo(*lpInfo);

    // DECTCE: 光标显隐
    std::string s = lpInfo->bVisible ? "\x1b[?25h" : "\x1b[?25l";
    SendToMediator(s.data(), s.size());

    // 不调原 API：ConHost 不再收到光标信息变更
    return TRUE;
}

// ============================================================
// GetConsoleCursorInfo Hook
// ============================================================
// 返回缓存（不调原 API）
BOOL WINAPI GetConsoleCursorInfo_Detour(HANDLE hConsoleOutput,
                                       PCONSOLE_CURSOR_INFO lpInfo) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    // LazyInit 期间调原始 API（同 GetConsoleScreenBufferInfo 理由）
    if (IsInLazyInit()) {
        return GetConsoleCursorInfo_orig(hConsoleOutput, lpInfo);
    }

    if (!IsConsoleHandle(hConsoleOutput) || lpInfo == nullptr) {
        return GetConsoleCursorInfo_orig(hConsoleOutput, lpInfo);
    }

    *lpInfo = ConsoleState::Instance().GetCursorInfo();
    return TRUE;
}

// ============================================================
// 注册所有光标类 Hook
// ============================================================
void RegisterCursorHooks() {
    // 优先 kernelbase，回退 kernel32（与 OutputHooks 同策略）
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
    entries.push_back({"SetConsoleCursorPosition",
        resolve("SetConsoleCursorPosition"),
        reinterpret_cast<void*>(&SetConsoleCursorPosition_Detour),
        reinterpret_cast<void**>(&SetConsoleCursorPosition_orig)});
    entries.push_back({"GetConsoleScreenBufferInfo",
        resolve("GetConsoleScreenBufferInfo"),
        reinterpret_cast<void*>(&GetConsoleScreenBufferInfo_Detour),
        reinterpret_cast<void**>(&GetConsoleScreenBufferInfo_orig)});
    entries.push_back({"SetConsoleCursorInfo",
        resolve("SetConsoleCursorInfo"),
        reinterpret_cast<void*>(&SetConsoleCursorInfo_Detour),
        reinterpret_cast<void**>(&SetConsoleCursorInfo_orig)});
    entries.push_back({"GetConsoleCursorInfo",
        resolve("GetConsoleCursorInfo"),
        reinterpret_cast<void*>(&GetConsoleCursorInfo_Detour),
        reinterpret_cast<void**>(&GetConsoleCursorInfo_orig)});

    for (const auto& e : entries) {
        if (e.target == nullptr) {
            LOG_ERROR("RegisterCursorHooks: failed to resolve %s", e.name);
            return;
        }
    }

    HookManager::RegisterBatch(entries);
    LOG_INFO("CursorHooks registered (%zu hooks)", entries.size());
}

// ============================================================
// Phase 10：暴露 orig trampoline 给 StatePoller 等模块
// ============================================================
// 用途：StatePoller 需读取 ConHost 真实状态，若直接调 GetConsoleScreenBufferInfo
//       会进入自己的 Detour 返回缓存，拿不到真实值。通过 orig trampoline 绕过 Hook。
// 前提：Hook 已安装（InstallAll 后 orig 非 null）。StatePoller 在 LazyInit 完成
//       后启动，此时 Hook 必已安装，故此处不判空（若为 null 属 bug 应暴露）
BOOL CallRealGetConsoleScreenBufferInfo(HANDLE hConsoleOutput,
                                        PCONSOLE_SCREEN_BUFFER_INFO lpInfo) {
    return GetConsoleScreenBufferInfo_orig(hConsoleOutput, lpInfo);
}

} // namespace terminjector::hooks
