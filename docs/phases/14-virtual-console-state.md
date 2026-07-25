# Phase 14: 虚拟 Console 状态 + WT 反向同步

> 本 Phase 在 DLL 内部维护一份完整的虚拟 Console 状态（光标位置、缓冲区大小、属性、窗口区域等），所有写入操作同时更新虚拟状态，所有查询 API 从虚拟状态返回。同时建立 WT → DLL 的反向同步通道，让 WT 的真实渲染状态（窗口尺寸变化）回流到 DLL，保证程序查询到的是与 WT 一致的真实状态。
>
> **核心原则**：WT 是物理终端，是状态的唯一真实源；ConHost 跟随虚拟状态变化，仅作为程序可见的镜像。

---

## 1. 背景与动机

### 1.1 问题

Phase 5/6/8 实现了输出/输入 Hook，把程序的 Console API 调用转换为 VT 序列发给 mediator → WT 渲染。但程序查询 Console 状态时存在严重不同步：

1. **光标位置不同步**：程序调 `WriteConsoleW` 写字符 → DLL 转 VT 发给 WT → WT 渲染并移动光标。但程序调 `GetConsoleScreenBufferInfo` 查光标位置时，ConHost 维护的光标位置还是旧值（没收到任何更新）。
2. **缓冲区大小不同步**：WT resize → mediator 通知 DLL → DLL 调 `SetConsoleScreenBufferSize` 让 ConHost 跟随。但程序调 `SetConsoleScreenBufferSize` 改缓冲区 → DLL 转 VT 给 WT → ConHost 没被更新（如果不调 orig）。
3. **窗口区域（srWindow）不同步**：WT 的可见区域变化，程序查询 `srWindow` 拿到旧值，导致 vim/less 滚动行为异常。
4. **属性（wAttributes）不同步**：程序调 `SetConsoleTextAttribute` → DLL 转 SGR 给 WT → ConHost 属性未变。程序查询时拿到旧属性。

### 1.2 目标

引入「虚拟 Console 状态」+「WT 反向同步」：

```
┌────────────────────────────────────────────────────────────┐
│                       DLL 内部                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  VirtualConsoleState（唯一权威状态）                 │  │
│  │  - cursorPos / bufferSize / srWindow / attributes    │  │
│  └────────┬─────────────────────┬───────────────────────┘  │
│           │                     │                          │
│  程序写入触发更新        程序查询从这返回                  │
│  (Set* Hook)            (Get* Hook)                        │
│           │                     │                          │
│           ▼                     │                          │
│  ┌────────────────┐              │                          │
│  │ 转 VT → WT     │              │                          │
│  └────────────────┘              │                          │
│           ▲                     │                          │
│           │                     │                          │
│  ┌────────┴─────────────────────┴───────────────────────┐  │
│  │  WT 反向同步（mediator → DLL）                       │  │
│  │  - WT resize → 更新 bufferSize / srWindow            │  │
│  │  - WT 光标报告（DSR CPR）→ 更新 cursorPos            │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                              │
│  ConHost 跟随：调 orig 让 ConHost 状态同步到虚拟状态        │
│  （程序绕过 Hook 直接查 ConHost 也能拿到一致值）            │
└──────────────────────────────────────────────────────────────┘
```

### 1.3 与已有 Phase 的关系

- **Phase 5（光标/缓冲区 Hook）**：已有 `SetConsoleCursorPosition` / `SetConsoleScreenBufferSize` Hook，但仅转 VT，未维护虚拟状态
- **Phase 8（高级特性）**：已有 `SetConsoleTextAttribute` / `SetConsoleWindowInfo` Hook，同样未维护虚拟状态
- **Phase 10（状态同步）**：已有 `StatePoller` 单向同步 ConHost → WT，但反向（WT → 程序查询）未做
- **Phase 13（VT 直通）**：VT 模式下程序不查询状态，但模式切换时需保持状态一致

本 Phase 把上述 Hook 全部改造为「同时更新虚拟状态」，并新增 WT 反向同步通道。

---

## 2. 前置依赖

- Phase 5 完成（光标/缓冲区 Hook 基础）
- Phase 8 完成（属性/窗口 Hook 基础）
- Phase 10 完成（`StatePoller` 提供 ConHost 状态初始快照）
- Phase 13 完成（VT 模式与行编辑模式切换就绪，本 Phase 仅在行编辑模式下生效）

---

## 3. 涉及文件清单

```
src/
├── common/protocol/
│   └── Message.h                    # 扩展：WtStateReport 消息（WT→DLL）
├── dll/
│   ├── state/
│   │   ├── VirtualConsoleState.h    # 新建：虚拟 Console 状态
│   │   ├── VirtualConsoleState.cpp  # 新建：状态维护 + 查询接口
│   │   └── ConsoleState.h           # 重构：合并到 VirtualConsoleState
│   ├── hooks/
│   │   ├── CursorHooks.cpp          # 改造：SetConsoleCursorPosition 更新虚拟状态
│   │   ├── BufferHooks.cpp          # 改造：SetConsoleScreenBufferSize / SetConsoleWindowInfo 更新虚拟状态
│   │   ├── OutputHooks.cpp          # 改造：WriteConsole 更新光标位置（按字符宽度推算）
│   │   └── FontHooks.cpp            # 改造：SetCurrentConsoleFontEx 更新字体字段
│   ├── translator/
│   │   └── ConsoleToVt.cpp          # 扩展：写入时回调 VirtualConsoleState 更新
│   └── LazyInit.cpp                 # 扩展：初始化时从 StatePoller 加载初始状态
└── mediator/
    ├── Mediator.cpp                 # 扩展：解析 WT 的 DSR CPR 响应 → 发 WtStateReport 给 DLL
    └── VtParser.h / .cpp            # 新增：轻量 VT 解析器（识别 CSI 6n 响应、resize 通知）
```

---

## 4. 详细任务

### 4.1 VirtualConsoleState 数据结构

```cpp
// VirtualConsoleState.h
#pragma once
#include <windows.h>
#include <atomic>
#include <mutex>
#include <cstdint>

namespace terminjector {

// 虚拟 Console 状态：DLL 内唯一权威状态
// 所有 Set* Hook 写入时更新，所有 Get* Hook 查询时返回
// WT 反向同步（resize / DSR CPR）也更新此状态
class VirtualConsoleState {
public:
    static VirtualConsoleState& Instance();

    // === 初始化 ===
    // 从 StatePoller 拿到 ConHost 初始状态（仅启动时调用一次）
    void InitializeFromConHost();

    // === 状态字段（线程安全访问） ===

    // 光标位置（cell 坐标，0-based）
    COORD GetCursorPos() const;
    void SetCursorPos(COORD pos);
    void MoveCursor(int32_t dx, int32_t dy);  // 相对移动
    void MoveCursorNextLine();                 // 换行（\r\n）

    // 缓冲区大小（cell 单位）
    COORD GetBufferSize() const;
    void SetBufferSize(COORD size);

    // 可见窗口区域（srWindow，cell 坐标）
    SMALL_RECT GetWindowRect() const;
    void SetWindowRect(SMALL_RECT rect);

    // 文本属性（前景/背景色）
    WORD GetAttributes() const;
    void SetAttributes(WORD attr);

    // 字体信息（Phase 8 字体 Hook 用）
    CONSOLE_FONT_INFOEX GetFontInfo() const;
    void SetFontInfo(const CONSOLE_FONT_INFOEX& font);

    // 最大窗口大小（基于缓冲区与字体推算）
    COORD GetLargestWindowSize() const;

    // === WT 反向同步接口 ===
    // WT resize → 更新 bufferSize / srWindow
    void ApplyWtResize(int32_t cols, int32_t rows);
    // WT DSR CPR 响应 → 更新 cursorPos
    void ApplyWtCursorReport(int32_t col, int32_t row);

    // === 与 ConHost 同步 ===
    // 把虚拟状态推送到 ConHost（调 orig 让 ConHost 跟随）
    void SyncToConHost();

private:
    VirtualConsoleState() = default;

    mutable std::mutex m_lock;
    COORD m_cursorPos{0, 0};
    COORD m_bufferSize{80, 25};
    SMALL_RECT m_windowRect{0, 0, 79, 24};
    std::atomic<WORD> m_attributes{FOREGROUND_BLUE | FOREGROUND_GREEN | FOREGROUND_RED};
    CONSOLE_FONT_INFOEX m_fontInfo{};
    std::atomic<bool> m_initialized{false};
};

} // namespace terminjector
```

**关键点**：
- 单例（Meyers's Singleton）
- 字段用 `std::mutex` 保护（写入频率不高，锁开销可接受）
- `m_attributes` 用原子变量（高频访问，无锁优化）
- `m_initialized` 防止未初始化查询

### 4.2 程序写入时更新虚拟状态

改造 Phase 5/8 的 Hook，在转 VT 的同时更新虚拟状态：

```cpp
// CursorHooks.cpp 改造
BOOL WINAPI SetConsoleCursorPosition_Detour(HANDLE h, COORD pos) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;
    if (!IsOutputHandle(h)) {
        return SetConsoleCursorPosition_orig(h, pos);
    }

    // 1. 更新虚拟状态
    VirtualConsoleState::Instance().SetCursorPos(pos);

    // 2. 转 VT 序列（CUP: \x1b[y;xD）
    char vt[16];
    int n = std::snprintf(vt, sizeof(vt), "\x1b[%d;%dH",
                          pos.Y + 1, pos.X + 1);  // VT 是 1-based
    SendToMediator(vt, n);

    // 3. 调 orig 让 ConHost 跟随
    return SetConsoleCursorPosition_orig(h, pos);
}
```

```cpp
// BufferHooks.cpp 改造
BOOL WINAPI SetConsoleScreenBufferSize_Detour(HANDLE h, COORD size) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;
    if (!IsOutputHandle(h)) {
        return SetConsoleScreenBufferSize_orig(h, size);
    }

    // WT 为唯一尺寸源：程序改尺寸不直接影响 WT
    // 1. 更新虚拟状态（程序查询时返回这个值）
    VirtualConsoleState::Instance().SetBufferSize(size);
    LOG_INFO("SetConsoleScreenBufferSize: virtual size=(%d,%d) (WT not resized)",
             size.X, size.Y);

    // 2. 不调 orig（避免 ConHost 状态与 WT 不一致）
    //    ConHost 状态由 SyncToConHost 统一同步
    return TRUE;
}
```

```cpp
// OutputHooks.cpp 改造：WriteConsole 推算光标移动
BOOL WINAPI WriteConsoleW_Detour(HANDLE h, const VOID* buf, DWORD len,
                                  LPDWORD written, LPVOID reserved) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;
    if (!IsOutputHandle(h)) {
        return WriteConsoleW_orig(h, buf, len, written, reserved);
    }

    auto* wbuf = static_cast<const wchar_t*>(buf);

    // 1. 转 VT 发给 mediator
    std::string vt = ConsoleToVt::ConvertW(wbuf, len);
    SendToMediator(vt.data(), vt.size());

    // 2. 更新虚拟光标位置（按字符宽度推算）
    //    ConsoleToVt 转换时已知每个字符的宽度（用 wcwidth）
    auto& state = VirtualConsoleState::Instance();
    COORD pos = state.GetCursorPos();
    for (DWORD i = 0; i < len; ++i) {
        wchar_t ch = wbuf[i];
        if (ch == L'\r') {
            pos.X = 0;
        } else if (ch == L'\n') {
            pos.Y++;
            // 不重置 X（Windows \n 语义不带回车，但 cmd 通常发 \r\n）
        } else if (ch == L'\b') {
            if (pos.X > 0) pos.X--;
        } else {
            int w = WcwidthW(ch);  // 字符宽度（0/1/2）
            pos.X += static_cast<SHORT>(w);
            // 超出缓冲区宽度自动换行（Windows Console 语义）
            COORD bufSize = state.GetBufferSize();
            if (pos.X >= bufSize.X) {
                pos.X = 0;
                pos.Y++;
            }
        }
    }
    state.SetCursorPos(pos);

    if (written) *written = len;
    return TRUE;
}
```

**关键点**：
- `WriteConsoleW` 推算光标位置是复杂点，需处理 `\r` / `\n` / `\b` / 制表符 / 全角字符
- `ConsoleToVt::ConvertW` 已知字符宽度，可直接复用其内部计算结果（避免重复计算）

### 4.3 程序查询时从虚拟状态返回

改造 Phase 5 的查询 Hook：

```cpp
// CursorHooks.cpp 改造
BOOL WINAPI GetConsoleScreenBufferInfo_Detour(HANDLE h,
                                                PCONSOLE_SCREEN_BUFFER_INFO info) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;
    if (!IsOutputHandle(h) || info == nullptr) {
        return GetConsoleScreenBufferInfo_orig(h, info);
    }

    auto& state = VirtualConsoleState::Instance();
    info->dwSize = state.GetBufferSize();
    info->dwCursorPosition = state.GetCursorPos();
    info->wAttributes = state.GetAttributes();
    info->srWindow = state.GetWindowRect();
    info->dwMaximumWindowSize = state.GetLargestWindowSize();
    return TRUE;
}
```

**关键点**：不调 `orig`，直接返回虚拟状态。这样程序查询到的状态始终与 DLL 维护的一致。

### 4.4 WT 反向同步通道

#### 4.4.1 WT resize 同步

WT 窗口 resize 时，mediator 检测到（通过 `WINDOW_BUFFER_SIZE_EVENT` 或 WT 自身的事件），发 `WtStateReport` 给 DLL：

```cpp
// Message.h 扩展
enum class MessageType : uint32_t {
    // ... 已有消息 ...

    // WT 状态反向同步（Phase 14）
    WtStateReport = 0x0080,  // mediator→DLL：WT 状态报告
};

// WtStateReport payload
struct WtStateReportPayload {
    uint32_t type;        // 0=resize, 1=cursor_report
    int32_t cols;         // resize: 新列数；cursor: col
    int32_t rows;         // resize: 新行数；cursor: row
};
static_assert(sizeof(WtStateReportPayload) == 12, "...");
```

```cpp
// DLL 处理 WtStateReport
void OnWtStateReport(const WtStateReportPayload& payload) {
    auto& state = VirtualConsoleState::Instance();
    if (payload.type == 0) {
        // resize
        state.ApplyWtResize(payload.cols, payload.rows);
        LOG_INFO("WtStateReport: resize to (%d, %d)", payload.cols, payload.rows);
    } else if (payload.type == 1) {
        // cursor report
        state.ApplyWtCursorReport(payload.cols, payload.rows);
    }
    // 同步到 ConHost
    state.SyncToConHost();
}
```

```cpp
// VirtualConsoleState.cpp
void VirtualConsoleState::ApplyWtResize(int32_t cols, int32_t rows) {
    std::lock_guard lock(m_lock);
    m_bufferSize.X = static_cast<SHORT>(cols);
    m_bufferSize.Y = static_cast<SHORT>(rows);
    // srWindow 跟随缓冲区（WT 无 scrollback 概念，可见区 = 缓冲区）
    m_windowRect.Left = 0;
    m_windowRect.Top = 0;
    m_windowRect.Right = static_cast<SHORT>(cols - 1);
    m_windowRect.Bottom = static_cast<SHORT>(rows - 1);
    // 光标位置裁剪到新缓冲区内
    if (m_cursorPos.X >= m_bufferSize.X) m_cursorPos.X = static_cast<SHORT>(m_bufferSize.X - 1);
    if (m_cursorPos.Y >= m_bufferSize.Y) m_cursorPos.Y = static_cast<SHORT>(m_bufferSize.Y - 1);
}

void VirtualConsoleState::SyncToConHost() {
    // 调 orig 让 ConHost 跟随
    HANDLE hOut = GetStdHandle(STD_OUTPUT_HANDLE);
    SetConsoleScreenBufferSize_orig(hOut, m_bufferSize);
    SetConsoleWindowInfo_orig(hOut, TRUE, &m_windowRect);
    SetConsoleCursorPosition_orig(hOut, m_cursorPos);
    SetConsoleTextAttribute_orig(hOut, m_attributes.load());
}
```

#### 4.4.2 WT DSR CPR 响应同步

程序调 `SetConsoleCursorPosition` 后，WT 渲染的光标位置可能与虚拟状态不一致（如 Alt Buffer 边界、滚动后）。DLL 主动发 `CSI 6n` 查询 WT 真实光标位置：

```cpp
// DLL 发 DSR CPR 查询
void QueryWtCursorPos() {
    const char* dsr = "\x1b[6n";
    SendToMediator(dsr, 4);
}

// mediator 解析 WT 响应（CSI row;col R）
// WT 收到 DSR 后回应 \x1b[row;colR
// mediator VtParser 识别此响应，发 WtStateReport(type=1) 给 DLL
```

**触发时机**：
- 模式切换时（行编辑 → VT 或反向）
- Alt Buffer 进出时
- 程序主动调 `SetConsoleCursorPosition` 后（可选，验证用）

### 4.5 mediator VtParser

mediator 需要解析 WT 的 VT 响应（DSR CPR 等），新增轻量 VT 解析器：

```cpp
// VtParser.h
#pragma once
#include <cstdint>
#include <string>

namespace terminjector {

// 轻量 VT 解析器：识别 DSR CPR 响应（CSI row;col R）
// 仅解析需要的几个序列，不做完整 VT 解析
class VtParser {
public:
    // 输入 WT 字节流，识别到 DSR CPR 时回调
    void Feed(const uint8_t* data, size_t len);

    // 设置回调（col, row 是 1-based）
    void SetCursorReportCallback(std::function<void(int, int)> cb) {
        m_cursorCb = std::move(cb);
    }

private:
    std::string m_pending;  // 累积未识别字节
    std::function<void(int, int)> m_cursorCb;
};

} // namespace terminjector
```

**关键点**：
- 仅解析 DSR CPR，不解析其他序列（避免复杂度）
- WT 的其他输出（程序输出的 VT）原样转发给 WT 渲染，不经过解析

### 4.6 初始化流程

LazyInit 时从 `StatePoller` 拿到 ConHost 初始状态，加载到 `VirtualConsoleState`：

```cpp
// LazyInit.cpp 扩展
void LazyInit() {
    // ... 已有初始化 ...

    // 初始化虚拟 Console 状态
    auto& state = VirtualConsoleState::Instance();
    state.InitializeFromConHost();

    // 发初始 DSR 查询 WT 真实光标位置
    QueryWtCursorPos();

    // 启动 StatePoller 持续同步（已有）
    StatePoller::Instance().Start();
}
```

```cpp
// VirtualConsoleState.cpp
void VirtualConsoleState::InitializeFromConHost() {
    HANDLE hOut = GetStdHandle(STD_OUTPUT_HANDLE);
    CONSOLE_SCREEN_BUFFER_INFO info{};
    GetConsoleScreenBufferInfo_orig(hOut, &info);  // 用 orig 绕过 Hook

    std::lock_guard lock(m_lock);
    m_bufferSize = info.dwSize;
    m_cursorPos = info.dwCursorPosition;
    m_attributes.store(info.wAttributes);
    m_windowRect = info.srWindow;
    m_initialized.store(true);
}
```

### 4.7 与 ConHost 的同步策略

**原则**：ConHost 是程序可见的镜像，跟随虚拟状态变化。

| 触发点 | 操作 |
|--------|------|
| 程序 `Set*` Hook | 更新虚拟状态 + 转 VT 给 WT + 调 orig 让 ConHost 跟随 |
| 程序 `WriteConsole` | 更新虚拟光标 + 转 VT 给 WT + **不调 orig**（输出走 VT，不走 ConHost） |
| WT resize 反向同步 | 更新虚拟状态 + 调 orig 让 ConHost 跟随 |
| WT DSR CPR 响应 | 更新虚拟光标 + 调 orig 让 ConHost 跟随 |
| 程序 `Get*` 查询 | 从虚拟状态返回（不调 orig） |
| LazyInit 初始化 | 从 ConHost 加载初始状态到虚拟状态 |

**关键决策**：
- `Set*` 类 Hook 调 orig：让 ConHost 跟随，程序若绕过 Hook 直接查 ConHost 也能拿到一致值
- `WriteConsole` 不调 orig：输出走 VT 路径，ConHost 不参与输出（否则双输出）
- `Get*` 类 Hook 不调 orig：保证查询返回的是虚拟状态，与 WT 一致

### 4.8 WT 为唯一尺寸源

程序调 `SetConsoleScreenBufferSize` 时：
- **不**直接 resize WT（WT 是物理窗口，由用户控制）
- 更新虚拟状态（程序下次查询返回这个值）
- 不调 orig（避免 ConHost 状态与 WT 不一致）
- 可选：转 VT 给 WT（DECSET 8 等序列，让 WT 知道程序期望的尺寸，但 WT 可忽略）

```cpp
// 完整 SetConsoleScreenBufferSize_Detour
BOOL WINAPI SetConsoleScreenBufferSize_Detour(HANDLE h, COORD size) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;
    if (!IsOutputHandle(h)) {
        return SetConsoleScreenBufferSize_orig(h, size);
    }

    // WT 为唯一尺寸源：程序改尺寸不影响 WT
    // 1. 更新虚拟状态
    VirtualConsoleState::Instance().SetBufferSize(size);
    LOG_INFO("SetConsoleScreenBufferSize: virtual=(%d,%d) (WT not affected)",
             size.X, size.Y);
    // 2. 不调 orig（ConHost 由 SyncToConHost 统一同步）
    // 3. 不转 VT（WT 不会按程序意愿 resize）
    return TRUE;
}
```

---

## 5. 验证标准

| 测试 | 预期 | 说明 |
|------|------|------|
| cmd 启动后 `GetConsoleScreenBufferInfo` | 返回与 WT 一致的尺寸和光标位置 | 初始化正确 |
| cmd `echo hello` 后查询光标位置 | 光标在 `hello` 后面（行首+5） | WriteConsole 推算正确 |
| cmd `cls` 后查询光标位置 | 光标在 (0,0) | 清屏 Hook 更新虚拟状态 |
| cmd `SetConsoleCursorPosition(10, 5)` 后查询 | 返回 (10, 5) | Set Hook 更新虚拟状态 |
| WT resize 后程序查询 `srWindow` | 返回新尺寸 | WT 反向同步生效 |
| WT resize 后程序查询 `dwSize` | 返回新尺寸 | 缓冲区跟随 WT |
| 程序调 `SetConsoleScreenBufferSize(200, 100)` | WT 不 resize，但程序查询返回 (200, 100) | WT 为唯一源 |
| 程序调 `SetConsoleTextAttribute` 后查询 `wAttributes` | 返回设置的值 | 属性虚拟状态 |
| 长时间运行后状态无漂移 | 多次查询返回一致值 | 状态同步稳定 |
| vim 内 `:q` 退出后 cmd 查询状态 | 返回与 WT 一致的值 | 模式切换后状态正确 |

---

## 6. 风险点

| 风险 | 影响 | 缓解 |
|------|------|------|
| WriteConsole 光标推算错误（特殊字符/转义） | 光标位置漂移 | 仅推算可见字符 + 控制字符；复杂场景用 DSR CPR 校正 |
| WT DSR CPR 响应延迟 | 短暂状态不一致 | 异步更新，下次查询时已同步 |
| 多线程同时写 Console | 虚拟状态竞争 | std::mutex 保护，写入路径串行化 |
| ConHost 同步调用 orig 重入 | Hook 递归 | HookReentryGuard + 调 orig 前释放 guard |
| WT 不响应 DSR CPR | 光标位置无法校正 | 降级为纯推算模式，定期日志告警 |
| Alt Buffer 切换时光标位置错位 | vim 进入后光标位置错误 | Alt Buffer 进出时主动发 DSR CPR 重新校正 |
| 程序绕过 Hook 直接 syscall 查 ConHost | 拿到 ConHost 而非虚拟状态 | SyncToConHost 让 ConHost 跟随，保证一致 |
| wcwidth 字符宽度计算错误（组合字符/emoji） | 光标推算偏差 | Phase 17 字符宽度审计专门处理 |

---

## 7. 交付物清单

- [ ] `VirtualConsoleState.h/.cpp`：虚拟状态数据结构 + 接口实现
- [ ] `CursorHooks.cpp` 改造：`SetConsoleCursorPosition` / `GetConsoleScreenBufferInfo` 走虚拟状态
- [ ] `BufferHooks.cpp` 改造：`SetConsoleScreenBufferSize` / `SetConsoleWindowInfo` 走虚拟状态
- [ ] `OutputHooks.cpp` 改造：`WriteConsoleW` 推算光标位置
- [ ] `FontHooks.cpp` 改造：`SetCurrentConsoleFontEx` 更新虚拟状态
- [ ] `Message.h` 扩展：`WtStateReport` 消息类型
- [ ] `Mediator.cpp` 扩展：解析 WT DSR CPR 响应 → 发 `WtStateReport`
- [ ] `VtParser.h/.cpp` 新增：轻量 VT 解析器
- [ ] `LazyInit.cpp` 扩展：初始化 `VirtualConsoleState` + 发初始 DSR 查询
- [ ] 验证：状态查询一致性 + WT resize 反向同步 + 模式切换后状态正确

---

## 8. 与其他 Phase 的关系

```
Phase 5 (光标/缓冲区 Hook) ──┐
Phase 8 (属性/字体 Hook)   ──┼──► Phase 14 (虚拟 Console 状态)
Phase 10 (StatePoller)     ──┤                │
Phase 13 (VT 直通模式)    ──┘                │
                                              ▼
                                    程序查询状态与 WT 一致
                                    WT 反向同步通道建立
                                              │
                                              ▼
                                    Phase 15 (DSR/DA 查询)
                                    Phase 17 (字符宽度审计)
                                    Phase 18 (滚动缓冲区)
```

**依赖**：
- Phase 5/8/10 提供 Hook 基础 + `StatePoller` 初始状态
- Phase 13 提供模式切换（行编辑模式才需虚拟状态，VT 模式程序自维护）

**被依赖**：
- Phase 15（终端属性查询）：DSR/DA 处理复用 `VtParser` + 反向同步通道
- Phase 17（字符宽度审计）：`WriteConsoleW` 光标推算依赖字符宽度正确性
- Phase 18（滚动缓冲区）：scrollback 一致性依赖虚拟缓冲区状态

---

## 9. 备注

### 9.1 为什么不直接读 ConHost 状态

ConHost 在我们 Hook 后基本不参与输出（输出走 VT → WT），其维护的状态是过时的。直接读 ConHost 会拿到旧值，必须由 DLL 主动维护虚拟状态。

### 9.2 ConHost 跟随的必要性

理论上程序所有查询都走 Hook，ConHost 状态可忽略。但实际上：
- 程序可能绕过 Hook（直接 syscall 或 `NtDeviceIoControlFile` 到 ConHost 驱动）
- 调试时用 `cdb` 查 ConHost 状态便于排查
- ConHost 的 `WINDOW_BUFFER_SIZE_EVENT` 事件依赖其状态正确

所以调 orig 让 ConHost 跟随是必要的安全网。

### 9.3 性能考虑

- `VirtualConsoleState` 用 `std::mutex` 保护，写入频率不高（每次 `Set*` 调用），锁开销可接受
- `WriteConsoleW` 光标推算每字符一次 `WcwidthW` 调用，已有缓存优化
- `m_attributes` 用原子变量避免高频属性查询的锁开销
- `SyncToConHost` 仅在 WT 反向同步时调用，频率低

### 9.4 与 Phase 13 VT 直通模式的关系

VT 直通模式下（vim/less），程序通过 VT 序列自己控制光标和屏幕，不依赖 Console API 查询状态。本 Phase 的虚拟状态仅对行编辑模式（cmd）有意义。

但模式切换时（vim 进出 Alt Buffer），需要：
- 进入 VT 模式：保存当前虚拟状态，发 DSR CPR 校正 WT 光标
- 退出 VT 模式：恢复保存的虚拟状态，调 orig 让 ConHost 跟随

这部分在 Phase 13 的模式切换流程中处理，本 Phase 提供 `SaveState` / `RestoreState` 接口供其调用。
