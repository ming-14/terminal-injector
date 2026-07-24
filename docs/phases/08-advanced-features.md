# Phase 8: 高级特性

> 本 Phase 实现一组"高级"Console API 的 Hook，覆盖 Alt Buffer、标题、代码页、字体、以及最棘手的 **Wait 句柄假映射**。完成后，vim/less 等全屏 TUI 程序能正确进出 Alt Buffer，且不会因 `WaitForSingleObject` 假死。

---

## 1. Phase 目标

1. Hook Alt Buffer：
   - `SetConsoleActiveScreenBuffer`（切换主/备缓冲区 → 输出 `\x1b[?1049h/l`）
   - `CreateConsoleScreenBuffer`（程序创建新缓冲区，需拦截并模拟）
2. Hook 标题：
   - `SetConsoleTitleW/A`（→ OSC 序列 `\x1b]0;title\x07`，WT 标签页标题更新）
   - `GetConsoleTitleW/A`（返回缓存）
3. Hook 代码页：
   - `GetConsoleCP` / `SetConsoleCP`（输入代码页）
   - `GetConsoleOutputCP` / `SetConsoleOutputCP`（输出代码页）
   - `chcp` 命令生效
4. Hook 字体：
   - `GetCurrentConsoleFontEx` / `SetCurrentConsoleFontEx`（返回缓存，避免布局错乱）
   - `GetConsoleFontSize`
5. **Wait 句柄假映射**（最关键）：
   - `GetConsoleInputWaitHandle`（返回 DLL 内部手动重置事件句柄）
   - `WaitForSingleObject` / `WaitForMultipleObjects`（检测假句柄，替换等待）
6. 验证：vim 进出屏幕恢复；`chcp 65001` 切换；cmd 标签页标题变化

---

## 2. 前置依赖

- Phase 7 完成（模式状态机可用）

---

## 3. 涉及文件清单

```
src/dll/
├── hooks/
│   ├── BufferHooks.cpp              # 扩展：Alt Buffer
│   ├── ModeHooks.cpp                # 扩展：CP/Title
│   ├── FontHooks.h / .cpp           # 新建
│   └── WaitHooks.h / .cpp           # 新建：Wait 句柄
├── state/
│   └── ConsoleState.cpp             # 扩展：altBuffer/字体字段
└── translator/VtEscape.h            # 扩展：OSC/Alt Buffer 序列
```

---

## 4. 详细任务

### 4.1 Alt Buffer

#### 4.1.1 `SetConsoleActiveScreenBuffer` Hook

```cpp
DEFINE_ORIG_PTR(SetConsoleActiveScreenBuffer, BOOL WINAPI(HANDLE));

BOOL WINAPI SetConsoleActiveScreenBuffer_Detour(HANDLE h) {
    ENSURE_INITIALIZED();
    // 判断切换到主还是备缓冲区
    // 主缓冲区：GetStdHandle(STD_OUTPUT_HANDLE) 返回的句柄
    // 备缓冲区：程序用 CreateConsoleScreenBuffer 创建的句柄
    bool toAlt = (h != ConsoleState::Instance().GetMainBufferHandle());

    ConsoleState::Instance().SetAltBufferActive(toAlt);
    // DECSET/DECRST 1049：进入/退出 Alt Buffer（含保存光标与清屏）
    std::string s = toAlt ? "\x1b[?1049h" : "\x1b[?1049l";
    SendToMediator(s.data(), s.size());
    LOG_INFO("AltBuffer %s", toAlt ? "enter" : "exit");
    return TRUE; // 不调原 API
}
```

#### 4.1.2 `CreateConsoleScreenBuffer` Hook

程序创建新缓冲区时，我们不真的创建（避免 ConHost 状态混乱），返回一个"假句柄"标识：

```cpp
DEFINE_ORIG_PTR(CreateConsoleScreenBuffer, HANDLE WINAPI(
    DWORD, DWORD, const SECURITY_ATTRIBUTES*, DWORD, LPVOID));

HANDLE WINAPI CreateConsoleScreenBuffer_Detour(
    DWORD access, DWORD share, const SECURITY_ATTRIBUTES* sa, DWORD flags, LPVOID data) {
    ENSURE_INITIALIZED();
    // 返回一个伪句柄（用固定值标识 Alt Buffer）
    // 注意：不能用真实句柄值，避免与系统冲突
    static const HANDLE kAltBufferSentinel = reinterpret_cast<HANDLE>(static_cast<uintptr_t>(0xABCDEF12));
    ConsoleState::Instance().SetAltBufferHandle(kAltBufferSentinel);
    LOG_INFO("CreateConsoleScreenBuffer → sentinel %p", kAltBufferSentinel);
    return kAltBufferSentinel;
}
```

### 4.2 标题

```cpp
DEFINE_ORIG_PTR(SetConsoleTitleW, BOOL WINAPI(LPCWSTR));

BOOL WINAPI SetConsoleTitleW_Detour(LPCWSTR title) {
    ENSURE_INITIALIZED();
    std::wstring t = title ? title : L"";
    ConsoleState::Instance().SetTitle(t);
    // OSC 0/2：\x1b]0;<title>\x07
    std::string utf8;
    int len = WideCharToMultiByte(CP_UTF8, 0, t.c_str(), -1, nullptr, 0, nullptr, nullptr);
    utf8.resize(len - 1); // 去 \0
    WideCharToMultiByte(CP_UTF8, 0, t.c_str(), -1, utf8.data(), len, nullptr, nullptr);
    std::string osc = "\x1b]0;" + utf8 + "\x07";
    SendToMediator(osc.data(), osc.size());
    return TRUE;
}

DEFINE_ORIG_PTR(GetConsoleTitleW, DWORD WINAPI(LPWSTR, DWORD));

DWORD WINAPI GetConsoleTitleW_Detour(LPWSTR buf, DWORD size) {
    ENSURE_INITIALIZED();
    auto t = ConsoleState::Instance().GetTitle();
    wcsncpy_s(buf, size, t.c_str(), _TRUNCATE);
    return static_cast<DWORD>(wcslen(buf));
}
```

### 4.3 代码页

```cpp
DEFINE_ORIG_PTR(SetConsoleCP, BOOL WINAPI(UINT));
DEFINE_ORIG_PTR(SetConsoleOutputCP, BOOL WINAPI(UINT));

BOOL WINAPI SetConsoleCP_Detour(UINT cp) {
    ENSURE_INITIALIZED();
    ConsoleState::Instance().SetInputCp(cp);
    // 通知 mediator（mediator 记录用于输入解码）
    protocol::CpChangePayload p{cp, ConsoleState::Instance().GetOutputCp()};
    auto pkt = protocol::Serialize(protocol::MessageType::CpChange, &p, sizeof(p));
    SendToMediator(pkt.data(), pkt.size());
    LOG_INFO("InputCP → %u", cp);
    return TRUE;
}
// SetConsoleOutputCP 类似
// GetConsoleCP/GetConsoleOutputCP 返回缓存（已在 ConsoleState）
```

### 4.4 字体

```cpp
DEFINE_ORIG_PTR(GetCurrentConsoleFontEx, BOOL WINAPI(HANDLE, BOOL, PCONSOLE_FONT_INFOEX));

BOOL WINAPI GetCurrentConsoleFontEx_Detour(HANDLE h, BOOL max, PCONSOLE_FONT_INFOEX info) {
    ENSURE_INITIALIZED();
    if (!IsConsoleHandle(h) || !info) return GetCurrentConsoleFontEx_orig(h, max, info);
    // 返回缓存（来自 StateSnapshot）
    *info = ConsoleState::Instance().GetFontInfo();
    info->cbSize = sizeof(CONSOLE_FONT_INFOEX);
    return TRUE;
}
// SetCurrentConsoleFontEx：记录到缓存，不真的改（WT 字体由用户配置控制）
```

### 4.5 Wait 句柄假映射（核心难点）

#### 4.5.1 问题

目标程序典型模式：
```c
HANDLE h = GetConsoleInputWaitHandle(); // 返回内核事件句柄
WaitForSingleObject(h, INFINITE);       // 阻塞等键盘输入
ReadConsoleInput(...);                  // 有输入后读
```

`GetConsoleInputWaitHandle` 返回的是 ConHost 提供的**内核事件句柄**，输入到达时由内核置位。我们劫持后，输入来自中介管道，**内核事件永远不会被置位**，程序会永远卡在 `WaitForSingleObject`。

#### 4.5.2 方案

1. Hook `GetConsoleInputWaitHandle`：返回 DLL 内部创建的**手动重置事件**句柄
2. Hook `WaitForSingleObject` / `WaitForMultipleObjects`：
   - 检测等待对象是否为我们的事件句柄
   - 若是：替换为等待 `InputQueue` 的数据到达（用另一个内部事件 + 超时轮询，或直接 `WaitForSingleObject` 内部事件）
3. `InputQueue::SignalDataReady`（Phase 6 已有）触发该事件

#### 4.5.3 `GetConsoleInputWaitHandle` Hook

```cpp
// WaitHooks.cpp
static HANDLE g_fakeWaitHandle = nullptr; // DLL 内部手动重置事件

void InitFakeWaitHandle() {
    g_fakeWaitHandle = CreateEventW(nullptr, /*manualReset=*/TRUE, FALSE, nullptr);
}

// GetConsoleInputWaitHandle 没有标准导出，需通过 GetProcAddress 动态获取
typedef HANDLE (WINAPI *GetConsoleInputWaitHandle_t)();
static GetConsoleInputWaitHandle_t GetConsoleInputWaitHandle_orig = nullptr;
static GetConsoleInputWaitHandle_t GetConsoleInputWaitHandle_detour = []() -> HANDLE {
    ENSURE_INITIALIZED();
    return g_fakeWaitHandle;
};

// 注册时：
auto p = GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "GetConsoleInputWaitHandle");
if (p) HookManager::Register({"GetConsoleInputWaitHandle", p, ...});
```

#### 4.5.4 `WaitForSingleObject` Hook

```cpp
DEFINE_ORIG_PTR(WaitForSingleObject, DWORD WINAPI(HANDLE, DWORD));

DWORD WINAPI WaitForSingleObject_Detour(HANDLE h, DWORD ms) {
    ENSURE_INITIALIZED();
    if (h == g_fakeWaitHandle) {
        // 等待输入数据
        // InputQueue 内部已有事件，直接等待它
        HANDLE realEvent = InputQueue::Instance().GetWaitHandle();
        DWORD ret = WaitForSingleObject_orig(realEvent, ms);
        // 手动重置事件：取出数据后重置（ReadConsoleInput 出队时重置）
        return ret;
    }
    // 其他句柄：正常等待（但需检查是否为日志文件句柄等，避免误伤）
    return WaitForSingleObject_orig(h, ms);
}
```

#### 4.5.5 `WaitForMultipleObjects` Hook

```cpp
DEFINE_ORIG_PTR(WaitForMultipleObjects, DWORD WINAPI(DWORD, const HANDLE*, BOOL, DWORD));

DWORD WINAPI WaitMultipleObjects_Detour(DWORD count, const HANDLE* handles,
                                        BOOL waitAll, DWORD ms) {
    ENSURE_INITIALIZED();
    // 检查句柄数组中是否含 fakeWaitHandle，替换为 InputQueue 事件
    HANDLE localHandles[MAXIMUM_WAIT_OBJECTS];
    bool hasFake = false;
    for (DWORD i = 0; i < count && i < MAXIMUM_WAIT_OBJECTS; ++i) {
        if (handles[i] == g_fakeWaitHandle) {
            localHandles[i] = InputQueue::Instance().GetWaitHandle();
            hasFake = true;
        } else {
            localHandles[i] = handles[i];
        }
    }
    if (!hasFake) return WaitForMultipleObjects_orig(count, handles, waitAll, ms);
    return WaitForMultipleObjects_orig(count, localHandles, waitAll, ms);
}
```

#### 4.5.6 事件重置时机

`InputQueue` 的事件为**手动重置**，置位后需在 `ReadConsoleInput` 出队且队列空时重置：

```cpp
size_t InputQueue::Dequeue(INPUT_RECORD* out, size_t count) {
    std::lock_guard<std::mutex> lk(m_mutex);
    size_t n = 0;
    while (n < count && !m_queue.empty()) {
        out[n++] = m_queue.front();
        m_queue.pop_front();
    }
    if (m_queue.empty()) {
        ResetEvent(m_event);
    }
    return n;
}
```

### 4.6 VT 序列扩展

```cpp
// VtEscape.h 扩展
// Alt Buffer
constexpr const char* kEnterAltBuffer = "\x1b[?1049h";
constexpr const char* kExitAltBuffer  = "\x1b[?1049l";
// 光标显隐
constexpr const char* kShowCursor   = "\x1b[?25h";
constexpr const char* kHideCursor   = "\x1b[?25l";

// OSC 标题：\x1b]0;<title>\x07
std::string SetTitle(const std::string& utf8Title);
```

---

## 5. 验证标准

| 测试 | 预期 |
|------|------|
| vim 打开文件 → `:q` 退出 | 进入时切 Alt Buffer，退出后恢复原屏幕内容 |
| `less file.txt` → `q` | 同上 |
| cmd `title hello` | WT 标签页标题变 hello |
| `chcp 65001` + 输出中文 | UTF-8 中文正常显示 |
| `chcp 936` + 输出 GBK | 切换后中文按 GBK 解码 |
| python `input()` 阻塞等待 | 不假死，输入后返回（验证 Wait 句柄） |
| vim 中按方向键 | 光标移动不卡顿（验证 Wait 不阻塞） |

---

## 6. 风险点

| 风险 | 缓解 |
|------|------|
| `GetConsoleInputWaitHandle` 在部分 Windows 版本未导出 | `GetProcAddress` 判空；未导出时程序一般不用，跳过 |
| `WaitForSingleObject` Hook 误伤其他句柄（如 mutex/thread） | 仅替换 fakeWaitHandle，其他原样调用 |
| Alt Buffer 假句柄被程序 CloseHandle | Phase 9 CloseHandle Hook 跳过假句柄 |
| 手动重置事件未及时 ResetEvent 导致忙等 | Dequeue 空时立即 ResetEvent |
| `CreateConsoleScreenBuffer` 返回固定句柄值冲突 | 用不可能为真实句柄的魔数 |

---

## 7. 交付物清单

- [ ] `BufferHooks.cpp` Alt Buffer + CreateConsoleScreenBuffer
- [ ] `ModeHooks.cpp` Title/CP 扩展
- [ ] `FontHooks.cpp` 字体 Hook
- [ ] `WaitHooks.cpp` GetConsoleInputWaitHandle + WaitFor*
- [ ] `VtEscape.h` Alt Buffer/OSC 序列
- [ ] 验证 vim 进出 + chcp + input 不假死
