// 输入类 Console API Hook 实现
// 详见 docs/phases/06-input-chain.md 4.3/4.4/4.6/4.7
//
// 流程：
//   1. ENSURE_INITIALIZED() 触发懒加载
//   2. LazyInit 期间调原始 API（避免递归）
//   3. 非 Console 输入句柄（stdout/文件等）pass-through 调原 API
//   4. Read 类：从 InputQueue 出队返回（阻塞等待事件信号）
//   5. Peek 类：从 InputQueue 偷窥返回（不阻塞）
//   6. Write 类：入队到 InputQueue（程序主动塞事件）
//   7. Flush 类：清空 InputQueue
//   8. ReadFile(stdin)：透传模式（ENABLE_VIRTUAL_TERMINAL_INPUT）
//
// 阻塞退出策略（文档 6 风险点）：
//   ReadConsoleInput 阻塞等待时用 100ms 超时轮询，检查 transport 连接状态
//   管道断开时返回 FALSE，避免永久阻塞
#include "InputHooks.h"
#include "HookCommon.h"
#include "HookWhitelist.h"
#include "../HookManager.h"
#include "../state/ConsoleState.h"
#include "../state/InputQueue.h"
#include "../LazyInit.h"
#include "../Unloader.h"
#include "../lineedit/LineEditor.h"
#include "../translator/VtEscape.h"
#include "transport/ITransport.h"
#include "logging/Logger.h"

#include <windows.h>
#include <string>
#include <vector>
#include <cstring>
#include <atomic>

namespace terminjector::hooks {

// ============================================================
// 临时诊断：全局输入路径探针计数器（排查 Python 双 >>>，验证后移除）
// ============================================================
// 用原子计数器而非 thread_local：Python 可能在任意线程调用，
// thread_local 会在新线程重置导致漏日志。前 N 次调用必日志，
// 覆盖所有可能的输入路径，确认 Python 实际走哪个 Detour。
static std::atomic<int> g_readConsoleInputW_calls{0};
static std::atomic<int> g_readConsoleInputA_calls{0};
static std::atomic<int> g_readConsoleW_calls{0};
static std::atomic<int> g_readConsoleA_calls{0};
static std::atomic<int> g_readFile_stdin_calls{0};
static constexpr int kDiagLogLimit = 2000;

// ============================================================
// ReadDetourGuard：RAII 跟踪 Read 类 Detour 线程数（Phase 11 防 AV 崩溃）
// ============================================================
// 背景：Unloader 在 UninstallAll 前需等待所有 Read 类 Detour 线程离开 DLL 代码，
//       否则 MH_RemoveHook 释放 trampoline 时，线程调用 *_orig 会 AV 0xC0000005
//
// 使用方式：
//   ENSURE_INITIALIZED() 后构造 guard（计数++）；
//   pass-through 调 orig 前 release()（计数--，让 Unloader 可继续 UninstallAll）；
//   正常返回路径由析构自动 --。
//
// 关键：return 语句的表达式执行时局部变量还未析构，
//       若 orig 调用过程中 Uninstaller 执行 UninstallAll 会释放 trampoline → AV。
//       release() 让计数先归零，Unloader 看到 count==0 才执行 UninstallAll，
//       此时 orig（trampoline）仍有效（RemoveHook 还没执行）。
class ReadDetourGuard {
public:
    ReadDetourGuard() { Unloader::EnterReadDetour(); }
    ~ReadDetourGuard() {
        if (m_active) Unloader::LeaveReadDetour();
    }
    // 手动释放计数（pass-through 调 orig 前调用）
    void release() {
        if (m_active) {
            Unloader::LeaveReadDetour();
            m_active = false;
        }
    }
private:
    bool m_active = true;
};

// ============================================================
// 原函数指针定义
// ============================================================
DEFINE_ORIG_PTR(ReadConsoleInputW, BOOL WINAPI(HANDLE, PINPUT_RECORD, DWORD, LPDWORD));
DEFINE_ORIG_PTR(ReadConsoleInputA, BOOL WINAPI(HANDLE, PINPUT_RECORD, DWORD, LPDWORD));
DEFINE_ORIG_PTR(PeekConsoleInputW, BOOL WINAPI(HANDLE, PINPUT_RECORD, DWORD, LPDWORD));
DEFINE_ORIG_PTR(PeekConsoleInputA, BOOL WINAPI(HANDLE, PINPUT_RECORD, DWORD, LPDWORD));
DEFINE_ORIG_PTR(GetNumberOfConsoleInputEvents, BOOL WINAPI(HANDLE, LPDWORD));
DEFINE_ORIG_PTR(WriteConsoleInputW, BOOL WINAPI(HANDLE, const INPUT_RECORD*, DWORD, LPDWORD));
DEFINE_ORIG_PTR(WriteConsoleInputA, BOOL WINAPI(HANDLE, const INPUT_RECORD*, DWORD, LPDWORD));
DEFINE_ORIG_PTR(FlushConsoleInputBuffer, BOOL WINAPI(HANDLE));
DEFINE_ORIG_PTR(ReadFile, BOOL WINAPI(HANDLE, LPVOID, DWORD, LPDWORD, LPOVERLAPPED));
// Phase 6 修正：cmd.exe 用 ReadConsoleW 读取行输入（非 ReadConsoleInputW）
// 文档 06-input-chain.md 4.3 遗漏此 API，实测 cmd 输入失效，补 Hook
DEFINE_ORIG_PTR(ReadConsoleW, BOOL WINAPI(HANDLE, LPVOID, DWORD, LPDWORD, PCONSOLE_READCONSOLE_CONTROL));
DEFINE_ORIG_PTR(ReadConsoleA, BOOL WINAPI(HANDLE, LPVOID, DWORD, LPDWORD, PCONSOLE_READCONSOLE_CONTROL));

// ============================================================
// 辅助：获取缓存的 stdin 句柄
// ============================================================
// ReadFile Hook 快速路径用：非 stdin 直接 pass-through，避免 ENSURE_INITIALIZED 开销
// C++11 保证 static 变量初始化线程安全
static HANDLE GetCachedStdin() {
    static HANDLE s_stdin = GetStdHandle(STD_INPUT_HANDLE);
    return s_stdin;
}

// ============================================================
// IsInputHandleSlow：IsInputHandle 慢路径实现
// ============================================================
// 用途：识别 CreateFileW("CONIN$"/"CONOUT$") 直接打开的句柄（非 GetStdHandle 缓存）
//
// 判据：调真实 GetNumberOfConsoleInputEvents（通过 trampoline 绕过 Hook）
//   - 返回 TRUE  → input 句柄（CONIN$）
//   - 返回 FALSE → output 句柄（CONOUT$）或非 Console 句柄
//
// 这与 CPython _get_console_type 使用同一判据，语义最可靠
// （详见 HookCommon.h IsInputHandle 注释中的历史教训：模式位判断不可行）
//
// Hook 未安装时（GetNumberOfConsoleInputEvents_orig == nullptr）退化为直接调系统 API
bool IsInputHandleSlow(HANDLE h) {
    DWORD cnt = 0;
    if (GetNumberOfConsoleInputEvents_orig != nullptr) {
        return GetNumberOfConsoleInputEvents_orig(h, &cnt);
    }
    // Hook 未安装（DllMain 早期 / RegisterInputHooks 前）：直接调系统 API
    return GetNumberOfConsoleInputEvents(h, &cnt);
}

// ============================================================
// 辅助：检查 transport 是否已连接（管道断开时让 ReadConsoleInput 退出）
// ============================================================
static bool IsTransportConnected() {
    ITransport* t = GetMediatorTransport();
    return t != nullptr && t->IsConnected();
}

// ============================================================
// 辅助：A 版本字符转换
// ============================================================
// ReadConsoleInputA：把出队的 W 版 INPUT_RECORD 的 uChar.UnicodeChar → AsciiChar
static void ConvertRecordsToAnsi(INPUT_RECORD* records, DWORD count) {
    for (DWORD i = 0; i < count; ++i) {
        if (records[i].EventType == KEY_EVENT) {
            wchar_t wch = records[i].Event.KeyEvent.uChar.UnicodeChar;
            char ach = 0;
            if (wch != 0) {
                WideCharToMultiByte(CP_ACP, 0, &wch, 1, &ach, 1, nullptr, nullptr);
            }
            records[i].Event.KeyEvent.uChar.AsciiChar = ach;
        }
    }
}

// WriteConsoleInputA：把 A 版 INPUT_RECORD 的 uChar.AsciiChar → UnicodeChar
static void ConvertRecordsFromAnsi(INPUT_RECORD* records, DWORD count) {
    for (DWORD i = 0; i < count; ++i) {
        if (records[i].EventType == KEY_EVENT) {
            char ach = records[i].Event.KeyEvent.uChar.AsciiChar;
            wchar_t wch = 0;
            if (ach != 0) {
                MultiByteToWideChar(CP_ACP, 0, &ach, 1, &wch, 1);
            }
            records[i].Event.KeyEvent.uChar.UnicodeChar = wch;
        }
    }
}



// ============================================================
// ReadConsoleInputW Hook（核心）
// ============================================================
// 阻塞读按键/鼠标事件：从 InputQueue 出队
// 队列空时 WaitForSingleObject 等待事件信号（100ms 超时 + 连接检查）
BOOL WINAPI ReadConsoleInputW_Detour(HANDLE h, PINPUT_RECORD buf,
                                      DWORD count, LPDWORD read) {
    // 临时诊断：入口探针（ENSURE_INITIALIZED 前记录，确保即使 init 异常也能看到）
    int callId = g_readConsoleInputW_calls.fetch_add(1);
    bool log = (callId < kDiagLogLimit);
    if (log) {
        LOG_INFO("ReadConsoleInputW_Detour: ENTRY #%d tid=%lu h=%p count=%lu",
                 callId, GetCurrentThreadId(), h, count);
    }

    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    // ReadDetourGuard：进入 Detour 计数++，pass-through 调 orig 前 release（--）
    // 让 Unloader 在 UninstallAll 前等待所有 Read 类 Detour 线程离开 DLL 代码
    // Phase 22 修复：guard 提前到 LazyInit 检查之前——LazyInit pass-through
    // 的线程可能阻塞在原 API（如 cmd 主线程 ReadConsoleW_orig），
    // 若不计数，卸载时该线程返回路径（DLL 代码）已被 FreeLibrary 释放 → AV
    ReadDetourGuard readGuard;

    // LazyInit 期间调原始 API（避免递归）
    if (IsInLazyInit()) {
        if (log) LOG_INFO("ReadConsoleInputW_Detour: #%d LazyInit pass-through", callId);
        return ReadConsoleInputW_orig(h, buf, count, read);
    }

    if (!IsInputHandle(h)) {
        if (log) LOG_INFO("ReadConsoleInputW_Detour: #%d not input handle, pass-through orig", callId);
        readGuard.release();
        return ReadConsoleInputW_orig(h, buf, count, read);
    }

    auto& queue = InputQueue::Instance();
    // 单次轮询：出队 → 空则最多等 100ms 后返回空（TRUE + *read=0）
    // 返回空而非无限阻塞的原因（Textual 退出卡死 BUG 修复）：
    //   原实现队列空时无限循环（只检查 transport/unloader），调用方主循环被
    //   永久霸占，无法检查自身退出条件——Textual 事件线程在
    //   wait_for_handles([hIn],100) 返回后调本 API，队列空即陷入无限循环，
    //   Ctrl+Q 后 exit_event 永不被检查 → _event_thread.join() 卡死 → 进程永不退出。
    //   现在空队列时最多等 100ms 即返回空，调用方回到自己的主循环；
    //   配合 WaitHooks 的 WaitForMultipleObjects* 替换（输入句柄 → InputQueue
    //   事件），空队列时等待类 API 正常超时轮询，调用方节奏由自身决定。
    //   对 cmd 等直接循环调 ReadConsoleInputW 的程序：获得约 10Hz 节流（可接受），
    //   且能及时感知 mediator 断开（返回空 → 再调 → 连接检查命中）。
    {
        size_t n = queue.DequeueRecords(buf, static_cast<size_t>(count));

        if (n > 0) {
                        *read = static_cast<DWORD>(n);
            if (log) {
                WORD vk = (buf[0].EventType == KEY_EVENT)
                          ? buf[0].Event.KeyEvent.wVirtualKeyCode : 0;
                wchar_t ch = (buf[0].EventType == KEY_EVENT)
                             ? buf[0].Event.KeyEvent.uChar.UnicodeChar : 0;
                // 打印事件类型，区分 MOUSE_EVENT / KEY_EVENT / 其他
                // (MOUSE_EVENT 无 vk/ch，只靠 EventType 辨识；坐标转 1-based SGR)
                if (buf[0].EventType == MOUSE_EVENT) {
                    LOG_INFO("ReadConsoleInputW_Det: #%d tid=%lu return n=%zu type=MOUSE btn=0x%x flags=0x%x pos=(%d,%d)",
                             callId, GetCurrentThreadId(), n,
                             buf[0].Event.MouseEvent.dwButtonState,
                             buf[0].Event.MouseEvent.dwEventFlags,
                             buf[0].Event.MouseEvent.dwMousePosition.X,
                             buf[0].Event.MouseEvent.dwMousePosition.Y);
                } else {
                    LOG_INFO("ReadConsoleInputW_Det: #%d return n=%zu type=0x%x vk=0x%x ch=0x%x",
                             callId, n, buf[0].EventType, vk, ch);
                }
            }
            return TRUE;
        }
        // 队列空：检查连接状态 / 卸载标志
        // transport 断开 或 Unloader 启动：pass-through 到 orig，让调用方走 ConHost 输入路径
        // 不返回 FALSE 的原因：
        //   1. cmd 收到 ReadConsoleW 失败会退出（Phase 11 验收 2 要求 cmd 仍可交互）
        //   2. 调用栈停留在 DLL 代码内会阻塞 LDR 卸载（LoadCount 无法归零）
        //   pass-through 后线程阻塞在 kernelbase.dll 的原 ReadConsoleInputW，
        //   不在 DLL 代码内，LDR 可正常卸载
        // 关键：release 先于 orig 调用，让 Unloader 看到 count==0 才 UninstallAll
        if (!IsTransportConnected() || Unloader::IsUnloading()) {
            if (log) LOG_INFO("ReadConsoleInputW_Detour: #%d transport disconnected/unloading, pass-through to orig", callId);
            // Phase 22：不再 release()。阻塞在 orig 的线程必须保持计数，
            // Unloader 等计数归零（KickStart 唤醒）后才 FreeLibrary，
            // 否则线程从 orig 返回时走 DLL 代码（已卸载）→ AV
            return ReadConsoleInputW_orig(h, buf, count, read);
        }
        if (log) {
            LOG_INFO("ReadConsoleInputW_Detour: #%d queue empty, wait up to 100ms then return empty", callId);
        }
        // 等待数据到达（最多 100ms，超时返回空让调用方检查自身状态）
        WaitForSingleObject(queue.GetWaitHandle(), 100);
        *read = 0;
        return TRUE;
    }
}

// ============================================================
// ReadConsoleInputA Hook
// ============================================================
// 同 W 版本，出队后把 UnicodeChar 转为 AsciiChar
BOOL WINAPI ReadConsoleInputA_Detour(HANDLE h, PINPUT_RECORD buf,
                                      DWORD count, LPDWORD read) {
    // 临时诊断：入口探针
    int callId = g_readConsoleInputA_calls.fetch_add(1);
    bool log = (callId < kDiagLogLimit);
    if (log) {
        LOG_INFO("ReadConsoleInputA_Detour: ENTRY #%d h=%p count=%lu", callId, h, count);
    }

    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    // ReadDetourGuard：同 ReadConsoleInputW_Detour（Phase 22：提前到 LazyInit 检查前）
    ReadDetourGuard readGuard;

    if (IsInLazyInit()) {
        if (log) LOG_INFO("ReadConsoleInputA_Detour: #%d LazyInit pass-through", callId);
        return ReadConsoleInputA_orig(h, buf, count, read);
    }

    if (!IsInputHandle(h)) {
        if (log) LOG_INFO("ReadConsoleInputA_Detour: #%d not input handle, pass-through", callId);
        readGuard.release();
        return ReadConsoleInputA_orig(h, buf, count, read);
    }

    auto& queue = InputQueue::Instance();
    // 单次轮询：同 ReadConsoleInputW_Detour（空队列最多等 100ms 后返回空，
    // 不再无限阻塞——调用方主循环需能检查自身退出条件）
    {
        size_t n = queue.DequeueRecords(buf, static_cast<size_t>(count));

        if (n > 0) {
            ConvertRecordsToAnsi(buf, static_cast<DWORD>(n));
            *read = static_cast<DWORD>(n);
            return TRUE;
        }
        // transport 断开 或 Unloader 启动：pass-through 到 orig（同 ReadConsoleInputW_Detour 理由）
        if (!IsTransportConnected() || Unloader::IsUnloading()) {
            if (log) LOG_INFO("ReadConsoleInputA_Detour: #%d transport disconnected/unloading, pass-through to orig", callId);
            // Phase 22：不再 release()（见 ReadConsoleInputW_Detour 注释）
            return ReadConsoleInputA_orig(h, buf, count, read);
        }
        WaitForSingleObject(queue.GetWaitHandle(), 100);
        *read = 0;
        return TRUE;
    }
}

// ============================================================
// PeekConsoleInputW Hook
// ============================================================
// 偷窥队列（不消费）：从 InputQueue.PeekRecords 返回
BOOL WINAPI PeekConsoleInputW_Detour(HANDLE h, PINPUT_RECORD buf,
                                      DWORD count, LPDWORD read) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (IsInLazyInit()) {
        return PeekConsoleInputW_orig(h, buf, count, read);
    }

    if (!IsInputHandle(h)) {
        return PeekConsoleInputW_orig(h, buf, count, read);
    }

    size_t n = InputQueue::Instance().PeekRecords(buf, static_cast<size_t>(count));

    *read = static_cast<DWORD>(n);
    return TRUE;
}

// ============================================================
// PeekConsoleInputA Hook
// ============================================================
BOOL WINAPI PeekConsoleInputA_Detour(HANDLE h, PINPUT_RECORD buf,
                                      DWORD count, LPDWORD read) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (IsInLazyInit()) {
        return PeekConsoleInputA_orig(h, buf, count, read);
    }

    if (!IsInputHandle(h)) {
        return PeekConsoleInputA_orig(h, buf, count, read);
    }

    size_t n = InputQueue::Instance().PeekRecords(buf, static_cast<size_t>(count));

    if (n > 0) {
        ConvertRecordsToAnsi(buf, static_cast<DWORD>(n));
    }
    *read = static_cast<DWORD>(n);
    return TRUE;
}

// ============================================================
// GetNumberOfConsoleInputEvents Hook
// ============================================================
// 返回 InputQueue 中 INPUT_RECORD 队列长度
BOOL WINAPI GetNumberOfConsoleInputEvents_Detour(HANDLE h, LPDWORD count) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (IsInLazyInit()) {
        return GetNumberOfConsoleInputEvents_orig(h, count);
    }

    if (!IsInputHandle(h)) {
        return GetNumberOfConsoleInputEvents_orig(h, count);
    }

    *count = static_cast<DWORD>(InputQueue::Instance().RecordCount());
    return TRUE;
}

// ============================================================
// WriteConsoleInputW Hook
// ============================================================
// 程序主动塞事件：入队到 InputQueue（让后续 ReadConsoleInput 能读到）
// 不调原 API（ConHost 输入缓冲区不被使用）
BOOL WINAPI WriteConsoleInputW_Detour(HANDLE h, const INPUT_RECORD* buf,
                                       DWORD count, LPDWORD written) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (IsInLazyInit()) {
        return WriteConsoleInputW_orig(h, buf, count, written);
    }

    if (!IsInputHandle(h)) {
        return WriteConsoleInputW_orig(h, buf, count, written);
    }

    InputQueue::Instance().EnqueueRecords(buf, static_cast<size_t>(count));
    if (written != nullptr) *written = count;
    return TRUE;
}

// ============================================================
// WriteConsoleInputA Hook
// ============================================================
// 同 W 版本，入队前把 AsciiChar 转为 UnicodeChar
BOOL WINAPI WriteConsoleInputA_Detour(HANDLE h, const INPUT_RECORD* buf,
                                       DWORD count, LPDWORD written) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (IsInLazyInit()) {
        return WriteConsoleInputA_orig(h, buf, count, written);
    }

    if (!IsInputHandle(h)) {
        return WriteConsoleInputA_orig(h, buf, count, written);
    }

    // 复制一份并转换 A→W
    std::vector<INPUT_RECORD> records(buf, buf + count);
    ConvertRecordsFromAnsi(records.data(), count);
    InputQueue::Instance().EnqueueRecords(records.data(), records.size());
    if (written != nullptr) *written = count;
    return TRUE;
}

// ============================================================
// FlushConsoleInputBuffer Hook
// ============================================================
// 清空 DLL 内部队列（不清中介发来的，mediator 侧 stdin 仍有数据，下次会重发）
BOOL WINAPI FlushConsoleInputBuffer_Detour(HANDLE h) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (IsInLazyInit()) {
        return FlushConsoleInputBuffer_orig(h);
    }

    if (!IsInputHandle(h)) {
        return FlushConsoleInputBuffer_orig(h);
    }

    InputQueue::Instance().Clear();
    return TRUE;
}

// ============================================================
// ReadConsoleW Hook（cmd 行输入核心 + LineEditor 行编辑）
// ============================================================
// cmd.exe 用 ReadConsoleW 读取行输入（非 ReadConsoleInputW）
//
// 行编辑集成（Phase 6 扩展）：
//   - 原 ConHost 在 ENABLE_LINE_INPUT 模式下做行编辑（退格/方向键/Tab/历史）
//   - 我们 Hook 后 ConHost 不参与，由 DLL 内 LineEditor 实现等价行为
//   - LineEditor 维护行缓冲 + 光标，生成 VT 回显发给 mediator → WT 渲染
//   - Enter 完成行：lineOut 返回行内容（不含 \r\n），调用方补 \r\n
//   - Ctrl+C：输出 ^C\r\n，返回空行
//
// ReadConsoleW 语义：返回的行包含尾部 \r\n（若 buf 有空间）
BOOL WINAPI ReadConsoleW_Detour(HANDLE h, LPVOID buf, DWORD len,
                                 LPDWORD read, PCONSOLE_READCONSOLE_CONTROL ctrl) {
    // 临时诊断：入口探针（Python REPL 疑似走此路径，重点观测）
    int callId = g_readConsoleW_calls.fetch_add(1);
    bool log = (callId < kDiagLogLimit);
    if (log) {
        LOG_INFO("ReadConsoleW_Detour: ENTRY #%d tid=%lu h=%p len=%lu",
                 callId, GetCurrentThreadId(), h, len);
    }

    ENSURE_INITIALIZED();
    ASSERT_IN_HOOK();          // 关键 Detour：行编辑主路径，Python REPL 走此
    HookReentryGuard guard;

    // ReadDetourGuard：进入 Detour 计数++，pass-through 调 orig 前 release（--）
    // 让 Unloader 在 FreeLibrary 前等待所有 Read 类 Detour 线程离开 DLL 代码
    // Phase 22 修复：guard 提前到 LazyInit 检查之前（LazyInit pass-through
    // 可能阻塞在原 API，卸载时返回路径在已卸载 DLL → AV）
    // 关键：不再 release()——阻塞在 orig 的线程保持计数，Unloader 等计数归零
    // （持续 KickStart 唤醒）后才卸载 DLL
    ReadDetourGuard readGuard;

    if (IsInLazyInit()) {
        if (log) LOG_INFO("ReadConsoleW_Detour: #%d LazyInit pass-through", callId);
        return ReadConsoleW_orig(h, buf, len, read, ctrl);
    }

    if (!IsInputHandle(h) || buf == nullptr || read == nullptr) {
        if (log) LOG_INFO("ReadConsoleW_Detour: #%d skip (notInput=%d bufNull=%d readNull=%d), pass-through",
                          callId, !IsInputHandle(h), buf == nullptr, read == nullptr);
        readGuard.release();
        return ReadConsoleW_orig(h, buf, len, read, ctrl);
    }

    // 从 ConsoleState 获取回显开关（ENABLE_ECHO_INPUT）
    auto& state = ConsoleState::Instance();
    bool echoEnabled = (state.GetInputMode() & ENABLE_ECHO_INPUT) != 0;

    wchar_t* wbuf = static_cast<wchar_t*>(buf);

    auto& queue = InputQueue::Instance();

    // ---- 非行编辑模式（ENABLE_LINE_INPUT 未设置）：直接返回字符 ----
    // Textual 等 TUI 程序通过 ReadConsoleW 读取单字符（如 Python msvcrt.getwch()），
    // 不使用 LineEditor 行编辑。若走 LineEditor 路径，Ctrl+Q(0x11) 等控制字符会
    // 被追加到行缓冲区永不返回，导致程序无法响应退出快捷键。
    if ((state.GetInputMode() & ENABLE_LINE_INPUT) == 0) {
        if (log) LOG_INFO("ReadConsoleW_Detour: #%d non-line-input mode, reading character directly", callId);
        while (true) {
            INPUT_RECORD rec;
            size_t n = queue.DequeueRecords(&rec, 1);
            if (n > 0) {
                if (rec.EventType == KEY_EVENT && rec.Event.KeyEvent.bKeyDown) {
                    wchar_t wc = rec.Event.KeyEvent.uChar.UnicodeChar;
                    if (wc != L'\0') {
                        wbuf[0] = wc;
                        *read = 1;
                        if (log) LOG_INFO("ReadConsoleW_Detour: #%d non-line-input return wc=0x%x", callId, wc);
                        return TRUE;
                    }
                }
                // 非 KEY_EVENT 或空字符：跳过，继续读下一个
                continue;
            }
            // 队列空：检查 transport 断开或卸载
            if (!IsTransportConnected() || Unloader::IsUnloading()) {
                if (log) LOG_INFO("ReadConsoleW_Detour: #%d non-line-input transport disconnected/unloading, pass-through to orig", callId);
                // Phase 22：不再 release()（阻塞线程保持计数，见函数头注释）
                return ReadConsoleW_orig(h, buf, len, read, ctrl);
            }
            WaitForSingleObject(queue.GetWaitHandle(), 100);
        }
    }

    // ---- 行编辑模式（ENABLE_LINE_INPUT 已设置）：LineEditor 行编辑 ----
    auto& editor = LineEditor::Instance();
    // 开始新的行编辑会话：重置行缓冲、光标、历史导航状态
    editor.BeginSession();
    LOG_INFO("ReadConsoleW_Detour: BeginSession done, echo=%d", echoEnabled);

    // 行编辑临时变量（提升到函数作用域避免与 TLS 基址缓存栈布局冲突，
    // 详见 Phase 11 诊断：std::wstring/string 在循环内构造时其 SSO 缓冲区
    // 可能覆盖编译器缓存的 TLS 基址 [rbp-71h]，导致 HookReentryGuard 析构
    // 时 --t_hookDepth 写入错误地址，损坏 GS cookie 引发 STATUS_STACK_BUFFER_OVERRUN）
    std::wstring lineOut;
    std::string vtOut;

    // 行编辑主循环：dequeue 按键 → LineEditor 处理 → 回显 → 行完成返回
    while (true) {
        INPUT_RECORD rec;
        size_t n;
        // 等一个事件（100ms 超时 + 连接检查）
        while (true) {
            n = queue.DequeueRecords(&rec, 1);
            if (n > 0) break;
            // transport 断开 或 Unloader 启动：pass-through 到 orig
            // 关键：必须 pass-through 而非返回 FALSE
            //   - 返回 FALSE 会让 cmd 收到 ReadConsoleW 失败而退出（Phase 11 验收 2 要求 cmd 仍可交互）
            //   - 调用栈停留在 DLL 代码内（本函数循环）会阻塞 LDR 卸载，
            //     助手进程远程 FreeLibrary 时 LoadCount 因线程在 DLL 内无法归零
            //   pass-through 后线程阻塞在 kernelbase.dll 原 ReadConsoleW，
            //   不在 DLL 代码内，LDR 可正常卸载，cmd 也能从 ConHost 读取用户输入
            //   release 先于 orig 调用，让 Unloader 看到 count==0 才 UninstallAll
            if (!IsTransportConnected() || Unloader::IsUnloading()) {
                if (log) LOG_INFO("ReadConsoleW_Detour: #%d transport disconnected/unloading, pass-through to orig", callId);
                // Phase 22：不再 release()（阻塞线程保持计数，见函数头注释）
                return ReadConsoleW_orig(h, buf, len, read, ctrl);
            }
            WaitForSingleObject(queue.GetWaitHandle(), 100);
        }

        // 诊断日志：记录收到的每个事件
        LOG_INFO("ReadConsoleW_Detour: event type=%d keyDown=%d vk=%d ch=0x%x",
                 rec.EventType, rec.Event.KeyEvent.bKeyDown,
                 rec.Event.KeyEvent.wVirtualKeyCode,
                 rec.Event.KeyEvent.uChar.UnicodeChar);

        // 仅处理按键按下事件（释放事件忽略）
        if (rec.EventType != KEY_EVENT || !rec.Event.KeyEvent.bKeyDown) {
            continue;
        }

        // 重用临时变量（清空内容，保留 SSO 缓冲区，避免在循环内反复构造/析构）
        lineOut.clear();
        vtOut.clear();

        // 交给 LineEditor 处理按键
        bool done = editor.ProcessKey(rec.Event.KeyEvent, echoEnabled, lineOut, vtOut);

        LOG_INFO("ReadConsoleW_Detour: ProcessKey done=%d vtLen=%zu lineLen=%zu",
                 done, vtOut.size(), lineOut.size());

        // 发送 VT 回显（按键回显、行重绘、\r\n 等）给 mediator → WT 渲染
        if (!vtOut.empty()) {
            // Phase 21：子进程行编辑回显前补发 CursorPosition
            // 父 cmd 启动回显会偏移共享 ConPTY 光标，行编辑相对定位（\r/CSI D）
            // 从错位位置开始，回车后光标多一行（long_line_enter 失败）。
            // 仅子进程：目标进程 cmd 的行编辑回显已与 ConPTY 对齐。
            // 补发用独立消息类型 CursorSync 即时发送（不经 BatchSender），
            // 保证回显内容消息字节原样（modes 测试精确断言 hex）。
            if (!IsTargetProcess()) {
                COORD cur = editor.GetCurrentUiCursor();
                if (cur.X < 0) cur.X = 0;
                if (cur.Y < 0) cur.Y = 0;
                std::string sync = vt::CursorPosition(cur.Y + 1, cur.X + 1);
                SendToMediator(sync.data(), sync.size(),
                               protocol::MessageType::CursorSync);
            }
            SendToMediator(vtOut.data(), vtOut.size());
        }

        if (done) {
            // 行完成（Enter 或 Ctrl+C）：复制行内容到 buf，追加 \r\n
            DWORD lineLen = static_cast<DWORD>(lineOut.size());
            // 缓冲区不足时截断（ReadConsoleW 语义）
            DWORD copyLen = (lineLen < len) ? lineLen : len;
            for (DWORD i = 0; i < copyLen; ++i) {
                wbuf[i] = lineOut[i];
            }
            DWORD collected = copyLen;

            // 行非空且 buf 还有 2 字节空间：补 \r\n（ReadConsoleW 语义）
            // Ctrl+C 时 lineOut 为空（vtOut 已含 ^C\r\n），不补 \r\n
            if (copyLen > 0 && collected + 2 <= len) {
                wbuf[collected++] = L'\r';
                wbuf[collected++] = L'\n';
            }
            *read = collected;
            LOG_DEBUG("ReadConsoleW_Detour: return %lu chars", collected);
            return TRUE;
        }
    }
}

// ============================================================
// ReadConsoleA Hook（行编辑集成版）
// ============================================================
// 同 W 版本逻辑，LineEditor 返回 wchar_t 行后转为 ANSI 填入 buf
// 行编辑由 LineEditor 统一处理，A 版本仅做 W→A 编码转换
BOOL WINAPI ReadConsoleA_Detour(HANDLE h, LPVOID buf, DWORD len,
                                 LPDWORD read, PCONSOLE_READCONSOLE_CONTROL ctrl) {
    // 临时诊断：入口探针
    int callId = g_readConsoleA_calls.fetch_add(1);
    bool log = (callId < kDiagLogLimit);
    if (log) {
        LOG_INFO("ReadConsoleA_Detour: ENTRY #%d h=%p len=%lu", callId, h, len);
    }

    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    // ReadDetourGuard：同 ReadConsoleW_Detour（Phase 22：提前到 LazyInit 检查前，
    // 且阻塞型 pass-through 不再 release，见 ReadConsoleW_Detour 头注释）
    ReadDetourGuard readGuard;

    if (IsInLazyInit()) {
        if (log) LOG_INFO("ReadConsoleA_Detour: #%d LazyInit pass-through", callId);
        return ReadConsoleA_orig(h, buf, len, read, ctrl);
    }

    if (!IsInputHandle(h) || buf == nullptr || read == nullptr) {
        if (log) LOG_INFO("ReadConsoleA_Detour: #%d skip, pass-through", callId);
        readGuard.release();
        return ReadConsoleA_orig(h, buf, len, read, ctrl);
    }

    // 从 ConsoleState 获取回显开关（ENABLE_ECHO_INPUT）
    auto& state = ConsoleState::Instance();
    bool echoEnabled = (state.GetInputMode() & ENABLE_ECHO_INPUT) != 0;

    auto& queue = InputQueue::Instance();

    // ---- 非行编辑模式（ENABLE_LINE_INPUT 未设置）：直接返回字符 ----
    // 同 ReadConsoleW_Detour 非行编辑模式处理
    if ((state.GetInputMode() & ENABLE_LINE_INPUT) == 0) {
        if (log) LOG_INFO("ReadConsoleA_Detour: #%d non-line-input mode, reading character directly", callId);
        while (true) {
            INPUT_RECORD rec;
            size_t n = queue.DequeueRecords(&rec, 1);
            if (n > 0) {
                if (rec.EventType == KEY_EVENT && rec.Event.KeyEvent.bKeyDown) {
                    wchar_t wc = rec.Event.KeyEvent.uChar.UnicodeChar;
                    if (wc != L'\0') {
                        // wchar_t → ANSI 单字节
                        char ach = 0;
                        WideCharToMultiByte(CP_ACP, 0, &wc, 1, &ach, 1, nullptr, nullptr);
                        *static_cast<char*>(buf) = ach;
                        *read = 1;
                        if (log) LOG_INFO("ReadConsoleA_Detour: #%d non-line-input return ach=0x%02x", callId, static_cast<uint8_t>(ach));
                        return TRUE;
                    }
                }
                continue;
            }
            if (!IsTransportConnected() || Unloader::IsUnloading()) {
                if (log) LOG_INFO("ReadConsoleA_Detour: #%d non-line-input transport disconnected/unloading, pass-through to orig", callId);
                // Phase 22：不再 release()（见 ReadConsoleW_Detour 头注释）
                return ReadConsoleA_orig(h, buf, len, read, ctrl);
            }
            WaitForSingleObject(queue.GetWaitHandle(), 100);
        }
    }

    // ---- 行编辑模式（ENABLE_LINE_INPUT 已设置）：LineEditor 行编辑 ----
    auto& editor = LineEditor::Instance();
    editor.BeginSession();

    // 行编辑临时变量（提升到函数作用域避免与 TLS 基址缓存栈布局冲突，同 ReadConsoleW_Detour）
    std::wstring lineOut;
    std::string vtOut;

    // 行编辑主循环：与 ReadConsoleW_Detour 一致，仅最后编码转换不同
    while (true) {
        INPUT_RECORD rec;
        size_t n;
        while (true) {
            n = queue.DequeueRecords(&rec, 1);
            if (n > 0) break;
            // transport 断开 或 Unloader 启动：pass-through 到 orig（同 ReadConsoleW_Detour 理由）
            if (!IsTransportConnected() || Unloader::IsUnloading()) {
                if (log) LOG_INFO("ReadConsoleA_Detour: #%d transport disconnected/unloading, pass-through to orig", callId);
                // Phase 22：不再 release()（见 ReadConsoleW_Detour 头注释）
                return ReadConsoleA_orig(h, buf, len, read, ctrl);
            }
            WaitForSingleObject(queue.GetWaitHandle(), 100);
        }

        if (rec.EventType != KEY_EVENT || !rec.Event.KeyEvent.bKeyDown) {
            continue;
        }

        lineOut.clear();
        vtOut.clear();
        bool done = editor.ProcessKey(rec.Event.KeyEvent, echoEnabled, lineOut, vtOut);

        // VT 回显发送给 mediator
        if (!vtOut.empty()) {
            // Phase 21：子进程行编辑回显前补发 CursorPosition（同 ReadConsoleW 路径）
            // 独立消息类型 CursorSync 即时发送，内容消息字节保持原样
            if (!IsTargetProcess()) {
                COORD cur = editor.GetCurrentUiCursor();
                if (cur.X < 0) cur.X = 0;
                if (cur.Y < 0) cur.Y = 0;
                std::string sync = vt::CursorPosition(cur.Y + 1, cur.X + 1);
                SendToMediator(sync.data(), sync.size(),
                               protocol::MessageType::CursorSync);
            }
            SendToMediator(vtOut.data(), vtOut.size());
        }

        if (done) {
            // 行完成：先构造 wchar_t 行（含 \r\n），再转 ANSI
            std::wstring wline = lineOut;
            // 行非空且空间足够：补 \r\n（同 ReadConsoleW 语义）
            // Ctrl+C 时 lineOut 为空（vtOut 已含 ^C\r\n），不补
            if (!lineOut.empty()) {
                wline.push_back(L'\r');
                wline.push_back(L'\n');
            }

            // wchar_t → ANSI（CP_ACP）一次性转换填入 buf
            int converted = WideCharToMultiByte(
                CP_ACP, 0, wline.data(),
                static_cast<int>(wline.size()),
                static_cast<char*>(buf),
                static_cast<int>(len),
                nullptr, nullptr);
            *read = static_cast<DWORD>(converted > 0 ? converted : 0);
            LOG_DEBUG("ReadConsoleA_Detour: return %lu chars", *read);
            return *read > 0 ? TRUE : FALSE;
        }
    }
}

// ============================================================
// ReadFile Hook（stdin 透传模式）
// ============================================================
// 仅拦截 stdin 句柄，其他 ReadFile（管道、文件等）直接 pass-through
// 透传模式（ENABLE_VIRTUAL_TERMINAL_INPUT）：从原始字节队列读
//   vim/less 等程序开启 VT 输入模式，期望收到原始 VT 字节而非 INPUT_RECORD
BOOL WINAPI ReadFile_Detour(HANDLE h, LPVOID buf, DWORD len,
                             LPDWORD read, LPOVERLAPPED ov) {
    // 临时诊断：覆盖所有 console 句柄的 ReadFile 调用
    // 快速路径前先判断 console 句柄，避免 Python 用非 GetStdHandle 缓存句柄
    // （如 _get_osfhandle(fileno(stdin)) 或 CreateFileW("CONIN$")）调 ReadFile 时漏日志
    bool isCachedStdin = (h == GetCachedStdin());
    bool isConsole = isCachedStdin || IsConsoleHandle(h);
    int callId = -1;
    bool log = false;
    if (isConsole) {
        callId = g_readFile_stdin_calls.fetch_add(1);
        log = (callId < kDiagLogLimit);
        if (log) {
            LOG_INFO("ReadFile_Detour: ENTRY #%d tid=%lu h=%p len=%lu isCachedStdin=%d isConsole=%d",
                     callId, GetCurrentThreadId(), h, len, isCachedStdin, isConsole);
        }
    }

    // 快速路径：非 stdin 直接调原 API（避免 ENSURE_INITIALIZED 开销）
    // transport Recv 读管道走此路径，不受影响
    if (h != GetCachedStdin()) {
        if (log) LOG_INFO("ReadFile_Detour: #%d fast-path pass-through (h != cachedStdin)", callId);
        return ReadFile_orig(h, buf, len, read, ov);
    }

    ENSURE_INITIALIZED();
    HookReentryGuard guard;
    if (log) LOG_INFO("ReadFile_Detour: #%d ENSURE_INITIALIZED done", callId);

    // ReadDetourGuard：同 ReadConsoleW_Detour（Phase 22：提前到 LazyInit 检查前，
    // 且阻塞型 pass-through 不再 release，见 ReadConsoleW_Detour 头注释）
    ReadDetourGuard readGuard;

    // LazyInit 期间调原始 API（避免递归：transport Recv 在 LazyInit 中调 ReadFile）
    if (IsInLazyInit()) {
        if (log) LOG_INFO("ReadFile_Detour: #%d LazyInit pass-through", callId);
        return ReadFile_orig(h, buf, len, read, ov);
    }

    // 检查是否透传模式
    auto& state = ConsoleState::Instance();
    DWORD inMode = state.GetInputMode();
    if ((inMode & ENABLE_VIRTUAL_TERMINAL_INPUT) == 0) {
        // 非透传模式：从记录队列消费 KEY_EVENT，提取 UnicodeChar 转换为字节返回
        // Textual 等 TUI 应用通过 ReadFile(stdin) 读输入，不走 ReadConsoleInput，
        // 而 DllRecvLoop 翻译后的 INPUT_RECORD 在 record 队列中，不可 pass-through 到 orig
        if (log) LOG_INFO("ReadFile_Detour: #%d non-VT mode (inMode=0x%x), reading from record queue", callId, inMode);
        auto& queue = InputQueue::Instance();
        while (true) {
            INPUT_RECORD rec = {};
            size_t n = queue.DequeueRecords(&rec, 1);
            if (n > 0) {
                if (rec.EventType == KEY_EVENT && rec.Event.KeyEvent.bKeyDown) {
                    wchar_t wc = rec.Event.KeyEvent.uChar.UnicodeChar;
                    if (wc != L'\0') {
                        *static_cast<uint8_t*>(buf) = static_cast<uint8_t>(wc & 0xFF);
                        *read = 1;
                        if (log) LOG_INFO("ReadFile_Detour: #%d non-VT return byte=0x%02x", callId, static_cast<uint8_t>(wc & 0xFF));
                        return TRUE;
                    }
                }
                // 非 KEY_EVENT（如 MOUSE_EVENT）或空字符：跳过，继续读下一个
                continue;
            }
            // 队列空：检查 transport 断开或卸载
            if (!IsTransportConnected() || Unloader::IsUnloading()) {
                if (log) LOG_INFO("ReadFile_Detour: #%d non-VT transport disconnected/unloading, pass-through to orig", callId);
                // Phase 22：不再 release()（阻塞线程保持计数，见函数头注释）
                return ReadFile_orig(h, buf, len, read, ov);
            }
            WaitForSingleObject(queue.GetWaitHandle(), 100);
        }
    }

    // 透传模式：从原始字节队列读
    if (log) LOG_INFO("ReadFile_Detour: #%d VT passthrough mode, reading from queue", callId);
    auto& queue = InputQueue::Instance();
    while (true) {
        size_t n = queue.DequeueRaw(static_cast<uint8_t*>(buf),
                                     static_cast<size_t>(len));
        if (n > 0) {
            *read = static_cast<DWORD>(n);
            if (log) LOG_INFO("ReadFile_Detour: #%d return n=%zu", callId, n);
            return TRUE;
        }
        // transport 断开 或 Unloader 启动：pass-through 到 orig（同 ReadConsoleW_Detour 理由，
        // 让 vim/less 等程序从 ConHost 读取 VT 输入，避免返回 FALSE 导致程序退出
        // 及调用栈滞留 DLL 内阻碍 LDR 卸载）
        if (!IsTransportConnected() || Unloader::IsUnloading()) {
            if (log) LOG_INFO("ReadFile_Detour: #%d transport disconnected/unloading, pass-through to orig", callId);
            // Phase 22：不再 release()（阻塞线程保持计数，见函数头注释）
            return ReadFile_orig(h, buf, len, read, ov);
        }
        WaitForSingleObject(queue.GetWaitHandle(), 100);
    }
}

// ============================================================
// 注册所有输入类 Hook
// ============================================================
void RegisterInputHooks() {
    // 优先 kernelbase，回退 kernel32（与 CursorHooks 同策略）
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
    entries.push_back({"ReadConsoleInputW",
        resolve("ReadConsoleInputW"),
        reinterpret_cast<void*>(&ReadConsoleInputW_Detour),
        reinterpret_cast<void**>(&ReadConsoleInputW_orig)});
    entries.push_back({"ReadConsoleInputA",
        resolve("ReadConsoleInputA"),
        reinterpret_cast<void*>(&ReadConsoleInputA_Detour),
        reinterpret_cast<void**>(&ReadConsoleInputA_orig)});
    entries.push_back({"PeekConsoleInputW",
        resolve("PeekConsoleInputW"),
        reinterpret_cast<void*>(&PeekConsoleInputW_Detour),
        reinterpret_cast<void**>(&PeekConsoleInputW_orig)});
    entries.push_back({"PeekConsoleInputA",
        resolve("PeekConsoleInputA"),
        reinterpret_cast<void*>(&PeekConsoleInputA_Detour),
        reinterpret_cast<void**>(&PeekConsoleInputA_orig)});
    entries.push_back({"GetNumberOfConsoleInputEvents",
        resolve("GetNumberOfConsoleInputEvents"),
        reinterpret_cast<void*>(&GetNumberOfConsoleInputEvents_Detour),
        reinterpret_cast<void**>(&GetNumberOfConsoleInputEvents_orig)});
    entries.push_back({"WriteConsoleInputW",
        resolve("WriteConsoleInputW"),
        reinterpret_cast<void*>(&WriteConsoleInputW_Detour),
        reinterpret_cast<void**>(&WriteConsoleInputW_orig)});
    entries.push_back({"WriteConsoleInputA",
        resolve("WriteConsoleInputA"),
        reinterpret_cast<void*>(&WriteConsoleInputA_Detour),
        reinterpret_cast<void**>(&WriteConsoleInputA_orig)});
    entries.push_back({"FlushConsoleInputBuffer",
        resolve("FlushConsoleInputBuffer"),
        reinterpret_cast<void*>(&FlushConsoleInputBuffer_Detour),
        reinterpret_cast<void**>(&FlushConsoleInputBuffer_orig)});
    entries.push_back({"ReadFile",
        resolve("ReadFile"),
        reinterpret_cast<void*>(&ReadFile_Detour),
        reinterpret_cast<void**>(&ReadFile_orig)});
    // Phase 6 修正：cmd.exe 用 ReadConsoleW 读取行输入
    entries.push_back({"ReadConsoleW",
        resolve("ReadConsoleW"),
        reinterpret_cast<void*>(&ReadConsoleW_Detour),
        reinterpret_cast<void**>(&ReadConsoleW_orig)});
    entries.push_back({"ReadConsoleA",
        resolve("ReadConsoleA"),
        reinterpret_cast<void*>(&ReadConsoleA_Detour),
        reinterpret_cast<void**>(&ReadConsoleA_orig)});

    for (const auto& e : entries) {
        if (e.target == nullptr) {
            LOG_ERROR("RegisterInputHooks: failed to resolve %s", e.name);
            return;
        }
    }

    HookManager::RegisterBatch(entries);
    LOG_INFO("InputHooks registered (%zu hooks)", entries.size());
}

// ============================================================
// KickStartBlockedReaders：唤醒阻塞在原 ReadConsoleW 的线程
// ============================================================
// 背景：MinHook 是 inline hook（修改函数入口字节），
//   若目标线程在 Hook 安装前已进入 ReadConsoleW 阻塞，
//   Hook 对它无效——它永远卡在原 ReadConsoleW 等 ConHost 返回。
//
// 方案：向 ConHost 写一个回车键事件（用 orig 绕过 Hook），
//   让原 ReadConsoleW 返回（空行），目标程序下次调用走 Detour。
//
// 注意：
//   - 必须在 Hook 已 InstallAll 后调用（orig 已被 MinHook 填充）
//   - 只调一次（避免重复唤醒）
//   - 用 WriteConsoleInputW_orig 而非 WriteConsoleInputW（后者会走 Hook 入队）
void KickStartBlockedReaders() {
    // orig 未初始化说明 Hook 未安装，跳过
    if (WriteConsoleInputW_orig == nullptr) {
        LOG_WARN("KickStart: WriteConsoleInputW_orig is null, skip");
        return;
    }

    HANDLE hStdin = GetStdHandle(STD_INPUT_HANDLE);
    if (hStdin == nullptr || hStdin == INVALID_HANDLE_VALUE) {
        LOG_WARN("KickStart: stdin handle invalid, skip");
        return;
    }

    // 构造回车键事件（按下 + 释放）
    // ReadConsoleW 在 ENABLE_LINE_INPUT 模式下收到 \r 才返回
    INPUT_RECORD recs[2];
    ZeroMemory(recs, sizeof(recs));

    // 回车按下
    recs[0].EventType = KEY_EVENT;
    recs[0].Event.KeyEvent.bKeyDown = TRUE;
    recs[0].Event.KeyEvent.wRepeatCount = 1;
    recs[0].Event.KeyEvent.wVirtualKeyCode = VK_RETURN;
    recs[0].Event.KeyEvent.wVirtualScanCode = static_cast<WORD>(MapVirtualKeyW(VK_RETURN, MAPVK_VK_TO_VSC));
    recs[0].Event.KeyEvent.uChar.UnicodeChar = L'\r';
    recs[0].Event.KeyEvent.dwControlKeyState = 0;

    // 回车释放
    recs[1] = recs[0];
    recs[1].Event.KeyEvent.bKeyDown = FALSE;

    DWORD written = 0;
    // 用 orig 调用，绕过 Hook（避免写到 InputQueue）
    BOOL ok = WriteConsoleInputW_orig(hStdin, recs, 2, &written);
    DWORD err = ok ? 0 : GetLastError();
    LOG_INFO("KickStart: wrote ENTER to ConHost, ok=%d written=%lu err=%lu",
             ok, written, err);
}

} // namespace terminjector::hooks
