# Phase 9: 自保护

> 本 Phase 实现"防越狱"机制，阻止目标程序脱离中介管道重新绑定到 ConHost，让 `GetStdHandle` 返回真实句柄（配合类型拦截）、`CloseHandle` 跳过假句柄、`GetConsoleWindow` 隔离 ConHost 窗口操作。同时把前面各 Phase 中"仍调原 API 让 ConHost 同步"的行为改为**静默返回**，消除原 cmd 黑框的更新闪烁。

---

## 1. Phase 目标

1. Hook 进程级 Console 控制 API（全部拦截，不让目标脱离管道）：
   - `AttachConsole`（返回 FALSE，`ERROR_ACCESS_DENIED`）
   - `FreeConsole`（返回 FALSE 或静默成功但不真断开）
   - `AllocConsole`（返回 FALSE，`ERROR_NOT_ENOUGH_MEMORY`）
   - `GetConsoleWindow`（返回 NULL，隔离 ConHost 原生窗口操作）
2. Hook `GetStdHandle`：
   - 返回 DLL 控制的"假"标准句柄（标识为 Console，但不指向真实 ConHost）
   - 缓存首次调用结果，保证多次调用返回一致
   - **最终决策：不 Hook**（见 4.3，读写 API 已按句柄类型拦截）
3. Hook `CloseHandle`：
   - 对假句柄（Alt Buffer sentinel、fakeWaitHandle、假 std handle）：静默返回 TRUE，不真关
   - 对日志文件句柄：放行
   - 对其他真实句柄：调原 API
4. **改为静默模式**：回顾 Phase 3~8 中所有"仍调原 API"的地方，改为不调（或调但不影响）
   - `WriteConsoleW_orig` / `WriteFile_orig` 等：不再让 ConHost 更新
   - 原理：Hook 后 ConHost 不再收到数据，原 cmd 黑框停止更新（消除闪烁）
5. 验证：目标程序调 `AllocConsole` 不会弹新黑框；多次 `GetStdHandle` 返回一致；原 cmd 窗口不再闪烁；`GetConsoleWindow` 返回 NULL 后程序走"无窗口"分支

---

## 2. 前置依赖

- Phase 8 完成（假句柄体系已建立：Alt Buffer sentinel、fakeWaitHandle）

---

## 3. 涉及文件清单

```
src/dll/
├── hooks/
│   ├── ProtectionHooks.h / .cpp      # 新建：自保护 Hook
│   └── HandleRegistry.h / .cpp       # 新建：假句柄注册表
├── hooks/OutputHooks.cpp             # 修改：移除 _orig 调用
├── hooks/CursorHooks.cpp             # 修改：移除 _orig 调用
└── hooks/*.cpp                       # 所有 Hook 修改为静默
```

---

## 4. 详细任务

### 4.1 假句柄注册表

统一管理所有"假"句柄，供 `CloseHandle`/`GetStdHandle`/`IsConsoleHandle` 查询：

```cpp
// HandleRegistry.h
#pragma once
#include <windows.h>
#include <set>
#include <mutex>

namespace terminjector {

class HandleRegistry {
public:
    static HandleRegistry& Instance();

    // 注册一个假句柄
    void RegisterFake(HANDLE h);
    // 注销
    void UnregisterFake(HANDLE h);
    // 是否为假句柄
    bool IsFake(HANDLE h) const;

    // 注册受保护的真实句柄（如日志文件句柄，CloseHandle 放行但不被其他 Hook 拦截）
    void RegisterProtected(HANDLE h);
    bool IsProtected(HANDLE h) const;

private:
    HandleRegistry();
    mutable std::mutex m_mutex;
    std::set<HANDLE> m_fakes;
    std::set<HANDLE> m_protected;
};

} // namespace terminjector
```

注册时机：
- Alt Buffer sentinel → Phase 8 `CreateConsoleScreenBuffer` 时注册
- fakeWaitHandle → `InitFakeWaitHandle` 时注册
- 假 std handle → 本 Phase `GetStdHandle` 首次调用时注册
- 日志文件句柄 → `Logger::Initialize` 时注册（Phase 1 已建，本 Phase 补注册）

### 4.2 `GetStdHandle` Hook

```cpp
DEFINE_ORIG_PTR(GetStdHandle, HANDLE WINAPI(DWORD));

// 假的 std 句柄（首次调用时创建并缓存）
static HANDLE g_fakeStdIn  = nullptr;
static HANDLE g_fakeStdOut = nullptr;
static HANDLE g_fakeStdErr = nullptr;

HANDLE WINAPI GetStdHandle_Detour(DWORD nStdHandle) {
    ENSURE_INITIALIZED();
    // 返回缓存的假句柄，保证多次调用一致
    switch (nStdHandle) {
        case STD_INPUT_HANDLE:
            if (!g_fakeStdIn) {
                g_fakeStdIn = CreateFakeHandle();
                HandleRegistry::Instance().RegisterFake(g_fakeStdIn);
            }
            return g_fakeStdIn;
        case STD_OUTPUT_HANDLE:
            if (!g_fakeStdOut) {
                g_fakeStdOut = CreateFakeHandle();
                HandleRegistry::Instance().RegisterFake(g_fakeStdOut);
            }
            return g_fakeStdOut;
        case STD_ERROR_HANDLE:
            if (!g_fakeStdErr) {
                g_fakeStdErr = CreateFakeHandle();
                HandleRegistry::Instance().RegisterFake(g_fakeStdErr);
            }
            return g_fakeStdErr;
        default:
            return GetStdHandle_orig(nStdHandle);
    }
}

// 创建一个"假"句柄：用 CreateEvent 返回的有效句柄（避免传 INVALID_HANDLE_VALUE 给程序崩溃）
// 但要能被 IsConsoleHandle 识别为 Console 类型
// 方案：用 CreateFileW 打开 CONIN$/CONOUT$（系统会返回真实 Console 句柄），
//       但我们不希望它真的绑定 ConHost —— 矛盾。
// 折中：直接复用真实 std handle（GetStdHandle_orig 一次），后续所有读写都由 Hook 拦截
HANDLE CreateFakeHandle() {
    // 实际上保留真实句柄，但所有 Read/Write 都被 Hook 拦截
    // 这样 GetFileType(h) == FILE_TYPE_CHAR 仍成立，IsConsoleHandle 正常
    return nullptr; // 见 4.3 说明
}
```

### 4.3 `GetStdHandle` 策略调整

实际上"创建假句柄"很麻烦（要伪装 Console 类型）。**更实用的方案**：

- `GetStdHandle` **不 Hook**，让程序拿到真实 Console 句柄
- 所有读写 API（`WriteConsole`/`ReadFile`/...）已 Hook，拦截基于句柄类型而非具体值
- `IsConsoleHandle` 用 `GetFileType(h) == FILE_TYPE_CHAR` 判断，真实 Console 句柄天然满足

这样 `GetStdHandle` 无需 Hook，省去大量假句柄管理。**仅当目标程序缓存 std handle 后用 `SetStdHandle` 重置时才需处理**（极少见）。

→ 本 Phase 调整：`GetStdHandle` 暂不 Hook，仅在文档中记录此决策。若 Phase 11 测试发现程序依赖 `GetStdHandle` 返回特定值再补。

### 4.4 `AttachConsole` / `FreeConsole` / `AllocConsole` Hook

```cpp
DEFINE_ORIG_PTR(AllocConsole, BOOL WINAPI());
DEFINE_ORIG_PTR(AttachConsole, BOOL WINAPI(DWORD));
DEFINE_ORIG_PTR(FreeConsole, BOOL WINAPI());

BOOL WINAPI AllocConsole_Detour() {
    ENSURE_INITIALIZED();
    // 拒绝：假装分配失败
    SetLastError(ERROR_NOT_ENOUGH_MEMORY);
    LOG_WARN("AllocConsole blocked");
    return FALSE;
}

BOOL WINAPI AttachConsole_Detour(DWORD pid) {
    ENSURE_INITIALIZED();
    // 拒绝：不允许附加到其他进程的 Console
    SetLastError(ERROR_ACCESS_DENIED);
    LOG_WARN("AttachConsole(%u) blocked", pid);
    return FALSE;
}

BOOL WINAPI FreeConsole_Detour() {
    ENSURE_INITIALIZED();
    // 关键：不能让程序真的 Free，否则后续 Console API 失效
    // 假装成功（很多程序 Free 后会 Alloc 新的，但 Alloc 已被拦）
    LOG_WARN("FreeConsole blocked (returning TRUE)");
    return TRUE;
}
```

**注意**：`FreeConsole` 返回 TRUE 但不真断开。若程序后续调 `AllocConsole`（被拦返回 FALSE），程序可能进入错误处理路径。需测试常见程序行为。

### 4.5 `GetConsoleWindow` Hook

Far 等窗口操作型 TUI 拿 ConHost HWND 改标题/置顶/子类化，会旁路 mediator 管道（标题应走 `SetConsoleTitleW` Detour 转发 WT），且可能把 LazyInit 已隐藏的原 ConHost 窗口重新显示。Hook 后返回 NULL 让程序走"无窗口"分支。

```cpp
DEFINE_ORIG_PTR(GetConsoleWindow, HWND WINAPI());

HWND WINAPI GetConsoleWindow_Detour() {
    if (IsInLazyInit()) {
        return GetConsoleWindow_orig();
    }
    return NULL;
}
```

**风险缓解**：尺寸等信息由 `GetConsoleScreenBufferInfo` Detour 缓存提供，不依赖窗口句柄。

**内部真实 HWND 需求**：DLL 自身需在 LazyInit 隐藏窗口、StateSnapshot 记录可见性、Unloader 恢复显示。这些内部调用统一走封装函数（orig trampoline）绕过 Hook：

```cpp
// ProtectionHooks.h
HWND CallRealGetConsoleWindow();  // 返回真实 ConHost HWND
```

调用点：`LazyInit.cpp` 隐藏窗口、`state/StateSnapshot.cpp` 可见性、`Unloader.cpp` 恢复显示。

### 4.6 `CloseHandle` Hook

```cpp
DEFINE_ORIG_PTR(CloseHandle, BOOL WINAPI(HANDLE));

BOOL WINAPI CloseHandle_Detour(HANDLE h) {
    ENSURE_INITIALIZED();
    // 假句柄：静默返回 TRUE，不真关
    if (HandleRegistry::Instance().IsFake(h)) {
        LOG_DEBUG("CloseHandle(fake %p) silently ignored", h);
        return TRUE;
    }
    // 受保护句柄（日志文件）：放行，但 Logger 内部要标记已关
    // 实际上日志文件句柄不应被程序关，若程序尝试关，放行让其成功
    return CloseHandle_orig(h);
}
```

**风险**：`CloseHandle` 是极高频调用（每次 `CreateFile` 后都会关）。Hook 必须极快。`HandleRegistry::IsFake` 用 `std::set` 查找有锁开销，优化为无锁哈希或固定值比较。

优化：假句柄用固定魔数值（如 `0xABCDEF12`），`IsFake` 直接位比较，无需查表：

```cpp
inline bool IsFakeHandleFast(HANDLE h) {
    uintptr_t v = reinterpret_cast<uintptr_t>(h);
    // 假句柄高位为魔数
    return (v & 0xFFFF0000) == 0xABCD0000;
}
```

### 4.7 静默模式改造（移除 _orig 调用）

回顾 Phase 3~8，所有 Hook 末尾的 `_orig` 调用需评估：

| Hook | 原行为 | 改造后 |
|------|--------|--------|
| `WriteConsoleW` | 调 `_orig` 让 ConHost 同步 | 不调，仅返回 TRUE |
| `WriteFile(CONOUT$)` | 调 `_orig` | 不调 |
| `SetConsoleCursorPosition` | 调 `_orig` | 不调 |
| `SetConsoleTextAttribute` | 调 `_orig` | 不调 |
| `FillConsoleOutput*` | 调 `_orig` | 不调 |
| `ReadConsoleInput` | 不调 `_orig`（已从队列读） | 无需改 |
| `GetConsoleScreenBufferInfo` | 不调 `_orig`（返回缓存） | 无需改 |

改造示例：

```cpp
// OutputHooks.cpp 改造
BOOL WINAPI WriteConsoleW_Detour(...) {
    ENSURE_INITIALIZED();
    if (!IsConsoleHandle(h)) return WriteConsoleW_orig(...); // 非 Console 仍走原
    // ... 翻译 + 发中介 ...
    if (lpNumberOfCharsWritten) *lpNumberOfCharsWritten = nNumberOfCharsToWrite;
    return TRUE; // 不调 _orig
}
```

**效果**：ConHost 不再收到任何输出，原 cmd 黑框停止更新，消除闪烁。但原 cmd 窗口仍可见（空白或停在注入前状态）。可考虑 Phase 11 用 `ShowWindow(CallRealGetConsoleWindow(), SW_HIDE)` 隐藏原窗口（GetConsoleWindow 已 Hook 返回 NULL，内部路径必须走 orig）。

### 4.8 隐藏原 Console 窗口（可选）

```cpp
// 在 LazyInit 末尾（GetConsoleWindow 已 Hook，内部必须走 orig）
void HideOriginalConsole() {
    HWND hCon = hooks::CallRealGetConsoleWindow();
    if (hCon && IsWindowVisible(hCon)) {
        ShowWindow(hCon, SW_HIDE);
        LOG_INFO("Original console window hidden");
    }
}
```

**风险**：若目标程序依赖 Console 窗口可见性（如 `IsWindowVisible` 判断），可能行为异常。作为可选功能，默认开启，配置项可关。

---

## 5. 验证标准

| 测试 | 预期 |
|------|------|
| 目标程序调 `AllocConsole()` | 返回 FALSE，无新黑框 |
| 目标程序调 `AttachConsole(pid)` | 返回 FALSE |
| 目标程序调 `FreeConsole()` | 返回 TRUE 但仍可正常 IO |
| 目标程序调 `GetConsoleWindow()` | 返回 NULL |
| 注入后原 cmd 窗口 | 不再更新（静默模式） |
| 多次 `GetStdHandle(STD_OUTPUT_HANDLE)` | 返回一致句柄 |
| `CloseHandle(GetStdHandle(...))` | 静默成功，后续 IO 仍可用 |
| 日志文件写入 | 不受 CloseHandle Hook 影响 |

---

## 6. 风险点

| 风险 | 缓解 |
|------|------|
| `FreeConsole` 返回 TRUE 但未真断，程序状态混乱 | 文档说明；若程序强制要求真断，需特殊处理 |
| `CloseHandle` Hook 性能（高频） | 假句柄用魔数位比较，O(1) |
| 静默模式后程序检测 ConHost 无响应 | 极少见，接受 |
| 隐藏原窗口后程序调 `GetConsoleWindow` | Hook 返回 NULL；程序走"无窗口"分支，尺寸走 GCSBI 缓存 |
| 依赖 Console 窗口可见性的程序（如 `IsWindowVisible`） | 极少见；内部路径（隐藏/恢复/可见性）走 `CallRealGetConsoleWindow` 绕过 |
| 程序用 `SetStdHandle` 重置 | 极少见，Phase 11 测试覆盖后再决定是否 Hook |

---

## 7. 交付物清单

- [ ] `ProtectionHooks.cpp` Alloc/Attach/Free/GetConsoleWindow/CloseHandle Hook
- [ ] `HandleRegistry` 假句柄管理（含魔数快速判断）
- [ ] 所有输出/光标类 Hook 改为静默模式（移除 _orig 调用）
- [ ] 日志文件句柄注册为 protected
- [ ] 可选：隐藏原 Console 窗口
- [ ] 验证防越狱 + 无闪烁
