// 模式类 Console API Hook 实现（Phase 7）
// 详见 docs/phases/07-mode-signal.md 4.1/4.2/4.3
//
// 欺骗策略：
//   - 输出方向：始终返回含 ENABLE_VIRTUAL_TERMINAL_PROCESSING
//     让程序认为支持 VT，主动发 VT 序列（省翻译工作）
//   - 输入方向：返回 ConsoleState 缓存的 inputMode（程序请求什么就返回什么）
//     程序可主动请求 ENABLE_VIRTUAL_TERMINAL_INPUT 进入透传模式
//
// SetConsoleMode 不调原 API（避免 ConHost 真改，Phase 9 后 ConHost 不参与）
// 模式变更时发 ModeChange 消息给 mediator（仅日志/调试用）
//
// Phase 8 扩展：Title W/A + CP 系列
//   - SetConsoleTitle W/A → 缓存 + 发 OSC 0 序列更新 WT 标签页标题
//   - GetConsoleTitle W/A → 返回缓存
//   - SetConsoleCP/OutputCP → 缓存 + 发 CpChange 消息（不调原 API，mediator 固定 UTF-8）
//   - GetConsoleCP/OutputCP → 返回缓存
#include "ModeHooks.h"
#include "HookCommon.h"
#include "../HookManager.h"
#include "../state/ConsoleState.h"
#include "../state/InputQueue.h"
#include "../translator/VtEscape.h"
#include "protocol/MessageSerializer.h"
#include "protocol/Message.h"
#include "logging/Logger.h"

#include <windows.h>
#include <vector>
#include <string>

namespace terminjector::hooks {

// ============================================================
// 原函数指针定义
// ============================================================
DEFINE_ORIG_PTR(GetConsoleMode, BOOL WINAPI(HANDLE, LPDWORD));
DEFINE_ORIG_PTR(SetConsoleMode, BOOL WINAPI(HANDLE, DWORD));
// Phase 8：Title
DEFINE_ORIG_PTR(SetConsoleTitleW, BOOL WINAPI(LPCWSTR));
DEFINE_ORIG_PTR(SetConsoleTitleA, BOOL WINAPI(LPCSTR));
DEFINE_ORIG_PTR(GetConsoleTitleW, DWORD WINAPI(LPWSTR, DWORD));
DEFINE_ORIG_PTR(GetConsoleTitleA, DWORD WINAPI(LPSTR, DWORD));
// Phase 8：CP
DEFINE_ORIG_PTR(SetConsoleCP, BOOL WINAPI(UINT));
DEFINE_ORIG_PTR(SetConsoleOutputCP, BOOL WINAPI(UINT));
DEFINE_ORIG_PTR(GetConsoleCP, UINT WINAPI());
DEFINE_ORIG_PTR(GetConsoleOutputCP, UINT WINAPI());

// ============================================================
// 模式变更通知中介
// ============================================================
// 发送 ModeChangePayload 给 mediator（仅日志/调试用，输入翻译在 DLL 侧完成）
static void NotifyModeChange() {
    protocol::ModeChangePayload p{};
    p.inputMode  = ConsoleState::Instance().GetInputMode();
    p.outputMode = ConsoleState::Instance().GetOutputMode();
    auto pkt = protocol::Serialize(protocol::MessageType::ModeChange, &p, sizeof(p));
    SendToMediator(pkt.data(), pkt.size(), protocol::MessageType::ModeChange);
    LOG_INFO("ModeHooks: ModeChange sent inputMode=0x%lx outputMode=0x%lx",
             p.inputMode, p.outputMode);
}

// ============================================================
// GetConsoleMode Hook
// ============================================================
// 输入句柄：返回 ConsoleState inputMode
// 输出句柄：返回 ConsoleState outputMode | ENABLE_VIRTUAL_TERMINAL_PROCESSING（强制 VT）
BOOL WINAPI GetConsoleMode_Detour(HANDLE h, LPDWORD mode) {
    ENSURE_INITIALIZED();

    if (IsInLazyInit()) {
        return GetConsoleMode_orig(h, mode);
    }

    if (!IsConsoleHandle(h) || mode == nullptr) {
        return GetConsoleMode_orig(h, mode);
    }

    auto& state = ConsoleState::Instance();
    if (IsInputHandle(h)) {
        // 输入：返回程序请求的模式（已在 SetConsoleMode 中记录）
        *mode = state.GetInputMode();
    } else {
        // 输出：强制加 VT 处理标志（欺骗程序认为支持 VT）
        *mode = state.GetOutputMode() | ENABLE_VIRTUAL_TERMINAL_PROCESSING;
    }
    return TRUE;
}

// ============================================================
// SetConsoleMode Hook
// ============================================================
// 输入句柄：更新 ConsoleState，模式变更时清空 InputQueue + 发 ModeChange
// 输出句柄：更新 ConsoleState（强制 | VT_PROCESSING），发 ModeChange
// 不调原 API（避免 ConHost 真改）
BOOL WINAPI SetConsoleMode_Detour(HANDLE h, DWORD mode) {
    ENSURE_INITIALIZED();

    if (IsInLazyInit()) {
        return SetConsoleMode_orig(h, mode);
    }

    if (!IsConsoleHandle(h)) {
        return SetConsoleMode_orig(h, mode);
    }

    auto& state = ConsoleState::Instance();

    if (IsInputHandle(h)) {
        DWORD oldMode = state.GetInputMode();
        state.SetInputMode(mode);
        if (mode != oldMode) {
            // 模式切换：清空输入队列，避免残留数据混淆
            InputQueue::Instance().ClearAllOnModeSwitch();
            NotifyModeChange();
            LOG_INFO("ModeHooks: input mode 0x%lx -> 0x%lx (VT_INPUT=%d)",
                     oldMode, mode,
                     (mode & ENABLE_VIRTUAL_TERMINAL_INPUT) ? 1 : 0);
        }
    } else {
        // 输出：强制保留 VT 处理标志
        DWORD forced = mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING;
        DWORD oldMode = state.GetOutputMode();
        state.SetOutputMode(forced);
        if (forced != oldMode) {
            NotifyModeChange();
            LOG_INFO("ModeHooks: output mode 0x%lx -> 0x%lx (forced VT)",
                     oldMode, forced);
        }
    }
    return TRUE;  // 不调原 API
}

// ============================================================
// CP 变更通知中介（Phase 8）
// ============================================================
// mediator 收到后仅记录用于日志/调试，不真改自身 CP（mediator 固定 UTF-8）
// DLL 翻译的 VT 输出始终是 UTF-8 字节流，与目标程序请求的 CP 无关
static void NotifyCpChange() {
    protocol::CpChangePayload p{};
    p.inputCp  = ConsoleState::Instance().GetInputCp();
    p.outputCp = ConsoleState::Instance().GetOutputCp();
    auto pkt = protocol::Serialize(protocol::MessageType::CpChange, &p, sizeof(p));
    SendToMediator(pkt.data(), pkt.size(), protocol::MessageType::CpChange);
    LOG_INFO("ModeHooks: CpChange sent inputCp=%u outputCp=%u",
             p.inputCp, p.outputCp);
}

// ============================================================
// SetConsoleTitleW Hook（Phase 8）
// ============================================================
// 缓存标题 + 发 OSC 0 序列更新 WT 标签页标题
// OSC 0 ; <title> BEL：\x1b]0;<title>\x07
// title 转 UTF-8 后发送（mediator 固定 UTF-8 代码页）
BOOL WINAPI SetConsoleTitleW_Detour(LPCWSTR title) {
    ENSURE_INITIALIZED();

    if (IsInLazyInit()) {
        return SetConsoleTitleW_orig(title);
    }

    std::wstring t = (title != nullptr) ? std::wstring(title) : std::wstring();
    ConsoleState::Instance().SetTitle(t);

    // 转 UTF-8 发 OSC 序列
    std::string utf8;
    if (!t.empty()) {
        int len = WideCharToMultiByte(CP_UTF8, 0, t.c_str(),
                                      static_cast<int>(t.size()),
                                      nullptr, 0, nullptr, nullptr);
        if (len > 0) {
            utf8.resize(static_cast<size_t>(len));
            WideCharToMultiByte(CP_UTF8, 0, t.c_str(),
                                static_cast<int>(t.size()),
                                utf8.data(), len, nullptr, nullptr);
        }
    }
    std::string osc = vt::SetTitleOsc(utf8);
    SendToMediator(osc.data(), osc.size());
    LOG_INFO("ModeHooks: SetConsoleTitleW '%ls' -> %zu bytes OSC",
             t.c_str(), osc.size());
    return TRUE;  // 不调原 API
}

// ============================================================
// SetConsoleTitleA Hook（Phase 8）
// ============================================================
// ANSI 版本：用系统 ACP 转 W 后复用 W 逻辑
// 注意：用 GetACP() 而非 CP_ACP，避免 Hook 后 GetConsoleOutputCP 返回缓存值
//       误导转换（Console CP 与 ANSI CP 是不同概念）
BOOL WINAPI SetConsoleTitleA_Detour(LPCSTR title) {
    ENSURE_INITIALIZED();

    if (IsInLazyInit()) {
        return SetConsoleTitleA_orig(title);
    }

    std::wstring w;
    if (title != nullptr) {
        int len = MultiByteToWideChar(GetACP(), 0, title, -1, nullptr, 0);
        if (len > 0) {
            w.resize(static_cast<size_t>(len - 1));  // 去 \0
            MultiByteToWideChar(GetACP(), 0, title, -1, w.data(), len);
        }
    }
    return SetConsoleTitleW_Detour(w.c_str());
}

// ============================================================
// GetConsoleTitleW Hook（Phase 8）
// ============================================================
// 返回 ConsoleState 缓存的标题
// 返回值：不含结尾 \0 的字符数（与系统 API 一致）
DWORD WINAPI GetConsoleTitleW_Detour(LPWSTR buf, DWORD size) {
    ENSURE_INITIALIZED();

    if (IsInLazyInit()) {
        return GetConsoleTitleW_orig(buf, size);
    }

    if (buf == nullptr || size == 0) return 0;
    auto t = ConsoleState::Instance().GetTitle();
    wcsncpy_s(buf, size, t.c_str(), _TRUNCATE);
    return static_cast<DWORD>(wcslen(buf));
}

// ============================================================
// GetConsoleTitleA Hook（Phase 8）
// ============================================================
// 从 W 缓存转 A（用 GetACP，与 SetConsoleTitleA 对称）
DWORD WINAPI GetConsoleTitleA_Detour(LPSTR buf, DWORD size) {
    ENSURE_INITIALIZED();

    if (IsInLazyInit()) {
        return GetConsoleTitleA_orig(buf, size);
    }

    if (buf == nullptr || size == 0) return 0;
    auto t = ConsoleState::Instance().GetTitle();
    if (t.empty()) {
        buf[0] = '\0';
        return 0;
    }
    int len = WideCharToMultiByte(GetACP(), 0, t.c_str(),
                                  static_cast<int>(t.size()),
                                  buf, static_cast<int>(size), nullptr, nullptr);
    if (len <= 0) {
        buf[0] = '\0';
        return 0;
    }
    // WideCharToMultiByte 不会写结尾 \0（当 cbMultiByte 不含 \0 时）
    if (static_cast<DWORD>(len) < size) {
        buf[len] = '\0';
    } else {
        buf[size - 1] = '\0';
        len = static_cast<int>(size - 1);
    }
    return static_cast<DWORD>(len);
}

// ============================================================
// SetConsoleCP Hook（Phase 8）
// ============================================================
// 缓存 inputCp + 发 CpChange（不调原 API，mediator 固定 UTF-8）
// 目标程序调用 SetConsoleCP(936) 等，DLL 记录但 mediator 不改
//   原因：DLL 翻译的 VT 输出始终是 UTF-8 字节流，与目标程序请求的 CP 无关
//   程序通过 GetConsoleCP 拿到 936 后会按 936 解码读到的字节
//   但 DLL 的 ReadConsoleInputW Hook 返回 wchar_t，不走 CP 转换
BOOL WINAPI SetConsoleCP_Detour(UINT cp) {
    ENSURE_INITIALIZED();

    if (IsInLazyInit()) {
        return SetConsoleCP_orig(cp);
    }

    UINT old = ConsoleState::Instance().GetInputCp();
    ConsoleState::Instance().SetInputCp(cp);
    if (cp != old) {
        NotifyCpChange();
        LOG_INFO("ModeHooks: InputCP %u -> %u", old, cp);
    }
    return TRUE;  // 不调原 API
}

// ============================================================
// SetConsoleOutputCP Hook（Phase 8）
// ============================================================
// 缓存 outputCp + 发 CpChange（不调原 API）
// 目标程序用 GetConsoleOutputCP 拿到此值判断输出编码
//   DLL 的 WriteConsoleW Hook 已把 wchar_t 转 UTF-8 VT，与此值无关
BOOL WINAPI SetConsoleOutputCP_Detour(UINT cp) {
    ENSURE_INITIALIZED();

    if (IsInLazyInit()) {
        return SetConsoleOutputCP_orig(cp);
    }

    UINT old = ConsoleState::Instance().GetOutputCp();
    ConsoleState::Instance().SetOutputCp(cp);
    if (cp != old) {
        NotifyCpChange();
        LOG_INFO("ModeHooks: OutputCP %u -> %u", old, cp);
    }
    return TRUE;  // 不调原 API
}

// ============================================================
// GetConsoleCP Hook（Phase 8）
// ============================================================
UINT WINAPI GetConsoleCP_Detour() {
    ENSURE_INITIALIZED();

    if (IsInLazyInit()) {
        return GetConsoleCP_orig();
    }
    return ConsoleState::Instance().GetInputCp();
}

// ============================================================
// GetConsoleOutputCP Hook（Phase 8）
// ============================================================
UINT WINAPI GetConsoleOutputCP_Detour() {
    ENSURE_INITIALIZED();

    if (IsInLazyInit()) {
        return GetConsoleOutputCP_orig();
    }
    return ConsoleState::Instance().GetOutputCp();
}

// ============================================================
// 注册模式类 Hook
// ============================================================
void RegisterModeHooks() {
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
    entries.push_back({"GetConsoleMode",
        resolve("GetConsoleMode"),
        reinterpret_cast<void*>(&GetConsoleMode_Detour),
        reinterpret_cast<void**>(&GetConsoleMode_orig)});
    entries.push_back({"SetConsoleMode",
        resolve("SetConsoleMode"),
        reinterpret_cast<void*>(&SetConsoleMode_Detour),
        reinterpret_cast<void**>(&SetConsoleMode_orig)});
    // Phase 8：Title
    entries.push_back({"SetConsoleTitleW",
        resolve("SetConsoleTitleW"),
        reinterpret_cast<void*>(&SetConsoleTitleW_Detour),
        reinterpret_cast<void**>(&SetConsoleTitleW_orig)});
    entries.push_back({"SetConsoleTitleA",
        resolve("SetConsoleTitleA"),
        reinterpret_cast<void*>(&SetConsoleTitleA_Detour),
        reinterpret_cast<void**>(&SetConsoleTitleA_orig)});
    entries.push_back({"GetConsoleTitleW",
        resolve("GetConsoleTitleW"),
        reinterpret_cast<void*>(&GetConsoleTitleW_Detour),
        reinterpret_cast<void**>(&GetConsoleTitleW_orig)});
    entries.push_back({"GetConsoleTitleA",
        resolve("GetConsoleTitleA"),
        reinterpret_cast<void*>(&GetConsoleTitleA_Detour),
        reinterpret_cast<void**>(&GetConsoleTitleA_orig)});
    // Phase 8：CP
    entries.push_back({"SetConsoleCP",
        resolve("SetConsoleCP"),
        reinterpret_cast<void*>(&SetConsoleCP_Detour),
        reinterpret_cast<void**>(&SetConsoleCP_orig)});
    entries.push_back({"SetConsoleOutputCP",
        resolve("SetConsoleOutputCP"),
        reinterpret_cast<void*>(&SetConsoleOutputCP_Detour),
        reinterpret_cast<void**>(&SetConsoleOutputCP_orig)});
    entries.push_back({"GetConsoleCP",
        resolve("GetConsoleCP"),
        reinterpret_cast<void*>(&GetConsoleCP_Detour),
        reinterpret_cast<void**>(&GetConsoleCP_orig)});
    entries.push_back({"GetConsoleOutputCP",
        resolve("GetConsoleOutputCP"),
        reinterpret_cast<void*>(&GetConsoleOutputCP_Detour),
        reinterpret_cast<void**>(&GetConsoleOutputCP_orig)});

    for (const auto& e : entries) {
        if (e.target == nullptr) {
            LOG_ERROR("RegisterModeHooks: failed to resolve %s", e.name);
            return;
        }
    }

    HookManager::RegisterBatch(entries);
    LOG_INFO("ModeHooks registered (%zu hooks)", entries.size());
}

} // namespace terminjector::hooks
