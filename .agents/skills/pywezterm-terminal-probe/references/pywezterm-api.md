# pywezterm 原生 API 速查

`pywezterm` 把 wezterm 的 `portable-pty` 与 `wezterm-term` 做成 pyo3 绑定。
一个 `.pyd` 提供四类能力：**伪终端（Pty）**、**终端模型（Terminal）**、
**增量渲染面（Surface）**、**多 pane 复用器（Mux）**，外加 Windows 宿主控制台输入采集
（ConsoleInput）。

- 源码：`reference/pywezterm-main`（`wezterm/pywezterm/src/*.rs` 是绑定层，
  `wezterm/` 下是 vendored 的 wezterm crates）
- 已编译包：`reference/pywezterm/`（`pywezterm.pyd` + `__init__.py` + `conpty/` 侧载）
- 用法：把**含 `pywezterm/` 子包的那个目录**加进 `sys.path`
  （本机即 `C:\Users\rikka\Desktop\terminal-injector\reference`），或设 `PWTERM_DIR`

```python
import sys; sys.path.insert(0, r"<含 pywezterm/ 的目录>")
import pywezterm
pywezterm.version()          # '0.1.0'
```

顶层符号：`version` `cursor_seq` `clipboard_read` `clipboard_write`
`Pty` `Terminal` `Surface` `Mux` `ConsoleInput`

---

## Pty —— 伪终端（真 ConPTY）

```python
Pty(cols=80, rows=24)
```

Windows 上侧载 `conpty/OpenConsole.exe` + `conpty.dll`（真 ConPTY），
其余平台走系统 ConPTY/PTY。

| 方法 | 签名 | 说明 |
|---|---|---|
| `spawn` | `(argv, cwd=None, env=None, raw_cmdline=None)` | 返回 `(pid, 进程句柄)`；`raw_cmdline` 仅 Windows：整条命令行原样传，绕过 argv 引号序列化（给 `cmd.exe /c` 这类自解析命令行的程序用） |
| `read` | `(n=65536, timeout=None)` | 最多读 n 字节；EOF 返回 `b""`；`timeout=None` 阻塞到有数据或 EOF；**返回 `bytes`** |
| `write` | `(data)` | 写输入字节；**`bytes` 与 `list[int]` 都接受**（实测） |
| `resize` | `(cols, rows)` | 调 pty 尺寸 |
| `get_size` | `()` | `(cols, rows)`；close 后为 `(0, 0)` |
| `try_wait` | `()` | 非阻塞退出码；`None` = 仍在跑 |
| `kill` / `close` | `()` | 终止子进程 / 关闭 pty（释放 HPCON 解除 reader 阻塞，**幂等**，close 后 `read` 返回空） |
| `child_pid` / `child_handle` / `hpcon` | 属性 | pid / 进程句柄（注册 Job 用）/ 底层 HPCON（沙箱场景外部 spawn 用） |
| `buffered_bytes` | 属性 | 读缓冲待取字节数（观测"读缓冲有上限"这条不变量） |

**注意**：`write` 在 PTY 写满时会阻塞等待，且 `#[pymethods]` 默认全程持有 GIL
——实测该实现已在放掉 GIL 之后再阻塞写，但写大量数据时仍不要与"等读"串成死循环。

## Terminal —— 终端模型（wezterm-term 本体）

```python
Terminal(cols=80, rows=24, scrollback=10000)
```

**这就是 WT 用的同一份终端模型**，语义比第三方仿真器（pyte 等）更贴近 WT。
函数式用法：`feed(bytes)` 喂程序输出，然后读状态。

### 屏幕读取（计入视图滚动偏移）

| 方法 | 返回 |
|---|---|
| `text()` | 可见区纯文本（每行去尾空白、去掉末尾空行、行间 `\n`） |
| `snapshot()` | 每行 `[(col, ch, fg, bg, bold, italic, underline, reverse, ?, width), ...]` |
| `snapshot_lines()` | 每行 `(wrapped, cells)`，`wrapped=True` 表示该物理行以折行结尾（拼逻辑行用） |
| `logical_lines()` | 可见区逻辑行：每项 `(first_stable, last_stable, cells)`，跨 wrap 已重组 |
| `cursor()` | `(row, col, visible)` 0-based；滚出可见区则 `visible=False` |
| `scrollback()` / `scrollback_count()` / `clear_scrollback()` | 历史区字符网格 / 行数 / 清空 |

### 状态查询（应用声明的模式，feed 时同步跟踪）

`is_alt_screen_active()` · `is_mouse_grabbed()` · `get_mouse_encoding()` → `(mode, sgr)`，
mode ∈ {0,1000,1002,1003} · `get_keyboard_encoding()` → `xterm|csi-u|win32|kitty` ·
`bracketed_paste_enabled()` · `get_title()`（OSC 0/2，图标优先） ·
`get_current_dir()`（OSC 7，未设返回 None） · `get_progress()`（OSC 9） ·
`get_semantic_zones()` → `(start_y, start_x, end_y, end_x, type)`（OSC 133） ·
`mode_restore_seq()`（新订阅者重建 xterm 状态，只发应用真设置过的那些）

### 输入编码（模式感知，**只返回字节，不写 pty**）

| 方法 | 签名 | 说明 |
|---|---|---|
| `key_down` / `key_up` | `(key, mods)` | `key`：`Up/Down/Left/Right/Home/End/Insert/Delete/PageUp/PageDown/Backspace/Tab/Enter/Esc/Space/F1-F24/单字符` |
| `mouse` | `(x, y, kind="press", button="left", mods=0)` | `kind`：`press/release/move`；`button`：`left/middle/right/wheel_up/wheel_down/none` |
| `send_paste` | `(text)` | bracketed paste 开启时自动包裹 |

**mods 位定义：`SHIFT=2`、`ALT=4`、`CTRL=8`**（不是常见的 1/2/4）。

编码结果是"下发到 pty 的字节"，配合 `Pty.write()` 即闭环：
```python
t.feed(b"\x1b[?1h")            # 应用光标模式
t.key_down("Up", 0)            # → b"\x1bOA"（普通模式是 b"\x1b[A"）
```

### 视图滚动 / 选区 / 回调 / 渲染

- 滚动：`scroll(delta)`（>0 上滚看历史，<0 回落）· `scroll_to_bottom()`
- 选区（stable 行坐标，跨 scrollback）：`selection_set(r0,c0,r1,c1)` ·
  `selection_select_word(r,c)`（双击选词）· `selection_select_line(r,c)`（三击选行）·
  `selection_text()` · `selection_active()` · `selection_clear()`
- 脏行差分：`current_seqno()` · `changed_stable_rows(since_seqno)` · `make_all_lines_dirty()`
- 回调：`set_clipboard_callback(cb)`（OSC 52，`cb(selection_name, text)`，抛异常不崩终端）·
  `set_device_control_callback` · `set_download_callback` · `set_notification_callback` ·
  `focus_changed(bool)`
- 渲染：`render_ansi(include_cursor=?)` · `render_scrollback(keep_ansi=?)` ·
  `render_svg(compression_level=?)` · `render_image(scale, fmt)`
- `reset()`（喂 RIS 清屏清 scrollback）· `resize(cols, rows)`

### 应答闭环（**关键**）

应用会向终端发查询（如 `\x1b[6n` DSR），应答要由宿主回写 pty，否则应用等应答卡住：

```python
chunk = p.read(4096, timeout=0.2)
if chunk:
    t.feed(chunk)
    resp = t.drain_written()     # 取走模型产生的应答字节
    if resp:
        p.write(resp)
```
`drain_written()` 是这条链的出口，**不要忘**。

## Surface —— 增量渲染面

```python
Surface(cols, rows)
set_cell(x, y, text, style=None)      # x/y 与 row/col 均 0-based
current_seqno() -> int
get_changes_bytes(since_seqno) -> (新 seqno, bytes)   # bytes 为空 = 无变化
repaint_bytes() -> bytes              # 全量重绘
resize(cols, rows)                    # 尺寸变化会丢 change 流，下次全量
clear()
```
`get_changes_bytes` 内部按 seqno 做差量；`since_seqno` 过旧会自动退化为全量重绘。
首次（seqno=0）恒为全量。

## Mux —— 多 pane 复用器（真实 Pty + Terminal）

```python
Mux(cols, rows)
add_pane(argv) -> pane_id       # 每 pane 一个 reader 线程自动喂终端模型
```
布局：第 1 个 pane 填满，第 2 个起左右二分；**最多 2 个 pane**，第 3 个显式报错。

- pane 级：`pane_text/ pane_cursor / pane_rects / pane_at(x,y) / pane_try_wait /
  pane_write / pane_output_len / pane_take_output / pane_resize / close_pane`
- pane 级输入：`pane_key_down / pane_key_up / pane_mouse / pane_send_paste`
- 焦点：`set_focus(id)` → `focused()`；随后 `key_down/key_up/mouse/scroll` 路由到焦点 pane
- 滚动 / 选区：`pane_scroll / pane_scroll_to_bottom / pane_selection_*`
- 合成：`render()` → `(bytes, cursor_row, cursor_col, cursor_visible)`，
  增量 ANSI（含 CUP）；无变化帧返回 `b""`；`force_repaint()` 强制下帧全量
- 布局控制：`set_sep(bool)` · `set_split_col(col)` · `set_status_rows(n)` · `set_status(text)` ·
  `resize(cols, rows)`
- 回调：`set_output_callback(cb)`（任一 pane 有新输出后调用，事件驱动渲染，
  替代定时轮询）· `set_focus_selection_callback(cb)`
- `close()` 关全部；`close_pane(id)` 幂等

## ConsoleInput —— Windows 宿主控制台输入采集

**仅 Windows**。构造即保存并改写控制台模式、输出代码页切 UTF-8；`restore()`（幂等）恢复。

| 方法 | 说明 |
|---|---|
| `wait_input(timeout_ms)` | 等待事件可用；`False` = 超时（0 = 立即返回） |
| `read_inputs()` | 返回归一化事件列表：`("key", key, mods, down)` / `("mouse", x, y, kind, button, mods)` / `("resize",)` |
| `size()` | 窗口逻辑尺寸 `(cols, rows)` |
| `restore()` | 恢复原模式与代码页 |

在非交互/无宿主控制台的进程里构造会抛异常（自测里按"跳过"处理）。

## 模块级函数

| 函数 | 说明 |
|---|---|
| `version()` | 绑定库版本，实测 `'0.1.0'` |
| `cursor_seq(row, col, visible)` | 0-based → 1-based CUP + 光标显隐。实测 `(5,3,True)` → `'\x1b[6;4H\x1b[?25h'`，`visible=False` → 末尾 `\x1b[?25l` |
| `clipboard_read()` | 剪贴板文本（UTF-16LE）；无文本返回空串 |
| `clipboard_write(text)` | 写剪贴板（UTF-16LE + NUL） |

---

## 实测结论（2026-09-24，本机）

```bash
cd <pywezterm-main>/tests
PYTHONPATH="C:/Users/rikka/Desktop/terminal-injector/reference" python -m pytest -q
# 64 passed, 4 skipped in 7.14s
```
跳过的 4 项是 `test_console_input.py`（当前进程无宿主交互控制台）。
各文件也可独立跑（多数带 `__main__` 自跑块），依赖只有 pytest + 本机 python。

> **`PYTHONPATH` 必须指向含 `pywezterm/` 子包的目录**（本机 `reference`，其中有
> `pywezterm/__init__.py` + `pywezterm.pyd` + `conpty/`）。指成 `reference/pywezterm`
> 或 `reference/pywezterm-main` 都会 `ModuleNotFoundError: No module named
> 'pywezterm.pywezterm'` —— 后者里的 `pywezterm/` 只有 Rust 源码，没有已编译的 `.pyd`。

### 附加实测（2026-09-25）：Pty 起进程 + Terminal 还原屏幕

```bash
python <skill>/scripts/smoke.py
# vywezterm version = 0.1.0
# [spawn] pid=4204 handle=480 hpcon=True size=(100, 30)
# [bytes] 75
# [screen] 'PYWEZ_SMOKE_OK'
# [cursor] (1, 0, True)
# [RESULT] PASS 屏幕还原含标记
```

## 重建（改 Rust 侧绑定时）

`pywezterm-main/BUILD.py`：`maturin build --release` + Windows 侧自动探测
`vcvars64.bat`（或 `--vcvars` 指定）注入 MSVC 环境，cargo 从 `~/.cargo/bin` 或 PATH 找。
需要 Rust 工具链 + maturin（缺失会自动 pip 装）。
```bash
python BUILD.py --rebuild                     # 全量
python BUILD.py --wheel-dir dist              # 指定 wheel 输出
```

已知对原版 wezterm 的改动（见 `pywezterm-main/AGENTS.md` 变更记录）：
1. `wezterm/pty` 新增 Windows 专属 `raw_cmdline`（`CommandBuilder::set_raw_cmdline`）；
2. `wezterm/pty` Windows x86 栈破坏修复：`shared_library!` 生成的 `extern "Rust"`
   函数指针与 Win32 的 stdcall 不匹配，改为手工 `LoadLibraryW`/`GetProcAddress` +
   `extern "system"`。

> 该仓库的 `AGENTS.md` 明确：不要修改 vendored 的 wezterm 原文件；确实必须改时，
> 把变更写进 AGENTS.md 的变更记录。

---

## 与本项目（terminal-injector）的关系

1. **扮演 WT**：`Pty.spawn([mediator, "--mediator", "--target-pid", N])` 即用真 ConPTY
   承载 mediator，`write`/`read` 直接收发字节 —— 无窗口焦点问题。这是技能的
   `PwSession` 用法。
2. **替代 pyte 做屏幕还原**：`Terminal` 是 wezterm-term 本体，与 WT 同源，
   能还原文本/光标/颜色属性/宽字符/scrollback/alt-screen，
   比第三方仿真器更接近"WT 上到底长什么样"。
3. **增量/多 pane**：`Surface` 与 `Mux` 目前未被本项目使用；写"多会话对照"或
   "逐帧增量比对"类探针时可用，不必自己造差分。
