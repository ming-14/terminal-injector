# Phase 7: 模式与信号

> 本 Phase 实现 Console 模式状态机与 Ctrl+C 信号传递。完成后，目标程序的 `GetConsoleMode` 调用会被欺骗（强制认为 VT 已开启，从而主动发 VT 序列），且 WT 中的 Ctrl+C 能正确中断目标程序。

---

## 1. Phase 目标

1. Hook `GetConsoleMode`：根据劫持策略篡改返回值
   - 输出方向：强制返回含 `ENABLE_VIRTUAL_TERMINAL_PROCESSING`（让程序发 VT 序列）
   - 输入方向：根据 DLL 当前翻译能力返回（老式模式让 DLL 翻译，VT 模式让 DLL 透传）
2. Hook `SetConsoleMode`：维护模式状态机，同步给中介
   - 程序切换模式时，DLL 更新 `ConsoleState` 并发 `ModeChange` 消息
   - 中介据此调整输入翻译策略
3. Hook `SetConsoleCtrlHandler`：记录目标程序注册的回调
4. Hook `GenerateConsoleCtrlEvent`：拦截程序自发 Ctrl+C
5. 实现 Ctrl+C 完整链路：
   - WT 按 Ctrl+C → 中介收到 `\x03` → DLL 识别为 Ctrl+C
   - DLL 触发目标程序注册的 CtrlHandler 回调（在目标进程内模拟信号）
   - 若程序未注册回调，模拟 `CTRL_C_EVENT` 默认行为
6. 验证：python `while True: pass` 可被 Ctrl+C 中断；cmd 中 Ctrl+Break

---

## 2. 前置依赖

- Phase 6 完成（输入链路可用，`VtToInputRecord` 能识别 `\x03`）
- `ConsoleState` 已有 `inputMode`/`outputMode` 字段

---

## 3. 涉及文件清单

```
src/dll/
├── hooks/
│   ├── ModeHooks.h / .cpp            # 新建：模式类 Hook
│   └── SignalHooks.h / .cpp          # 新建：Ctrl 信号类 Hook
├── state/
│   └── ConsoleState.cpp              # 扩展：模式变更通知
└── common/protocol/Message.h         # 已有 ModeChange，无需改
```

---

## 4. 详细任务

### 4.1 `GetConsoleMode` 欺骗策略

目标程序调用 `GetConsoleMode` 查询能力。若返回值不含 `ENABLE_VIRTUAL_TERMINAL_PROCESSING`，程序会走老式 Console API（`WriteConsoleOutput` 等）而非发 VT 序列。我们的策略：

**输出方向**：始终返回含 `ENABLE_VIRTUAL_TERMINAL_PROCESSING`，**欺骗**程序认为支持 VT，让它主动发 VT 序列（更省翻译工作）。即使程序原本没开，也假装开了。

**输入方向**：
- 若 DLL 翻译能力就绪（Phase 6 完成）：返回老式模式（`ENABLE_ECHO_INPUT | ENABLE_LINE_INPUT | ...`），让程序用 `ReadConsoleInput` 读结构体，DLL 负责翻译
- 若程序主动请求 `ENABLE_VIRTUAL_TERMINAL_INPUT`：允许，进入透传模式

```cpp
DEFINE_ORIG_PTR(GetConsoleMode, BOOL WINAPI(HANDLE, LPDWORD));

BOOL WINAPI GetConsoleMode_Detour(HANDLE h, LPDWORD mode) {
    ENSURE_INITIALIZED();
    if (!IsConsoleHandle(h) || !mode) return GetConsoleMode_orig(h, mode);

    auto& state = ConsoleState::Instance();
    DWORD realMode = state.GetOutputMode(); // 缓存值
    DWORD inputMode = state.GetInputMode();

    // 判断句柄是输入还是输出（用 GetConsoleMode 原始调用判断，或缓存句柄类型）
    // 简化：CONOUT$ 返回 outputMode，CONIN$ 返回 inputMode
    if (IsInputHandle(h)) {
        // 输入：返回程序请求的模式（已在 SetConsoleMode 中记录）
        // 但强制清除我们不支持翻译的标志？实际上都支持，原样返回
        *mode = inputMode;
    } else {
        // 输出：强制加 VT 处理标志
        *mode = realMode | ENABLE_VIRTUAL_TERMINAL_PROCESSING;
    }
    return TRUE;
}
```

### 4.2 `SetConsoleMode` 状态机

```cpp
DEFINE_ORIG_PTR(SetConsoleMode, BOOL WINAPI(HANDLE, DWORD));

BOOL WINAPI SetConsoleMode_Detour(HANDLE h, DWORD mode) {
    ENSURE_INITIALIZED();
    if (!IsConsoleHandle(h)) return SetConsoleMode_orig(h, mode);

    auto& state = ConsoleState::Instance();
    DWORD oldInputMode = state.GetInputMode();
    DWORD oldOutputMode = state.GetOutputMode();

    if (IsInputHandle(h)) {
        state.SetInputMode(mode);
        if (mode != oldInputMode) {
            NotifyModeChange(); // 发 ModeChange 给中介
            // 切换时清空输入队列，避免模式混杂
            InputQueue::Instance().Clear();
            LOG_INFO("Input mode changed: 0x%lx → 0x%lx (VT_INPUT=%d)",
                     oldInputMode, mode, (mode & ENABLE_VIRTUAL_TERMINAL_INPUT) ? 1 : 0);
        }
    } else {
        // 输出：程序可能想关 VT，但我们强制保留（欺骗）
        DWORD forced = mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING;
        state.SetOutputMode(forced);
        if (forced != oldOutputMode) NotifyModeChange();
    }
    return TRUE; // 不调原 API（避免 ConHost 真改）
}
```

### 4.3 模式变更通知中介

```cpp
void NotifyModeChange() {
    protocol::ModeChangePayload p{};
    p.inputMode = ConsoleState::Instance().GetInputMode();
    p.outputMode = ConsoleState::Instance().GetOutputMode();
    auto pkt = protocol::Serialize(protocol::MessageType::ModeChange, &p, sizeof(p));
    SendToMediator(pkt.data(), pkt.size());
}
```

mediator 收到后仅记录用于日志/调试，无需特殊处理（输入翻译在 DLL 侧完成）。

### 4.4 Ctrl+C 信号链路

#### 4.4.1 问题背景

WT 中按 Ctrl+C，会生成 VT 字符 `\x03` 通过 stdin 发给中介。中介透传给 DLL。DLL 需要识别 `\x03` 并触发目标程序的 Ctrl+C 处理逻辑。

但目标程序可能：
- 用 `SetConsoleCtrlHandler` 注册了回调 → DLL 应调用该回调
- 未注册 → 默认行为（终止进程，但**不能真终止**，应转为 `CTRL_C_EVENT` 让程序自行处理）

#### 4.4.2 Hook `SetConsoleCtrlHandler`

记录目标程序注册的回调函数指针：

```cpp
// SignalHooks.cpp
struct CtrlHandlerEntry {
    PHANDLER_ROUTINE handler;
    BOOL addOrRemove;
};

static std::vector<CtrlHandlerEntry> g_ctrlHandlers;
static SRWLOCK g_handlersLock = SRWLOCK_INIT;

DEFINE_ORIG_PTR(SetConsoleCtrlHandler, BOOL WINAPI(PHANDLER_ROUTINE, BOOL));

BOOL WINAPI SetConsoleCtrlHandler_Detour(PHANDLER_ROUTINE handler, BOOL add) {
    ENSURE_INITIALIZED();
    // 记录到本地表（不调原 API，避免 ConHost 真注册）
    AcquireSRWLockExclusive(&g_handlersLock);
    if (add) g_ctrlHandlers.push_back({handler, TRUE});
    else {
        g_ctrlHandlers.erase(
            std::remove_if(g_ctrlHandlers.begin(), g_ctrlHandlers.end(),
                           [&](const CtrlHandlerEntry& e) { return e.handler == handler; }),
            g_ctrlHandlers.end());
    }
    ReleaseSRWLockExclusive(&g_handlersLock);
    LOG_INFO("SetConsoleCtrlHandler %p add=%d (total=%zu)",
             handler, add, g_ctrlHandlers.size());
    return TRUE;
}
```

#### 4.4.3 触发 CtrlHandler

DLL 收到 `\x03` 时（在 `VtToInputRecord::ParseKeyboard` 中识别），除入队 `KEY_EVENT` 外，额外触发 CtrlHandler：

```cpp
// 在 VtInputParser 识别到 \x03 时调用
void TriggerCtrlC() {
    AcquireSRWLockShared(&g_handlersLock);
    // 逆序调用（后注册的优先）
    for (auto it = g_ctrlHandlers.rbegin(); it != g_ctrlHandlers.rend(); ++it) {
        if (it->handler && it->handler(CTRL_C_EVENT)) {
            // 回调返回 TRUE 表示已处理，停止传播
            break;
        }
    }
    ReleaseSRWLockShared(&g_handlersLock);
    // 若无回调或都返回 FALSE，默认行为：入队 KEY_EVENT 让程序自己读
}
```

**注意**：CtrlHandler 回调在目标进程的上下文执行（DLL 在目标进程内），可直接调用。但回调可能期待在独立线程执行（系统原本是新线程调用），简单同步调用可能死锁。**优化**：用 `CreateThread` 在新线程触发回调。

#### 4.4.4 `GenerateConsoleCtrlEvent` Hook

程序可能自发 Ctrl+C（如 Python 的 `KeyboardInterrupt` 传播）：

```cpp
DEFINE_ORIG_PTR(GenerateConsoleCtrlEvent, BOOL WINAPI(DWORD, DWORD));

BOOL WINAPI GenerateConsoleCtrlEvent_Detour(DWORD event, DWORD pidGroup) {
    ENSURE_INITIALIZED();
    // 拦截：不真的发信号给 ConHost，转为触发本地 CtrlHandler
    if (event == CTRL_C_EVENT || event == CTRL_BREAK_EVENT) {
        AcquireSRWLockShared(&g_handlersLock);
        for (auto it = g_ctrlHandlers.rbegin(); it != g_ctrlHandlers.rend(); ++it) {
            if (it->handler && it->handler(event)) break;
        }
        ReleaseSRWLockShared(&g_handlersLock);
        return TRUE;
    }
    return GenerateConsoleCtrlEvent_orig(event, pidGroup);
}
```

### 4.5 输入流中 Ctrl+C 识别

在 `VtInputParser::Feed` 中，遇到 `\x03` 字节时：

```cpp
if (vt[i] == '\x03') {
    TriggerCtrlC();
    // 同时入队 KEY_EVENT（程序若 ReadConsoleInput 也能读到）
    records.push_back(MakeKeyRecord(true, 'C', L'\x03', LEFT_CTRL_PRESSED));
    records.push_back(MakeKeyRecord(false, 'C', L'\x03', LEFT_CTRL_PRESSED));
    i += 1;
    continue;
}
```

### 4.6 句柄类型判断（`IsInputHandle`）

```cpp
// 缓存 stdin/stdout 句柄（GetStdHandle 在 Phase 9 Hook，此处先用真实值）
static HANDLE g_realStdin  = nullptr;
static HANDLE g_realStdout = nullptr;

void CacheStdHandles() {
    g_realStdin  = GetStdHandle(STD_INPUT_HANDLE);
    g_realStdout = GetStdHandle(STD_OUTPUT_HANDLE);
}

bool IsInputHandle(HANDLE h) {
    return h == g_realStdin || h == g_realStdinPlaceholder;
}
```

---

## 5. 验证标准

| 测试 | 预期 |
|------|------|
| python `import time; while True: time.sleep(1)` + Ctrl+C | 抛 `KeyboardInterrupt` |
| cmd 中 Ctrl+C | 中断当前命令（如 `ping -t`） |
| cmd 中 Ctrl+Break | 中断并显示 `^C` |
| 程序 `SetConsoleMode(ENABLE_VIRTUAL_TERMINAL_INPUT)` | DLL 切透传模式，日志记录 |
| 程序 `GetConsoleMode` 查输出 | 返回值含 `ENABLE_VIRTUAL_TERMINAL_PROCESSING` |
| vim 中 Ctrl+C | 退出插入模式 |

---

## 6. 风险点

| 风险 | 缓解 |
|------|------|
| CtrlHandler 同步调用死锁 | 用 `CreateThread` 异步触发 |
| 程序检测 `GetConsoleMode` 返回值与预期不符 | 仔细模拟：保留程序请求的标志，仅强制加 VT |
| `SetConsoleMode` 不调原 API 导致 ConHost 模式不同步 | 接受（Phase 9 后 ConHost 不参与） |
| Ctrl+Break 与 Ctrl+C 区分 | VT 中 Ctrl+Break 无标准序列，依赖 WT 配置；`VK_CANCEL` 单独处理 |
| 信号触发时目标程序正在 ReadConsoleInput 阻塞 | CtrlHandler 在独立线程，不影响阻塞 |

---

## 7. 交付物清单

- [ ] `ModeHooks.cpp` Get/SetConsoleMode 状态机
- [ ] `SignalHooks.cpp` SetConsoleCtrlHandler + GenerateConsoleCtrlEvent
- [ ] Ctrl+C 触发逻辑（独立线程）
- [ ] `IsInputHandle` 句柄类型判断
- [ ] 验证 Ctrl+C 中断 python 死循环
