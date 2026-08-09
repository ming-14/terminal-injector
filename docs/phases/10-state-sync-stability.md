# Phase 10: 状态同步优化与稳定性

> 本 Phase 聚焦性能与稳定性。前面各 Phase 的实现优先"能用"，本 Phase 解决高频场景下的性能瓶颈、死锁隐患、状态漂移，使劫持体验达到"流畅"标准。

---

## 1. Phase 目标

1. **后台状态轮询线程**：注入后前 3 秒高频（100ms）轮询真实 ConHost 状态补全快照，3 秒后停止依赖 Hook 缓存
2. **鼠标事件攒批**：DLL 内部对鼠标 `INPUT_RECORD` 攒批（16ms 或 20 条），打包一次入队，减少队列锁竞争
3. **WT resize 改用事件**：mediator 侧用 `ReadConsoleInput` 监听 `WINDOW_BUFFER_SIZE_EVENT`，替代 100ms 轮询
4. **死锁防护**：
   - Hook 内禁用任何可能重入的 API（梳理白名单）
   - `SendToMediator` 用独立锁，与 `ConsoleState` 锁分离
   - 日志路径无锁化（用 ring buffer + 后台刷盘线程）
5. **性能调优**：
   - `WriteConsoleOutput` diff 算法（仅输出变化的 cell）
   - VT 序列合并（连续 SGR 合并）
   - IPC 包合并（小包攒批发送）
6. **共享内存传输（可选扩展）**：实现 `SharedMemoryTransport` 作为 `ITransport` 的备选实现
7. 验证：opencode 满屏重绘 60fps 无撕裂；鼠标拖拽无延迟；长时间运行无内存泄漏

---

## 2. 前置依赖

- Phase 9 完成（自保护就绪，Hook 已静默化）

---

## 3. 涉及文件清单

```
src/dll/
├── state/
│   ├── StatePoller.h / .cpp          # 新建：后台轮询线程
│   └── InputQueue.cpp                # 修改：攒批逻辑
├── hooks/HookWhitelist.h             # 新建：Hook 内可调 API 白名单
├── translator/
│   └── ConsoleToVt.cpp               # 修改：diff 算法
└── common/
    ├── transport/
    │   └── SharedMemoryTransport.h / .cpp  # 新建（可选）
    └── logging/
        └── RingBufferLogger.h        # 新建：无锁日志
```

---

## 4. 详细任务

### 4.1 后台状态轮询线程

```cpp
// StatePoller.h
#pragma once
#include <windows.h>
#include <thread>
#include <atomic>

namespace terminjector {

// 注入后短期轮询真实 ConHost 状态，补全快照
// 3 秒后停止（Hook 已接管所有 Set/Write 调用）
class StatePoller {
public:
    static StatePoller& Instance();

    void Start(); // 在 LazyInit 末尾调用
    void Stop();

private:
    void PollLoop();

    std::thread       m_thread;
    std::atomic<bool> m_running{false};
    static constexpr int kPollIntervalMs = 100;  // 100ms
    static constexpr int kPollDurationMs = 3000; // 总轮询 3 秒
};

} // namespace terminjector
```

```cpp
void StatePoller::PollLoop() {
    auto start = GetTickCount64();
    HANDLE hOut = GetStdHandle(STD_OUTPUT_HANDLE);
    while (m_running) {
        // 读取真实 ConHost 状态（Hook 已装，但 _orig 仍可调真实 API）
        CONSOLE_SCREEN_BUFFER_INFO info;
        if (GetConsoleScreenBufferInfo_orig(hOut, &info)) {
            // 比较缓存，若 ConHost 有变化（用户可能拖动了原 cmd 窗口），同步到 ConsoleState
            auto& state = ConsoleState::Instance();
            if (info.dwCursorPosition.X != state.GetCursorPosition().X ||
                info.dwCursorPosition.Y != state.GetCursorPosition().Y) {
                state.SetCursorPosition(info.dwCursorPosition);
                // 同步给 mediator
                std::string s = vt::CursorPosition(info.dwCursorPosition.Y + 1,
                                                   info.dwCursorPosition.X + 1);
                SendToMediator(s.data(), s.size());
            }
        }

        if (GetTickCount64() - start > kPollDurationMs) break;
        Sleep(kPollIntervalMs);
    }
    LOG_INFO("StatePoller stopped after %lld ms", GetTickCount64() - start);
}
```

> **注意**：轮询用 `GetConsoleScreenBufferInfo_orig`（原始函数指针），绕过我们自己的 Hook，拿到 ConHost 真实状态。Phase 9 静默化后 ConHost 不再更新，此轮询主要在注入后 3 秒内（Hook 刚装、程序可能正在输出）发挥作用。

### 4.2 鼠标事件攒批

```cpp
// InputQueue 扩展
class InputQueue {
    // ... 既有 ...
    void EnqueueBatched(const INPUT_RECORD* records, size_t count);
private:
    std::vector<INPUT_RECORD> m_batch;
    std::chrono::steady_clock::time_point m_batchStart;
    static constexpr int kBatchMaxCount = 20;
    static constexpr int kBatchMaxMs = 16; // ~60fps
};

void InputQueue::EnqueueBatched(const INPUT_RECORD* records, size_t count) {
    std::lock_guard<std::mutex> lk(m_mutex);
    auto now = std::chrono::steady_clock::now();
    if (m_batch.empty()) m_batchStart = now;

    for (size_t i = 0; i < count; ++i) m_batch.push_back(records[i]);

    auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(now - m_batchStart);
    if (m_batch.size() >= kBatchMaxCount || elapsed.count() >= kBatchMaxMs) {
        // flush
        for (const auto& r : m_batch) m_queue.push_back(r);
        m_batch.clear();
        SetEvent(m_event);
    }
}
```

DLL 接收线程对鼠标事件调 `EnqueueBatched`，键盘事件仍走 `Enqueue`（即时）。

### 4.3 WT resize 用事件

```cpp
// mediator/WtSizeWatcher 改造
void WtSizeWatcher::WatchLoop() {
    HANDLE hIn = GetStdHandle(STD_INPUT_HANDLE);
    // 临时把 stdin 切到非 VT 模式以读 INPUT_RECORD
    DWORD oldMode;
    GetConsoleMode(hIn, &oldMode);
    SetConsoleMode(hIn, oldMode & ~ENABLE_VIRTUAL_TERMINAL_INPUT);

    INPUT_RECORD rec[16];
    while (m_running) {
        DWORD read = 0;
        if (!ReadConsoleInputW(hIn, rec, 16, &read)) break;
        for (DWORD i = 0; i < read; ++i) {
            if (rec[i].EventType == WINDOW_BUFFER_SIZE_EVENT) {
                auto& ws = rec[i].Event.WindowBufferSizeEvent;
                if (m_callback) m_callback(ws.dwSize.X, ws.dwSize.Y, ws.dwSize.X, ws.dwSize.Y);
            }
        }
    }
    SetConsoleMode(hIn, oldMode);
}
```

**冲突**：mediator 的 stdin 同时被 `BridgeLoop` 的 stdin→pipe 线程读取（VT 模式）。若 `WtSizeWatcher` 用 `ReadConsoleInputW` 读结构体，会抢走 stdin 数据。

**解决方案**：mediator 的 stdin 统一用 `ReadConsoleInputW` 读取所有事件，再分类：
- `WINDOW_BUFFER_SIZE_EVENT` → 触发 resize 通知
- `KEY_EVENT` / `MOUSE_EVENT` → 转成 VT 序列发给 DLL

但 mediator 自身是 ConPTY 子进程，stdin 收到的应是 VT 字节流（WT 已转译）。`ReadConsoleInputW` 可能拿不到结构体事件。

**结论**：mediator stdin 是 ConPTY 提供的 VT 流，`WINDOW_BUFFER_SIZE_EVENT` 不会出现在其中。resize 只能靠：
- 解析 VT 序列中的 `\x1b[8;<rows>;<cols>t`（WT 发的 resize 响应）
- 或轮询 stdout 的 `GetConsoleScreenBufferInfo`

本 Phase 保留轮询方案，但优化为**仅当 stdout 句柄的 `srWindow` 变化时才通知**，并降低到 50ms 轮询。

### 4.4 死锁防护白名单

```cpp
// HookWhitelist.h
// Hook 内部允许调用的 API（不会重入我们 Hook 的）
// 原则：只用 kernel32 的非 Console API，或已确认不触发 Hook 的 API

namespace terminjector::hooks {

// 允许的 API：
// - OutputDebugStringW            (Logger)
// - WriteFile(日志文件句柄)        (Logger，句柄已注册 protected)
// - CreateFileW(日志文件)          (Logger 初始化)
// - Sleep / WaitForSingleObject   (轮询，但注意 Phase 8 WaitFor Hook)
// - MultiByteToWideChar / WideCharToMultiByte  (编码转换)
// - CreateEventW / SetEvent / ResetEvent       (InputQueue)
// - GetCurrentProcessId / GetTickCount         (元信息)

// 禁止的 API（会重入 Hook）：
// - WriteConsoleW/A               (Hook 目标)
// - WriteFile(CONOUT$/STDERR)     (Hook 目标)
// - GetConsoleScreenBufferInfo    (Hook 目标)
// - 所有其他 Console API

// 检查宏（Debug 模式下用）
#ifdef _DEBUG
#define ASSERT_IN_HOOK() ::terminjector::hooks::CheckNotInReentry()
#else
#define ASSERT_IN_HOOK()
#endif

} // namespace terminjector::hooks
```

### 4.5 `SendToMediator` 锁分离

```cpp
// 之前可能隐式用 ConsoleState 锁，现独立
static SRWLOCK g_sendLock = SRWLOCK_INIT;

void SendToMediator(const void* data, size_t len) {
    AcquireSRWLockExclusive(&g_sendLock);
    if (g_transport && g_transport->IsConnected()) {
        protocol::MessageType type = protocol::MessageType::VtOutput;
        auto pkt = protocol::Serialize(type, data, len);
        g_transport->Send(pkt.data(), pkt.size());
    }
    ReleaseSRWLockExclusive(&g_sendLock);
}
```

**注意**：`SendToMediator` 内不能持有 `ConsoleState` 锁，否则 Hook 链路可能死锁。调用方先 `GetTextAttribute()`（带锁，返回值）再 `SendToMediator`（独立锁）。

### 4.6 无锁日志（可选优化）

若 Phase 1 的 SRWLOCK 日志在高频输出时成为瓶颈，改用 ring buffer + 后台刷盘：

```cpp
// RingBufferLogger.h
class RingBufferLogger {
    // 每个 thread 独立 ring buffer（thread_local）
    // 后台线程每 10ms 刷盘一次
};
```

本 Phase 仅在性能测试证明日志是瓶颈时才实现。先用 Phase 1 的 SRWLOCK 版本。

### 4.7 `WriteConsoleOutput` diff 算法

```cpp
// ConsoleToVt.cpp 改造
// 维护上次输出的 cell 矩阵，仅输出差异
static std::vector<CHAR_INFO> s_lastBuffer;
static COORD s_lastBufferSize{};

std::string ConsoleToVt::WriteConsoleOutput(const CHAR_INFO* buffer, COORD size,
                                             COORD coord, SMALL_RECT region) {
    std::string out;
    // 若尺寸变化，全量输出
    if (s_lastBufferSize.X != size.X || s_lastBufferSize.Y != size.Y ||
        s_lastBuffer.size() != static_cast<size_t>(size.X) * size.Y) {
        s_lastBuffer.assign(buffer, buffer + size.X * size.Y);
        s_lastBufferSize = size;
        return WriteConsoleOutputFull(buffer, size, coord, region); // 原全量逻辑
    }
    // diff：仅输出变化的 cell
    for (int i = 0; i < size.X * size.Y; ++i) {
        if (buffer[i].Char.UnicodeChar != s_lastBuffer[i].Char.UnicodeChar ||
            buffer[i].Attributes != s_lastBuffer[i].Attributes) {
            int row = i / size.X, col = i % size.X;
            out += vt::CursorPosition(region.Top + row + 1, region.Left + col + 1);
            out += vt::SgrFromAttribute(buffer[i].Attributes);
            char utf8[4];
            int len = WideCharToMultiByte(CP_UTF8, 0, &buffer[i].Char.UnicodeChar, 1,
                                          utf8, sizeof(utf8), nullptr, nullptr);
            out.append(utf8, len);
            s_lastBuffer[i] = buffer[i];
        }
    }
    return out;
}
```

### 4.8 IPC 小包合并

```cpp
// SendToMediator 改造：维护一个待发缓冲，16ms 攒批
static std::string g_sendBuffer;
static SRWLOCK g_sendBufLock = SRWLOCK_INIT;
static HANDLE g_flushEvent = nullptr;
static std::thread g_flushThread;

void InitBatchSender() {
    g_flushEvent = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    g_flushThread = std::thread([]{
        while (WaitForSingleObject(g_flushEvent, 16) == WAIT_OBJECT_0) {
            FlushSendBuffer();
        }
    });
}

void SendToMediator(const void* data, size_t len) {
    AcquireSRWLockExclusive(&g_sendBufLock);
    g_sendBuffer.append(reinterpret_cast<const char*>(data), len);
    ReleaseSRWLockExclusive(&g_sendBufLock);
    SetEvent(g_flushEvent);
}

void FlushSendBuffer() {
    AcquireSRWLockExclusive(&g_sendBufLock);
    if (g_sendBuffer.empty()) { ReleaseSRWLockExclusive(&g_sendBufLock); return; }
    std::string pkt = std::move(g_sendBuffer);
    g_sendBuffer.clear();
    ReleaseSRWLockExclusive(&g_sendBufLock);
    // 一次性发
    protocol::MessageType type = protocol::MessageType::VtOutput;
    auto msg = protocol::Serialize(type, pkt.data(), pkt.size());
    g_transport->Send(msg.data(), msg.size());
}
```

### 4.9 共享内存传输（可选）

```cpp
// SharedMemoryTransport.h
class SharedMemoryTransport : public ITransport {
    // 用 CreateFileMapping + MapViewOfFile
    // 双缓冲：DLL 写 buf A，mediator 读 buf B，通过 Event 通知切换
    // 性能远高于命名管道，但实现复杂
};
```

本 Phase 仅作为接口实现存在，默认仍用命名管道。性能测试若不达标再切换。

---

## 5. 验证标准

| 测试 | 预期 |
|------|------|
| opencode 满屏重绘 | 60fps 无撕裂 |
| python 鼠标拖拽画图 | 跟手无延迟（< 50ms） |
| vim 持续按键 1 分钟 | 无卡顿、无内存增长 |
| `tree <盘符>:\ /f` 大量输出 | CPU < 30%，无丢失 |
| 注入后 3 秒内拖动原 cmd 窗口 | WT 同步反映变化 |
| 长时间运行（1 小时）| 无内存泄漏（用 `windows-debugging` 的 umdh 检查） |

---

## 6. 风险点

| 风险 | 缓解 |
|------|------|
| 攒批导致延迟过高 | 16ms 上限，单事件即时发 |
| diff 算法内存占用（双 buffer） | 仅 `WriteConsoleOutput` 用，矩阵 ≤ 200KB |
| 后台线程与 Hook 线程竞争 ConsoleState | 用 SRWLOCK 读写锁，读多写少 |
| `SendToMediator` 攒批丢失（管道断开时） | 退出时强制 flush |
| 共享内存双缓冲同步复杂 | 默认不用，仅作扩展 |

---

## 7. 交付物清单

- [ ] `StatePoller` 后台轮询线程（3 秒）
- [ ] `InputQueue` 鼠标攒批
- [ ] `WtSizeWatcher` 优化轮询
- [ ] `HookWhitelist` 死锁防护文档与断言
- [ ] `SendToMediator` 锁分离 + 小包合并
- [ ] `WriteConsoleOutput` diff 算法
- [ ] 可选：`SharedMemoryTransport` 骨架
- [ ] 性能验证达标
