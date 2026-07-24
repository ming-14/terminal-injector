# Phase 3: DLL 核心 Hook 框架 + 状态快照

> 本 Phase 是整个项目的"心脏"。搭建 DLL 的 Hook 生命周期管理框架，实现注入瞬间的状态快照，完成懒加载初始化，并以 `WriteConsoleW` 作为第一个验证 Hook 跑通端到端链路：**目标程序输出 → DLL Hook → 翻译 VT → IPC → 中介 → WT 渲染**。

---

## 1. Phase 目标

1. 改造 `DllMain`：仅 `DisableThreadLibraryCalls` + `MH_Initialize`，移除 Phase 2 的重活
2. 实现 `HookManager`：MinHook 创建/启用/禁用/卸载的统一管理
3. 实现**懒加载初始化**：首个 Hook 触发时执行 `ConnectToMediator` + `ReadSnapshot` + `InstallAllHooks`
4. 实现 `StateSnapshot`：注入瞬间读取全部 Console 状态（BufferInfo/Cursor/Mode/CP/Title）
5. 实现 `ConsoleState`：运行期状态缓存（光标位置、窗口尺寸、模式等）
6. 实现**第一个 Hook：`WriteConsoleW`**（含 W/A 双版本骨架）
7. 实现 `ConsoleToVt` 翻译器的 `WriteConsoleW` 分支（文本透传 + 颜色映射）
8. 实现 mediator 的 `BridgeLoop`：stdin → pipe，pipe → stdout 双向桥接
9. **端到端验证**：劫持 cmd.exe，在 WT 中看到 cmd 的输出

---

## 2. 前置依赖

- Phase 1 全部完成
- Phase 2 全部完成（注入器、mediator 握手、DLL 连接管道）
- `NamedPipeTransport` Server 端已拆分 `Create()` + `WaitClient()`

---

## 3. 涉及文件清单

```
src/
├── dll/
│   ├── dllmain.cpp                    # 改造：懒加载
│   ├── HookManager.h                  # 完整接口
│   ├── HookManager.cpp                # MinHook 生命周期
│   ├── LazyInit.h                     # 新建：懒加载守卫
│   ├── LazyInit.cpp
│   ├── state/
│   │   ├── ConsoleState.h             # 运行期状态缓存
│   │   ├── ConsoleState.cpp
│   │   ├── StateSnapshot.h            # 注入瞬间快照
│   │   └── StateSnapshot.cpp
│   ├── translator/
│   │   ├── VtEscape.h                 # VT 转义常量
│   │   ├── ConsoleToVt.h              # 翻译器接口
│   │   ├── ConsoleToVt.cpp            # WriteConsoleW 分支实现
│   │   └── Color.h                    # Console 属性 → VT 颜色映射
│   └── hooks/
│       ├── HookCommon.h               # 新建：Hook 共享宏/类型
│       ├── OutputHooks.h              # 输出 Hook 声明
│       └── OutputHooks.cpp            # WriteConsoleW/A 实现
├── mediator/
│   ├── Mediator.cpp                   # 修改：实现 BridgeLoop
│   └── VtPassThrough.h                # 新建：VT 透传逻辑
└── common/
    └── protocol/
        └── Message.h                  # 修改：补 VtOutput payload
```

---

## 4. 详细任务

### 4.1 DllMain 改造（懒加载）

遵循 AGENTS.md 与 Phase 1 风险表：**DllMain 绝不干重活**。

```cpp
// dllmain.cpp
#include <windows.h>
#include <MinHook.h>
#include "logging/Logger.h"
#include "LazyInit.h"

namespace terminjector {

// 全局懒加载状态
static LONG g_initInProgress = 0;
static bool g_initialized = false;

// 首个 Hook 触发时调用（线程安全，仅一次执行）
void EnsureLazyInitialized() {
    // 双检锁 + 原子标志，避免重入
    if (g_initialized) return;
    if (InterlockedCompareExchange(&g_initInProgress, 1, 0) != 0) {
        // 另一线程正在初始化，简单等待（Hook 路径不会高频进入此处）
        while (!g_initialized) Sleep(1);
        return;
    }

    Logger::Initialize(L"C:\\temp\\injected.log", LogLevel::Debug);
    LOG_INFO("LazyInit starting, pid=%u", GetCurrentProcessId());

    // 1. 读取注入瞬间状态快照（此时真实 Console 还未被 Hook 拦截）
    StateSnapshot::CaptureAndSync();

    // 2. 连接 mediator 并完成 Hello 握手
    //    Hello 中携带刚才快照的状态
    if (!ConnectToMediatorWithSnapshot()) {
        LOG_FATAL("ConnectToMediator failed, hooks will pass-through");
        // 失败时仍允许初始化完成，Hook 走 pass-through（直接调原 API）
    }

    // 3. 安装全部 Hook（Phase 3 仅 WriteConsoleW/A，Phase 4+ 逐步添加）
    HookManager::InstallAll();

    g_initialized = true;
    LOG_INFO("LazyInit done");
}

} // namespace terminjector

BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID) {
    using namespace terminjector;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hModule);

        // 仅初始化 MinHook，不创建任何 Hook
        if (MH_Initialize() != MH_OK) {
            // 无法用 Logger（未初始化），用 OutputDebugString 兜底
            OutputDebugStringW(L"[terminjector] MH_Initialize failed");
            return FALSE;
        }
        OutputDebugStringW(L"[terminjector] DllMain attach, MH initialized");
    } else if (reason == DLL_PROCESS_DETACH) {
        if (g_initialized) {
            HookManager::UninstallAll();
        }
        MH_Uninitialize();
        Logger::Shutdown();
    }
    return TRUE;
}
```

### 4.2 HookManager

#### 4.2.1 `HookManager.h`

```cpp
#pragma once
#include <windows.h>
#include <MinHook.h>
#include <functional>
#include <vector>
#include <string>

namespace terminjector {

// 单个 Hook 的注册信息
struct HookEntry {
    const char* name;          // 用于日志的可读名
    void*       target;        // 被 Hook 的 API 地址
    void*       detour;        // 我们的替代函数
    void**      original;      // 接收原函数指针的指针
};

// Hook 生命周期管理（进程级单例）
class HookManager {
public:
    // 注册一个 Hook（不立即启用）
    static void Register(const HookEntry& entry);

    // 批量注册（每个模块调用一次）
    static void RegisterBatch(const std::vector<HookEntry>& entries);

    // 安装全部已注册的 Hook（调用 MH_CreateHook + MH_EnableHook）
    // 失败则回滚已创建的
    static bool InstallAll();

    // 卸载全部 Hook（MH_DisableHook + MH_RemoveHook）
    static void UninstallAll();

    // 状态查询
    static bool IsInstalled() { return s_installed; }
    static size_t RegisteredCount() { return s_entries.size(); }

private:
    static std::vector<HookEntry> s_entries;
    static bool s_installed;
};

} // namespace terminjector
```

#### 4.2.2 `HookManager.cpp`

```cpp
#include "HookManager.h"
#include "logging/Logger.h"

namespace terminjector {

std::vector<HookEntry> HookManager::s_entries;
bool HookManager::s_installed = false;

void HookManager::Register(const HookEntry& entry) {
    s_entries.push_back(entry);
    LOG_DEBUG("Hook registered: %s target=%p detour=%p",
              entry.name, entry.target, entry.detour);
}

void HookManager::RegisterBatch(const std::vector<HookEntry>& entries) {
    for (const auto& e : entries) Register(e);
}

bool HookManager::InstallAll() {
    if (s_installed) {
        LOG_WARN("InstallAll called twice");
        return true;
    }

    // 1. CreateHook 全部
    std::vector<void*> created; // 用于失败回滚
    for (auto& e : s_entries) {
        MH_STATUS st = MH_CreateHook(e.target, e.detour, e.original);
        if (st != MH_OK) {
            LOG_ERROR("MH_CreateHook(%s) failed: %d", e.name, st);
            // 回滚已创建的
            for (void* t : created) MH_RemoveHook(t);
            return false;
        }
        created.push_back(e.target);
    }
    LOG_INFO("Created %zu hooks", s_entries.size());

    // 2. EnableHook 全部（一次性）
    MH_STATUS st = MH_EnableHook(MH_ALL_HOOKS);
    if (st != MH_OK) {
        LOG_ERROR("MH_EnableHook(MH_ALL) failed: %d", st);
        for (auto& e : s_entries) MH_RemoveHook(e.target);
        return false;
    }

    s_installed = true;
    LOG_INFO("All hooks enabled");
    return true;
}

void HookManager::UninstallAll() {
    if (!s_installed) return;
    MH_DisableHook(MH_ALL_HOOKS);
    for (auto& e : s_entries) MH_RemoveHook(e.target);
    s_entries.clear();
    s_installed = false;
    LOG_INFO("All hooks uninstalled");
}

} // namespace terminjector
```

### 4.3 状态快照（StateSnapshot）

注入瞬间读取真实 Console 的全部状态，**这是劫持后界面不乱的关键**。

#### 4.3.1 `StateSnapshot.h`

```cpp
#pragma once
#include <windows.h>
#include "protocol/Message.h"

namespace terminjector {

// 注入瞬间的 Console 状态快照
// 在 Hook 安装前读取，确保拿到的是真实 ConHost 状态
struct StateSnapshot {
    // 屏幕缓冲区信息
    CONSOLE_SCREEN_BUFFER_INFO screenBufferInfo{};
    // 光标信息
    CONSOLE_CURSOR_INFO   cursorInfo{};
    // 字体信息
    CONSOLE_FONT_INFOEX   fontInfo{};
    // 输入/输出模式
    DWORD inputMode = 0;
    DWORD outputMode = 0;
    // 代码页
    UINT  inputCp = 0;
    UINT  outputCp = 0;
    // 标题
    wchar_t title[260] = {};
    // 窗口可见性
    BOOL  windowVisible = FALSE;

    // 读取当前进程的真实 Console 状态（在 Hook 安装前调用）
    // 返回 false 表示读取失败
    bool Capture();

    // 将快照同步到 mediator（封装为 Hello 消息发送）
    // mediator 收到后立即用 VT 序列把 WT 调整到该状态
    void SyncToMediator();

    // 转成 HelloPayload（供 ConnectToMediatorWithSnapshot 使用）
    protocol::HelloPayload ToHelloPayload() const;
};

} // namespace terminjector
```

#### 4.3.2 `StateSnapshot.cpp`

```cpp
#include "StateSnapshot.h"
#include "logging/Logger.h"
#include "transport/ITransport.h"
#include "protocol/MessageSerializer.h"

namespace terminjector {

// 注意：本函数在 Hook 安装前调用，可直接用真实 API
bool StateSnapshot::Capture() {
    HANDLE hOut = GetStdHandle(STD_OUTPUT_HANDLE);
    HANDLE hIn  = GetStdHandle(STD_INPUT_HANDLE);

    if (!GetConsoleScreenBufferInfo(hOut, &screenBufferInfo)) {
        LOG_ERROR("GetConsoleScreenBufferInfo failed: %lu", GetLastError());
        return false;
    }
    if (!GetConsoleCursorInfo(hOut, &cursorInfo)) {
        LOG_WARN("GetConsoleCursorInfo failed: %lu", GetLastError());
    }

    fontInfo.cbSize = sizeof(fontInfo);
    if (!GetCurrentConsoleFontEx(hOut, FALSE, &fontInfo)) {
        LOG_WARN("GetCurrentConsoleFontEx failed: %lu", GetLastError());
    }

    GetConsoleMode(hIn, &inputMode);
    GetConsoleMode(hOut, &outputMode);

    inputCp  = GetConsoleCP();
    outputCp = GetConsoleOutputCP();

    if (!GetConsoleTitleW(title, 260)) {
        title[0] = L'\0';
    }

    windowVisible = IsWindowVisible(GetConsoleWindow());

    LOG_INFO("Snapshot: size=%dx%d win=%dx%d cursor=(%d,%d) mode(in=0x%lx out=0x%lx) cp(in=%u out=%u)",
             screenBufferInfo.dwSize.X, screenBufferInfo.dwSize.Y,
             screenBufferInfo.srWindow.Right - screenBufferInfo.srWindow.Left + 1,
             screenBufferInfo.srWindow.Bottom - screenBufferInfo.srWindow.Top + 1,
             screenBufferInfo.dwCursorPosition.X, screenBufferInfo.dwCursorPosition.Y,
             inputMode, outputMode, inputCp, outputCp);
    return true;
}

protocol::HelloPayload StateSnapshot::ToHelloPayload() const {
    protocol::HelloPayload p{};
    p.targetPid = GetCurrentProcessId();
    p.targetBitness = 64;
    p.consoleMode = static_cast<uint16_t>(outputMode);
    p.consoleCp = static_cast<uint16_t>(inputCp);
    p.consoleOutputCp = static_cast<uint16_t>(outputCp);
    p.bufferCols = static_cast<uint16_t>(screenBufferInfo.dwSize.X);
    p.bufferRows = static_cast<uint16_t>(screenBufferInfo.dwSize.Y);
    p.cursorX = static_cast<uint16_t>(screenBufferInfo.dwCursorPosition.X);
    p.cursorY = static_cast<uint16_t>(screenBufferInfo.dwCursorPosition.Y);
    return p;
}

} // namespace terminjector
```

#### 4.3.3 mediator 侧：根据 Hello 调整 WT

mediator 收到 `Hello` 后，把快照状态翻译成 VT 序列写到 stdout，让 WT 立即"跳到"目标程序的当前状态：

```cpp
// mediator/Mediator.cpp 中 Handshake() 收到 Hello 后：
void ApplySnapshotToWt(const protocol::HelloPayload& hello) {
    std::string vt;
    // 1. 设置窗口尺寸（若与 WT 当前不同）
    //    \x1b[8;<rows>;<cols>t  设置窗口大小
    char buf[64];
    int n = std::snprintf(buf, sizeof(buf),
        "\x1b[8;%u;%ut", hello.bufferRows, hello.bufferCols);
    vt.append(buf, n);

    // 2. 移动光标到快照位置
    //    \x1b[<row+1>;<col+1>H  (VT 是 1-based)
    n = std::snprintf(buf, sizeof(buf),
        "\x1b[%u;%uH", hello.cursorY + 1, hello.cursorX + 1);
    vt.append(buf, n);

    // 3. 设置代码页（chcp 不能直接做，但 mediator 可记录用于后续输入解码）
    //    输出方向靠 VT 透传，不需要显式 chcp

    // 4. 写到 stdout（WT 收到并渲染）
    WriteFile(GetStdHandle(STD_OUTPUT_HANDLE), vt.data(), vt.size(), nullptr, nullptr);
    LOG_INFO("Applied snapshot to WT: %s", vt.c_str());
}
```

### 4.4 ConsoleState（运行期状态缓存）

#### 4.4.1 `ConsoleState.h`

```cpp
#pragma once
#include <windows.h>
#include <atomic>
#include <mutex>
#include "StateSnapshot.h"

namespace terminjector {

// 运行期 Console 状态缓存
// Hook 安装后，所有 Get* 类 API 返回这里缓存的值
// 所有 Set* 类 API 与 Write* 类 API 更新这里
class ConsoleState {
public:
    static ConsoleState& Instance();

    // 用快照初始化
    void InitFromSnapshot(const StateSnapshot& snap);

    // ---- 各状态字段的 getter/setter（线程安全） ----

    // 屏幕缓冲区尺寸
    COORD GetBufferSize() const;
    void  SetBufferSize(COORD size);

    // 窗口位置/尺寸（srWindow）
    SMALL_RECT GetWindow() const;
    void  SetWindow(SMALL_RECT w);

    // 光标位置
    COORD GetCursorPosition() const;
    void  SetCursorPosition(COORD pos);
    // 输出后自动推进光标（WriteConsole Hook 调用）
    void  AdvanceCursor(int charsWritten, bool wrapAtEol);

    // 光标显隐/大小
    CONSOLE_CURSOR_INFO GetCursorInfo() const;
    void SetCursorInfo(const CONSOLE_CURSOR_INFO& info);

    // 模式
    DWORD GetInputMode() const;
    void  SetInputMode(DWORD m);
    DWORD GetOutputMode() const;
    void  SetOutputMode(DWORD m);

    // 代码页
    UINT GetInputCp() const;
    void SetInputCp(UINT cp);
    UINT GetOutputCp() const;
    void SetOutputCp(UINT cp);

    // 当前颜色属性（WriteConsole 时用于 VT 颜色生成）
    WORD  GetTextAttribute() const;
    void  SetTextAttribute(WORD attr);

    // 标题
    std::wstring GetTitle() const;
    void SetTitle(const std::wstring& t);

    // Alt Buffer 状态（Phase 8）
    bool IsAltBufferActive() const;
    void SetAltBufferActive(bool b);

private:
    ConsoleState() = default;
    mutable SRWLOCK m_lock = SRWLOCK_INIT;

    // 状态字段（受 m_lock 保护）
    CONSOLE_SCREEN_BUFFER_INFO m_screenInfo{};
    CONSOLE_CURSOR_INFO        m_cursorInfo{};
    DWORD m_inputMode = 0;
    DWORD m_outputMode = 0;
    UINT  m_inputCp = 0;
    UINT  m_outputCp = 0;
    WORD  m_textAttr = 0;
    std::wstring m_title;
    std::atomic<bool> m_altBuffer{false};
};

} // namespace terminjector
```

#### 4.4.2 `AdvanceCursor` 实现要点

WriteConsole Hook 拦截到输出后，必须更新光标缓存，否则下次 `GetConsoleScreenBufferInfo` 返回的坐标是旧的：

```cpp
void ConsoleState::AdvanceCursor(int charsWritten, bool wrapAtEol) {
    AcquireSRWLockExclusive(&m_lock);
    COORD& c = m_screenInfo.dwCursorPosition;
    SHORT cols = m_screenInfo.dwSize.X;

    c.X += charsWritten;
    while (c.X >= cols) {
        if (wrapAtEol) {
            c.X -= cols;
            c.Y++;
            // 滚屏处理（若 Y 超出 buffer 底部）简化：先不滚，Phase 5 补
            if (c.Y >= m_screenInfo.dwSize.Y) {
                c.Y = m_screenInfo.dwSize.Y - 1;
                // TODO Phase 5: 触发 ScrollConsoleScreenBuffer 等价逻辑
            }
        } else {
            c.X = cols - 1;
            break;
        }
    }
    ReleaseSRWLockExclusive(&m_lock);
}
```

### 4.5 第一个 Hook：`WriteConsoleW`

#### 4.5.1 `HookCommon.h`（共享宏）

```cpp
#pragma once
#include <windows.h>
#include "LazyInit.h"

namespace terminjector::hooks {

// 每个 Hook 函数入口都要调这个宏，确保懒加载完成
#define ENSURE_INITIALIZED() ::terminjector::EnsureLazyInitialized()

// 原函数指针类型定义宏
#define DEFINE_ORIG_PTR(name, sig) using name##_t = sig; \
    static name##_t* name##_orig = nullptr

} // namespace terminjector::hooks
```

#### 4.5.2 `OutputHooks.h`

```cpp
#pragma once
#include <windows.h>

namespace terminjector::hooks {

// 注册所有输出类 Hook（Phase 3 仅 WriteConsoleW/A）
// 由 HookManager::InstallAll 之前调用
void RegisterOutputHooks();

} // namespace terminjector::hooks
```

#### 4.5.3 `OutputHooks.cpp`

```cpp
#include "OutputHooks.h"
#include "HookCommon.h"
#include "HookManager.h"
#include "state/ConsoleState.h"
#include "translator/ConsoleToVt.h"
#include "logging/Logger.h"

namespace terminjector::hooks {

// 原函数指针
DEFINE_ORIG_PTR(WriteConsoleW, BOOL WINAPI(
    HANDLE hConsoleOutput, const VOID* lpBuffer,
    DWORD nNumberOfCharsToWrite, LPDWORD lpNumberOfCharsWritten, LPVOID lpReserved));
DEFINE_ORIG_PTR(WriteConsoleA, BOOL WINAPI(
    HANDLE hConsoleOutput, const VOID* lpBuffer,
    DWORD nNumberOfCharsToWrite, LPDWORD lpNumberOfCharsWritten, LPVOID lpReserved));

// Hook 实现：WriteConsoleW
BOOL WINAPI WriteConsoleW_Detour(
    HANDLE hConsoleOutput, const VOID* lpBuffer,
    DWORD nNumberOfCharsToWrite, LPDWORD lpNumberOfCharsWritten, LPVOID lpReserved) {

    ENSURE_INITIALIZED();

    // 仅拦截真实 Console 输出句柄（排除日志文件句柄等）
    if (!IsConsoleHandle(hConsoleOutput)) {
        return WriteConsoleW_orig(hConsoleOutput, lpBuffer,
                                  nNumberOfCharsToWrite, lpNumberOfCharsWritten, lpReserved);
    }

    // 翻译为 VT 序列并发给 mediator
    auto& state = ConsoleState::Instance();
    WORD attr = state.GetTextAttribute();
    std::string vt = ConsoleToVt::WriteConsoleW(
        reinterpret_cast<const wchar_t*>(lpBuffer), nNumberOfCharsToWrite, attr);

    SendToMediator(vt.data(), vt.size());

    // 更新光标缓存
    state.AdvanceCursor(nNumberOfCharsToWrite, /*wrapAtEol=*/true);

    // 仍然调用原 API（让真实 ConHost 同步更新，避免目标程序内部状态不一致）
    // 注意：Phase 9 自保护阶段会改为不调用原 API（防止 ConHost 黑框闪烁）
    BOOL ret = WriteConsoleW_orig(hConsoleOutput, lpBuffer,
                                  nNumberOfCharsToWrite, lpNumberOfCharsWritten, lpReserved);
    return ret;
}

// Hook 实现：WriteConsoleA（先转 UTF-16 再走 W 路径）
BOOL WINAPI WriteConsoleA_Detour(
    HANDLE hConsoleOutput, const VOID* lpBuffer,
    DWORD nNumberOfCharsToWrite, LPDWORD lpNumberOfCharsWritten, LPVOID lpReserved) {

    ENSURE_INITIALIZED();

    if (!IsConsoleHandle(hConsoleOutput)) {
        return WriteConsoleA_orig(hConsoleOutput, lpBuffer,
                                  nNumberOfCharsToWrite, lpNumberOfCharsWritten, lpReserved);
    }

    // A → W 转换（按当前输出代码页）
    UINT cp = ConsoleState::Instance().GetOutputCp();
    int wlen = MultiByteToWideChar(cp, 0, reinterpret_cast<const char*>(lpBuffer),
                                   nNumberOfCharsToWrite, nullptr, 0);
    std::wstring wbuf(wlen, L'\0');
    MultiByteToWideChar(cp, 0, reinterpret_cast<const char*>(lpBuffer),
                        nNumberOfCharsToWrite, wbuf.data(), wlen);

    // 复用 W 路径
    return WriteConsoleW_Detour(hConsoleOutput, wbuf.data(), wlen,
                                lpNumberOfCharsWritten, lpReserved);
}

void RegisterOutputHooks() {
    HookManager::RegisterBatch({
        {"WriteConsoleW", GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "WriteConsoleW"),
         reinterpret_cast<void*>(&WriteConsoleW_Detour),
         reinterpret_cast<void**>(&WriteConsoleW_orig)},
        {"WriteConsoleA", GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "WriteConsoleA"),
         reinterpret_cast<void*>(&WriteConsoleA_Detour),
         reinterpret_cast<void**>(&WriteConsoleA_orig)},
    });
    LOG_INFO("OutputHooks registered (WriteConsoleW/A)");
}

} // namespace terminjector::hooks
```

#### 4.5.4 `IsConsoleHandle` 与 `SendToMediator` 工具

```cpp
// 在 hooks/HookCommon.cpp 或 InlineCommon.h
namespace terminjector::hooks {

// 判断句柄是否为真实 Console 句柄（CONOUT$/CONIN$）
// 用 GetFileType 或缓存已知 Console 句柄集合
inline bool IsConsoleHandle(HANDLE h) {
    return GetFileType(h) == FILE_TYPE_CHAR; // Console 是 char device
}

// 发送 VT 字节到 mediator（线程安全，封装 ITransport::Send + Serialize）
void SendToMediator(const void* data, size_t len);

} // namespace terminjector::hooks
```

### 4.6 翻译器：ConsoleToVt（Phase 3 仅 WriteConsoleW 分支）

#### 4.6.1 `VtEscape.h`

```cpp
#pragma once
#include <string>

namespace terminjector::vt {

// 常用 VT 转义序列常量
constexpr const char* kCsi = "\x1b[";        // Control Sequence Introducer
constexpr const char* kOsc = "\x1b]";        // Operating System Command
constexpr const char* kReset = "\x1b[0m";    // 重置所有属性

// 颜色映射：Console 16 色属性 → VT SGR
// attr 是 Windows WORD（低 4 位前景，4-7 位背景，8 位高强度，15 位闪烁等）
std::string SgrFromAttribute(WORD attr);

// 光标定位（1-based）
std::string CursorPosition(int row, int col);

// 设置窗口尺寸
std::string ResizeWindow(int rows, int cols);

} // namespace terminjector::vt
```

#### 4.6.2 `Color.h` 颜色映射表

Windows 16 色到 VT 颜色索引的映射：

| Windows 位 | 含义 | VT SGR |
|------------|------|--------|
| bit 0 (0x1) | 前景红 | 31 |
| bit 1 (0x2) | 前景绿 | 32 |
| bit 2 (0x4) | 前景蓝 | 34 |
| bit 3 (0x8) | 前景强度 | 1 |
| bit 4 (0x10) | 背景红 | 41 |
| bit 5 (0x20) | 背景绿 | 42 |
| bit 6 (0x40) | 背景蓝 | 44 |
| bit 7 (0x80) | 背景强度 | 5 |

实际 Console 颜色是 RGB 混合（红+绿=黄等），VT 也支持，需完整映射 16 色。

```cpp
// translator/Color.cpp
std::string vt::SgrFromAttribute(WORD attr) {
    // 仅当与上次不同时才输出 SGR（缓存上次 attr）
    static WORD lastAttr = 0xFFFF; // 初始无效值
    if (attr == lastAttr) return {};
    lastAttr = attr;

    std::string s = kCsi;
    bool first = true;
    auto add = [&](int code) {
        if (!first) s += ';';
        s += std::to_string(code);
        first = false;
    };

    // 前景
    WORD fg = attr & 0xF;
    if (fg & 0x8) add(1); // 高强度
    // 红+绿+蓝 → 30+位组合
    static const int fgMap[8] = {0, 31, 32, 33, 34, 35, 36, 37};
    // fg 低 3 位是 RGB 组合（0=黑 1=红 2=绿 3=黄 4=蓝 5=品红 6=青 7=白）
    add(fgMap[fg & 0x7]);

    // 背景
    WORD bg = (attr >> 4) & 0xF;
    if (bg & 0x8) add(5);
    static const int bgMap[8] = {0, 41, 42, 43, 44, 45, 46, 47};
    add(bgMap[bg & 0x7]);

    s += 'm';
    return s;
}
```

#### 4.6.3 `ConsoleToVt.cpp`

```cpp
#include "ConsoleToVt.h"
#include "VtEscape.h"
#include "Color.h"
#include "state/ConsoleState.h"

namespace terminjector {

std::string ConsoleToVt::WriteConsoleW(const wchar_t* buf, DWORD len, WORD attr) {
    std::string out;
    out.reserve(len * 3);

    // 1. 设置颜色（仅变化时输出 SGR）
    out += vt::SgrFromAttribute(attr);

    // 2. 文本转 UTF-8
    int utf8Len = WideCharToMultiByte(CP_UTF8, 0, buf, len, nullptr, 0, nullptr, nullptr);
    out.resize(out.size() + utf8Len);
    WideCharToMultiByte(CP_UTF8, 0, buf, len, out.data() + out.size() - utf8Len,
                        utf8Len, nullptr, nullptr);

    return out;
}

} // namespace terminjector
```

### 4.7 mediator 的 BridgeLoop（真实桥接）

#### 4.7.1 `VtPassThrough.h`

```cpp
#pragma once
#include <string>

namespace terminjector {

// VT 透传逻辑（中介侧）
// 负责在 WT stdin ↔ DLL pipe 之间搬运字节，不解析 VT 内容
class VtPassThrough {
public:
    // stdin (WT) → pipe (DLL)
    static void ForwardStdinToPipe(class ITransport& transport);

    // pipe (DLL) → stdout (WT)
    static void ForwardPipeToStdout(class ITransport& transport);
};

} // namespace terminjector
```

#### 4.7.2 `Mediator::BridgeLoop` 实现

```cpp
void Mediator::BridgeLoop() {
    // 起两个线程：一个 stdin→pipe，一个 pipe→stdout
    HANDLE hStdin  = GetStdHandle(STD_INPUT_HANDLE);
    HANDLE hStdout = GetStdHandle(STD_OUTPUT_HANDLE);

    // 线程 1：stdin → pipe
    std::thread tIn([this]() {
        char buf[4096];
        while (m_transport->IsConnected()) {
            DWORD read = 0;
            if (!ReadFile(GetStdHandle(STD_INPUT_HANDLE), buf, sizeof(buf), &read, nullptr)
                || read == 0) break;
            // 包成 VtInput 消息发 DLL
            auto pkt = protocol::Serialize(protocol::MessageType::VtInput, buf, read);
            if (m_transport->Send(pkt.data(), pkt.size()) != pkt.size()) break;
        }
        LOG_INFO("stdin→pipe thread exit");
    });

    // 线程 2：pipe → stdout（主线程）
    std::vector<uint8_t> recvBuf(8192);
    while (m_transport->IsConnected()) {
        // 收一个完整包
        protocol::MessageType type;
        std::vector<uint8_t> payload;
        if (!RecvPacket(*m_transport, type, payload)) {
            LOG_INFO("pipe closed");
            break;
        }
        if (type == protocol::MessageType::VtOutput) {
            // 直接写 stdout（WT 渲染）
            DWORD written = 0;
            WriteFile(GetStdHandle(STD_OUTPUT_HANDLE), payload.data(),
                      payload.size(), &written, nullptr);
        } else {
            LOG_DEBUG("mediator got msg type=%u len=%zu", type, payload.size());
        }
    }

    tIn.join();
    LOG_INFO("BridgeLoop exit");
}
```

---

## 5. 端到端验证（Phase 3 核心验证）

### 5.1 验证场景

```powershell
# 1. 启动一个 cmd.exe，记下 PID（假设 1234）
# 2. 在 WT 中运行
wt.exe terminal-injector.exe --mediator --target-pid 1234
```

### 5.2 预期结果

| 检查项 | 预期 |
|--------|------|
| mediator 日志 | `Handshake OK` 后 `BridgeLoop` 进入 |
| `C:\temp\injected.log` | 出现 `LazyInit done`、`All hooks enabled` |
| WT 窗口 | 显示 cmd 的当前输出（注入前已有的内容不会出现，新输出会显示） |
| 在原 cmd 窗口输入 `echo hello` | WT 窗口同步出现 `hello` |
| 在 WT 窗口输入 `echo world` | **不工作**（Phase 6 才实现输入） |
| 关闭 WT tab | mediator 退出，DLL 检测管道断开（Phase 11 实现卸载） |

### 5.3 已知限制（Phase 3 不解决）

- 无输入处理（键盘/鼠标在 Phase 6）
- 无光标移动 Hook（Phase 5）
- 无 VT 模式欺骗（Phase 7）
- 原 cmd 窗口仍会闪烁更新（Phase 9 自保护改为不调原 API）
- 关闭 WT 后 DLL 不会卸载（Phase 11）

### 5.4 调试技巧

- 用 DebugView 实时看 DLL 日志
- `C:\temp\injected.log` 看完整 DLL 日志
- `terminal-injector.log` 看 mediator 日志
- 若 cmd 无输出到 WT：检查 `IsConsoleHandle` 是否误判、`SendToMediator` 是否成功
- 若 DLL 未连接：检查 `MakePipeName(GetCurrentProcessId())` 与 mediator 创建的名称是否一致
- 用 `windows-debugging` 工具的 cdb/windbg 附加目标进程查看 Hook 是否生效

---

## 6. 风险点

| 风险 | 缓解 |
|------|------|
| 懒加载在首个 Hook 重入（同线程递归调用被 Hook 的 API） | `ENSURE_INITIALIZED` 用原子标志 + 自旋等待；Logger 在 Init 内第一时间启用 |
| `WriteConsoleW_orig` 仍调原 API 导致原 cmd 黑框更新 | Phase 3 接受此现象，Phase 9 自保护改为静默返回 |
| `AdvanceCursor` 滚屏逻辑不全 | Phase 5 补全 ScrollConsoleScreenBuffer 等价逻辑 |
| 颜色 SGR 缓存 `lastAttr` 是 static，多线程不安全 | 改为 `thread_local` 或 ConsoleState 字段（受锁保护） |
| mediator 的 `ReadFile(stdin)` 阻塞，DLL 退出时无法唤醒 | 用 `PeekNamedPipe` 或 `CancelIoEx` 在退出时打断 |
| `MH_EnableHook(MH_ALL_HOOKS)` 一次性启用可能短暂窗口期 | 接受，Hook 安装是同步的，目标程序此期间调用概率极低 |

---

## 7. 交付物清单

- [ ] `dllmain.cpp` 改造为懒加载（仅 MH_Initialize + DisableThreadLibraryCalls）
- [ ] `LazyInit.h/cpp` `EnsureLazyInitialized` 双检锁实现
- [ ] `HookManager.h/cpp` 完整生命周期管理（含回滚）
- [ ] `StateSnapshot.h/cpp` 全状态读取 + ToHelloPayload
- [ ] `ConsoleState.h/cpp` 运行期缓存（含 AdvanceCursor）
- [ ] `translator/VtEscape.h`、`Color.cpp`、`ConsoleToVt.cpp`（WriteConsoleW 分支）
- [ ] `hooks/HookCommon.h`、`OutputHooks.cpp`（WriteConsoleW/A）
- [ ] `mediator/VtPassThrough.h`、`Mediator::BridgeLoop` 双向桥接
- [ ] mediator 收到 Hello 后 `ApplySnapshotToWt`
- [ ] 5.2 端到端验证：cmd 输出在 WT 同步显示
