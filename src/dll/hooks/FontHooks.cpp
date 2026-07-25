// 字体类 Console API Hook 实现（Phase 8）
// 详见 docs/phases/08-advanced-features.md 4.4
//
// 关键点：
//   - WT 字体由用户 settings.json 配置，DLL 无法也无需改变
//   - GetCurrentConsoleFontEx 返回注入瞬间快照，保证目标程序读到稳定字体尺寸
//   - SetCurrentConsoleFontEx 假装接受（返回 TRUE），仅记录到缓存，不真改
//   - GetConsoleFontSize 返回缓存中的 dwFontSize
//
// 不调原 API 的原因：
//   - GetCurrentConsoleFontEx 调原 API 会读到 ConHost 的真实字体，但 WT 渲染
//     用的是用户配置的字体，两者尺寸可能不一致，导致目标程序布局错乱
//   - SetCurrentConsoleFontEx 调原 API 会改 ConHost 字体，但 WT 不受影响
//     （ConHost 仅是 ConPTY 的后端，WT 是前端渲染器），无意义且可能引发副作用
#include "FontHooks.h"
#include "HookCommon.h"
#include "HookWhitelist.h"
#include "../HookManager.h"
#include "../state/ConsoleState.h"
#include "logging/Logger.h"

#include <windows.h>
#include <vector>

namespace terminjector::hooks {

// ============================================================
// 原函数指针定义
// ============================================================
DEFINE_ORIG_PTR(GetCurrentConsoleFontEx, BOOL WINAPI(HANDLE, BOOL, PCONSOLE_FONT_INFOEX));
DEFINE_ORIG_PTR(SetCurrentConsoleFontEx, BOOL WINAPI(HANDLE, BOOL, PCONSOLE_FONT_INFOEX));
DEFINE_ORIG_PTR(GetConsoleFontSize, COORD WINAPI(HANDLE, DWORD));

// ============================================================
// GetCurrentConsoleFontEx Hook
// ============================================================
// 返回 ConsoleState 缓存的字体信息（注入瞬间快照）
//
// cbSize 校验：调用方传入的 info->cbSize 必须等于 sizeof(CONSOLE_FONT_INFOEX)
//   系统 API 在 cbSize 不符时返回 FALSE，此处保持一致语义
//   修正 cbSize 字段后填充缓存值（避免调用方读到错误 cbSize 后续误用）
BOOL WINAPI GetCurrentConsoleFontEx_Detour(HANDLE h, BOOL bMaximumWindow,
                                            PCONSOLE_FONT_INFOEX info) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (IsInLazyInit()) {
        return GetCurrentConsoleFontEx_orig(h, bMaximumWindow, info);
    }

    if (!IsConsoleHandle(h) || info == nullptr) {
        return GetCurrentConsoleFontEx_orig(h, bMaximumWindow, info);
    }

    // cbSize 校验（与系统 API 一致）
    if (info->cbSize != sizeof(CONSOLE_FONT_INFOEX)) {
        // 修正 cbSize 后继续填充（部分程序传入旧版结构大小，宽容处理）
        info->cbSize = sizeof(CONSOLE_FONT_INFOEX);
    }

    CONSOLE_FONT_INFOEX cached = ConsoleState::Instance().GetFontInfo();
    *info = cached;
    info->cbSize = sizeof(CONSOLE_FONT_INFOEX);  // 确保 cbSize 正确
    return TRUE;
}

// ============================================================
// SetCurrentConsoleFontEx Hook
// ============================================================
// 仅记录到 ConsoleState 缓存，不真改（WT 字体由用户配置控制）
// 返回 TRUE 让目标程序认为设置成功
BOOL WINAPI SetCurrentConsoleFontEx_Detour(HANDLE h, BOOL bMaximumWindow,
                                            PCONSOLE_FONT_INFOEX info) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (IsInLazyInit()) {
        return SetCurrentConsoleFontEx_orig(h, bMaximumWindow, info);
    }

    if (!IsConsoleHandle(h) || info == nullptr) {
        return SetCurrentConsoleFontEx_orig(h, bMaximumWindow, info);
    }

    ConsoleState::Instance().SetFontInfo(*info);
    LOG_INFO("FontHooks: SetCurrentConsoleFontEx recorded (face=%ls size=%dx%d)"
             " (no real change, WT font controlled by user config)",
             info->FaceName, info->dwFontSize.X, info->dwFontSize.Y);
    return TRUE;  // 不调原 API
}

// ============================================================
// GetConsoleFontSize Hook
// ============================================================
// 返回缓存中的 dwFontSize
// 目标程序用此值计算字符单元格大小（如 vim 计算 columns/rows）
COORD WINAPI GetConsoleFontSize_Detour(HANDLE h, DWORD nFont) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (IsInLazyInit()) {
        return GetConsoleFontSize_orig(h, nFont);
    }

    if (!IsConsoleHandle(h)) {
        return GetConsoleFontSize_orig(h, nFont);
    }

    CONSOLE_FONT_INFOEX cached = ConsoleState::Instance().GetFontInfo();
    return cached.dwFontSize;
}

// ============================================================
// 注册字体类 Hook
// ============================================================
void RegisterFontHooks() {
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
    entries.push_back({"GetCurrentConsoleFontEx",
        resolve("GetCurrentConsoleFontEx"),
        reinterpret_cast<void*>(&GetCurrentConsoleFontEx_Detour),
        reinterpret_cast<void**>(&GetCurrentConsoleFontEx_orig)});
    entries.push_back({"SetCurrentConsoleFontEx",
        resolve("SetCurrentConsoleFontEx"),
        reinterpret_cast<void*>(&SetCurrentConsoleFontEx_Detour),
        reinterpret_cast<void**>(&SetCurrentConsoleFontEx_orig)});
    entries.push_back({"GetConsoleFontSize",
        resolve("GetConsoleFontSize"),
        reinterpret_cast<void*>(&GetConsoleFontSize_Detour),
        reinterpret_cast<void**>(&GetConsoleFontSize_orig)});

    for (const auto& e : entries) {
        if (e.target == nullptr) {
            LOG_ERROR("RegisterFontHooks: failed to resolve %s", e.name);
            return;
        }
    }

    HookManager::RegisterBatch(entries);
    LOG_INFO("FontHooks registered (%zu hooks)", entries.size());
}

} // namespace terminjector::hooks
