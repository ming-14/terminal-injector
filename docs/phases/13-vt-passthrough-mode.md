# Phase 13: VT 直通模式（混合模式自动切换）

> 本 Phase 实现 DLL 在「行编辑模式」与「VT 直通模式」之间的自动切换。完成后，cmd.exe 等行编辑程序走 LineEditor + INPUT_RECORD 翻译路径；vim/less/ncurses 等 VT 程序走字节流直通路径，避免冗余翻译，保证 TUI 程序的输入输出语义与原生 VT 终端一致。
>
> **触发条件**：检测到程序调用 `SetConsoleMode(stdin, ENABLE_VIRTUAL_TERMINAL_INPUT)` 时切到 VT 直通；程序清除该标志时切回行编辑模式。

---

## 1. 背景与动机

### 1.1 问题

Phase 6/7 已实现「行编辑模式」：mediator 把 WT 的 VT 输入翻译为 `INPUT_RECORD`，DLL 通过 `ReadConsoleInputW` / `ReadConsoleW` 返回给程序。该路径对 cmd.exe 等行编辑程序合适，但对 vim/less/ncurses 这类 TUI 程序存在以下问题：

1. **冗余翻译**：TUI 程序期望收到原始 VT 字节（`\x1b[A` 等）， mediator→DLL 路径却把 VT 翻译为 `INPUT_RECORD`（`KEY_EVENT` 上/下键），DLL 再把 `INPUT_RECORD` 翻译回 VT，等价于「VT → INPUT_RECORD → VT」无意义往返。
2. **语义丢失**：SGR 1006 鼠标序列、修饰键组合（如 Ctrl+Shift+方向键）在翻译过程中可能丢失信息。
3. **状态查询错位**：TUI 程序不依赖 `GetConsoleScreenBufferInfo`，自己维护虚拟屏幕；DLL 仍维护 ConsoleState 是浪费。
4. **输出反向问题**：TUI 程序直接调 `WriteFile(stdout)` 发 VT 序列，Phase 4 的 `WriteConsoleW` Hook 拦不到，需要 `WriteFile` Hook 在 VT 模式下直通。

### 1.2 目标

引入「VT 直通模式」，与「行编辑模式」并存，由 `SetConsoleMode` 自动切换：

```
┌─────────────┬──────────────────────┬──────────────────────┐
│             │ 行编辑模式（cmd）    │ VT 直通模式（vim）   │
├─────────────┼──────────────────────┼──────────────────────┤
│ 输入路径    │ mediator → VtToInput │ mediator → 字节流    │
│             │ → InputQueue         │ → RawByteQueue       │
│             │ → INPUT_RECORD       │ → ReadFile 直通      │
├─────────────┼──────────────────────┼──────────────────────┤
│ 输出路径    │ WriteConsole →       │ WriteFile(stdout) →  │
│             │ ConsoleToVt → mediator│ 直通 mediator → WT  │
├─────────────┼──────────────────────┼──────────────────────┤
│ 状态维护    │ ConsoleState +       │ 不需要（程序自维护） │
│             │ LineEditor           │                      │
└─────────────┴──────────────────────┴──────────────────────┘
```

### 1.3 与 Phase 7 的关系

Phase 7 已实现 `SetConsoleMode` Hook 维护模式状态机。本 Phase 在此基础上：
- 检测到 `ENABLE_VIRTUAL_TERMINAL_INPUT` 切换时，触发模式切换流程
- 清空 `InputQueue` 中残留的 `INPUT_RECORD`
- 通知 mediator 切到 VT 直通模式（停止 VtToInput 翻译，直接转发字节）

---

## 2. 前置依赖

- Phase 6 完成（输入链路、`InputQueue` 已就绪）
- Phase 7 完成（`SetConsoleMode` Hook、`ConsoleState` 模式字段就绪）
- Phase 12 完成（子进程注入，vim/python/less 子进程可被注入）

---

## 3. 涉及文件清单

```
src/
├── common/protocol/
│   └── Message.h                    # 扩展：ModeSwitchNotify 消息
├── dll/
│   ├── hooks/
│   │   ├── ModeHooks.cpp            # 扩展：SetConsoleMode 检测 VT_INPUT 切换
│   │   ├── InputHooks.cpp           # 扩展：ReadFile 根据模式走 RawByteQueue 或 InputQueue
│   │   └── OutputHooks.cpp          # 扩展：WriteFile(stdout) 在 VT 模式下直通
│   ├── state/
│   │   ├── InputQueue.h             # 扩展：RawByteQueue 子队列
│   │   ├── InputQueue.cpp           # 实现：原始字节入队/出队
│   │   └── ConsoleState.h           # 扩展：IsVTInputMode() 接口
│   └── translator/
│       └── VtToInputRecord.h        # 标注：仅在行编辑模式调用
└── mediator/
    ├── Mediator.cpp                 # 扩展：根据 ModeSwitchNotify 切换输入翻译策略
    └── VtPassThrough.cpp            # 新增：VT 模式下字节流透传
```

---

## 4. 详细任务

### 4.1 模式判定

DLL 内通过 `ConsoleState` 维护当前输入模式：

```cpp
// ConsoleState.h 扩展
class ConsoleState {
    // 输入模式（来自 SetConsoleMode）
    std::atomic<DWORD> m_inputMode{0};

public:
    bool IsVTInputMode() const {
        return (m_inputMode.load(std::memory_order_acquire)
                & ENABLE_VIRTUAL_TERMINAL_INPUT) != 0;
    }
    void SetInputMode(DWORD mode) {
        m_inputMode.store(mode, std::memory_order_release);
    }
};
```

判定逻辑：`IsVTInputMode()` 为 true → VT 直通模式；false → 行编辑模式。

### 4.2 SetConsoleMode Hook 触发模式切换

扩展 Phase 7 的 `SetConsoleMode_Detour`：

```cpp
BOOL WINAPI SetConsoleMode_Detour(HANDLE h, DWORD mode) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (!IsInputHandle(h)) {
        return SetConsoleMode_orig(h, mode);
    }

    auto& state = ConsoleState::Instance();
    DWORD oldMode = state.GetInputMode();
    bool oldVT = (oldMode & ENABLE_VIRTUAL_TERMINAL_INPUT) != 0;
    bool newVT = (mode & ENABLE_VIRTUAL_TERMINAL_INPUT) != 0;

    // 更新 ConsoleState
    state.SetInputMode(mode);

    // 模式切换：清空 InputQueue，通知 mediator
    if (oldVT != newVT) {
        InputQueue::Instance().Clear();  // 清空残留 INPUT_RECORD 或字节
        NotifyModeSwitch(newVT);
        LOG_INFO("SetConsoleMode: switch to %s mode (old=0x%x new=0x%x)",
                 newVT ? "VT passthrough" : "line edit", oldMode, mode);
    }

    // 不调 orig（ConHost 模式状态由 DLL 管理，避免 ConHost 也进入 VT 模式后行为异常）
    // 但需调 orig 让 ConHost 跟随（程序若直接调 GetConsoleMode 查 ConHost 也能拿到一致值）
    return SetConsoleMode_orig(h, mode);
}
```

**关键点**：
- 模式切换时**必须清空 InputQueue**，避免残留的 `INPUT_RECORD` 在 VT 模式下被 `ReadFile` 误读为字节
- 通知 mediator 同步切换翻译策略，避免双翻译

### 4.3 InputQueue 双模式实现

`InputQueue` 同时维护两种数据：`INPUT_RECORD` 队列（行编辑模式）和原始字节队列（VT 模式）。

```cpp
// InputQueue.h 扩展
class InputQueue {
    // 行编辑模式：INPUT_RECORD 队列（已有）
    std::vector<INPUT_RECORD> m_records;
    std::mutex m_recordsMutex;
    HANDLE m_recordsEvent;  // 数据到达事件

    // VT 直通模式：原始字节队列（新增）
    std::vector<uint8_t> m_rawBytes;
    std::mutex m_rawMutex;
    HANDLE m_rawEvent;  // 字节到达事件（可与 m_recordsEvent 共用）

public:
    // 行编辑模式 API（已有）
    size_t DequeueRecords(PINPUT_RECORD buf, size_t count);
    void EnqueueRecords(const INPUT_RECORD* buf, size_t count);

    // VT 直通模式 API（新增）
    size_t DequeueRaw(uint8_t* buf, size_t count);
    void EnqueueRaw(const uint8_t* buf, size_t count);

    // 公用：清空两个队列
    void Clear();

    // 公用：等待事件（两种模式共用一个 event，简化 WaitForSingleObject 逻辑）
    HANDLE GetWaitHandle() const { return m_recordsEvent; }
};
```

**关键点**：
- 两种模式共用一个等待事件，简化 `WaitForSingleObject` 调用
- `Clear()` 同时清空两个队列，模式切换时调用
- 入队时根据当前模式选择 `EnqueueRecords` 或 `EnqueueRaw`（由 mediator 调用）

### 4.4 ReadFile Hook 根据模式分流

扩展 Phase 6 的 `ReadFile_Detour`（已部分实现 VT 透传，本 Phase 完善）：

```cpp
BOOL WINAPI ReadFile_Detour(HANDLE h, LPVOID buf, DWORD len,
                             LPDWORD read, LPOVERLAPPED ov) {
    // 快速路径：非 stdin 直接调原 API
    if (h != GetCachedStdin()) {
        return ReadFile_orig(h, buf, len, read, ov);
    }

    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (IsInLazyInit()) {
        return ReadFile_orig(h, buf, len, read, ov);
    }

    ReadDetourGuard readGuard;

    auto& state = ConsoleState::Instance();
    if (!state.IsVTInputMode()) {
        // 行编辑模式：走原 ReadConsoleW 路径（ReadFile 不应被 cmd 调用）
        // 简化：直接调原 ReadFile，让 ConHost 处理
        readGuard.release();
        return ReadFile_orig(h, buf, len, read, ov);
    }

    // VT 直通模式：从原始字节队列读
    auto& queue = InputQueue::Instance();
    while (true) {
        size_t n = queue.DequeueRaw(static_cast<uint8_t*>(buf),
                                     static_cast<size_t>(len));
        if (n > 0) {
            *read = static_cast<DWORD>(n);
            return TRUE;
        }
        // transport 断开 / 卸载：pass-through 到 orig
        if (!IsTransportConnected() || Unloader::IsUnloading()) {
            readGuard.release();
            return ReadFile_orig(h, buf, len, read, ov);
        }
        WaitForSingleObject(queue.GetWaitHandle(), 100);
    }
}
```

**关键点**：VT 模式下 `ReadFile(stdin)` 是 vim/less 的主路径，必须从 `RawByteQueue` 返回原始字节。

### 4.5 WriteFile Hook 在 VT 模式下直通

新增 `WriteFile` Hook（之前未实现），仅拦截 stdout：

```cpp
DEFINE_ORIG_PTR(WriteFile, BOOL WINAPI(HANDLE, LPCVOID, DWORD, LPDWORD, LPOVERLAPPED));

BOOL WINAPI WriteFile_Detour(HANDLE h, LPCVOID buf, DWORD len,
                              LPDWORD written, LPOVERLAPPED ov) {
    // 快速路径：非 stdout 直接调原 API（避免拦截文件 IO/管道 IO）
    if (h != GetCachedStdout()) {
        return WriteFile_orig(h, buf, len, written, ov);
    }

    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (IsInLazyInit()) {
        return WriteFile_orig(h, buf, len, written, ov);
    }

    auto& state = ConsoleState::Instance();
    // 仅在 VT 输出模式下直通（程序发的就是 VT 序列）
    if ((state.GetOutputMode() & ENABLE_VIRTUAL_TERMINAL_PROCESSING) == 0) {
        // 行编辑模式：调原 WriteFile，让 ConHost 处理（cmd 极少调 WriteFile 写 stdout）
        return WriteFile_orig(h, buf, len, written, ov);
    }

    // VT 直通模式：直接转发字节给 mediator → WT
    SendToMediator(static_cast<const uint8_t*>(buf), len);
    if (written) *written = len;
    return TRUE;
}
```

**关键点**：
- 必须判断 `h == GetCachedStdout()`，避免拦截文件写、管道写（transport Recv 用 ReadFile，transport Send 用 WriteFile 写管道，会递归）
- 用 `HookReentryGuard` 防 `SendToMediator` 内部 `WriteFile` 重入
- 仅在 `ENABLE_VIRTUAL_TERMINAL_PROCESSING` 开启时直通，避免误拦截 cmd 的 raw 输出

### 4.6 mediator 模式切换处理

mediator 收到 `ModeSwitchNotify` 后切换输入翻译策略：

```cpp
// Mediator.cpp 扩展
void Mediator::OnModeSwitchNotify(bool vtMode) {
    m_vtInputMode.store(vtMode);
    LOG_INFO("Mediator: switch to %s input mode", vtMode ? "VT passthrough" : "line edit");
}

// 从 WT 收到 VtInput 后的处理
void Mediator::OnVtInput(const uint8_t* data, size_t len) {
    if (m_vtInputMode.load()) {
        // VT 直通模式：原样转发字节给 DLL
        SendToDll(data, len);
    } else {
        // 行编辑模式：翻译 VT → INPUT_RECORD 字节流（已有 VtToInputRecord 逻辑）
        auto records = VtToInputRecord(data, len);
        SendToDll(records.data(), records.size());
    }
}
```

**关键点**：mediator 与 DLL 模式状态必须一致，否则双翻译或漏翻译。

### 4.7 模式切换时序

```
程序调 SetConsoleMode(stdin, ENABLE_VIRTUAL_TERMINAL_INPUT)
  │
  ▼
DLL: SetConsoleMode_Detour
  │  1. 检测 VT_INPUT 标志变化
  │  2. 更新 ConsoleState
  │  3. InputQueue.Clear()
  │  4. 发 ModeSwitchNotify(vtMode=true) 给 mediator
  │  5. 调 orig（让 ConHost 跟随）
  │
  ▼
mediator: OnModeSwitchNotify(true)
  │  切换到 VT 直通，停止 VtToInput 翻译
  │
  ▼
后续 WT 输入：mediator 原样转发字节 → DLL → RawByteQueue → ReadFile 返回
```

### 4.8 新增消息类型

```cpp
// Message.h 扩展
enum class MessageType : uint32_t {
    // ... 已有消息 ...

    // 模式切换通知（Phase 13）
    ModeSwitchNotify = 0x0070,  // DLL→mediator：VT 模式切换
};

// ModeSwitchNotify payload
struct ModeSwitchNotifyPayload {
    uint32_t vtInputMode;   // 1=VT 直通, 0=行编辑
    uint32_t vtOutputMode;  // 1=VT 处理, 0=老式
};
static_assert(sizeof(ModeSwitchNotifyPayload) == 8, "...");
```

---

## 5. 验证标准

| 测试 | 预期 | 说明 |
|------|------|------|
| cmd 启动 vim，正常输入命令 | vim 收到原始 VT 字节，无翻译损耗 | VT 输入直通 |
| vim 内 `:q` 退出 | cmd 恢复行编辑模式，方向键历史可用 | 模式回退 |
| vim 内鼠标点击 | SGR 1006 序列直达 vim，无坐标转换丢失 | VT 鼠标直通 |
| vim 输出满屏重绘 | VT 序列直达 WT，无 ConsoleToVt 翻译 | VT 输出直通 |
| 模式切换瞬间无字符丢失 | SetConsoleMode 前后输入的字符都能正确处理 | 队列清空时序正确 |
| cmd `python -c "import sys; sys.stdin.read(10)"` 输入 10 字符 | python 收到原始字节 | ReadFile VT 直通 |
| cmd 行编辑模式下 `WriteFile(stdout)` | 走 ConHost，不直通 | 模式判定正确 |
| 高频输出 `cat 大文件` | 无翻译开销，性能优于行编辑模式 | 性能验证 |

---

## 6. 风险点

| 风险 | 影响 | 缓解 |
|------|------|------|
| 模式切换时序竞态（切换瞬间输入丢失） | 字符丢失或残留 | 切换前清空 InputQueue，切换后立即生效；ModeSwitchNotify 同步等待 mediator 确认 |
| 程序未调 SetConsoleMode 直接 ReadFile(stdin) | 模式判定失败，走错路径 | 行编辑模式下 ReadFile 仍调 orig，让 ConHost 处理（兼容老程序） |
| WriteFile Hook 拦截范围过广 | 文件 IO/管道 IO 被误拦截 | 严格判断 `h == GetCachedStdout()` + `ENABLE_VIRTUAL_TERMINAL_PROCESSING` |
| SendToMediator 内 WriteFile 递归 | 死锁 | HookReentryGuard + HookWhitelist 保护 transport 写句柄 |
| 程序同时设置 VT_INPUT 和 ENABLE_LINE_INPUT | 模式歧义 | VT_INPUT 优先（与 Windows Console 语义一致） |
| 子进程模式继承 | 子进程模式与父进程不同 | Phase 12 子进程独立 ConsoleState，独立模式切换 |
| ConHost 与 DLL 模式不一致 | 程序查询 ConHost 状态错位 | 调 orig 让 ConHost 跟随（Phase 14 虚拟状态完整解决） |

---

## 7. 交付物清单

- [ ] `ConsoleState` 扩展：`IsVTInputMode()` / `GetOutputMode()` 接口
- [ ] `InputQueue` 扩展：`RawByteQueue` 子队列 + `DequeueRaw` / `EnqueueRaw`
- [ ] `ModeHooks.cpp` 扩展：`SetConsoleMode` 检测 VT_INPUT 切换 + 通知 mediator
- [ ] `InputHooks.cpp` 扩展：`ReadFile_Detour` 根据模式分流
- [ ] `OutputHooks.cpp` 新增：`WriteFile_Detour`（VT 模式下直通 stdout）
- [ ] `Message.h` 扩展：`ModeSwitchNotify` 消息类型
- [ ] `Mediator.cpp` 扩展：`OnModeSwitchNotify` + 输入翻译策略切换
- [ ] `VtPassThrough.cpp` 新增：VT 字节流透传逻辑
- [ ] 验证：vim/less 进出 + 鼠标 + 模式切换时序

---

## 8. 与其他 Phase 的关系

```
Phase 6 (输入链路) ──┐
Phase 7 (模式 Hook) ──┼──► Phase 13 (VT 直通模式)
Phase 12 (子进程注入)─┘            │
                                    ▼
                              vim/less/ncurses 可用
                                    │
                                    ▼
                              Phase 14 (虚拟状态) 解决程序查询状态
                              Phase 16 (鼠标坐标) 解决 VT 鼠标映射
```

**依赖**：
- Phase 6/7 提供 `InputQueue` / `ConsoleState` / `SetConsoleMode` Hook 基础
- Phase 12 提供子进程注入（vim/python/less 才能被注入并触发模式切换）

**被依赖**：
- Phase 14（虚拟状态）：VT 模式下程序不查询状态，但模式切换时需保持状态一致
- Phase 16（鼠标坐标）：VT 模式下鼠标序列直通，无需坐标转换
- Phase 18（滚动缓冲区）：VT 模式下程序用 Alt Buffer，无 scrollback 一致性问题

---

## 9. 备注

### 9.1 与 ConPTY 的对比

ConPTY 是 Windows 官方的伪终端方案，本 Phase 的 VT 直通模式在效果上类似 ConPTY：
- 程序通过 VT 序列与终端交互
- 输入输出都是字节流

但本方案保留了行编辑模式，对 cmd.exe 等行编辑程序提供更精细的控制（LineEditor 实现等价 ConHost 行编辑行为），这是 ConPTY 不提供的。

### 9.2 模式切换的「滞后」问题

程序调 `SetConsoleMode` 后立即 `ReadFile`，可能遇到：
- ModeSwitchNotify 还没到 mediator
- mediator 仍在用旧翻译策略发数据

解决方案：
- DLL 内模式切换立即生效（`ConsoleState` 是原子变量）
- InputQueue 是 DLL 本地数据，不受 mediator 状态影响
- mediator 模式切换滞后最多导致一帧数据用错翻译策略，可接受（程序下次输入时已同步）

如需严格一致，可在 `ModeSwitchNotify` 后等 mediator 回 ACK 再切换，但会增加延迟。
