# Phase 5: 光标与缓冲区信息

> 本 Phase 实现光标与屏幕缓冲区相关 API 的 Hook，以及 WT 窗口尺寸变化的双向同步。完成后，目标程序的 `SetConsoleCursorPosition`、`GetConsoleScreenBufferInfo` 等调用能正确反映在 WT 中，且拖动 WT 边框时目标程序能感知新尺寸重绘。

---

## 1. Phase 目标

1. Hook 光标类 API：
   - `SetConsoleCursorPosition`（输出 `\x1b[r;cH` 并更新缓存）
   - `GetConsoleCursorInfo` / `SetConsoleCursorInfo`（光标显隐与大小 → `\x1b[?25h/l`）
2. Hook 缓冲区信息 API：
   - `GetConsoleScreenBufferInfo`（返回 `ConsoleState` 缓存，**不**调原 API）
   - `SetConsoleWindowInfo`（更新窗口矩形缓存）
   - `SetConsoleScreenBufferSize`（更新尺寸缓存 + 通知 mediator）
   - `GetLargestConsoleWindowSize`
3. 实现 **WT 窗口尺寸变化双向同步**：
   - mediator 侧：监听自身 stdout 的 `CONSOLE_SCREEN_BUFFER_INFO` 变化（WT resize 触发）
   - 通过 IPC 发 `ResizeNotify` 给 DLL
   - DLL 更新 `ConsoleState`，目标程序下次 `GetConsoleScreenBufferInfo` 拿到新值
4. 验证：cmd 中 `mode con: cols=120 lines=40` 能改变 WT 显示；拖动 WT 边框后 cmd 提示符换行正确

---

## 2. 前置依赖

- Phase 4 完成（输出链路可用，`ConsoleState` 已有尺寸/光标字段）

---

## 3. 涉及文件清单

```
src/dll/
├── hooks/
│   ├── CursorHooks.h                # 新建
│   ├── CursorHooks.cpp              # 光标类 Hook
│   └── BufferHooks.h / .cpp         # 新建：缓冲区类 Hook
├── state/
│   └── ConsoleState.cpp             # 扩展：尺寸变更通知
└── mediator/
    ├── WtSizeWatcher.h              # 新建
    ├── WtSizeWatcher.cpp            # WT 尺寸监听线程
    └── Mediator.cpp                 # 集成 size watcher
```

---

## 4. 详细任务

### 4.1 待 Hook API 与策略

| API | 行为 | VT/缓存策略 |
|-----|------|-------------|
| `SetConsoleCursorPosition` | 程序移动光标 | 输出 `\x1b[r;cH`（1-based）+ 更新缓存 |
| `GetConsoleScreenBufferInfo` | 程序查询尺寸/光标 | 返回 `ConsoleState` 缓存，不调原 API |
| `GetConsoleCursorInfo` | 查询光标显隐/大小 | 返回缓存 |
| `SetConsoleCursorInfo` | 设置光标显隐/大小 | 输出 `\x1b[?25h/l` + 更新缓存 |
| `SetConsoleWindowInfo` | 设置窗口矩形 | 更新缓存 + 输出 DECSTBM |
| `SetConsoleScreenBufferSize` | 改缓冲区尺寸 | 更新缓存 + 通知 mediator |
| `GetLargestConsoleWindowSize` | 查询最大窗口 | 返回缓存值 |

### 4.2 `SetConsoleCursorPosition` Hook

```cpp
DEFINE_ORIG_PTR(SetConsoleCursorPosition, BOOL WINAPI(HANDLE, COORD));

BOOL WINAPI SetConsoleCursorPosition_Detour(HANDLE hConsoleOutput, COORD pos) {
    ENSURE_INITIALIZED();
    if (!IsConsoleHandle(hConsoleOutput)) {
        return SetConsoleCursorPosition_orig(hConsoleOutput, pos);
    }
    // 更新缓存
    ConsoleState::Instance().SetCursorPosition(pos);
    // 输出 VT 光标定位（1-based）
    std::string s = vt::CursorPosition(pos.Y + 1, pos.X + 1);
    SendToMediator(s.data(), s.size());
    return SetConsoleCursorPosition_orig(hConsoleOutput, pos); // Phase 9 改为静默
}
```

### 4.3 `GetConsoleScreenBufferInfo` Hook（关键）

```cpp
DEFINE_ORIG_PTR(GetConsoleScreenBufferInfo, BOOL WINAPI(HANDLE, PCONSOLE_SCREEN_BUFFER_INFO));

BOOL WINAPI GetConsoleScreenBufferInfo_Detour(HANDLE h, PCONSOLE_SCREEN_BUFFER_INFO info) {
    ENSURE_INITIALIZED();
    if (!IsConsoleHandle(h) || info == nullptr) {
        return GetConsoleScreenBufferInfo_orig(h, info);
    }
    // 返回缓存（不调原 API，避免拿到 ConHost 的旧值）
    ConsoleState::Instance().FillScreenBufferInfo(*info);
    return TRUE;
}
```

`ConsoleState::FillScreenBufferInfo` 填充全部字段：
```cpp
void ConsoleState::FillScreenBufferInfo(CONSOLE_SCREEN_BUFFER_INFO& info) {
    AcquireSRWLockShared(&m_lock);
    info = m_screenInfo; // dwSize / dwCursorPosition / srWindow / dwMaximumWindowSize / wAttributes
    ReleaseSRWLockShared(&m_lock);
}
```

### 4.4 光标显隐 Hook

```cpp
DEFINE_ORIG_PTR(SetConsoleCursorInfo, BOOL WINAPI(HANDLE, const CONSOLE_CURSOR_INFO*));
DEFINE_ORIG_PTR(GetConsoleCursorInfo, BOOL WINAPI(HANDLE, PCONSOLE_CURSOR_INFO));

BOOL WINAPI SetConsoleCursorInfo_Detour(HANDLE h, const CONSOLE_CURSOR_INFO* info) {
    ENSURE_INITIALIZED();
    if (!IsConsoleHandle(h) || !info) return SetConsoleCursorInfo_orig(h, info);
    ConsoleState::Instance().SetCursorInfo(*info);
    // DECTCE: \x1b[?25h 显示 / \x1b[?25l 隐藏
    std::string s = info->bVisible ? "\x1b[?25h" : "\x1b[?25l";
    SendToMediator(s.data(), s.size());
    return SetConsoleCursorInfo_orig(h, info);
}

BOOL WINAPI GetConsoleCursorInfo_Detour(HANDLE h, PCONSOLE_CURSOR_INFO info) {
    ENSURE_INITIALIZED();
    if (!IsConsoleHandle(h) || !info) return GetConsoleCursorInfo_orig(h, info);
    *info = ConsoleState::Instance().GetCursorInfo();
    return TRUE;
}
```

### 4.5 WT 尺寸监听（mediator 侧）

#### 4.5.1 `WtSizeWatcher.h`

```cpp
#pragma once
#include <windows.h>
#include <thread>
#include <atomic>
#include <functional>

namespace terminjector {

// 监听 WT 窗口尺寸变化（通过自身 stdout 的 CONSOLE_SCREEN_BUFFER_INFO）
class WtSizeWatcher {
public:
    using OnResize = std::function<void(int cols, int rows, int bufCols, int bufRows)>;

    WtSizeWatcher();
    ~WtSizeWatcher();

    void Start(OnResize callback);
    void Stop();

private:
    void WatchLoop();

    std::thread       m_thread;
    std::atomic<bool> m_running{false};
    int m_lastCols = 0, m_lastRows = 0;
    OnResize m_callback;
};

} // namespace terminjector
```

#### 4.5.2 实现要点

```cpp
void WtSizeWatcher::WatchLoop() {
    HANDLE hOut = GetStdHandle(STD_OUTPUT_HANDLE);
    while (m_running) {
        CONSOLE_SCREEN_BUFFER_INFO info;
        if (GetConsoleScreenBufferInfo(hOut, &info)) {
            int cols = info.srWindow.Right - info.srWindow.Left + 1;
            int rows = info.srWindow.Bottom - info.srWindow.Top + 1;
            if (cols != m_lastCols || rows != m_lastRows) {
                m_lastCols = cols;
                m_lastRows = rows;
                if (m_callback) m_callback(cols, rows, info.dwSize.X, info.dwSize.Y);
            }
        }
        Sleep(100); // 10fps 轮询，足够响应
    }
}
```

> **优化**：可改用 `ReadConsoleInput` 监听 `WINDOW_BUFFER_SIZE_EVENT`，更精确且不轮询。但需处理 stdin 句柄的 input 事件流，可能与 stdin→pipe 桥接冲突。本 Phase 先轮询，Phase 10 优化。

#### 4.5.3 mediator 集成

```cpp
int Mediator::Run(...) {
    // ... 握手后 ...
    m_sizeWatcher.Start([this](int cols, int rows, int bufCols, int bufRows) {
        protocol::ResizePayload p{static_cast<uint16_t>(cols), static_cast<uint16_t>(rows),
                                  static_cast<uint16_t>(bufCols), static_cast<uint16_t>(bufRows)};
        auto pkt = protocol::Serialize(protocol::MessageType::ResizeNotify, &p, sizeof(p));
        m_transport->Send(pkt.data(), pkt.size());
        LOG_INFO("ResizeNotify sent: %dx%d", cols, rows);
    });
    BridgeLoop();
    m_sizeWatcher.Stop();
    return 0;
}
```

### 4.6 DLL 侧接收 ResizeNotify

在 `BridgeLoop` 的消息处理中（DLL 侧也需一个接收线程）增加：

```cpp
// DLL 侧后台接收线程（Phase 3 已建立，此处补 ResizeNotify 分支）
void DllRecvLoop() {
    while (g_transport->IsConnected()) {
        protocol::MessageType type;
        std::vector<uint8_t> payload;
        if (!RecvPacket(*g_transport, type, payload)) break;

        switch (type) {
            case protocol::MessageType::ResizeNotify: {
                protocol::ResizePayload p{};
                std::memcpy(&p, payload.data(), std::min(payload.size(), sizeof(p)));
                ConsoleState::Instance().SetBufferSize({p.bufferCols, p.bufferRows});
                ConsoleState::Instance().SetWindow({0, 0,
                    static_cast<SHORT>(p.cols - 1), static_cast<SHORT>(p.rows - 1)});
                LOG_INFO("Resize applied: win=%dx%d buf=%dx%d",
                         p.cols, p.rows, p.bufferCols, p.bufferRows);
                break;
            }
            case protocol::MessageType::VtInput:
                // Phase 6 处理
                break;
            default:
                LOG_DEBUG("DLL recv msg type=%u", type);
        }
    }
}
```

### 4.7 `SetConsoleScreenBufferSize` Hook

```cpp
DEFINE_ORIG_PTR(SetConsoleScreenBufferSize, BOOL WINAPI(HANDLE, COORD));

BOOL WINAPI SetConsoleScreenBufferSize_Detour(HANDLE h, COORD size) {
    ENSURE_INITIALIZED();
    if (!IsConsoleHandle(h)) return SetConsoleScreenBufferSize_orig(h, size);
    ConsoleState::Instance().SetBufferSize(size);
    // 通知 mediator（WT 可能需调整）
    // 实际 WT 的 buffer 通常 = 窗口尺寸，这里仅记录
    return SetConsoleScreenBufferSize_orig(h, size);
}
```

---

## 5. 验证标准

| 测试 | 预期 |
|------|------|
| cmd `mode con: cols=120 lines=40` | WT 显示区域调整，cmd 提示符换行按 120 列 |
| 拖动 WT 边框缩小 | cmd 提示符在新宽度下换行正确 |
| 拖动 WT 边框放大 | cmd 输出利用新宽度 |
| Python `print(f"\033[10;5H*")` | WT 在 10 行 5 列显示 * |
| vim 编辑器（Phase 8 后）| 光标移动正确 |

---

## 6. 风险点

| 风险 | 缓解 |
|------|------|
| WT resize 与目标程序重绘竞态 | DLL 收 ResizeNotify 后立即更新缓存；目标下次查询即得新值 |
| `GetConsoleScreenBufferInfo` 不调原 API 导致 ConHost 状态不同步 | 接受（Phase 9 后 ConHost 不再参与） |
| 轮询 100ms 延迟 | Phase 10 改 `WINDOW_BUFFER_SIZE_EVENT` |
| 窗口矩形与缓冲区尺寸混淆 | `ConsoleState` 区分 `srWindow`（可视区）与 `dwSize`（缓冲区） |

---

## 7. 交付物清单

- [ ] `CursorHooks.cpp` 4 个光标 API Hook
- [ ] `BufferHooks.cpp` 3 个缓冲区 API Hook
- [ ] `WtSizeWatcher` 监听线程
- [ ] DLL 侧 ResizeNotify 处理
- [ ] `ConsoleState` 尺寸/窗口字段完善
- [ ] 验证 cmd `mode` 与 WT resize 互通
