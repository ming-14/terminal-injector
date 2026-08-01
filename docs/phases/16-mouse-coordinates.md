# Phase 16: 鼠标坐标状态管理

> 本 Phase 解决鼠标事件处理中静态变量状态污染问题，并增加坐标验证 e2e 测试。
> 完成后，鼠标事件在多会话场景下状态正确，坐标转换链路完整可验证。

---

## 1. 背景与动机

### 1.1 问题

当前鼠标事件处理链路中存在两个静态变量，它们在多会话场景下存在状态污染风险：

**问题 1：`InputRecordToVt::ConvertMouse` 的 `s_prevButtonState`（mediator 侧）**

```cpp
// InputRecordToVt.cpp 第 211 行
static DWORD s_prevButtonState = 0;
```

该变量用于跟踪上一次鼠标事件的按键状态，以便检测按下/释放转换。但：
- 静态变量在 mediator 全局共享，子进程会话（ChildSession）的 `InputRecordToVt` 实例也能访问
- 虽然当前 mediator 只用一个 `InputRecordToVt` 实例（`VtPassThrough::ForwardStdinToPipe` 中），但设计上不应依赖此假设
- 模式切换时无法重置

**问题 2：`VtToInputRecord::ParseMouse` 的 `s_buttonState`（DLL 侧）**

```cpp
// VtToInputRecord.cpp 第 401 行
static DWORD s_buttonState = 0;
```

该变量用于跟踪当前鼠标按键状态，将 SGR 1006 的按下/释放事件转换为 `INPUT_RECORD` 的 `dwButtonState`。但：
- 静态变量在 DLL 全局共享，所有 VtInputParser 实例共享同一个状态
- 代码注释已说明"文档建议用 ConsoleState 持久化，但 Phase 6 先用 static 简化"
- 模式切换时无法重置

**问题 3：缺少坐标验证**

现有鼠标测试（`test_python_curses_mouse.py`、`test_vim_mouse.py`）只验证"鼠标事件到达"，不验证坐标值是否正确。

### 1.2 目标

1. 消除静态变量，将鼠标按键状态纳入实例字段或 ConsoleState 管理
2. 增加 `MOUSE_HWHEELED` 横向滚轮处理
3. 新增 e2e 测试验证鼠标坐标精确性

---

## 2. 前置依赖

- Phase 6 完成（输入链路、`InputQueue`、`VtToInputRecord` 已就绪）
- Phase 13 完成（VT 直通模式，鼠标序列直通）

---

## 3. 涉及文件清单

```
docs/
└── phases/
    └── 16-mouse-coordinates.md       # 新增：本 Phase 文档

src/
├── dll/
│   ├── state/
│   │   └── ConsoleState.h            # 扩展：GetMouseButtonState / SetMouseButtonState
│   │   └── ConsoleState.cpp          # 实现：鼠标按键状态存取
│   └── translator/
│       └── VtToInputRecord.cpp       # 修改：s_buttonState → ConsoleState
└── mediator/
    ├── InputRecordToVt.h             # 修改：添加 m_prevButtonState 实例字段
    └── InputRecordToVt.cpp           # 修改：s_prevButtonState → m_prevButtonState

tests/
├── phase16_mouse_test.py            # 新增：鼠标坐标验证目标程序
└── runners/
    └── test_phase16.py              # 新增：Phase 16 e2e 测试套件
```

---

## 4. 详细任务

### 4.1 InputRecordToVt：静态变量 → 实例字段

**`InputRecordToVt.h`** 添加 `m_prevButtonState`：

```cpp
class InputRecordToVt {
    // ... 已有字段 ...
private:
    // 鼠标按键状态（跨 ConvertMouse 调用跟踪按下/释放转换）
    DWORD m_prevButtonState = 0;
};
```

**`InputRecordToVt.cpp`** 使用实例字段替代静态变量：

```cpp
void InputRecordToVt::ConvertMouse(const MOUSE_EVENT_RECORD& me, std::string& out) {
    DWORD cur = me.dwButtonState;
    DWORD prev = m_prevButtonState;
    m_prevButtonState = cur;
    // ... 后续逻辑不变 ...
}
```

### 4.2 VtToInputRecord::ParseMouse：静态变量 → ConsoleState

**`ConsoleState.h`** 添加鼠标按键状态接口：

```cpp
// ---- 鼠标按键状态（Phase 16） ----
// 跟踪跨 ParseMouse 调用的鼠标按键状态，用于 SGR 1006 → INPUT_RECORD 转换
DWORD GetMouseButtonState() const;
void SetMouseButtonState(DWORD state);
```

**`ConsoleState.cpp`** 实现：

```cpp
DWORD ConsoleState::GetMouseButtonState() const {
    AcquireSRWLockShared(&m_lock);
    DWORD s = m_mouseButtonState;
    ReleaseSRWLockShared(&m_lock);
    return s;
}

void ConsoleState::SetMouseButtonState(DWORD state) {
    AcquireSRWLockExclusive(&m_lock);
    m_mouseButtonState = state;
    ReleaseSRWLockExclusive(&m_lock);
}
```

**`ConsoleState.h`** 添加字段：

```cpp
    // ... 已有字段 ...
    DWORD m_mouseButtonState = 0;  // Phase 16：鼠标按键状态
```

**`VtToInputRecord.cpp`** 使用 ConsoleState 替代静态变量：

```cpp
size_t VtToInputRecord::ParseMouse(const uint8_t* data, size_t len,
                                    std::vector<INPUT_RECORD>& out) {
    // ...
    // 替代 static DWORD s_buttonState
    auto& state = ConsoleState::Instance();
    DWORD s_buttonState = state.GetMouseButtonState();
    
    // ... 使用 s_buttonState 逻辑 ...
    
    // 修改后写回
    state.SetMouseButtonState(s_buttonState);
    // ...
}
```

### 4.3 MOUSE_HWHEELED 处理

在 `InputRecordToVt::ConvertMouse` 中增加横向滚轮处理：

```cpp
// 横向滚轮（MOUSE_HWHEELED）
if (me.dwEventFlags & MOUSE_HWHEELED) {
    int wheel = static_cast<int>(cur) >> 16;
    int btn = (wheel > 0) ? 66 : 67;  // 右滚=66, 左滚=67
    // 修饰键（同纵向滚轮）
    if (me.dwControlKeyState & SHIFT_PRESSED) btn |= 4;
    if (me.dwControlKeyState & LEFT_ALT_PRESSED) btn |= 8;
    if (me.dwControlKeyState & LEFT_CTRL_PRESSED) btn |= 16;
    
    char buf[64];
    std::snprintf(buf, sizeof(buf), "\x1b[<%d;%d;%dM",
                  btn, me.dwMousePosition.X + 1, me.dwMousePosition.Y + 1);
    out += buf;
    return;
}
```

### 4.4 e2e 测试

**目标程序 `tests/phase16_mouse_test.py`**：
- 启用 `ENABLE_MOUSE_INPUT`
- 循环 `ReadConsoleInputW` 读取事件
- 鼠标事件：记录坐标/按键/标志位到结果文件
- 键盘 'q'：退出

**测试套件 `tests/runners/test_phase16.py`**：
- 测试 1：鼠标左键点击坐标验证（点击特定位置，验证 X/Y 坐标正确）
- 测试 2：鼠标右键点击坐标验证
- 测试 3：滚轮事件验证
- 测试 4：鼠标移动事件验证（如果程序支持）

---

## 5. 验证标准

| 测试 | 预期 | 说明 |
|------|------|------|
| 鼠标左键点击坐标 | 目标程序读到正确的 0-based 坐标 | 坐标转换正确 |
| 鼠标右键点击坐标 | 坐标正确，按钮状态正确 | 右键映射正确 |
| 滚轮事件 | 滚轮标志位正确，坐标正确 | 滚轮处理正确 |
| 多会话/模式切换后鼠标 | 状态清零，坐标正确 | 静态变量已消除 |

---

## 6. 风险点

| 风险 | 影响 | 缓解 |
|------|------|------|
| ConsoleState 的 SRWLOCK 在 ParseMouse 高频调用下性能下降 | 鼠标事件延迟 | 鼠标事件频率通常 < 100Hz，SRWLOCK 开销可忽略 |
| 模式切换时鼠标按钮状态未重置 | 残留状态导致首次按键误判 | 在 SetConsoleMode Hook 中调用 SetMouseButtonState(0) |
| 坐标转换精度 | 1-based ↔ 0-based 转换可能因边界条件出错 | 测试验证坐标 ±1 精度 |

---

## 7. 交付物清单

- [x] Phase 16 文档
- [x] `InputRecordToVt` 静态变量 → 实例字段
- [x] `VtToInputRecord::ParseMouse` 静态变量 → ConsoleState
- [x] `ConsoleState` 鼠标按键状态接口
- [x] `InputRecordToVt::ConvertMouse` MOUSE_HWHEELED 处理
- [x] Phase 16 e2e 测试（目标程序 + 测试套件）
- [x] 编译 + 回归测试前序 Phase

---

## 8. 与其他 Phase 的关系

```
Phase 6 (输入链路) ──► 提供 VtToInputRecord 基础
                             │
                             ▼
                        Phase 13 (VT 直通模式)
                             │
                             ▼
                        Phase 16 (鼠标坐标) ◄── 本 Phase
                             │
                             ▼
                    Phase 17 (字符宽度) 依赖鼠标坐标正确
```

**依赖**：
- Phase 6 提供 `VtToInputRecord` / `InputQueue` 基础
- Phase 13 提供 VT 模式下鼠标直通

**被依赖**：
- Phase 17（字符宽度）：鼠标坐标正确是字符宽度对齐的前提
- 后续 TUI 程序（vim/opencode）鼠标交互依赖本 Phase