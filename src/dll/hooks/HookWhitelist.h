// HookWhitelist：Hook 内可调 API 白名单 + 重入死锁防护
// 详见 docs/phases/10-state-sync-stability.md 4.4
//
// 目标：
//   1. 文档化 Hook Detour 内允许/禁止调用的 Windows API，防止误调触发递归 Hook
//   2. 提供 HookReentryGuard（RAII）+ ASSERT_IN_HOOK 宏，Debug 模式下检测
//      非预期 Hook 嵌套（如 Hook→Logger→WriteFile→Hook 死锁链路）
//   3. 允许设计内的合法 A→W 复用路径（深度=2），仅断言超过阈值的非预期重入
//
// 设计决策：
//   - 用 thread_local 深度计数器而非单一 bool 标志：
//     bool 无法区分"合法 A→W 复用（深度 2）"与"非预期递归（深度 3+）"
//   - 阈值 kMaxReentryDepth=3：允许 1 层正常进入 + 1 层 A→W 复用 + 1 层余量
//     实测合法路径最深=2（A 版 Detour → W 版 Detour），3 已是异常
//   - Release 模式零开销：ASSERT_IN_HOOK 展开为 ((void)0)
//
// 历史教训（project_memory）：
//   - CloseHandle_Detour 的 LOG_INFO 在 DllMain 期间触发 Loader Lock 死锁
//   - Logger worker 线程的 WriteFile 触发 WriteFile_Detour 递归调用
//   这些是 HookWhitelist 要防护的典型场景
#pragma once

#include <windows.h>

namespace terminjector::hooks {

// ============================================================
// 1. Hook 内可调 API 白名单
// ============================================================
//
// 原则：只用 kernel32 的非 Console API，或已确认不触发 Hook 的 API
// 调用前必读：本列表与 src/dll/hooks/*_Detour 的实际调用一一对应
//
// --- 允许的 API（已确认不重入我们 Hook）---
//
// 日志与调试：
//   - OutputDebugStringW                 (Logger；不经过 WriteConsole*)
//   - WriteFile(日志文件句柄)             (Logger worker；句柄是 FILE_TYPE_DISK，
//                                         IsConsoleHandle=false 直接 pass-through)
//   - CreateFileW(日志文件路径)           (Logger 初始化；非 CONOUT$)
//
// 编码与字符串转换（纯计算，无系统调用副作用）：
//   - MultiByteToWideChar / WideCharToMultiByte
//   - std::snprintf / std::wcsncpy_s / std::wcslen / std::memcpy
//
// 同步与等待（非 Console 句柄）：
//   - Sleep                              (轮询；不 Hook)
//   - WaitForSingleObject                (等待非 Console 句柄；Phase 8 仅 Hook
//                                         GetConsoleInputWaitHandle，不 Hook 通用 WaitFor)
//   - AcquireSRWLockExclusive / ReleaseSRWLockExclusive   (SendToMediator 锁)
//   - std::lock_guard / std::mutex        (InputQueue / ConsoleState 内部锁)
//
// 事件与线程同步对象（InputQueue / DllRecvLoop 用）：
//   - CreateEventW / SetEvent / ResetEvent
//   - CreateThread（仅 LazyInit 启动 DllRecvLoop / Logger worker）
//
// 元信息与原子操作（无副作用）：
//   - GetCurrentProcessId / GetCurrentThreadId / GetTickCount64
//   - GetLastError / SetLastError
//   - InterlockedIncrement / InterlockedCompareExchange / InterlockedExchange
//
// 模块与进程操作（ProcessHooks 注入子进程用，非 Console API）：
//   - GetModuleHandleExW / GetModuleFileNameW / GetModuleHandleW / GetProcAddress
//   - VirtualAllocEx / WriteProcessMemory / VirtualFreeEx
//   - CreateRemoteThread / GetExitCodeThread / ResumeThread
//
// 句柄类型判定（IsConsoleHandle 内部用）：
//   - GetFileType                        (返回 FILE_TYPE_CHAR/DISK/PIPE，非 Hook 目标)
//
// 内存分配（CRT）：
//   - HeapAlloc / HeapFree（经 new/delete 间接调用，无 Console 副作用）
//
// --- 禁止的 API（会重入 Hook 目标，不可在 Hook 路径调用）---
//
// 输出类（OutputHooks 目标）：
//   - WriteConsoleW / WriteConsoleA
//   - WriteConsoleOutputW / WriteConsoleOutputA
//   - WriteConsoleOutputCharacterW / WriteConsoleOutputCharacterA
//   - FillConsoleOutputCharacterW / FillConsoleOutputCharacterA
//   - FillConsoleOutputAttribute
//   - ScrollConsoleScreenBufferW / ScrollConsoleScreenBufferA
//   - SetConsoleTextAttribute
//   - WriteFile(CONOUT$ / STDERR 句柄)   (Console 句柄会触发 WriteFile_Detour)
//
// 输入类（InputHooks 目标）：
//   - ReadConsoleInputW / ReadConsoleInputA
//   - ReadConsoleW / ReadConsoleA
//   - PeekConsoleInputW / PeekConsoleInputA
//   - WriteConsoleInputW / WriteConsoleInputA
//   - GetNumberOfConsoleInputEvents
//   - FlushConsoleInputBuffer
//   - ReadFile(CONIN$ 句柄)              (Console 句柄会触发 ReadFile_Detour)
//
// 光标类（CursorHooks 目标）：
//   - SetConsoleCursorPosition / SetConsoleCursorInfo
//   - GetConsoleScreenBufferInfo / GetConsoleCursorInfo
//
// 缓冲区类（BufferHooks 目标）：
//   - SetConsoleScreenBufferSize / SetConsoleWindowInfo
//   - SetConsoleActiveScreenBuffer / CreateConsoleScreenBuffer
//   - GetLargestConsoleWindowSize
//
// 模式与标题类（ModeHooks 目标）：
//   - GetConsoleMode / SetConsoleMode
//   - SetConsoleTitleW / SetConsoleTitleA / GetConsoleTitleW / GetConsoleTitleA
//   - SetConsoleCP / SetConsoleOutputCP / GetConsoleCP / GetConsoleOutputCP
//
// 字体类（FontHooks 目标）：
//   - GetCurrentConsoleFontEx / SetCurrentConsoleFontEx / GetConsoleFontSize
//
// 保护类（ProtectionHooks 目标）：
//   - AllocConsole / AttachConsole / FreeConsole
//   - GetConsoleInputWaitHandle           (WaitHooks 目标)
//
// 进程类（ProcessHooks 目标）：
//   - CreateProcessW / CreateProcessA     (会触发 CreateProcess*_Detour；
//                                         ProcessHooks 用 t_inCreateProcess 防护)
//
// --- 设计内的合法 A→W 复用路径（不属于重入死锁）---
//
// 以下 A 版 Detour 内部转 W 字符串后调用 W 版 Detour，是设计内复用，
// 嵌套深度=2 合法，HookReentryGuard 不会误报：
//   - WriteConsoleA_Detour       → WriteConsoleW_Detour
//   - WriteFile_Detour           → WriteConsoleW_Detour (Console 句柄路径)
//   - FillConsoleOutputCharacterA_Detour → FillConsoleOutputCharacterW_Detour
//   - WriteConsoleOutputCharacterA_Detour → WriteConsoleOutputCharacterW_Detour
//   - SetConsoleTitleA_Detour    → SetConsoleTitleW_Detour
//
// 这些路径中 A 版会先做 MultiByteToWideChar 转换（白名单 API），再调 W 版
// W 版 Detour 会再次 ENSURE_INITIALIZED（幂等）和 IsConsoleHandle（无副作用）

// ============================================================
// 2. Hook 重入深度计数器
// ============================================================
//
// thread_local 深度：记录当前线程 Hook Detour 嵌套层数
//   - 首次进入 Detour：构造 HookReentryGuard，深度 0 → 1
//   - A→W 复用：进入 W 版 Detour，深度 1 → 2（合法）
//   - 非预期重入（如 Logger→WriteFile→Hook）：深度 2 → 3（断言失败）
//
// 注意：CloseHandle_Detour 故意不加 HookReentryGuard
//   原因：DllMain 期间 thread_local 访问会触发 __tls_get_addr，
//         可能卡在 Loader Lock（详见 ProtectionHooks.cpp 文件头注释）
//   影响：CloseHandle_Detour 不计入深度，但其内部不调 Console API，
//         不会引发 Console Hook 重入，断言结果仍准确
namespace reentry_detail {

// Hook Detour 嵌套深度（thread_local，每线程独立）
// inline 保证 header-only 包含时 ODR 合并（C++17 inline 变量）
// 项目最低标准 C++17（src/dll/CMakeLists.txt 已设定），可用 inline 变量
inline thread_local int t_hookDepth = 0;

// 最大允许的 Hook 嵌套深度
//   = 1（首次进入）+ 1（A→W 合法复用）+ 1（余量，允许 Logger 等间接调用一层）
// 超过此值视为非预期重入，Debug 断言失败
// 实测合法路径最深=2，3 已是异常，4+ 必然死锁
constexpr int kMaxReentryDepth = 3;

} // namespace reentry_detail

// ============================================================
// 3. HookReentryGuard：RAII 深度计数守卫
// ============================================================
//
// 用法：在 Hook Detour 函数体首行（ENSURE_INITIALIZED 之后）写：
//   HookReentryGuard guard;
//
// 构造时 ++t_hookDepth，析构时 --t_hookDepth
// 异常路径自动释放（RAII），无需手动管理
//
// 不加的位置（已知例外）：
//   - CloseHandle_Detour：DllMain 期间 thread_local 访问会 Loader Lock
//   - 非Detour 辅助函数（如 IsInputHandleSlow、ConvertRecordsToAnsi）：
//     它们由 Detour 调用，已在 Detour 的 guard 范围内
class HookReentryGuard {
public:
    HookReentryGuard() noexcept {
        ++reentry_detail::t_hookDepth;
    }
    ~HookReentryGuard() noexcept {
        --reentry_detail::t_hookDepth;
    }

    HookReentryGuard(const HookReentryGuard&) = delete;
    HookReentryGuard& operator=(const HookReentryGuard&) = delete;
    HookReentryGuard(HookReentryGuard&&) = delete;
    HookReentryGuard& operator=(HookReentryGuard&&) = delete;

    // 当前线程的 Hook 嵌套深度（调试/日志用）
    static int CurrentDepth() noexcept { return reentry_detail::t_hookDepth; }
};

// ============================================================
// 4. CheckNotInReentry：检查当前线程是否在非预期 Hook 重入中
// ============================================================
//
// 返回 true 表示当前深度合法（≤ kMaxReentryDepth）
// 返回 false 表示发生非预期重入（可能死锁）
//
// 被 ASSERT_IN_HOOK 宏调用；也可在关键路径（如 SendToMediator）手动调用
inline bool CheckNotInReentry() noexcept {
    return reentry_detail::t_hookDepth <= reentry_detail::kMaxReentryDepth;
}

// ============================================================
// 5. ASSERT_IN_HOOK 宏：Debug 模式重入断言
// ============================================================
//
// 放置位置：Hook Detour 入口（ENSURE_INITIALIZED 之后、HookReentryGuard 之前）
// 语义：断言"进入本 Detour 时，当前线程未处于非预期 Hook 重入状态"
//
// 失败行为（仅 Debug）：
//   - OutputDebugStringW 输出诊断信息（不经过 WriteConsole*，无重入风险）
//   - DebugBreak 触发断点，让开发者排查调用栈
//
// Release 模式：展开为 ((void)0)，零开销
//
// 注意：本宏检测的是"进入时"的深度，HookReentryGuard 在其后才递增深度
//       因此首次进入 Detour 时深度=0，A→W 复用进入 W 版时深度=1，均通过
//       非预期重入进入时深度≥kMaxReentryDepth，断言失败
#ifdef _DEBUG
#define ASSERT_IN_HOOK()                                                  \
    do {                                                                  \
        if (!::terminjector::hooks::CheckNotInReentry()) {                \
            ::OutputDebugStringW(                                         \
                L"[terminjector] ASSERT_IN_HOOK failed: hook reentry "    \
                L"depth exceeded, potential deadlock");                   \
            ::DebugBreak();                                               \
        }                                                                 \
    } while (0)
#else
#define ASSERT_IN_HOOK() ((void)0)
#endif

} // namespace terminjector::hooks
