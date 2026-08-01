# Phase 18: 滚动缓冲区一致性（Scrollback Buffer）

> 本 Phase 解决滚动缓冲区（scrollback buffer）在虚拟状态与 ConHost 间的一致性问题。
> 完成后，程序通过 `GetConsoleScreenBufferInfo` 查到的缓冲区尺寸包含滚动历史，
> `ReadConsoleOutput` 可读取视口外的内容，ConHost 的滚动缓冲区与虚拟状态同步。

---

## 1. 背景与动机

### 1.1 问题

当前 `VirtualConsoleState` 中 `m_bufferSize == m_windowRect`，即缓冲区尺寸等于可视窗口尺寸。
这是 Phase 14 为简化 WT 反向同步所做的假设，但实际 Console 语义中缓冲区可以远大于视口。

**问题 1：滚动时缓冲区尺寸不增长**

```
初始状态：bufferSize=(80,25),  windowRect=(0,0)-(79,24)
写入 26 行文本后，光标在 Y=24，再写入一行触发滚动：
  AdvanceCursor 把 Y 钳制在 24（rows-1），但 scrollback 未跟踪
  bufferSize 仍为 (80,25)，实际应有 1 行滚出视口
```

**问题 2：`ApplyWtResize` 重置缓冲区尺寸**

```
SetConsoleScreenBufferSize 设置 bufferSize=(80,300)
WT resize 触发 ApplyWtResize(120,30)：
  bufferSize 被重置为 (120,30)，用户设置的 300 行丢失
```

**问题 3：`ReadConsoleOutput` 无法读取滚出内容**

```
程序在滚动后调用 ReadConsoleOutput 读取视口外的行，
当前仅返回虚拟状态中的可见区域，滚出内容丢失。
```

### 1.2 目标

1. 滚动缓冲区跟踪：`AdvanceCursor` 滚动时递增滚动计数
2. 缓冲区尺寸管理：`SetConsoleScreenBufferSize` 设置的用户尺寸 + 滚动增长
3. `FillScreenBufferInfo` 正确报告 `bufferSize > windowSize`
4. `SyncToConHost` 同步滚动缓冲区尺寸到 ConHost
5. e2e 测试验证滚动缓冲区一致性

---

## 2. 前置依赖

- Phase 14 完成（`VirtualConsoleState` 框架已就绪）
- Phase 17 完成（字符宽度计算正确，光标推进准确）

---

## 3. 涉及文件清单

```
docs/
└── phases/
    └── 18-scrollback-buffer.md        # 新增：本 Phase 文档

src/
├── dll/
│   ├── state/
│   │   └── VirtualConsoleState.h      # 扩展：滚动计数、最大缓冲区尺寸
│   │   └── VirtualConsoleState.cpp    # 实现：AdvanceCursor 滚动跟踪、ApplyWtResize 保留缓冲区
│   │   └── ConsoleState.h             # 扩展：同步滚动计数接口
│   │   └── ConsoleState.cpp           # 实现：AdvanceCursor 滚动跟踪
│   └── hooks/
│       └── BufferHooks.cpp            # 修改：SetConsoleScreenBufferSize 跟踪用户请求尺寸
│       └── CursorHooks.cpp            # 修改：FillScreenBufferInfo 报告 bufferSize > windowSize
│       └── OutputHooks.cpp            # 修改：ScrollConsoleScreenBuffer 更新滚动计数

tests/
├── phase18_scrollback_test.py         # 新增：滚动缓冲区验证目标程序
└── runners/
    └── test_phase18.py                # 新增：Phase 18 e2e 测试套件
```

---

## 4. 详细设计

### 4.1 VirtualConsoleState：滚动计数

**VirtualConsoleState.h** 新增字段：

```cpp
// ---- 滚动缓冲区（Phase 18） ----
// 跟踪内容滚出视口顶部的行数，用于正确报告 bufferSize.Y
// 初始为 0，AdvanceCursor 在底部换行时递增
int32_t m_scrollbackLines = 0;

// 用户通过 SetConsoleScreenBufferSize 请求的最大缓冲区高度
// 当 WT resize 时，以此值为下限保留缓冲区高度
// 初始为 0（未由用户设置时，使用可见窗口高度）
int32_t m_userBufferHeight = 0;
```

**VirtualConsoleState.cpp** 修改 `AdvanceCursor`：

```cpp
// 在底部换行（wrapLine）时递增滚动计数
auto wrapLine = [&]() {
    m_cursorPos.X = 0;
    m_cursorPos.Y++;
    if (m_cursorPos.Y >= rows) {
        m_cursorPos.Y = rows - 1;
        m_scrollbackLines++;  // Phase 18：递增滚动计数
    }
};
```

**VirtualConsoleState.cpp** 修改 `ApplyWtResize`：

```cpp
void VirtualConsoleState::ApplyWtResize(int32_t cols, int32_t rows) {
    std::lock_guard<std::mutex> lock(m_lock);
    m_bufferSize.X = static_cast<SHORT>(cols);
    // Phase 18：保留用户设置的缓冲区高度 + 滚动计数
    int32_t minHeight = std::max(m_userBufferHeight, 
                                  rows + m_scrollbackLines);
    m_bufferSize.Y = static_cast<SHORT>(std::max(rows, minHeight));
    m_windowRect.Left = 0;
    m_windowRect.Top = 0;
    m_windowRect.Right = static_cast<SHORT>(cols - 1);
    m_windowRect.Bottom = static_cast<SHORT>(rows - 1);
    // 光标位置裁剪
    if (m_cursorPos.X >= m_bufferSize.X) {
        m_cursorPos.X = static_cast<SHORT>(m_bufferSize.X - 1);
    }
    if (m_cursorPos.Y >= m_bufferSize.Y) {
        m_cursorPos.Y = static_cast<SHORT>(m_bufferSize.Y - 1);
    }
}
```

**VirtualConsoleState.cpp** 新增 `SetUserBufferHeight` / `GetScrollbackLines`：

```cpp
void VirtualConsoleState::SetUserBufferHeight(int32_t height) {
    std::lock_guard<std::mutex> lock(m_lock);
    m_userBufferHeight = height;
    // 同步更新 bufferSize
    SHORT rows = static_cast<SHORT>(m_windowRect.Bottom - m_windowRect.Top + 1);
    int32_t minHeight = std::max(height, rows + m_scrollbackLines);
    m_bufferSize.Y = static_cast<SHORT>(std::max(rows, minHeight));
}

int32_t VirtualConsoleState::GetScrollbackLines() const {
    std::lock_guard<std::mutex> lock(m_lock);
    return m_scrollbackLines;
}
```

### 4.2 ConsoleState：同步滚动跟踪

**ConsoleState.h** 新增：

```cpp
// ---- 滚动缓冲区（Phase 18） ----
// 同步跟踪 VirtualConsoleState 的滚动计数
int32_t m_scrollbackLines = 0;
int32_t GetScrollbackLines() const;
void SetScrollbackLines(int32_t n);
```

**ConsoleState.cpp** 修改 `AdvanceCursor`：

```cpp
// 在底部换行时递增滚动计数（Phase 18）
auto wrapLine = [&]() {
    c.X = 0;
    c.Y++;
    if (c.Y >= rows) {
        c.Y = rows - 1;
        m_scrollbackLines++;  // Phase 18
    }
};
```

### 4.3 BufferHooks：用户请求缓冲区尺寸

**BufferHooks.cpp** 修改 `SetConsoleScreenBufferSize_Detour`：

```cpp
BOOL WINAPI SetConsoleScreenBufferSize_Detour(HANDLE hConsoleOutput, COORD dwSize) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (!IsConsoleHandle(hConsoleOutput)) {
        return SetConsoleScreenBufferSize_orig(hConsoleOutput, dwSize);
    }

    // 更新缓存
    ConsoleState::Instance().SetBufferSize(dwSize);
    VirtualConsoleState::Instance().SetBufferSize(dwSize);
    // Phase 18：记录用户请求的缓冲区高度
    VirtualConsoleState::Instance().SetUserBufferHeight(dwSize.Y);
    LOG_DEBUG("SetConsoleScreenBufferSize: %dx%d (userBufferHeight=%d)",
              dwSize.X, dwSize.Y, dwSize.Y);

    // 不调原 API：ConHost 不再收到尺寸变更
    return TRUE;
}
```

### 4.4 FillScreenBufferInfo：正确报告缓冲区尺寸

**CursorHooks.cpp** 修改 `GetConsoleScreenBufferInfo` 相关逻辑：

`FillScreenBufferInfo` 已由 `VirtualConsoleState` 接管。当前实现：

```cpp
void VirtualConsoleState::FillScreenBufferInfo(CONSOLE_SCREEN_BUFFER_INFO& info) const {
    std::lock_guard<std::mutex> lock(m_lock);
    info.dwSize = m_bufferSize;
    info.dwCursorPosition = m_cursorPos;
    info.wAttributes = m_attributes.load();
    info.srWindow = m_windowRect;
    info.dwMaximumWindowSize.X = static_cast<SHORT>(
        m_windowRect.Right - m_windowRect.Left + 1);
    info.dwMaximumWindowSize.Y = static_cast<SHORT>(
        m_windowRect.Bottom - m_windowRect.Top + 1);
}
```

Phase 18 无需修改此函数——`m_bufferSize` 已通过 `ApplyWtResize` 和 `SetUserBufferHeight` 维护正确值。

### 4.5 SyncToConHost：同步缓冲区尺寸

**VirtualConsoleState.cpp** 修改 `SyncToConHost`：

```cpp
void VirtualConsoleState::SyncToConHost() {
    HANDLE hOut = GetStdHandle(STD_OUTPUT_HANDLE);
    // Phase 18：同步缓冲区尺寸（含滚动计数）
    SetConsoleScreenBufferSize_orig(hOut, m_bufferSize);
    SetConsoleWindowInfo_orig(hOut, TRUE, &m_windowRect);
    SetConsoleCursorPosition_orig(hOut, m_cursorPos);
    SetConsoleTextAttribute_orig(hOut, m_attributes.load());
}
```

### 4.6 模式切换时重置滚动缓冲

**ModeHooks.cpp** 模式切换时：

```cpp
if (mode != oldMode) {
    // Phase 18：模式切换时重置滚动缓冲区（Alt Buffer 不共享 scrollback）
    // VT 模式下程序自主管理滚动，行编辑模式重新开始计数
    VirtualConsoleState::Instance().ResetScrollback();
    // ...
}
```

**VirtualConsoleState.h** 新增：

```cpp
void ResetScrollback();  // Phase 18：模式切换时重置滚动计数
```

### 4.7 e2e 测试

**目标程序 `tests/phase18_scrollback_test.py`**：
- 写 N 行文本使内容滚动出视口
- 查询 `GetConsoleScreenBufferInfo` 验证 `dwSize.Y > window height`
- 用 `ReadConsoleOutput` 验证可读取滚出内容
- 键盘 'q'：退出

**测试套件 `tests/runners/test_phase18.py`**：
- 测试 1：滚动后缓冲区尺寸查询（验证 `dwSize.Y > srWindow`)
- 测试 2：用户设置缓冲区尺寸保留（`SetConsoleScreenBufferSize` 后用 `GetConsoleScreenBufferInfo` 验证）
- 测试 3：WT resize 后缓冲区尺寸保留（模拟 resize 后验证）
- 测试 4：模式切换后滚动计数重置

---

## 5. 验证标准

| 测试 | 预期 | 说明 |
|------|------|------|
| 滚动后缓冲区尺寸 | `dwSize.Y` = 视口高度 + 滚动行数 | 滚动计数正确递增 |
| 用户设置缓冲区尺寸 | `SetConsoleScreenBufferSize` 设置的高度在 resize 后保留 | 用户请求优先级高于 WT resize |
| WT resize 后缓冲区 | 缓冲区高度 >= max(用户设置, 视口+滚动) | 滚动计数不丢失 |
| 模式切换重置 | 进入 VT 模式后滚动计数清零 | 不污染新会话 |

---

## 6. 风险点

| 风险 | 影响 | 缓解 |
|------|------|------|
| 滚动计数无限增长 | `bufferSize.Y` 超 SHORT 上限（32767） | 限制 `m_scrollbackLines` 上限为 32767 - 视口高度 |
| `ReadConsoleOutput` 读滚出内容 | 程序可能读取到空/脏数据 | 调用 orig 委托 ConHost 返回真实数据 |
| 模式切换时滚动计数残留 | Alt Buffer 带回旧滚动行 | 进入/退出 Alt Buffer 时重置滚动计数 |

---

## 7. 交付物清单

- [ ] Phase 18 文档
- [ ] `VirtualConsoleState` 滚动计数 + 用户缓冲区尺寸
- [ ] `ConsoleState` 同步滚动计数
- [ ] `BufferHooks` 跟踪用户缓冲区尺寸
- [ ] `ModeHooks` 模式切换重置滚动计数
- [ ] `SyncToConHost` 同步缓冲区尺寸
- [ ] Phase 18 e2e 测试（目标程序 + 测试套件）
- [ ] 编译 + 回归测试

---

## 8. 与其他 Phase 的关系

```
Phase 14 (虚拟 Console 状态) ──► 提供 VirtualConsoleState 框架
Phase 17 (字符宽度审计)     ──► 提供准确的 AdvanceCursor
                                      │
                                      ▼
                                 Phase 18 (滚动缓冲区) ◄── 本 Phase
                                      │
                                      ▼
                              滚动缓冲区一致性保证
```

**依赖**：
- Phase 14 提供 `VirtualConsoleState` 基础
- Phase 17 提供准确的 `AdvanceCursor`（字符宽度正确才能准确跟踪滚动）

**被依赖**：
- 后续 TUI 程序（opencode）的滚动行为依赖本 Phase 的缓冲区一致性