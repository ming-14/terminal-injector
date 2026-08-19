# Phase 6: 输入链路（VT → INPUT_RECORD）

> 本 Phase 实现键盘与鼠标输入的完整链路。完成后，用户在 WT 中的按键、点击、滚轮、拖拽都能正确传递给目标程序。这是整个项目最复杂的 Phase 之一，涉及 VT 输入序列与 Windows `INPUT_RECORD` 结构体的双向翻译。

---

## 1. Phase 目标

1. Hook 输入类 API（W/A 双版本）：
   - `ReadConsoleInputW/A`（核心：读按键/鼠标事件）
   - `PeekConsoleInputW/A`（偷窥队列，不消费）
   - `GetNumberOfConsoleInputEvents`（查询队列长度）
   - `WriteConsoleInputW/A`（程序主动塞事件，需拦截同步到缓存）
   - `ReadFile`（针对 `CONIN$` 句柄，VT 输入透传模式）
   - `FlushConsoleInputBuffer`（清空 DLL 内部队列，不清中介发来的）
2. 实现 `VtToInputRecord` 翻译器：
   - 键盘 VT 序列 → `KEY_EVENT_RECORD`（含修饰键、Unicode 字符、虚拟键码）
   - 鼠标 VT 序列（SGR 1006 格式 `\x1b[<btn;col;rowM/m`）→ `MOUSE_EVENT_RECORD`
3. DLL 内部维护**输入事件队列**（中介发来的 VT 经翻译后入队，Hook 读取时出队）
4. 根据 `ConsoleMode` 决定**透传模式**（`ENABLE_VIRTUAL_TERMINAL_INPUT`）vs **翻译模式**（老式）
5. 验证：cmd 中键盘输入、方向键、Tab 补全；python TUI 中鼠标点击、滚轮

---

## 2. 前置依赖

- Phase 5 完成（光标/缓冲区 Hook 可用，鼠标坐标依赖正确的窗口尺寸）
- Phase 7 部分依赖（`GetConsoleMode` 欺骗），但本 Phase 可先用真实 mode，Phase 7 再完善状态机

---

## 3. 涉及文件清单

```
src/dll/
├── hooks/
│   ├── InputHooks.h                 # 新建
│   └── InputHooks.cpp               # 输入类 Hook
├── translator/
│   ├── VtToInputRecord.h            # 新建
│   ├── VtToInputRecord.cpp          # VT 输入解析器
│   └── VtInputParser.h              # 新建：VT 输入流分帧
├── state/
│   └── InputQueue.h                 # 新建：INPUT_RECORD 队列
└── common/
    └── protocol/
        └── Message.h                # 已有 VtInput，无需改
```

---

## 4. 详细任务

### 4.1 输入数据流

```
用户在 WT 按键/点击
  → WT 生成 VT 输入字节流（如 "\x1b[A" 上箭头, "\x1b[<0;10;20M" 鼠标按下）
  → mediator stdin 读取 → IPC 发 VtInput 消息给 DLL
  → DLL 接收线程：
      if (ConsoleMode & ENABLE_VIRTUAL_TERMINAL_INPUT):
          # 透传模式：直接把 VT 字节入队为"原始字节"，ReadFile(CONIN$) 直接返回
          EnqueueRawBytes(vt)
      else:
          # 翻译模式：解析 VT → INPUT_RECORD 数组 → 入队
          records = VtToInputRecord::Parse(vt)
          EnqueueRecords(records)
  → 目标程序 ReadConsoleInput Hook 从队列出队返回
```

### 4.2 输入事件队列（`InputQueue.h`）

```cpp
#pragma once
#include <windows.h>
#include <deque>
#include <mutex>

namespace terminjector {

// DLL 内部输入事件队列
// 线程安全，供 ReadConsoleInput/PeekConsoleInput 消费
class InputQueue {
public:
    static InputQueue& Instance();

    // 入队 INPUT_RECORD 数组（翻译模式）
    void Enqueue(const INPUT_RECORD* records, size_t count);

    // 出队（ReadConsoleInput 用，消费式）
    // 返回实际出队数量
    size_t Dequeue(INPUT_RECORD* out, size_t count);

    // 偷窥（PeekConsoleInput 用，不消费）
    size_t Peek(INPUT_RECORD* out, size_t count) const;

    // 当前队列长度
    size_t Count() const;

    // 清空（FlushConsoleInputBuffer 用）
    void Clear();

    // 信号：有数据时唤醒等待 ReadConsoleInput 的线程
    void SignalDataReady();
    HANDLE GetWaitHandle() const { return m_event; }

private:
    InputQueue();
    mutable std::mutex m_mutex;
    std::deque<INPUT_RECORD> m_queue;
    HANDLE m_event = nullptr; // 手动重置事件，供 WaitForSingleObject Hook 用
};

} // namespace terminjector
```

### 4.3 `ReadConsoleInput` Hook

```cpp
DEFINE_ORIG_PTR(ReadConsoleInputW, BOOL WINAPI(HANDLE, INPUT_RECORD*, DWORD, LPDWORD));

BOOL WINAPI ReadConsoleInputW_Detour(HANDLE h, INPUT_RECORD* buf,
                                     DWORD count, LPDWORD read) {
    ENSURE_INITIALIZED();
    if (!IsConsoleHandle(h)) return ReadConsoleInputW_orig(h, buf, count, read);

    // 阻塞直到队列有数据
    auto& queue = InputQueue::Instance();
    while (queue.Count() == 0) {
        WaitForSingleObject(queue.GetWaitHandle(), INFINITE);
    }
    *read = static_cast<DWORD>(queue.Dequeue(buf, count));
    return TRUE;
}
```

### 4.4 `PeekConsoleInput` 与 `GetNumberOfConsoleInputEvents`

```cpp
BOOL WINAPI PeekConsoleInputW_Detour(HANDLE h, INPUT_RECORD* buf,
                                     DWORD count, LPDWORD read) {
    ENSURE_INITIALIZED();
    if (!IsConsoleHandle(h)) return PeekConsoleInputW_orig(h, buf, count, read);
    *read = static_cast<DWORD>(InputQueue::Instance().Peek(buf, count));
    return TRUE;
}

BOOL WINAPI GetNumberOfConsoleInputEvents_Detour(HANDLE h, LPDWORD count) {
    ENSURE_INITIALIZED();
    if (!IsConsoleHandle(h)) return GetNumberOfConsoleInputEvents_orig(h, count);
    *count = static_cast<DWORD>(InputQueue::Instance().Count());
    return TRUE;
}
```

### 4.5 `VtToInputRecord` 翻译器

#### 4.5.1 键盘 VT 序列映射表

| VT 序列 | 按键 | Virtual Key | Unicode |
|---------|------|-------------|---------|
| `\x1b[A` | 上 | VK_UP | 0 |
| `\x1b[B` | 下 | VK_DOWN | 0 |
| `\x1b[C` | 右 | VK_RIGHT | 0 |
| `\x1b[D` | 左 | VK_LEFT | 0 |
| `\x1b[H` | Home | VK_HOME | 0 |
| `\x1b[F` | End | VK_END | 0 |
| `\x1b[2~` | Insert | VK_INSERT | 0 |
| `\x1b[3~` | Delete | VK_DELETE | 0 |
| `\x1b[5~` | PageUp | VK_PRIOR | 0 |
| `\x1b[6~` | PageDown | VK_NEXT | 0 |
| `\x1b[1;2A` | Shift+上 | VK_UP+SHIFT | 0 |
| `\x1b[1;5A` | Ctrl+上 | VK_UP+CTRL | 0 |
| `\x1b[1;3A` | Alt+上 | VK_UP+ALT | 0 |
| `\t` | Tab | VK_TAB | L'\t' |
| `\r` | Enter | VK_RETURN | L'\r' |
| `\x7f` | Backspace | VK_BACK | L'\b' |
| `\x03` | Ctrl+C | VK_C+CTRL | L'\x03' |
| 可打印字符 | 字符本身 | VkKeyScanW(ch) | ch |

每个按键产生**两个** `INPUT_RECORD`（按下 + 释放），`bKeyDown` 分别为 TRUE/FALSE。

#### 4.5.2 键盘翻译实现骨架

```cpp
// VtToInputRecord.cpp
std::vector<INPUT_RECORD> VtToInputRecord::ParseKeyboard(const std::string& vt) {
    std::vector<INPUT_RECORD> result;
    size_t i = 0;
    while (i < vt.size()) {
        wchar_t ch = 0;
        WORD vk = 0;
        DWORD ctrlState = 0;

        if (vt[i] == '\x1b' && i + 1 < vt.size() && vt[i+1] == '[') {
            // CSI 序列
            auto [keyVk, keyCtrl, consumed] = ParseCsi(vt.data() + i, vt.size() - i);
            vk = keyVk; ctrlState = keyCtrl;
            i += consumed;
        } else if (vt[i] == '\x1b' && i + 1 < vt.size()) {
            // Alt+字符：\x1b<ch>
            ch = static_cast<wchar_t>(static_cast<unsigned char>(vt[i+1]));
            vk = VkKeyScanW(ch);
            ctrlState = LEFT_ALT_PRESSED;
            i += 2;
        } else {
            // 普通字符（UTF-8 解码）
            int consumed = DecodeUtf8(vt.data() + i, vt.size() - i, ch);
            vk = ch ? VkKeyScanW(ch) : 0;
            i += consumed;
        }

        // 生成按下 + 释放两个事件
        if (vk || ch) {
            result.push_back(MakeKeyRecord(true, vk, ch, ctrlState));
            result.push_back(MakeKeyRecord(false, vk, ch, ctrlState));
        }
    }
    return result;
}

INPUT_RECORD MakeKeyRecord(bool down, WORD vk, wchar_t ch, DWORD ctrlState) {
    INPUT_RECORD r{};
    r.EventType = KEY_EVENT;
    r.Event.KeyEvent.bKeyDown = down ? TRUE : FALSE;
    r.Event.KeyEvent.wRepeatCount = 1;
    r.Event.KeyEvent.wVirtualKeyCode = vk;
    r.Event.KeyEvent.wVirtualScanCode = MapVirtualKeyW(vk, MAPVK_VK_TO_VSC);
    r.Event.KeyEvent.uChar.UnicodeChar = ch;
    r.Event.KeyEvent.dwControlKeyState = ctrlState;
    return r;
}
```

#### 4.5.3 鼠标 VT 序列（SGR 1006 格式）

WT 默认用 SGR 1006 鼠标格式：`\x1b[<button;col;row M`（按下/移动）或 `m`（释放）。

- `button` 编码：
  - 0: 左键按下
  - 1: 中键按下
  - 2: 右键按下
  - 3: 释放（任何键）
  - 64: 滚轮上
  - 65: 滚轮下
  - +8: Shift，+16: Alt，+32: Ctrl（位组合）
- `col`/`row`：1-based，需 -1 转 0-based 给 `MOUSE_EVENT_RECORD`

#### 4.5.4 鼠标翻译实现

```cpp
std::vector<INPUT_RECORD> VtToInputRecord::ParseMouse(const std::string& vt) {
    // 格式：\x1b[<btn;col;rowM 或 \x1b[<btn;col;rowm
    int btn, col, row;
    char type; // 'M' 或 'm'
    if (sscanf(vt.c_str(), "\x1b[<%d;%d;%d%c", &btn, &col, &row, &type) != 4) return {};

    MOUSE_EVENT_RECORD mer{};
    mer.dwMousePosition.X = static_cast<SHORT>(col - 1); // 1-based → 0-based
    mer.dwMousePosition.Y = static_cast<SHORT>(row - 1);

    // 修饰键
    if (btn & 8)  mer.dwControlKeyState |= SHIFT_PRESSED;
    if (btn & 16) mer.dwControlKeyState |= LEFT_ALT_PRESSED;
    if (btn & 32) mer.dwControlKeyState |= LEFT_CTRL_PRESSED;

    int baseBtn = btn & 3;
    bool isRelease = (type == 'm') || baseBtn == 3;
    int wheel = btn & 64;

    if (wheel) {
        mer.dwEventFlags = MOUSE_WHEELED;
        mer.dwButtonState = (baseBtn == 1) ? 0 : (0xFFFFFFFF); // 上滚正，下滚负
    } else {
        // 维护按键状态（静态，跨事件持续）
        static DWORD s_buttonState = 0;
        if (baseBtn == 0 && !isRelease) s_buttonState |= FROM_LEFT_1ST_BUTTON_PRESSED;
        else if (baseBtn == 1 && !isRelease) s_buttonState |= FROM_LEFT_2ND_BUTTON_PRESSED;
        else if (baseBtn == 2 && !isRelease) s_buttonState |= RIGHTMOST_BUTTON_PRESSED;
        else if (isRelease) {
            // 简化：释放时清对应位
            s_buttonState &= ~(FROM_LEFT_1ST_BUTTON_PRESSED | FROM_LEFT_2ND_BUTTON_PRESSED | RIGHTMOST_BUTTON_PRESSED);
        }
        mer.dwButtonState = s_buttonState;
        mer.dwEventFlags = isRelease ? 0 : 0; // 按下/释放都是 0；移动是 MOUSE_MOVED
    }

    INPUT_RECORD r{};
    r.EventType = MOUSE_EVENT;
    r.Event.MouseEvent = mer;
    return {r};
}
```

#### 4.5.5 VT 输入流分帧（`VtInputParser`）

VT 输入是字节流，一次 `Recv` 可能拿到半个序列或多条序列。需要状态机分帧：

```cpp
class VtInputParser {
public:
    // 喂入字节流，返回解析出的完整事件
    std::vector<INPUT_RECORD> Feed(const uint8_t* data, size_t len);

private:
    std::string m_buf; // 未解析的缓冲
    // 状态机：普通字符 / ESC / CSI（参数收集）/ 鼠标 / 键盘
};
```

**关键**：CSI 序列以 `\x1b[` 开头，以 `0x40-0x7E` 之间的字节结束。鼠标序列固定含 3 个分号参数 + `M/m`。

### 4.6 透传模式（`ENABLE_VIRTUAL_TERMINAL_INPUT`）

若目标程序开启了 VT 输入模式（如 vim、less），则不翻译，直接把 VT 字节当作 `ReadFile(CONIN$)` 的返回：

```cpp
BOOL WINAPI ReadFile_Detour_ForInput(HANDLE h, LPVOID buf, DWORD len,
                                     LPDWORD read, LPOVERLAPPED ov) {
    ENSURE_INITIALIZED();
    if (h != GetStdHandle(STD_INPUT_HANDLE)) {
        return ReadFile_orig(h, buf, len, read, ov); // 文件等不拦截
    }
    // 透传模式：从原始字节队列读
    *read = static_cast<DWORD>(InputQueue::Instance().DequeueRaw(
        reinterpret_cast<uint8_t*>(buf), len));
    return TRUE;
}
```

`InputQueue` 增加 `m_rawQueue`（字节队列）与 `m_recordQueue`（结构体队列），根据 mode 选择。或更简单：DLL 收到 VtInput 时根据当前 mode 决定入哪个队列。

### 4.7 `FlushConsoleInputBuffer`

```cpp
BOOL WINAPI FlushConsoleInputBuffer_Detour(HANDLE h) {
    ENSURE_INITIALIZED();
    if (!IsConsoleHandle(h)) return FlushConsoleInputBuffer_orig(h);
    // 仅清空 DLL 内部队列，不清中介发来的（中介侧 stdin 仍有数据，下次会重发）
    InputQueue::Instance().Clear();
    return TRUE;
}
```

### 4.8 DLL 接收线程整合（Phase 3 已建，补输入分支）

```cpp
void DllRecvLoop() {
    VtInputParser parser;
    while (g_transport->IsConnected()) {
        protocol::MessageType type;
        std::vector<uint8_t> payload;
        if (!RecvPacket(*g_transport, type, payload)) break;

        if (type == protocol::MessageType::VtInput) {
            auto& state = ConsoleState::Instance();
            if (state.GetInputMode() & ENABLE_VIRTUAL_TERMINAL_INPUT) {
                // 透传
                InputQueue::Instance().EnqueueRaw(payload.data(), payload.size());
            } else {
                // 翻译
                auto records = parser.Feed(payload.data(), payload.size());
                InputQueue::Instance().Enqueue(records.data(), records.size());
            }
            InputQueue::Instance().SignalDataReady();
        }
        // ... ResizeNotify 等其他消息 ...
    }
}
```

---

## 5. 验证标准

| 测试 | 预期 |
|------|------|
| cmd 中输入 `dir` + Enter | WT 执行 dir，显示结果 |
| cmd 中按上箭头 | 调出上一条历史命令 |
| cmd 中按 Tab | 路径补全 |
| cmd 中 Ctrl+C | 中断当前命令（Phase 7 完善信号） |
| python REPL 输入 `print("hi")` | 正常执行 |
| python curses 程序点击按钮 | 鼠标点击响应 |
| python curses 程序滚轮 | 滚动响应 |
| vim 中点击定位光标 | 光标移到点击位置 |

### 已知限制

- Ctrl+C 信号传递在 Phase 7 完善
- 鼠标坐标依赖窗口尺寸正确（Phase 5）
- VT 输入模式欺骗在 Phase 7（目标可能未开 VT 输入模式，需 Hook GetConsoleMode 强制返回）

### 4.9 握手初始化鼠标报告（2026-08-19，BUG-010/011）

**问题**：目标在**注入前**已启用 `ENABLE_MOUSE_INPUT`（如注入运行中的全屏 TUI：
winui 启动即 `SetConsoleMode(0x98)`），注入后不再调 SetConsoleMode → `ModeChange`
消息永不发出（ModeHooks 仅模式变化时发送）→ mediator 从未向 WT 发 `\x1b[?1002h\x1b[?1006h`
（`m_mouseReportEnabled` 初始 false 且握手不回填）→ WT 未启用鼠标报告，把点击/拖拽
当默认选择行为，目标收不到任何 MOUSE_EVENT。

**修复**：`Mediator::ApplyInitialMouseReport`（握手 `Handshake()` 中，收到 Hello 后
调用）按 `HelloPayload.inputMode` 的 ENABLE_MOUSE_INPUT 标志补发启用/禁用序列并
同步 `m_mouseReportEnabled`（与 `OnModeChange` 幂等共享状态，一致则不重复发）。
配套：`StateSnapshot::ToHelloPayload` 的 `consoleMode` 字段原误填 `outputMode`，
改填 `inputMode`（对齐协议注释"初始 GetConsoleMode（输入句柄）"）。
回归：`tests/e2e/mouse/test_presolve_mouse.py`（注入前 0x98 模式目标：握手日志含
1002h + 点击 down/up 到达 + 拖拽按下期间坐标移动）。

---

## 6. 风险点

| 风险 | 缓解 |
|------|------|
| 鼠标高频事件 IPC 风暴 | DLL 内攒批（Phase 10），或 mediator 侧限流 |
| VT 输入分帧状态机复杂、易漏序列 | 参考 `xterm` 鼠标协议规范，单元测试覆盖常见序列 |
| `ReadConsoleInput` 阻塞等待，管道断开时无法唤醒 | `WaitForSingleObject` 用超时 + 检查 `IsConnected`；Phase 8 Hook WaitFor 时配合事件句柄 |
| 鼠标按键状态跨事件维护出错 | 用 `ConsoleState` 字段持久化 `dwButtonState`，而非 static |
| 透传/翻译模式切换时队列残留 | 切换 mode 时清空两个队列 |

---

## 7. 交付物清单

- [ ] `InputHooks.cpp` 6 个输入 API Hook
- [ ] `InputQueue` 线程安全队列（含事件句柄）
- [ ] `VtToInputRecord` 键盘翻译（含 CSI 解析、修饰键）
- [ ] `VtToInputRecord` 鼠标翻译（SGR 1006）
- [ ] `VtInputParser` 流分帧状态机
- [ ] 透传模式（ReadFile for CONIN$）
- [ ] DLL 接收线程 VtInput 分支
- [ ] 验证键盘 + 鼠标全过
