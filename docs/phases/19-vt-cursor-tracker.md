# Phase 19：VT 输出直通光标跟踪（VtCursorTracker）

> 本文档记录目标进程启用 `ENABLE_VIRTUAL_TERMINAL_PROCESSING`（如 python/Node/vim
> 等）时，输出走 VT 直通分支不推进虚拟光标缓存的问题根因、方案选型与
> `VtCursorTracker` 的设计（**全面版**：承载复杂全屏 TUI 程序）。
>
> 前置 Phase：`13-vt-passthrough-mode.md`、`14-virtual-console-state.md`、
> `17-character-width.md`、`18-scrollback-buffer.md`。

---

## 1. 问题

### 1.1 现象

e2e 全量回归（PASS=83 FAIL=20）中约 10 个失败属于"**输出后查询光标偏差**"：

| 测试 | 现象 | 归因 |
|------|------|------|
| `set_cursor_position` | POS_AFTER_WRITE got (10,6) 期望 (11,6) | 写 "X" 后光标缓存未推进 |
| `set_text_attribute` | CURSOR_X got (0,5) 期望 (1,row) | 同上 |
| `write_console_ascii/unicode` | CURSOR_5 got (0,row) 期望 (5,row) | 同上 |
| `ascii/cjk/emoji/mixed_width` | X 与期望差 1 | 同上 |
| `sync_output` | SyncCursor 未计入软换行 | 同上 |

### 1.2 根因

目标进程调用 API 输出时（如 python `print`），`WriteFile_Detour` /
`WriteConsoleW_Detour` 检测到 `ENABLE_VIRTUAL_TERMINAL_PROCESSING` 开启，走
**VT 直通分支**（OutputHooks.cpp:123-140 / 315-321）：字节 UTF-8 原样转发给
mediator，**不做 `AdvanceCursor`、不更新 `ConsoleState` / `VirtualConsoleState`**。

后续程序调用 `GetConsoleScreenBufferInfo`（Http Hook 返回 `VirtualConsoleState`
缓存，CursorHooks.cpp:99-103）读到的是**陈旧光标**（注入时的行首或上次
`SetConsoleCursorPosition` 的值），于是所有"输出后查询"断言全部偏位。

VT 直通不推进是 Phase 13 的**有意设计**（注释 OutputHooks.cpp:113-122）：
- 前置 `CursorPosition` 同步会覆盖 TUI 程序 VT 流自带的光标定位；
- 把 `\x1b[` 等序列按可打印字符推进会造成光标缓存"疯涨"（曾观测 Y=1373）。

**结论**：直通时需要对 VT 流做**语义解析**来维护虚拟光标，而不是跳过。

---

## 2. 方案选型

在 `src/dll/hooks/OutputHooks.cpp:113-122` 注释与 Phase 14 基础上评估四个方案：

| 方案 | 说明 | 否决原因 |
|------|------|----------|
| **mediator 回传** | mediator 写 ConPTY 后查 `GetConsoleScreenBufferInfo` 光标回传 DLL | 时序不够："写 → 立即查询"发生在目标进程内几十 μs，回传来不及；只能兜底 |
| **查询直接走 ConPTY** | `GetConsoleScreenBufferInfo_Detour` 调 orig | 与 Phase 9 缓存架构冲突：`SetConsoleCursorPosition` 等写操作"只更新缓存不调 orig"（消除黑框闪烁），Get 走真实而 Set 不走则两边脱节，需推翻 Phase 9，影响面大 |
| **轻量解析** | 仅推进文本段，跳过序列，不解析 CSI 光标指令 | 覆盖面窄，python 输出 `\x1b[H` 等定位指令不被跟踪，TUI 会漂移 |
| **全面解析（采用）** | 本地迷你终端光标状态机，覆盖文本 + 全部光标定位/滚动/备用屏语义 | 唯一满足"写后立即查询"且承载全屏 TUI 的方案 |

选定：**本地 VT 输出光标跟踪器（VtCursorTracker）**，全面版。

---

## 3. VtCursorTracker 设计

### 3.1 定位

- 位置：`src/dll/translator/VtCursorTracker.{h,cpp}`；
- 职责：解析 VT 直通字节流，维护"与 ConPTY 一致"的**语义光标**，最终
  `SetCursorPos` 写回 `VirtualConsoleState`（查询真理来源）；
- 粒度：只在直通分支被喂入（WriteFile_Detour 原始字节 / WriteConsoleW_Detour
  的 UTF-16 先转 UTF-8 再喂）。

### 3.2 维护状态

| 状态 | 说明 |
|------|------|
| `m_pos` | 当前光标（0-based） |
| `m_wrapPending` | **wrap pending**：光标在末列且刚写过字符，再写才换行（真实终端语义，`CUB`/光标移动清除） |
| `m_autoWrap` | DECAWM（CSI ?7 h/l）。**注**：LIM-003 实测 ConPTY 不尊重 WRAP_AT_EOL，DECAWM 按标准语义实现，待实测对齐 |
| `m_regionTop/Bottom` | DECSTBM 滚动区（默认 0..rows-1），设置后光标回 region 左上 |
| `m_saved*` | DECSC 保存（光位置 + wrapPending），DECRC 恢复 |
| `m_alt*` / `m_altActive` | **备用屏**：DECSET 1047/1048/1049/47 切换，进入/退出时保存恢复主屏光标（屏幕内容不跟踪，只跟踪光标基准） |
| `m_tab stops` | 固定 8 列（CHT/CBT 按 8 列推进；不做 HTS 可变 tab stop） |

### 3.3 字节状态机

```
Ground ─ESC─▶ Esc ─'['─▶ Csi ─final▶ dispatch
  │                │' ]'─▶ Osc ─BEL/ST▶ Ground
  │                │'( )'▶ CharsetSel
  │                │'P'─▶ Dcs  ─ST▶ Ground
  │                │'7/8/D/E/M/H/…'▶ 单字符指令
  └─CR/LF/BS/TAB/可打印/UTF-8 多字节▶ 文本推进
```

- 文本分段累积，遇序列中断 flush；
- UTF-8：按前缀字节判断 2/3/4 字节，解码出 code point；
  >0xFFFF 拆为 UTF-16 代理对，`wcwidth32` 计算宽度（复用 Phase 17 体系）。

### 3.4 支持语义（全面版）

**C0 控制**：`CR`（回行首）、`LF`（CR+LF 下移一，ConPTY 语义）、`BS`（退格，
清 wrapPending）、`TAB`（下一 8 列）、`BEL`（忽略）。

**文本推进**：
- 宽度 `w`（0/1/2，代理对用 wcwidth32）；
- `autoWrap && m_pos.X == cols-1`：
  - 首字符 → `wrapPending=true`，光标不动；
  - 二次字符 → 换行（滚动区语义见下）后写入；
- 双宽字符 `w==2` 放不下末列时先换行；
- 每次滚动底部触发 `VirtualConsoleState::NotifyScrollLine()`（新增接口）保持
  scrollback 计数与 Phase 18 一致。

**CSI（C0x40-0x7E 终结）**：

| final | 指令 | 语义 |
|-------|------|------|
| `H`/`f` | CUP/HVP | 绝对 r;c（1-based → 0-based，clamp） |
| `A`/`B`/`C`/`D` | CUU/CUD/CUF/CUB | 相对移动（clamp 边界） |
| `E`/`F` | CNL/CPL | 行首 + 行移 |
| `G`/`` ` ``/`d`/`a`/`e` | CHA/HPA/VPA/HPR/VPR | 绝对/相对行列 |
| `I`/`Z` | CHT/CBT | 前/后移 8 列 tab |
| `S`/`T` | SU/SD | 滚动区上/下滚 n 行（光标不动） |
| `L`/`M`/`@`/`P`/`X` | IL/DL/ICH/DCH/ECH | 插入/删除行/字符，光标不动 |
| `r` | DECSTBM | 设滚动区，光标回 region 左上 |
| `s`/`u` | DECSC/DECRC（旧式） | 保存/恢复光标 |
| `K`/`J` | EL/ED | 内容擦除，光标不动；`ED3` 调 `VirtualConsoleState::ResetScrollback()` |
| `n` | DSR | 查询类，ConPTY 处理，不推进 |
| `m`/`h`/`l`/`?…`/`G` | SGR / DECSET / DECRST | 见下 |
| `c` | DA | 能力查询，忽略 |

**DECSET/DECRST（CSI ? Ps h/l）**：
- `7` DECAWM：autoWrap 记录（wrap 行为按标准）；
- `25` DECTCEM：光标可见性——不影响位置，忽略；
- `40`/`47`/`1047`/`1048`/`1049` ALT 屏：切换备用屏 + 光标保存/恢复；
- 其余（1000+ 鼠标、1 应用键、12 光标闪烁等）：记录不影响位置，忽略。

**ESC 单字符**：
- `7` DECSC、`8` DECRC：保存/恢复光标（位置 + wrapPending）；
- `D` IND、`M` RI、`E` NEL：下/上移一（滚动区滚动或移动光标）；
- `H` HTS：记录 8 列 tab（简化）；
- `c` RIS：复位（autoWrap on、备用屏退出、光标 home）、亦可累计清除 scrollback（按 ConPTY 语义，标注待实测）；
- `=`/`>`（键盘）、`#`（Screen Alignment）、`%`（字符集）、`_`/`^`/`P`/`]`（APC/PM/DCS/OSC）跳过。

**OSC/DCS**：字节收集到 `BEL` 或 `ST` 即丢弃，不影响光标。

### 3.5 与 VirtualConsoleState 的同步

- 每次指令 dispatch / 文本 flush 后：`VirtualConsoleState::SetCursorPos(m_pos)`；
- 文本推进触发滚动时：新增公开方法 `VirtualConsoleState::NotifyScrollLine()`
  （内部 `m_scrollbackLines++`，加锁）；
- `ED3` 清屏：`ResetScrollback()`（已有公开接口）。

### 3.6 线程安全与多线程输出

- VtCursorTracker 内部 `std::mutex` 串行化字节解析（一台进程内 VT 直通输出可能
  来自多个输出线程）；
- 每次 `Feed` 入口缓存 cols/rows（`VirtualConsoleState::GetBufferSize`），短缓冲内
  resize 差异可接受（`WtStateReport` resize 会回同步 vcs）。

### 3.7 明确不做 / 后续

| 项 | 原因 |
|----|------|
| 屏幕内容（cell 矩阵）跟踪 | 查询只关心光标；内容由 ConPTY/mediator 负责 |
| 可变 tab stop（HTS/TBC） | 常规终端固定 8 列即可 |
| 滚屏时 scrollback 精确回归算法 | 按"区域顶部滚动出屏"语义计数，测试校准 |
| 每指令前置 `CursorPosition` | 会污染直通 VT 流（Phase 13 理由不变） |

---

## 4. 接入点

`src/dll/hooks/OutputHooks.cpp`：

```
WriteFile_Detour VT 直通分支:     VtCursorTracker::Instance().Feed(bytes, n)
WriteConsoleW_Detour VT 直通分支: // UTF-16 → UTF-8 → Feed
```

`src/dll/translator/VtCursorTracker.{h,cpp}` 加入 `injected_dll` 构建
（`src/dll/CMakeLists.txt`）。

---

## 5. 验证

- 直接回归：`set_cursor_position`、`set_text_attribute`、`write_console_ascii/unicode`、
  `ascii_width`、`cjk_width`、`emoji_width`、`mixed_width`（均应转 PASS）；
- 前瞻回归：全屏 TUI（`vim`/`less`/`textual`）直通查询与滚动区场景（新增目标脚本
  `_targets/vt_cursor_tracker_*.py`：输出长文本 + CSI 定位 + DECSTBM + ALT 屏后
  查询光标）；
- 既有 TUI 测试（`textual`）与 `scrollback` 系列不回归。

## 6. 相关失败清单（本 Phase 覆盖）

`set_cursor_position` / `set_text_attribute` / `write_console_ascii` /
`write_console_ansi` / `write_console_unicode` / `query_console` /
`ascii_width` / `cjk_width` / `emoji_width` / `mixed_width` / `sync_output` /
`wrap_at_eol`（WRAP_OFF 段另行校准 ConPTY 折行）/ `long_line_enter`（联动
SyncCursor 行数，见 Phase 备注）。

---

## 7. Phase 21：补发（resync）时序一致性（2026-08-05）

### 7.1 问题背景

输入/输出补发（把当前光标位置作为 VT 序列重发到 WT，使 WT 视觉光标与
DLL 内部状态对齐）有三条路径：

- 输出侧 `OutputHooks.cpp`：`SyncChildVtCursorBeforeWrite`（写前置补发）
- 输入侧 `InputHooks.cpp`：两处回显补发（echo resync）

早期实现把补发字节**拼接进内容消息**（同一 VtOutput 消息，len = 补发 +
内容），问题：

1. e2e 精确字节断言被破坏（如 `ChildVtOutput: len=3 hex[3]=61 0A 62`
   变成 len=10）；
2. BatchSender 会把相邻发送合并为一条 VtOutput，即使分两次发送也无法
   保证字节边界，补发与内容无法区分。

### 7.2 修复：独立 `CursorSync` 消息类型

- `Message.h` 新增协议类型 `CursorSync = 0x0090`（DLL → mediator）；
- 补发序列改为 `SendToMediator(CursorSync, ...)`——**不经 BatchSender**，
  单消息即时发送，先于后续内容消息到达；
- `ChildSession.cpp` 新增 CursorSync case：直接把序列写入子进程 stdout
  （与 VtOutput 内容字节路径一致，不合并）；
- 内容消息字节保持原样，e2e 精确断言恢复。

### 7.3 验证

`processed_output` / `vt_output_mode` 由 FAIL 转 PASS；全量 4 批回归中
仅剩输入事件类（IME 问题，见 PHASES.md Phase 4 备注）与两个已知限制。