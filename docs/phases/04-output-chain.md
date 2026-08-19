# Phase 4: 输出链路（Console API → VT 翻译）

> 本 Phase 补全全部输出类 Console API 的 Hook 与 VT 翻译。完成后，目标程序的所有输出途径（`WriteConsole`/`WriteConsoleOutput`/`FillConsoleOutput*`/`ScrollConsoleScreenBuffer`/`WriteFile` 针对控制台与 stderr）都能正确翻译成 VT 序列在 WT 中渲染。

---

## 1. Phase 目标

1. Hook 全部输出类 API（W/A 双版本）：
   - `WriteConsoleW/A`（Phase 3 已完成，本 Phase 补 stderr 通道区分）
   - `WriteConsoleOutputW/A`（字符矩阵 → VT 光标定位 + SGR + 字符）
   - `WriteConsoleOutputCharacterW/A`（仅字符 → VT 光标定位 + 字符）
   - `FillConsoleOutputCharacterW/A`（重复字符填充 → VT 重复或 EL 序列）
   - `FillConsoleOutputAttribute`（颜色填充 → SGR）
   - `ScrollConsoleScreenBufferW/A`（滚屏 → VT SU/SD/ICH/DCH 序列）
   - `WriteFile`（针对 `CONOUT$` 与 `STD_ERROR_HANDLE` 两个句柄）
   - `SetConsoleTextAttribute`（更新颜色缓存 + 输出 SGR）
2. 扩展 `ConsoleToVt` 翻译器：为上述每个 API 实现翻译函数
3. 完善 `ConsoleState`：维护当前文本属性、光标位置（输出推进）
4. mediator 侧 `VtOutput` 消息统一透传到 stdout
5. 验证：cmd 的 `cls`、`color`、`tree`、`dir` 等输出在 WT 中颜色与布局正确

---

## 2. 前置依赖

- Phase 3 完成（`HookManager`、`ConsoleState`、`WriteConsoleW/A` Hook、`ConsoleToVt::WriteConsoleW` 可用）

---

## 3. 涉及文件清单

```
src/dll/
├── hooks/
│   ├── OutputHooks.cpp              # 扩展：补全其余输出 API
│   └── OutputHooks.h
├── translator/
│   ├── ConsoleToVt.h                # 扩展接口
│   ├── ConsoleToVt.cpp              # 各 API 翻译实现
│   ├── Color.cpp                    # 颜色映射（Phase 3 已建，本 Phase 完善）
│   └── VtEscape.h                   # 扩展：滚屏/清屏序列
└── state/
    └── ConsoleState.cpp             # 扩展：文本属性、滚屏状态
```

---

## 4. 详细任务

### 4.1 待 Hook API 清单与翻译策略

| API | 翻译策略 | VT 序列示例 |
|-----|----------|-------------|
| `WriteConsoleW/A` | 文本透传 + SGR（颜色变化时） | `\x1b[31m文本\x1b[0m` |
| `WriteConsoleOutputW/A` | 逐字符矩阵：光标定位 + SGR + 字符 | `\x1b[10;5H\x1b[31mA` |
| `WriteConsoleOutputCharacterW/A` | 光标定位 + 字符（不改颜色） | `\x1b[10;5HABC` |
| `FillConsoleOutputCharacterW/A` | 光标定位 + 重复字符（用 VT REP `b`） | `\x1b[10;5H \x1b[79b` |
| `FillConsoleOutputAttribute` | 光标定位 + SGR + 重复空格 | `\x1b[10;5H\x1b[31m \x1b[79b` |
| `ScrollConsoleScreenBufferW/A` | 区分方向：上滚 `SU`、下滚 `SD`、左右 `ICH/DCH` | `\x1b[5S` |
| `WriteFile(CONOUT$)` | 等价 `WriteConsole`，文本透传 | 同 `WriteConsole` |
| `WriteFile(STDERR)` | 同 `CONOUT$`，但走同一 stdout（WT 不区分） | 同上 |
| `SetConsoleTextAttribute` | 仅更新缓存 + 输出 SGR（不写字符） | `\x1b[31;44m` |

### 4.2 VT 序列扩展（`VtEscape.h`）

```cpp
#pragma once
#include <string>

namespace terminjector::vt {

// 已有（Phase 3）
constexpr const char* kCsi = "\x1b[";
constexpr const char* kOsc = "\x1b]";
constexpr const char* kReset = "\x1b[0m";

// 新增（Phase 4）
// 光标定位（1-based）：CSI row;col H
std::string CursorPosition(int row, int col);
// 光标上/下/前/后移动 N：CSI N A/B/C/D
std::string CursorUp(int n);
std::string CursorDown(int n);
std::string CursorForward(int n);
std::string CursorBack(int n);

// 清屏：CSI n J  (0=光标到屏末, 1=屏首到光标, 2=全屏, 3=全屏+回滚)
std::string EraseDisplay(int mode);
// 清行：CSI n K  (0=光标到行末, 1=行首到光标, 2=整行)
std::string EraseLine(int mode);

// 滚屏：CSI n S 上滚 N 行（内容下移），CSI n T 下滚 N 行
std::string ScrollUp(int n);
std::string ScrollDown(int n);
// 在光标位置插入/删除 N 行：CSI n L / CSI n M
std::string InsertLine(int n);
std::string DeleteLine(int n);
// 插入/删除 N 字符：CSI n @ / CSI n P
std::string InsertChar(int n);
std::string DeleteChar(int n);
// 重复上一个字符 N 次：CSI n b
std::string RepeatChar(int n);

// 设置滚动区域（DECSTBM）：CSI top;bottom r
std::string SetScrollRegion(int top, int bottom);
// 重置滚动区域：CSI r
std::string ResetScrollRegion();

// SGR（颜色）来自 Color.cpp
std::string SgrFromAttribute(WORD attr);

} // namespace terminjector::vt
```

### 4.3 `WriteConsoleOutput` 翻译（最复杂）

`WriteConsoleOutput` 直接写入字符矩阵（`CHAR_INFO` 数组），每个字符带属性。翻译策略：遍历矩阵，每个 cell 输出"光标定位 + SGR + 字符"。

```cpp
// translator/ConsoleToVt.cpp
std::string ConsoleToVt::WriteConsoleOutput(
    const CHAR_INFO* buffer, COORD bufferSize, COORD bufferCoord,
    SMALL_RECT writeRegion) {

    std::string out;
    out.reserve(static_cast<size_t>(bufferSize.X) * bufferSize.Y * 8);

    WORD lastAttr = 0xFFFF;
    for (int row = 0; row < bufferSize.Y; ++row) {
        for (int col = 0; col < bufferSize.X; ++col) {
            const CHAR_INFO& ci = buffer[row * bufferSize.X + col];
            // 跳过空格+默认属性的 cell（性能优化，避免满屏空格）
            if (ci.Char.UnicodeChar == L' ' && ci.Attributes == 0x07) continue;

            // VT 光标定位（1-based，基于 writeRegion 偏移）
            int vtRow = writeRegion.Top + row + 1;
            int vtCol = writeRegion.Left + col + 1;
            out += vt::CursorPosition(vtRow, vtCol);

            // 颜色（仅变化时输出 SGR）
            if (ci.Attributes != lastAttr) {
                out += vt::SgrFromAttribute(ci.Attributes);
                lastAttr = ci.Attributes;
            }

            // 字符转 UTF-8
            char utf8[4];
            int len = WideCharToMultiByte(CP_UTF8, 0, &ci.Char.UnicodeChar, 1,
                                          utf8, sizeof(utf8), nullptr, nullptr);
            out.append(utf8, len);
        }
    }
    // 更新状态：光标位置、文本属性
    ConsoleState::Instance().SetCursorPosition({writeRegion.Left, writeRegion.Top});
    ConsoleState::Instance().SetTextAttribute(lastAttr == 0xFFFF ? 0x07 : lastAttr);
    return out;
}
```

**性能注意**：`WriteConsoleOutput` 可能一次写 80×25=2000 个 cell，逐个输出 VT 序列会很长。优化：
- ~~跳过空格 + 默认属性的 cell~~（**已移除**，见下记 BUG-012）
- 合并同行同属性（SGR 仅变化时输出）
- 后期（Phase 10）可考虑 diff 算法只输出与上次不同的 cell

**BUG-012 修正（2026-08-19）**：全量路径（canDiff=false，如 resize 触发整屏重绘）原"跳过空格+默认属性"优化假设 WT 屏幕初始为空白——但注入时 LazyInit 已把目标 ConHost 旧屏幕补发到 WT（或上一帧仍在 WT 上），全量渲染跳过空格 → 旧帧内容永不覆盖 → 窗口 resize 后新旧布局叠画（DLL 日志全量渲染 4935 cells 仅 outBytes=1~23）。修复：全量路径不再跳过 `IsDefaultBlank`，输出包括空格在内的全部 cell；diff 路径（canDiff=true 与 lastBuffer 比较）不受影响。回归：`test_resize_overlay_clean`（0.6x/1.4x 连续 resize 无叠画）。

### 4.4 `FillConsoleOutput*` 翻译

```cpp
// 填充字符（通常用于 cls）
std::string ConsoleToVt::FillConsoleOutputCharacter(
    wchar_t character, DWORD count, COORD writeCoord) {
    std::string out;
    out += vt::CursorPosition(writeCoord.Y + 1, writeCoord.X + 1);
    // 用 VT REP 序列：输出一个字符然后重复 N-1 次
    char utf8[4];
    int len = WideCharToMultiByte(CP_UTF8, 0, &character, 1, utf8, sizeof(utf8), nullptr, nullptr);
    out.append(utf8, len);
    if (count > 1) out += vt::RepeatChar(count - 1);
    return out;
}

// 填充属性（颜色）
std::string ConsoleToVt::FillConsoleOutputAttribute(
    WORD attribute, DWORD count, COORD writeCoord) {
    std::string out;
    out += vt::CursorPosition(writeCoord.Y + 1, writeCoord.X + 1);
    out += vt::SgrFromAttribute(attribute);
    // 填充属性通常配合 FillConsoleOutputCharacter，不重复输出字符
    ConsoleState::Instance().SetTextAttribute(attribute);
    return out;
}
```

### 4.5 `ScrollConsoleScreenBuffer` 翻译

```cpp
// 滚动屏幕缓冲区
// scrollRect: 要滚动的区域
// clipRect:   裁剪区域（可为 null）
// destOrigin: 目标左上角坐标
// fillChar:   滚动后空出的位置填充字符
// fillAttr:   填充属性
std::string ConsoleToVt::ScrollConsoleScreenBuffer(
    SMALL_RECT scrollRect, const SMALL_RECT* clipRect,
    COORD destOrigin, wchar_t fillChar, WORD fillAttr) {

    std::string out;
    int dRow = destOrigin.Y - scrollRect.Top;
    int dCol = destOrigin.X - scrollRect.Left;

    if (dRow > 0) {
        // 内容下移 = 视觉上 ScrollDown
        out += vt::ScrollDown(dRow);
    } else if (dRow < 0) {
        // 内容上移
        out += vt::ScrollUp(-dRow);
    }
    if (dCol > 0) {
        out += vt::CursorForward(dCol);
    } else if (dCol < 0) {
        out += vt::CursorBack(-dCol);
    }
    // TODO: 完整实现需处理 clipRect 与 fillChar 填充
    // 完整版应：设置滚动区域为 scrollRect，移动光标到 destOrigin，输出原 scrollRect 内容，
    // 然后填充空出区域。此处先简化为整体滚屏，Phase 10 完善。
    return out;
}
```

> **注意**：`ScrollConsoleScreenBuffer` 是最复杂的输出 API（涉及区域裁剪、部分滚动、填充），本 Phase 先实现整体滚屏的简化版，Phase 10 补完整区域滚动逻辑。

### 4.6 `WriteFile` Hook（区分 CONOUT$ 与 STDERR）

```cpp
// OutputHooks.cpp 扩展
DEFINE_ORIG_PTR(WriteFile, BOOL WINAPI(
    HANDLE hFile, const VOID* lpBuffer, DWORD nNumberOfBytesToWrite,
    LPDWORD lpNumberOfBytesWritten, LPOVERLAPPED lpOverlapped));

BOOL WINAPI WriteFile_Detour(
    HANDLE hFile, const VOID* lpBuffer, DWORD nNumberOfBytesToWrite,
    LPDWORD lpNumberOfBytesWritten, LPOVERLAPPED lpOverlapped) {

    ENSURE_INITIALIZED();

    // 仅拦截 Console 输出句柄（CONOUT$ 或 STDERR）
    if (!IsConsoleHandle(hFile)) {
        // 文件句柄（含日志文件）直接放行
        return WriteFile_orig(hFile, lpBuffer, nNumberOfBytesToWrite,
                              lpNumberOfBytesWritten, lpOverlapped);
    }

    // 判断是 stdout 还是 stderr（两者都走 VT 输出，WT 不区分）
    // 直接当文本透传
    auto& state = ConsoleState::Instance();
    WORD attr = state.GetTextAttribute();
    // 假定是 UTF-8/ANSI 字节流（WriteFile 不做编码转换）
    std::string vt = vt::SgrFromAttribute(attr);
    vt.append(reinterpret_cast<const char*>(lpBuffer), nNumberOfBytesToWrite);

    SendToMediator(vt.data(), vt.size());
    state.AdvanceCursor(nNumberOfBytesToWrite, /*wrapAtEol=*/true);

    // 仍调原 API 让 ConHost 同步（Phase 9 改为不调）
    return WriteFile_orig(hFile, lpBuffer, nNumberOfBytesToWrite,
                          lpNumberOfBytesWritten, lpOverlapped);
}
```

**日志文件句柄保护**：Phase 1 提到的 Logger 文件句柄必须在 `IsConsoleHandle` 中被识别为非 Console（`GetFileType` 返回 `FILE_TYPE_DISK` 而非 `FILE_TYPE_CHAR`，天然区分）。Phase 9 Hook `CloseHandle` 时需额外排除。

### 4.7 `SetConsoleTextAttribute` Hook

```cpp
DEFINE_ORIG_PTR(SetConsoleTextAttribute, BOOL WINAPI(HANDLE, WORD));

BOOL WINAPI SetConsoleTextAttribute_Detour(HANDLE hConsoleOutput, WORD attr) {
    ENSURE_INITIALIZED();
    if (!IsConsoleHandle(hConsoleOutput)) {
        return SetConsoleTextAttribute_orig(hConsoleOutput, attr);
    }
    // 更新缓存
    ConsoleState::Instance().SetTextAttribute(attr);
    // 输出 SGR（下次 WriteConsole 时会用新属性，但为了立即生效也输出一次）
    std::string sgr = vt::SgrFromAttribute(attr);
    SendToMediator(sgr.data(), sgr.size());
    return SetConsoleTextAttribute_orig(hConsoleOutput, attr);
}
```

### 4.8 注册全部输出 Hook

```cpp
void RegisterOutputHooks() {
    HMODULE hk = GetModuleHandleW(L"kernel32.dll");
    HookManager::RegisterBatch({
        {"WriteConsoleW", GetProcAddress(hk, "WriteConsoleW"), &WriteConsoleW_Detour, (void**)&WriteConsoleW_orig},
        {"WriteConsoleA", GetProcAddress(hk, "WriteConsoleA"), &WriteConsoleA_Detour, (void**)&WriteConsoleA_orig},
        {"WriteConsoleOutputW", GetProcAddress(hk, "WriteConsoleOutputW"), &WriteConsoleOutputW_Detour, (void**)&WriteConsoleOutputW_orig},
        {"WriteConsoleOutputA", GetProcAddress(hk, "WriteConsoleOutputA"), &WriteConsoleOutputA_Detour, (void**)&WriteConsoleOutputA_orig},
        {"WriteConsoleOutputCharacterW", GetProcAddress(hk, "WriteConsoleOutputCharacterW"), &WriteConsoleOutputCharacterW_Detour, (void**)&WriteConsoleOutputCharacterW_orig},
        {"WriteConsoleOutputCharacterA", GetProcAddress(hk, "WriteConsoleOutputCharacterA"), &WriteConsoleOutputCharacterA_Detour, (void**)&WriteConsoleOutputCharacterA_orig},
        {"FillConsoleOutputCharacterW", GetProcAddress(hk, "FillConsoleOutputCharacterW"), &FillConsoleOutputCharacterW_Detour, (void**)&FillConsoleOutputCharacterW_orig},
        {"FillConsoleOutputCharacterA", GetProcAddress(hk, "FillConsoleOutputCharacterA"), &FillConsoleOutputCharacterA_Detour, (void**)&FillConsoleOutputCharacterA_orig},
        {"FillConsoleOutputAttribute", GetProcAddress(hk, "FillConsoleOutputAttribute"), &FillConsoleOutputAttribute_Detour, (void**)&FillConsoleOutputAttribute_orig},
        {"ScrollConsoleScreenBufferW", GetProcAddress(hk, "ScrollConsoleScreenBufferW"), &ScrollConsoleScreenBufferW_Detour, (void**)&ScrollConsoleScreenBufferW_orig},
        {"ScrollConsoleScreenBufferA", GetProcAddress(hk, "ScrollConsoleScreenBufferA"), &ScrollConsoleScreenBufferA_Detour, (void**)&ScrollConsoleScreenBufferA_orig},
        {"WriteFile", GetProcAddress(hk, "WriteFile"), &WriteFile_Detour, (void**)&WriteFile_orig},
        {"SetConsoleTextAttribute", GetProcAddress(hk, "SetConsoleTextAttribute"), &SetConsoleTextAttribute_Detour, (void**)&SetConsoleTextAttribute_orig},
    });
}
```

---

## 5. 验证标准

### 5.1 功能验证

| 测试命令 | 验证点 | 预期 |
|----------|--------|------|
| `echo hello` | WriteConsole 基本输出 | WT 显示 hello |
| `color 0A` | SetConsoleTextAttribute + FillConsoleOutputAttribute | WT 绿字黑底 |
| `cls` | FillConsoleOutputCharacter 清屏 | WT 清屏 |
| `dir` | 多行输出 + 属性 | WT 显示目录列表 |
| `tree /f` | 大量输出 | WT 完整渲染无截断 |
| Python `print("\033[31mred\033[0m")` | VT 透传（程序自发 VT） | WT 红色 red |
| Python traceback | STDERR 输出 | WT 显示错误信息 |
| `prompt $P$G` | WriteConsoleOutputCharacter | WT 显示新提示符 |

### 5.2 性能验证

- `tree %SystemRoot% /f` 满屏输出：WT 无明显延迟（< 1s 完成）
- `for /L %i in (1,1,1000) do echo %i`：1000 行输出流畅
- CPU 占用：< 20%（DLL 翻译开销）

### 5.3 已知限制

- 输入仍不可用（Phase 6）
- 光标位置 Hook 未完成（Phase 5），`SetConsoleCursorPosition` 还会让 ConHost 同步
- VT 模式欺骗未做（Phase 7），目标程序若检测 `GetConsoleMode` 可能不发 VT

---

## 6. 风险点

| 风险 | 缓解 |
|------|------|
| `WriteConsoleOutput` 逐字符 VT 性能差 | 跳过空格 cell、合并同行同属性、Phase 10 用 diff |
| `ScrollConsoleScreenBuffer` 区域滚动不准 | 本 Phase 简化整体滚屏，Phase 10 补 DECSTBM 区域滚动 |
| `WriteFile` 误拦截日志文件 | `GetFileType` 区分 Console/Disk；Phase 9 CloseHandle 额外排除 |
| A 版本编码转换丢字符 | 用 `MultiByteToWideChar` 按当前 CP 转换，失败时 fallback CP_ACP |
| 颜色 SGR 缓存 static 多线程不安全 | 改为 `ConsoleState` 字段受锁保护 |

---

## 7. 交付物清单

- [ ] `OutputHooks.cpp` 补全 13 个输出 API Hook
- [ ] `ConsoleToVt.cpp` 各 API 翻译函数完整
- [ ] `VtEscape.h` 扩展滚屏/清屏/重复序列
- [ ] `ConsoleState` 文本属性字段完善
- [ ] 5.1 功能验证全过
