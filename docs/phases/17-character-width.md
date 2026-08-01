# Phase 17: 字符宽度

> 本 Phase 集成 wcwidth 库，使 ConsoleState 和 VirtualConsoleState 的 AdvanceCursor 方法能正确处理 CJK 双宽字符、零宽字符和代理对，确保光标位置计算与 ConHost/ConPTY 行为一致。

---

## 1. Phase 目标

1. **wcwidth 集成**：引入第三方 wcwidth 库，提供 `wcwidth()` 和 `wcwidth32()` 函数
2. **ConsoleState AdvanceCursor 字符宽度处理**：WriteConsoleW 输出后按字符显示宽度推进光标
3. **VirtualConsoleState AdvanceCursor 字符宽度处理**：虚拟 Console 状态同步推进光标
4. **CJK 双宽字符支持**：中文字符等占 2 列宽度的字符正确处理
5. **代理对支持**：32 位 codepoint（如 emoji）组合后计算宽度

---

## 2. 实现细节

### 2.1 wcwidth 集成

从 `third_party/wcwidth/wcwidth.c` 提供：
- `wcwidth(wchar_t ch)`：16 位字符宽度
- `wcwidth32(uint32_t cp)`：32 位 codepoint 宽度

```cpp
// 返回：
//   -1  = 控制字符
//   0   = 零宽字符（组合用音符号等）
//   1   = 半宽字符（ASCII、拉丁字母等）
//   2   = 全宽字符（CJK 表意文字等）
```

### 2.2 ConsoleState AdvanceCursor 字符宽度处理

```cpp
// ConsoleState.cpp
// 按字符显示宽度推进光标（Phase 17 字符宽度审计）
int w;
// 检查当前字符是否为高代理，且下一个字符为低代理
if (ch >= 0xD800 && ch <= 0xDBFF && i + 1 < len &&
    buf[i + 1] >= 0xDC00 && buf[i + 1] <= 0xDFFF) {
    // 代理对：组合为 32 位 codepoint 后计算宽度
    uint32_t cp = 0x10000 +
        ((static_cast<uint32_t>(ch) - 0xD800) << 10) +
        (static_cast<uint32_t>(buf[i + 1]) - 0xDC00);
    w = wcwidth32(cp);
    ++i;  // 跳过低代理
} else {
    w = wcwidth(ch);
}
if (w < 0) w = 0;  // 控制字符按 0 宽度处理
c.X = static_cast<SHORT>(c.X + w);
```

### 2.3 VirtualConsoleState AdvanceCursor 相同逻辑

```cpp
// VirtualConsoleState.cpp
// 与 ConsoleState 相同逻辑
int w;
if (isHighSurrogate(ch) && i + 1 < len && isLowSurrogate(buf[i + 1])) {
    uint32_t cp = combineSurrogate(ch, buf[i + 1]);
    w = wcwidth32(cp);
    ++i;
} else {
    w = wcwidth(ch);
}
if (w < 0) w = 0;
m_cursorPos.X = static_cast<SHORT>(m_cursorPos.X + w);
```

---

## 3. 涉及文件

```
third_party/
└── wcwidth/
    ├── wcwidth.c      # wcwidth/wcwidth32 实现
    └── wcwidth.h      # 头文件声明
src/dll/
├── state/
│   ├── ConsoleState.cpp         # AdvanceCursor 字符宽度处理
│   └── VirtualConsoleState.cpp  # AdvanceCursor 字符宽度处理
├── lineedit/
│   └── LineEditor.cpp           # GetCharWidth32 辅助函数
└── CMakeLists.txt               # 添加 wcwidth.c 源文件
```

---

## 4. 测试

```
tests/runners/test_phase17.py  # 字符宽度测试
```

测试内容：
- 半宽字符（ASCII）宽度为 1
- 全宽字符（CJK）宽度为 2
- 零宽字符宽度为 0
- 代理对（emoji）宽度正确
- 混排文本光标位置正确