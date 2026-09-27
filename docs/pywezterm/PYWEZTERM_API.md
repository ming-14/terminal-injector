# pywezterm API

wezterm 终端引擎的 Python 绑定。五个类：`Terminal`（终端模型）、`Pty`（伪终端）、
`Surface`（增量渲染表面）、`Mux`（多窗格复用）、`ConsoleInput`（Windows 控制台输入）。

```python
import pywezterm
pywezterm.version()          # '0.1.0'
```

| 类 / 函数 | 职责 |
|---|---|
| `Terminal` | 纯软件终端：喂字节 → 解析 VT → 查询状态 / 快照 / 编码输入 |
| `Pty`      | 真实子进程 + 伪终端（ConPTY / openpty） |
| `Surface`  | 网格 → 增量 ANSI 字节流 |
| `Mux`      | 多个 pane（各含 Pty + Terminal）→ 合成一帧增量输出 |
| `ConsoleInput` | 采集宿主控制台的键鼠/resize 事件（仅 Windows） |

---

## 1. 通用约定

**坐标**：一律 0-based，`(x=列, y=行)`。

**Cell 元组**（`snapshot` / `scrollback` / `logical_lines` 返回的单元格）：

```python
(col, ch, fg, bg, bold, italic, underline, reverse, strike, width)
# 例: (2, 'c', 'p1', 'default', False, False, False, False, False, 1)
```
- `ch == ""` 表示宽字符的续格（宽字符占 2 格，只有首格带字符）
- `width` 为显示宽度：CJK/emoji = 2，其余 = 1

**颜色字符串**：`"default"` | `"p0"`…`"p15"`（ANSI 调色板） | `"#rrggbb"`

**修饰键**（`mods` 参数，按位或）：

```python
SHIFT, ALT, CTRL = 2, 4, 8
t.key_down("c", CTRL)              # -> b'\x03'
t.key_down("a", SHIFT | CTRL)      # -> b'\x01'
```

**键名**：
`Up Down Left Right Home End Insert Delete PageUp PageDown Backspace Tab Enter Esc Space`、
`F1`…`F24`、或任意单个字符（如 `"a"`、`"Z"`）。

**鼠标**：`kind ∈ {"press","release","move"}`，
`button ∈ {"left","middle","right","wheel_up","wheel_down","none"}`。

**字节去向**：`key_down` / `key_up` / `mouse` 直接返回本次编码字节；
`send_paste` 与终端**自发**产生的应答（DSR、DECACK 等）留在内部缓冲，
用 `drain_written()` 统一取出。

---

## 2. Terminal

### 2.1 离线解析：喂入 → 读取

```python
t = pywezterm.Terminal(cols=80, rows=24, scrollback=10000)

t.feed(b"hello \x1b[31mred\x1b[0m\r\n")   # 喂入原始 VT 字节

t.text()          # 'hello red'          可见区纯文本，行以 \n 连接，去行尾空白
t.cursor()        # (row, col, visible)  0-based
t.snapshot()      # [[cell, ...], ...]   每行一个 cell 列表（含样式）
t.snapshot_lines()# [(wrapped, cells)]   wrapped=True 表示该行被下一行续写
t.scrollback()    # 历史区 cell 网格（不含可见区）
t.scrollback_count()

t.resize(120, 30) # 改行列
t.reset()         # RIS：清屏 + 清 scrollback + 复位
t.clear_scrollback()
```

### 2.2 增量读取（渲染/同步用）

```python
base = t.current_seqno()
t.feed(b"more output\r\n")
dirty = t.changed_stable_rows(base)   # 只变了这些稳定行
# 只重画 dirty 行即可

t.logical_lines()
# [(first_stable, last_stable, cells), ...]  已把跨物理行的 wrap 重组成逻辑行
```

### 2.3 输入编码（不写 pty，返回字节）

```python
t.feed(b"\x1b[?1h")                    # 应用光标键模式
t.key_down("Up", 0)                    # -> b'\x1bOA'（普通模式则为 b'\x1b[A'）
t.key_up("Up", 0)                      # -> b''（xterm 模式无抬键序列，正常）
t.key_down("c", CTRL)                  # -> b'\x03'

t.feed(b"\x1b[?1000h\x1b[?1006h")      # 开鼠标上报 + SGR
t.mouse(5, 3)                          # -> b'\x1b[<0;6;4M'  (x,y 0-based → 序列 1-based)
t.mouse(5, 3, kind="release")          # 尾部 'm'
t.mouse(5, 3, button="wheel_up")

t.feed(b"\x1b[?2004h")                  # 开 bracketed paste
t.send_paste("hi")                     # 自动包 200~/201~
t.drain_written()                      # -> b'\x1b[200~hi\x1b[201~'，写回 pty 即可
```

### 2.4 选区（坐标 = stable 行 + 列，跨 scrollback、不随视图滚动变化）

```python
t.selection_set(anchor_row, anchor_col, end_row, end_col)   # 区域（顺序可反）
t.selection_select_word(row, col)      # 双击选词；空白处则无选区
t.selection_select_line(row, col)      # 三击选行（含结尾 \n）
t.selection_text()                     # 'abc\ndef'
t.selection_active()                   # bool
t.selection_clear()
```

### 2.5 模式 / 元数据查询

```python
t.get_keyboard_encoding()   # 'xterm' | 'csi-u' | 'win32' | 'kitty'
t.is_alt_screen_active()    # DECSET 1049
t.is_mouse_grabbed()        # DECSET 1000/1002/1003
t.get_mouse_encoding()      # (mode, sgr)  mode ∈ {0,1000,1002,1003}
t.bracketed_paste_enabled() # DECSET 2004
t.get_title()               # str（OSC 0/2）
t.get_current_dir()         # str | None（OSC 7）
t.get_progress()            # ('none'|'percentage'|'error'|'indeterminate', int|None)
t.get_semantic_zones()      # [(y0, x0, y1, x1, 'prompt'|'input'|'output'), ...]  OSC 133
t.mode_restore_seq()        # str：一段可直接喂入、还原当前模式的 DECSET 序列
t.focus_changed(True)       # 上报焦点（DECSET 1004）
```

### 2.6 整屏输出

```python
t.render_ansi(include_cursor=True)   # str：全屏 ANSI（CUP + SGR + \x1b[K）
t.render_scrollback(keep_ansi=False)  # str：历史区文本 / 带 SGR 文本
t.render_svg(compression_level=0)     # str：0=原样，>=1 压缩
t.render_image(scale=1.0, fmt="png")  # bytes：png | jpg | jpeg | bmp（8x17 像素/格 × scale）
```

### 2.7 回调

```python
t.set_clipboard_callback(lambda sel, data: ...)   # OSC 52，sel ∈ {'clipboard','primary'}, data 可为 None
t.set_download_callback(lambda name, data: ...)   # OSC 8 / 超链接下载，data: bytes
t.set_device_control_callback(lambda info: ...)   # DCS，info 为 str
t.set_notification_callback(lambda info: ...)     # 响铃/报警等，info 为 str
t.make_all_lines_dirty()                          # 标记全部行变更（选区高亮失效时全量重绘）
```
回调异常被捕获并打印，不会中断终端。

### 2.8 速查

```
Terminal(cols=80, rows=24, scrollback=10000)
feed(data) · resize(cols, rows) · reset() · clear_scrollback() · focus_changed(b)
text() · cursor() · snapshot() · snapshot_lines() · scrollback() · scrollback_count()
logical_lines() · current_seqno() · changed_stable_rows(since) · make_all_lines_dirty()
scroll(delta) · scroll_to_bottom()
key_down(key, mods)->bytes · key_up(key, mods)->bytes
mouse(x, y, kind='press', button='left', mods=0)->bytes · send_paste(text) · drain_written()->bytes
selection_set(r0,c0,r1,c1) · selection_select_word(r,c) · selection_select_line(r,c)
selection_text() · selection_active() · selection_clear()
get_keyboard_encoding() · is_alt_screen_active() · is_mouse_grabbed() · get_mouse_encoding()
bracketed_paste_enabled() · get_title() · get_current_dir() · get_progress()
get_semantic_zones() · mode_restore_seq()
render_ansi(include_cursor) · render_scrollback(keep_ansi) · render_svg(level) · render_image(scale, fmt)
set_clipboard_callback(cb) · set_download_callback(cb) · set_device_control_callback(cb) · set_notification_callback(cb)
```

---

## 3. Pty

### 3.1 手动闭环（读 → 喂终端 → 应答回写）

```python
p = pywezterm.Pty(cols=80, rows=24)
t = pywezterm.Terminal(cols=80, rows=24)
p.spawn([r"C:\Windows\System32\cmd.exe", "/c", "echo hi"])

while True:
    chunk = p.read(4096, timeout=0.2)   # bytes；timeout 到期/EOF -> b''
    if chunk:
        t.feed(chunk)
        resp = t.drain_written()        # 子进程的 DSR 等查询必须回写，否则它会卡住
        if resp:
            p.write(resp)
    elif p.try_wait() is not None:      # 已退出，继续排空到 EOF
        ...
```

### 3.2 常用操作

```python
pid, handle = p.spawn(["/bin/sh", "-c", "sleep 10"],
                      cwd="/tmp",
                      env={"PATH": "/usr/bin"})    # env 为覆盖，其余继承当前进程
# Windows 且子程序自行解析命令行（如 cmd.exe /c）时保留原始引号语义：
p.spawn([r"C:\Windows\System32\cmd.exe", "/c", "echo \"a b\""],
        raw_cmdline=r'cmd.exe /c echo "a b"')

p.resize(100, 30);  p.get_size()      # (cols, rows)
p.write(b"dir\r\n")
p.child_pid();     p.child_handle()   # Windows 进程句柄
p.hpcon()                             # Windows ConPTY 句柄
p.try_wait()                          # 退出码 | None(运行中)
p.kill()
p.buffered_bytes()                    # 读缓冲中待取字节数
p.close()                             # 幂等；关闭后 read() 恒为 b''，get_size()==(0,0)
```

### 3.3 速查

```
Pty(cols=80, rows=24)
spawn(argv, cwd=None, env=None, raw_cmdline=None) -> (pid, handle)
read(n=65536, timeout=None) -> bytes · write(data) · resize(cols, rows) · get_size() -> (cols, rows)
try_wait() -> int|None · kill() · close() · buffered_bytes() -> int
child_pid() -> int|None · child_handle() -> int|None · hpcon() -> int|None
```

---

## 4. Surface（网格 → 增量 ANSI 字节）

```python
s = pywezterm.Surface(cols=80, rows=24)

s.set_cell(0, 0, "Hi", fg="p1", bg="#000000", bold=True)   # 可选样式全默认
seq, frame = s.get_changes_bytes(0)      # 首帧：全量
# ... 画完整屏 ...
seq, frame = s.get_changes_bytes(seq)    # 之后只含变化；无变化则 frame == b''

s.repaint_bytes()                        # 强制全量 = get_changes_bytes(0)
s.resize(100, 30)                        # 尺寸变化 → 下帧全量
s.clear()
s.dimensions()                           # (cols, rows)
s.current_seqno()
```

输出为 TrueColor ANSI；直接写到真实终端即可。`set_cell` 的 `text` 可为多字符（如 `"Hello"`）。

```
Surface(cols=80, rows=24)
set_cell(x, y, text, fg='default', bg='default', bold=False, italic=False,
         underline=False, reverse=False, strike=False)
get_changes_bytes(since_seqno) -> (seq, bytes) · repaint_bytes() -> (seq, bytes)
resize(cols, rows) · clear() · dimensions() · current_seqno()
```

---

## 5. Mux（多 pane 宿主主循环）

**约定**：布局仅支持 2 个 pane、左右二分；`set_output_callback` 须在 `add_pane`
**之前**设置（已建好的 pane 不会更换回调）；`render()` 至少要有一个 pane。

```python
m = pywezterm.Mux(cols=80, rows=24)

# ---- 主循环：先设回调，再建 pane ----
def on_output():                            # 任一 pane 有新输出时被调用（无参）
    frame, row, col, visible = m.render()   # frame: 增量 ANSI 字节
    sys.stdout.buffer.write(frame); sys.stdout.buffer.flush()
    # row/col 为焦点光标 0-based 整屏坐标；frame 内 CUP 为 1-based

m.set_output_callback(on_output)            # 或 None 清除

a = m.add_pane(["/bin/sh"])                 # pane_id（0 起）；第二个起左右各半
b = m.add_pane([r"C:\Windows\System32\cmd.exe"])
m.pane_rects()                              # [(x,y,w,h), ...]

# ---- 输入路由：焦点版 + 显式 pane 版 ----
m.set_focus(b)
m.key_down("c", CTRL)                    # 发给焦点 pane，返回编码字节（已下发 pty）
m.pane_key_down(a, "Enter", 0)
m.pane_write(a, b"ls\r\n")               # 原始字节
m.pane_send_paste(a, "text")
m.mouse(x, y)                            # 整屏坐标 → 命中 pane → 换算 pane 内坐标
m.pane_at(x, y)                          # pane_id | None（分隔线/状态栏 → None）
m.scroll(10); m.pane_scroll(a, 10); m.pane_scroll_to_bottom(a)

# ---- 布局 ----
m.set_sep(True)                          # 两 pane 间画分隔线
m.set_split_col(50)                      # 指定分割列；None = 中点
m.set_status_rows(1)                     # 底部预留状态栏行数
m.set_status("STATUS_BAR_X")             # 状态栏文本
m.resize(120, 40); m.force_repaint()     # 强制下帧全量
m.pane_resize(a, 60, 40)                 # 单 pane 尺寸

# ---- 查询 ----
m.pane_text(a)                           # 可见区纯文本
m.pane_cursor(a)                         # (row, col, visible)，pane 内 0-based
m.pane_try_wait(a)                       # 退出码 | None
m.pane_is_mouse_grabbed(a)
m.pane_take_output(a)                    # 取走并清空子进程原始输出（录制用）
m.pane_output_len(a)

# ---- 选区（整屏坐标） ----
m.pane_selection_set(a, x0, y0, x1, y1)
m.pane_selection_select_word(a, x, y)
m.pane_selection_select_line(a, x, y)
m.pane_selection_text(a); m.pane_selection_active(a); m.pane_selection_clear(a)
m.set_focus_selection_callback(lambda sel, data: ...)   # OSC 52（作用于当前已存在的 pane）

m.close_pane(a)                          # 关闭单个（幂等）
m.close()                                # 关闭全部子进程
```

```
Mux(cols=80, rows=24)
add_pane(argv, cwd=None, env=None) -> pane_id · close_pane(id) · close()
pane_rects() · pane_count() · dimensions() · focused() · set_focus(id) · pane_at(x, y)
render() -> (bytes, row, col, visible) · resize(cols, rows) · force_repaint()
set_sep(sep=True) · set_split_col(col|None) · set_status_rows(n) · set_status(text)
key_down(key, mods) · key_up(key, mods) · mouse(x, y, kind='press', button='left', mods=0)
scroll(delta) · scroll_to_bottom() · send_paste(text) · set_output_callback(cb|None)
pane_write(id, data) · pane_key_down(id, key, mods) · pane_key_up(id, key, mods)
pane_mouse(id, x, y, ...) · pane_send_paste(id, text)
pane_text(id) · pane_cursor(id) · pane_is_mouse_grabbed(id) · pane_try_wait(id)
pane_resize(id, cols, rows) · pane_scroll(id, delta) · pane_scroll_to_bottom(id)
pane_take_output(id) · pane_output_len(id)
pane_selection_set(id, x0, y0, x1, y1) · pane_selection_select_word(id, x, y)
pane_selection_select_line(id, x, y) · pane_selection_text(id)
pane_selection_active(id) · pane_selection_clear(id) · set_focus_selection_callback(cb)
```

---

## 6. ConsoleInput（Windows）

构造即接管控制台输入/输出模式与代码页，`restore()`（或对象销毁）时还原。
事件读取非阻塞：先 `wait_input(ms)` 等待，再 `read_inputs()` 取全部。

```python
ci = pywezterm.ConsoleInput()      # mux = pywezterm.Mux(...) 等宿主对象
try:
    while True:
        if not ci.wait_input(100):
            continue
        for ev in ci.read_inputs():
            if ev[0] == "key":
                _, key, mods, down = ev           # ('key', 'Up', 0, True)
                if down:
                    mux.key_down(key, mods)
            elif ev[0] == "mouse":
                _, x, y, kind, button, mods = ev  # ('mouse', 12, 4, 'press', 'left', 0)
                mux.mouse(x, y, kind, button, mods)
            elif ev[0] == "resize":
                cols, rows = ci.size()            # ('resize',) 后立即取尺寸
                mux.resize(cols, rows)
finally:
    ci.restore()
```

```
ConsoleInput()
wait_input(ms) -> bool · read_inputs() -> list[tuple] · size() -> (cols, rows) · restore()
```

---

## 7. 模块函数

```python
pywezterm.version()                       # '0.1.0'
pywezterm.cursor_seq(row, col, visible)   # '\x1b[r+1;c+1H' + '\x1b[?25h' / '\x1b[?25l'
pywezterm.clipboard_read()  -> str        # Windows；无内容返回 ''
pywezterm.clipboard_write(text)           # Windows；空串为 no-op
```

---

## 8. 常用配方

**无子进程的终端仿真**（解析日志/测试转义序列）
```python
t = pywezterm.Terminal(120, 40)
t.feed(data)
text, snap, zones = t.text(), t.snapshot(), t.get_semantic_zones()
```

**子进程交互驱动**
```python
p, t = pywezterm.Pty(80, 24), pywezterm.Terminal(80, 24)
p.spawn(argv)
def pump():
    b = p.read(65536, timeout=0.1)
    if b:
        t.feed(b); p.write(t.drain_written())
def send(keys):                 # 键入
    for k, mods in keys: p.write(t.key_down(k, mods)); p.write(t.key_up(k, mods))
```

**截屏 / 导出**
```python
open("shot.png", "wb").write(t.render_image(scale=2, fmt="png"))
open("shot.svg", "w", encoding="utf-8").write(t.render_svg(1))
ansi = t.render_ansi(include_cursor=True)   # 可直接写到真实终端
```

**整屏差分渲染**
```python
base = t.current_seqno()
...
for row in t.changed_stable_rows(base):
    ...   # 只重绘这些行
```
