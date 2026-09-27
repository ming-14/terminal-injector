# e2e 全面测试套件 — 分阶段实施计划

> 目标：覆盖劫持后终端（目标 cmd + injected.dll + mediator + WT）的**全部特性**，
> 每个特性一个测试文件，文件头写明预期，终端不支持的特性预期标 `UNSUPPORTED`。
>
> 依赖：terminal-injector 的 `build/bin/Release`（terminal_injector.exe + injected.dll）、
> 测试基建 helpers（injector.py / input_sim.py / vt_capture.py）、Python 3.8+ / pywin32 / psutil。

---

## 阶段总览

| Phase | 名称 | 交付物 | 预计文件数 |
|-------|------|--------|-----------|
| 0 | 工程基建与冒烟 | 目录骨架、helpers 移植、common 库、run_all.py、冒烟测试 | 基建 6 + 冒烟 1 |
| 1 | VT 输出序列 | 颜色/光标/清屏/编辑/滚动/屏幕模式 | 18 |
| 2 | Console API 输出 | WriteConsole 系列/矩阵/填充/属性/标题/滚动 | 12 |
| 3 | 光标与缓冲区信息 | ScreenBufferInfo/光标/双缓冲/查询类 API | 7 |
| 4 | 键盘输入 | 字符/功能键/修饰键/信号/事件字段/输入队列 | 11 |
| 5 | 行编辑模式 | 回显/历史/Tab 补全/ReadConsole | 6 |
| 6 | 模式状态机 | 全部 ConsoleMode 标志 + 模式一致性 | 10 |
| 7 | VT 直通模式 | 模式切换/原始字节/鼠标直通 | 3 |
| 8 | 鼠标 | 按键/双击/拖动/滚轮/坐标/状态重置 | 8 |
| 9 | 特殊序列 OSC/DCS/查询 | 标题/超链接/剪贴板/DSR/DA/焦点/粘贴/同步输出/图形 | 15 |
| 10 | 代码页 | ConsoleCP/OutputCP/UTF-8 | 3 |
| 11 | 字符宽度 | ASCII/CJK/Emoji/混合 | 4 |
| 12 | 滚动缓冲区 | 回滚行数/用户高度/模式切换重置 | 3 |
| 13 | 注入生命周期 | 握手/子进程注入/卸载/反复注入/自保护/管道安全/枚举/同控制台后代接管 | 15 |
| 14 | 性能与稳定性 | 满屏重绘/高频输出/鼠标延迟/Logger | 4 |
| 15 | 全量回归与收尾 | 全量跑通、README 完善、结果报告 | 汇总 |

共 **119 个特性测试文件**（Phase 1~14 合计 119；Phase 0 的基建 helpers 与冒烟脚本不单独计为特性文件）。

---

## 通用约定（所有 Phase 适用）

### 文件结构

每个测试文件 `e2e/<类别>/test_<特性>.py`，统一结构：

```python
"""特性: <名称>    类别: <类别>

链路: <数据流方向，如 WT → mediator → DLL → 目标程序 ReadConsoleInputW>

预期:
  - <断言 1>
  - <断言 2>
  - 不支持特性: UNSUPPORTED（探测终端能力后跳过，计入 UNSUPPORTED 报告）

验证方式: 目标程序自检结果文件 + mediator 日志 VtOutput 字节 + 虚拟状态查询
"""

def run() -> int:   # 返回失败数；0 = 全过
    ...

if __name__ == "__main__":
    sys.exit(run())
```

### 运行链路（复用项目模式）

```
1. injector.start_target_cmd()           # 启动注入目标 cmd（独立控制台）
2. injector.start_wt_mediator(pid)       # 启动 WT，其中运行 mediator
3. injector.wait_for_handshake()         # 等握手成功（DLL 注入完成）
4. injector.focus_wt()                   # WT 置前台
5. 在注入 cmd 中运行内嵌目标脚本：
   python <e2e>/_targets/<name>.py
6. target 用 Console API 自检 → 写 results/<name>.txt（KEY=VALUE 协议）
7. runner 用 input_sim.SendInput 驱动 + 轮询结果文件断言
8. vt_capture.py 解析 mediator 日志验证 VT 字节流（输出侧特性）
9. injector.cleanup()                    # 清理 cmd/mediator/WT
```

### 结果文件协议（common/result.py）

- 每行 `KEY=VALUE`，UTF-8
- 约定 KEY：`PASS`、`FAIL=<原因>`、`<特性KEY>=<值>`、`UNSUPPORTED=<原因>`、`QUIT`
- `wait_for_result(key, timeout)` 轮询读取
- 结果文件路径：`<e2e>/results/<test_name>.txt`（gitignore）

### 输出验证三层（解决"如何验证 WT 渲染"）

1. **目标程序自检**（主要）：目标脚本用 Console API 读写 + GetConsoleScreenBufferInfo 查询虚拟状态（Phase 14），写结果文件
2. **mediator 日志字节**（输出侧）：vt_capture.py 解析 `pipe→stdout: VtOutput` 的 hex，验证 VT 序列到达链路
3. **UNSUPPORTED 探测**：OSC 52/Sixel/Kitty 图形等，发送后无响应/日志无 VtOutput → 记为 UNSUPPORTED，不算 FAIL

### 路径与依赖

- `helpers/injector.py` 的 `PROJECT_ROOT`：优先读环境变量 `TI_PROJECT_ROOT`，默认按 e2e 目录相对解析（与 common/paths.py 一致，不硬编码机器路径）
- 构建产物：`<TI_PROJECT_ROOT>\build\bin\Release\`
- 目标脚本内嵌在测试文件里（字符串），运行时写入 `_targets/`；该目录的 `*.py` 快照入库便于审阅，测试运行时会按测试正文覆盖重写，避免 103 份 target 手工维护散落
- 测试期间禁止手动操作 WT 窗口（SendInput 干扰）

---

# Phase 0：工程基建与冒烟

## 目标
搭建 e2e 完整骨架并验证端到端链路可用（启动 cmd → 注入 → 运行目标脚本 → 结果文件断言 → 清理）。

## 交付物

| 文件 | 内容 |
|------|------|
| `README.md` | 运行方式、特性矩阵、结果解读 |
| `docs/PHASES.md` | 本文档 |
| `helpers/` | 从 terminal-injector/tests/helpers 复制 `injector.py`、`input_sim.py`、`vt_capture.py`，patch PROJECT_ROOT |
| `common/__init__.py` | 空 |
| `common/result.py` | 结果文件协议：`ResultFile` 类（写入/追加/等待 KEY）、`wait_for_result()` |
| `common/target.py` | 内嵌目标脚本管理：`write_target(name, code)` → `_targets/<name>.py`；`start_target_in_cmd(script, ...)` 在注入 cmd 中执行；`wait_target_ready(key)` |
| `common/reporter.py` | `Reporter` 类：PASS/FAIL/UNSUPPORTED/SKIP 计数、按类汇总、退出码 |
| `common/runner_base.py` | 测试文件基类：setup（启动 cmd+WT+握手）/teardown（清理）、统一打印格式 |
| `run_all.py` | 参数：`--list` / `--phase N` / `--cat <名>` / `<文件路径>` / 全量；扫描 `*/test_*.py`，逐文件独立进程运行，汇总报告（含 UNSUPPORTED） |
| `_targets/` | 目标脚本目录：`*.py` 为 `write_target()` 生成的快照（入库，勿手改，测试运行时重写）；`tabcomplete_marker.txt` 手工维护 |
| `results/` | 运行时结果文件（.gitignore） |
| `.gitignore` | `results/`、`__pycache__/`、`*.pyc` |
| 冒烟测试 `vt_output/test_sgr_basic_colors.py` | 首个真实特性测试，验证整条链路 |

## 冒烟测试内容（提前做，验证基建）
`test_sgr_basic_colors.py`：
- 目标脚本：WriteConsoleW 输出 `\x1b[31mRED\x1b[0m` + `\x1b[41mBG\x1b[0m`，随后 GetConsoleScreenBufferInfo 读光标列，写 `POS=<x>` 与 `OK=1` 到结果文件
- runner：启动链路 → 运行目标脚本 → 断言 `OK=1`、`POS` 与预期列一致（RED=3 列）→ 清理
- 预期：目标程序收到 VT 输出（经 ConsoleToVt 或直通）后光标推进 3 列；结果文件 OK=1

## 验证标准
- [ ] `python run_all.py --list` 列出冒烟测试
- [ ] `python run_all.py vt_output/test_sgr_basic_colors.py` 通过，退出码 0
- [ ] 清理后无残留 cmd/terminal_injector.exe/WT 窗口（psutil 校验）

---

# Phase 1：VT 输出序列（18 文件，`vt_output/`）

目标：验证目标程序输出的 VT 序列经 DLL → mediator → WT 全链路正确，以**光标位置**（GetConsoleScreenBufferInfo 虚拟状态）与 **mediator 日志 VtOutput 字节**双重验证。

通用目标脚本模式：`WriteConsoleW("\x1b[<seq>")` → `GetConsoleScreenBufferInfo` 读 `dwCursorPosition` → 写 `POS=X,Y` 与 `OK=1`。

| # | 文件 | 特性 | 预期 |
|---|------|------|------|
| 1 | `test_sgr_basic_colors.py` | SGR 16 色前景 30-37 / 背景 40-47 / 默认 39,49 | 输出后光标推进与字符数一致；VT 序列 hex 出现在 mediator 日志 |
| 2 | `test_sgr_bright_colors.py` | 亮色 90-97 / 100-107 | 同上；颜色序列 `\x1b[9xm` 原样到达 |
| 3 | `test_sgr_256_colors.py` | 256 色 `38;5;n` / `48;5;n`（抽样 0/16/17/255 及边界） | 序列原样到达；`38;5;255` 等 hex 匹配 |
| 4 | `test_sgr_truecolor.py` | 真彩色 `38;2;r;g;b` / `48;2;r;g;b`（含 0,0,0 与 255,255,255 边界） | `\x1b[38;2;255;0;0m` 原样到达日志 |
| 5 | `test_sgr_styles.py` | 粗体 1 / 斜体 3 / 下划线 4 / 闪烁 5 / 反显 7 / 隐藏 8 / 删除线 9 / 重置 0 | 全部序列原样到达；重置后状态清空 |
| 6 | `test_cursor_move.py` | CUP `r;cH` / CUU A / CUD B / CUF C / CUB D / CHA G / VPA d / CNL E / CPL F | 每个序列后 `POS` 与预期坐标一致（虚拟状态推进正确） |
| 7 | `test_cursor_save_restore.py` | DECSC/DECRC（`ESC 7/8`）、CSI s/u | 保存点 (x1,y1)，移动后恢复，POS 回到 (x1,y1) |
| 8 | `test_cursor_visibility.py` | `?25l`/`?25h` 显隐、`?12h/l` 闪烁 | 序列到达日志；SetConsoleCursorInfo 查询可见性与设置一致（如有 hook） |
| 9 | `test_clear_screen.py` | ED 0/1/2/3 清除 光标下/上/全部/滚动缓冲区 | ED 2 后光标回 (0,0)；ED 3 后日志含 `\x1b[3J` |
| 10 | `test_clear_line.py` | EL 0/1/2 清除 光标右/左/整行 | 光标位置不变，日志序列匹配 |
| 11 | `test_insert_delete_chars.py` | ICH/DCH/ECH、IL/DL | 序列到达日志；虚拟状态行内容/光标位置正确 |
| 12 | `test_scroll_region.py` | DECSTBM `r;c r` + SU/SD、区域外光标不滚动 | SU 后首行内容上移（虚拟状态验证）；区域外光标行为正确 |
| 13 | `test_line_wrap.py` | `?7h/l` 自动换行、LNM `20h`（LF→CRLF）、CR 处理 | 关闭换行时写满行宽光标停在右边界；开启时自动折行 |
| 14 | `test_tabs.py` | HT、BS、TBC、HTS、制表位 8 列 | HT 后 POS.X 为 8 的倍数；日志含 `\x09` |
| 15 | `test_origin_mode.py` | `?6h/l` 原点模式（相对滚动区）+ 配合 DECSTBM | 开启后 CUP 坐标相对滚动区上边界 |
| 16 | `test_reverse_video.py` | `?5h/l` 屏幕反色 | 序列到达日志（渲染效果属 WT 内部，字节验证） |
| 17 | `test_clear_home.py` | ED 2 + CUP 0,0 组合（`\x1b[2J\x1b[H`） | 光标回 (0,0)；两序列原样到达日志 |
| 18 | `test_sgr_truecolor_colon.py` | 冒号式真彩色 `38:2::r:g:b`（空保留位）回归（2026-09-25 新增） | `38:2::255:0:0` 原样到达日志，**不得**被重写为带空分号参数的 `38;2;;255;0` |

执行：`python run_all.py --phase 1`（或 `--cat vt_output`）

## 验证标准
- [ ] 18 个文件全部 PASS
- [ ] 坐标类特性（6/7/9/13/14/15）POS 断言与真实 VT 语义一致
- [ ] 全部字节类断言在 mediator 日志命中

---

# Phase 2：Console API 输出（11 文件，`console_api/`）

目标：验证老式 Console API 调用经 DLL Hook → ConsoleToVt 翻译 → mediator → WT 的完整翻译链。

| # | 文件 | 特性 | 预期 |
|---|------|------|------|
| 17 | `test_write_console_ascii.py` | WriteConsoleW ASCII | 返回 TRUE、写入数正确；光标推进 n 列；日志 VtOutput 出现 |
| 18 | `test_write_console_unicode.py` | WriteConsoleW 中文/emoji | 中文 2 列/字、emoji 2 列（Phase 17）；写入数=字符数 |
| 19 | `test_write_console_ansi.py` | WriteConsoleA + ANSI 代码页（GBK） | 中文 GBK 字节正确转换；乱码检测：写回读内容一致 |
| 20 | `test_write_file_stdout.py` | WriteFile(stdout) 老式模式（无 VT 处理标志） | 直通 ConHost/原路径；内容到达，无翻译 |
| 21 | `test_write_file_vt.py` | WriteFile(stdout) VT 模式直通 | 字节原样进 mediator 日志（Phase 13 直通） |
| 22 | `test_write_console_output.py` | WriteConsoleOutput 字符+属性矩阵 | 部分区域更新正确；diff 算法（Phase 10）只发增量 |
| 23 | `test_fill_output.py` | FillConsoleOutputCharacter/Attribute | 填充后区域内容/属性正确（虚拟状态验证） |
| 24 | `test_write_console_output_attribute.py` | WriteConsoleOutputAttribute | 属性写入正确，字符不变 |
| 25 | `test_set_text_attribute.py` | SetConsoleTextAttribute（16 色属性→SGR） | 翻译成 `\x1b[3Xm`/`\x1b[4Xm`；随后输出字符带上该色 |
| 26 | `test_scroll_screen_buffer.py` | ScrollConsoleScreenBuffer | 矩形滚动后内容正确（虚拟状态）；日志有滚动序列 |
| 27 | `test_console_title.py` | SetConsoleTitle/GetConsoleTitle + OSC 0 | Set 后 Get 一致；日志含 `\x1b]0;...`（OSC 标题） |

## 验证标准
- [ ] 11 个文件全部 PASS
- [ ] 22/23 的 diff/增量逻辑无全量重绘（mediator 日志字节量断言）

---

# Phase 3：光标与缓冲区信息（7 文件，`cursor_buffer/`）

| # | 文件 | 特性 | 预期 |
|---|------|------|------|
| 28 | `test_get_screen_buffer_info.py` | GetConsoleScreenBufferInfo 窗口/缓冲区尺寸、光标、属性 | 与 WT 尺寸一致（mediator 同步）；光标位置与真实一致（Phase 14 虚拟状态） |
| 29 | `test_set_cursor_position.py` | SetConsoleCursorPosition | POS 更新为设置值；日志含 CUP 序列 |
| 30 | `test_set_cursor_info.py` | SetConsoleCursorInfo 显隐/大小 | 可见性查询一致；日志含 `?25h/l` |
| 31 | `test_screen_buffer_size.py` | SetConsoleScreenBufferSize + WINDOW_BUFFER_SIZE_EVENT | resize 后 Get 一致；目标收到 resize 事件 |
| 32 | `test_window_info.py` | SetConsoleWindowInfo（移动/裁剪） | 虚拟窗口坐标正确；事件/序列到达 |
| 33 | `test_dual_buffer.py` | CreateConsoleScreenBuffer + SetActiveConsoleScreenBuffer | 切换后输出进新缓冲；切换回恢复（含 GetConsoleScreenBufferInfo 区分） |
| 34 | `test_query_console.py` | GetConsoleWindow / GetLargestConsoleWindowSize / GetConsoleProcessList | 返回 0（Phase 9 设计：隔离 ConHost 原生窗口操作）/合理尺寸/进程数≥1（不崩溃、返回合理值） |

## 验证标准
- [x] 7 个文件全部 PASS
- [x] 31 resize 事件：目标程序收到 `WINDOW_BUFFER_SIZE_EVENT`（记录到结果文件）
  - 注：事件由 mediator 侧 WT resize 注入（DllRecvLoop），测试进程改 WT 窗口尺寸触发
  - 注：33 的 ?1049 字节断言已恢复（BUG-002 已修复，2026-08-02）

---

# Phase 4：键盘输入（11 文件，`keyboard/`）

目标：SendInput 到 WT → mediator → DLL → 目标程序 `ReadConsoleInputW`，验证字符/按键/信号全链路。

通用目标脚本：SetConsoleMode(INPUT_RECORD 模式) → 循环 ReadConsoleInputW → 记录 `KEY <vk> <scan> <ch> <repeat> <ctrl> <down>` → 结果文件。

| # | 文件 | 特性 | 预期 |
|---|------|------|------|
| 35 | `test_ascii_input.py` | ASCII 字符 a-z/0-9/符号 | 每字符 `uChar` 正确、VK 码正确、down+up 各一 |
| 36 | `test_unicode_input.py` | 中文输入 | `uChar` 为正确 Unicode；2 列宽输出正确 |
| 37 | `test_emoji_input.py` | Emoji 代理对 | 合成后正确码点（uChar 组合验证） |
| 38 | `test_navigation_keys.py` | 方向键/Home/End/PgUp/PgDn | VK 码正确；VT 序列到 DLL 翻译正确（或直通） |
| 39 | `test_edit_keys.py` | Insert/Delete/Backspace/Tab/Esc | VK 码正确；Esc 产生 `\x1b` |
| 40 | `test_function_keys.py` | F1-F12 | VK 码 VK_F1..F12 正确；F1 序列 `\x1bOP` 等（VT 模式） |
| 41 | `test_modifier_keys.py` | Ctrl/Alt/Shift 组合（Ctrl+C 除外） | `dwControlKeyState` 对应位正确；Alt+X 产生 `\x1bX` |
| 42 | `test_ctrl_c_signal.py` | Ctrl+C → SIGINT | 目标 python 死循环被中断（记录 SIGINT 处理）；Phase 7 信号传递 |
| 43 | `test_ctrl_z_eof.py` | Ctrl+Z → EOF（line input） | ReadFile/ReadConsole 读到 EOF（0） |
| 44 | `test_input_record_fields.py` | KEY_EVENT_RECORD 全字段 | repeatCount/scanCode/uChar/controlKeyState/down 逐字段验证 |
| 45 | `test_input_queue_ops.py` | Peek/Flush/GetNumberOfConsoleInputEvents/WaitForSingleObject | 计数正确、Peek 不消费、Flush 清空、Wait 事件被唤醒 |

## 验证标准
- [x] 11 个文件全部 PASS
- [x] 42 Ctrl+C：目标脚本记录 `SIGINT_RECEIVED`，python 被中断退出
- 备注（2026-08-05）：41 修饰键测试在中文输入法开启时 SendInput 组合键会被
  IME 截走组词（目标收不到事件）——TestSession setup 统一调用
  injector.ensure_english_layout() 轮询 Win+Space 切到英文布局
  （WT 是 XAML 窗口，ImmGetContext 返回 0，ImmSetOpenStatus 无法关闭其 IME）；
  并修复空事件列表时 ctrl 标志检查的假 PASS 分支

---

# Phase 5：行编辑模式（5 文件，`line_editor/`）

目标：cmd 等行编辑程序在劫持下的行为与原生 ConHost 一致（LineEditor，Phase 13）。

| # | 文件 | 特性 | 预期 |
|---|------|------|------|
| 46 | `test_echo_backspace.py` | 回显/退格/字符插入（在 cmd 中敲命令） | 输入字符回显；退格删除字符；命令执行结果正确（结果文件验证） |
| 47 | `test_history_nav.py` | 方向键命令历史 | 上/下键切换历史命令；Enter 执行正确项 |
| 48 | `test_tab_completion.py` | cmd Tab 补全 | 输入 `pi` + Tab → 补全为文件/命令（结果文件记录补全结果） |
| 49 | `test_read_console_modes.py` | ReadConsoleW line/raw、ReadConsole 各形态 | line 模式回车才返回；raw 模式按键即返回；返回内容正确 |
| 50 | `test_long_line_enter.py` | 长命令软换行后回车的光标定位（回归，2026-08-02 修复） | 回车后光标 Y = aligned.Y + 1 + 折行数（ChildExitSync 上报验证，基线取 DLL 日志 child cursor aligned 值） |
| 51 | `test_readfile_line_echo.py` | `ReadFile(stdin)` 行编辑回显（回归，2026-09-25 修复） | LINE+ECHO 下 `ab`+Enter → `os.read` 得 `61 62 0d 0a`；空行+Enter → `0d 0a`（空行也补换行，非 EOF） |

## 验证标准
- [x] 5 个文件全部 PASS（46-49、51 全过）
- [x] 46：`echo hello` 输出 `hello`，结果文件含输出内容
- [x] 50（回归）：修复 `LineEditor::SyncCursor` 未计软换行行数的 bug——长命令折行后回车，ConsoleState 光标少一行，cmd 新 prompt 的 CursorPosition 定位错行（覆盖命令续行）；修复后 `ChildExitSync sent cursor=(0,7)`（aligned (0,5) + 1 + wrap=1）；基线取 python DLL 日志 `child cursor aligned to WT` 值（ConsoleState 实际起点），不取目标自检 START（GetConsoleScreenBufferInfo 返回 VirtualConsoleState，与 ConsoleState 存在时序差，偶发读到滞后值）
- 备注：49 的 LINE 断言按真实 ReadConsoleW 语义为 `ok=1 n=4 ab`（返回行含尾部 `\r\n`，InputHooks.cpp:434）；RAW 断言需用新缓冲区（ReadConsoleW 不清残留）；50 的目标自检 `GetConsoleScreenBufferInfo` 在 python 进程走 ConHost pass-through（读不到 DLL 缓存），故用 mediator ChildExitSync 上报值断言

---

# Phase 6：模式状态机（10 文件，`modes/`）

目标：全部 ConsoleMode 标志的 Set/Get/行为，与原生语义一致（Phase 7/14）。

| # | 文件 | 特性 | 预期 |
|---|------|------|------|
| 50 | `test_echo_mode.py` | ENABLE_ECHO_INPUT 开关 | 开启回显、关闭不回显（读回无字符） |
| 51 | `test_line_input_mode.py` | ENABLE_LINE_INPUT 开关（raw） | 关闭后按键立即返回（raw mode） |
| 52 | `test_processed_input.py` | ENABLE_PROCESSED_INPUT（Ctrl+C/换行处理） | 开= Ctrl+C 中断；关= 读到 Ctrl+C 字符 0x03 |
| 53 | `test_window_input.py` | ENABLE_WINDOW_INPUT | 开启收到 WINDOW_BUFFER_SIZE_EVENT；关闭收不到 |
| 54 | `test_vt_input_mode.py` | ENABLE_VIRTUAL_TERMINAL_INPUT | 设置后 Get 一致；VT 模式切换生效（Phase 13） |
| 55 | `test_vt_output_mode.py` | ENABLE_VIRTUAL_TERMINAL_PROCESSING | 设置后 WriteFile 直通 VT 字节；清除后老式 |
| 56 | `test_wrap_at_eol.py` | ENABLE_WRAP_AT_EOL_OUTPUT | 关= 写满行后光标停在右边界；开= 折行 |
| 57 | `test_processed_output.py` | ENABLE_PROCESSED_OUTPUT | 开= `\n` 转 CRLF；关= 仅 LF 不回车 |
| 58 | `test_quick_edit.py` | ENABLE_QUICK_EDIT_MODE | Set 成功；Get 一致；不破坏鼠标输入模式 |
| 59 | `test_mode_sync.py` | GetConsoleMode 与 Set 一致性（多轮切换） | 每轮 set→get 一致；模式切换清空输入队列（无残留字符） |

## 验证标准
- [x] 10 个文件全部 PASS（50-59 全过）
- [x] 59：连续 10 轮随机模式切换，每轮 get==set；切换清队列（注入字符被清除）
- 备注：57/56/54 按工程实际语义断言（VT 直通/ConPTY 恒 wrap/无直通输入），差异见已知问题 LIM-002/003/004；53 的关闭过滤断言已恢复 PASS（BUG-005 已修复）；52 的行为断言 SKIP（LIM-001，ConPTY 架构限制）

---

# Phase 7：VT 直通模式（3 文件，`vt_passthrough/`）

| # | 文件 | 特性 | 预期 |
|---|------|------|------|
| 60 | `test_mode_switch.py` | SetConsoleMode VT_INPUT 自动切换与回退 | 开启后 mediator 日志出现直通标记；清除后回行编辑；切换瞬间无字符丢失 |
| 61 | `test_raw_byte_read.py` | python `sys.stdin.read` 原始字节 | 输入含 ESC 序列的字节原样读到（如 `\x1b[A`、方向键） |
| 62 | `test_vt_mouse_passthrough.py` | SGR 1006 鼠标直通（VT 模式） | 目标程序读到原始 `\x1b[<b;x;yM` 字节，坐标无转换 |

## 验证标准
- [x] 3 个文件全部 PASS（60-62 全过）
- [x] 61：`sys.stdin.read` 收到含 `\x1b` 的原始序列（os.read 读到 1b5b41 方向键 Up）
- 备注：60 验证切换/回退通知 + 透传输入往返（61/62 实测 1B 5B 41 与 1B 5B 3C 原始到达，无坐标转换）

---

# Phase 8：鼠标（8 文件，`mouse/`）

目标：SendInput 鼠标 → WT → mediator → DLL 全链路，坐标/按键/标志位精确（Phase 6/16）。

| # | 文件 | 特性 | 预期 |
|---|------|------|------|
| 63 | `test_click.py` | 左/右/中键 | buttonState 位 0x1/0x2/0x4 正确；down+up 各一 |
| 64 | `test_double_click.py` | 双击/三击 | MOUSE_DOUBLE_CLICK (0x2)/MOUSE_TRIPLE_CLICK (0x8) 标志正确 |
| 65 | `test_drag_move.py` | 拖动（按住移动） | down(0x1) → 拖拽按下态重复 down → up(0x0)，释放坐标反映终点（位置移动被 WT 跟踪） |
| 66 | `test_wheel.py` | 纵向滚轮 | MOUSE_WHEELED 标志 + dwButtonState 高 16 位 delta 符号正确 |
| 67 | `test_hwheel.py` | 横向滚轮 | MOUSE_HWHEELED 标志 + delta 方向正确 |
| 68 | `test_coords.py` | 坐标精度（0/1-based 转换） | 点击两处不同位置，坐标差与屏幕像素差一致（±1） |
| 69 | `test_state_reset.py` | 模式切换鼠标状态重置（Phase 16） | 按下-模式切换-释放：释放不误判为按下；状态清零 |
| 70 | `test_presolve_mouse.py` | 注入前已启用鼠标模式的 TUI（BUG-010/011 回归，2026-08-19 新增） | 目标注入前 set 0x98 → 握手后 mediator 日志含 1002h 启用序列；点击 down/up 到达；拖拽按下期间坐标移动 |

## 验证标准
- [x] 8 个文件全部 PASS
- [x] 68：目标读到的 0-based 坐标与窗口内点击相对位置换算一致（A=49,11 B=72,16，重复点击精确一致）
- 备注（实际行为与计划差异）：
  - 64：SGR 1006 无双击概念，MOUSE_DOUBLE_CLICK 标志不设置（VtToInputRecord.cpp ParseMouse 无该逻辑）——SKIP 分支记录
  - 65：WT ConPTY 对按下期间移动不输出 FR1（MOUSE_MOVED），而是按当前按钮状态重复输出 FR0 按下事件（4 步移动 → 4 个重复 down，flags 恒 0x0）；未按键悬停移动不输出任何事件（SendInput MOVE / PostMessage WM_MOUSEMOVE / SetCursorPos 三种方式实测均无效）——MOUSE_MOVED 不可达，已记录 LIM-005（2026-08-05 实测修正：拖拽按下态可观测为重复 down）；释放坐标反映终点（位置跟踪正常）
  - 67：SGR 66/67 横滚编码被 ParseMouse 译作垂直滚轮 0xFFFF0000（无 MOUSE_HWHEELED 区分）——已记录 LIM-006

---

# Phase 9：特殊序列 OSC/DCS/查询（15 文件，`special_sequences/`）

目标：OSC/DCS/查询类序列的目标程序发送侧（WriteFile VT 直通）与接收侧（ReadFile 原始字节）全链路。发送侧预期 = mediator 日志字节匹配；接收侧预期 = 目标程序记录响应。WT 不支持的记 UNSUPPORTED（探测标准见文件内"预期"）。

| # | 文件 | 特性 | 预期 |
|---|------|------|------|
| 70 | `test_osc_title.py` | OSC 0/2 标题 | 日志含 `\x1b]0;<title>\x07`；WT 窗口标题变化（EnumWindows 校验，可选） |
| 71 | `test_osc_hyperlink.py` | OSC 8 超链接 | 日志含 `\x1b]8;;<uri>\x1b\\` |
| 72 | `test_osc_workdir.py` | OSC 7 工作目录 | 日志含 `\x1b]7;file:///...` |
| 73 | `test_osc_color_query.py` | OSC 11;? 背景色查询 | 目标程序发送后读到 `\x1b]11;rgb:...` 响应 → PASS；超时无响应 → UNSUPPORTED |
| 74 | `test_osc_clipboard.py` | OSC 52 剪贴板写 | 写后 GetClipboardData 一致 → PASS；无效果 → UNSUPPORTED |
| 75 | `test_dsr_cpr.py` | DSR CPR `CSI 6n` 光标查询（Phase 15） | 目标程序读到 `\x1b[<r>;<c>R`，坐标与虚拟状态一致 |
| 76 | `test_da_query.py` | DA1 `CSI c` 终端能力（Phase 15） | 读到 `\x1b[?<attrs>;...c`；属性含常见集合（如 4/22/24） |
| 77 | `test_xtversion.py` | XTVERSION `CSI > 0 q` | 读到 `\x1b[>...;...;...c` → PASS；超时 → UNSUPPORTED |
| 78 | `test_kitty_keyboard.py` | Kitty 键盘协议 `CSI > 1 u` | 读到 `\x1b[>1u` 应答 → PASS；无应答 → UNSUPPORTED |
| 79 | `test_sync_output.py` | 同步输出 `?2026h/l` | 日志含 `\x1b[?2026h` / `l` |
| 80 | `test_focus_events.py` | 焦点事件 `?1004h`，`CSI I/O` | 目标程序收到 `\x1b[I`/`\x1b[O`（切窗口触发）；WT 不支持焦点事件 → UNSUPPORTED |
| 81 | `test_bracketed_paste.py` | `?2004h` bracketed paste | 粘贴（SendInput 不可行，用 OSC 52 写入剪贴板+Ctrl+V）→ 目标程序读到 `\x1b[200~...\x1b[201~`；不支持粘贴 → UNSUPPORTED |
| 82 | `test_alt_scroll.py` | `?1007h` 备用滚动 | 日志含序列；alt screen 下滚轮行为（可选验证） |
| 83 | `test_sixel.py` | Sixel 图形 DCS | 发送 `\x1bPq...\x1b\\` 后无崩溃、无错误 → PASS 字节；WT 不渲染六像素 → UNSUPPORTED（探测） |
| 84 | `test_kitty_graphics.py` | Kitty 图形协议 | `\x1b_G...\x1b\\` 无应答 → UNSUPPORTED（默认） |

## 验证标准
- [x] 15 个文件全部执行，其中 73/74/77/78/80/81/83/84 按探测结果记 PASS 或 UNSUPPORTED（不允许 FAIL）
- [x] 75/76（DSR/DA）必须 PASS（Phase 15 已实现）——均 PASS
- [x] 70-84 结果：
  - 70 `osc_title`：日志字节 PASS；WT 主窗口标题固定为 shell 名（OSC 0 只改标签页标题），GetWindowText 校验降级 SKIP
  - 71/72 `osc_hyperlink`/`osc_workdir`：PASS（日志含 OSC 8 / OSC 7 序列）
  - 73 `osc_color_query`：PASS——WT 响应 `\x1b]11;rgb:0c0c/0c0c/0c0c\x1b\\`，注意终止符是 **ST（1B 5C）** 而非 BEL（07）
  - 74 `osc_clipboard`：PASS——WT 支持 OSC 52，剪贴板读到目标内容
  - 75 `dsr_cpr` / 76 `da_query`：PASS——目标 os.read 收到 `1B 5B <r> 3B <c> 52` / `1B 5B 3F <attrs> 63`（WT→mediator route 回 child 生效）
  - 77 `xtversion` / 78 `kitty_keyboard`：UNSUPPORTED（4s 无响应，WT 不支持 `CSI > 0 q` / `CSI > 1 u`）
  - 79/80/81/82 `sync_output`/`focus_events`/`bracketed_paste`/`alt_scroll`：PASS（日志字节直通）
  - 83 `sixel`：PASS（DCS 头 `1B 50 30 3B 30 3B 31 71` 与 ST 字节直达）
  - 84 `kitty_graphics`：PASS——注意 payload `\x1b_G` 实际字节为 `1B 5F 47`（ESC+_+G），断言用 `1B 5F 47` 前缀
- 备注（测试经验）：
  - BatchSender 合并相邻写入 → 同批多条 ChildVtOutput 不能断言各自独立 `hex[n]=` 行，须用无前缀子串搜索
  - 长报文（如 sixel）可能跨多条 hex 行，子串搜索时分别断言头部与尾部

---

# Phase 10：代码页（3 文件，`codepage/`）

| # | 文件 | 特性 | 预期 |
|---|------|------|------|
| 85 | `test_console_cp.py` | GetConsoleCP/SetConsoleCP | Set 后 Get 一致；chcp 命令生效 |
| 86 | `test_output_cp.py` | GetConsoleOutputCP + chcp 65001 | 切换后输出 UTF-8 中文正确（乱码检测） |
| 87 | `test_utf8_output.py` | UTF-8 输出（WriteConsoleA + CP65001） | 中文 UTF-8 字节正确到达 WT；无乱码 |

## 验证标准
- [x] 3 个文件全部 PASS
- [x] 86/87：结果文件记录"输出后读回内容"一致（乱码即 FAIL）——87 以 mediator 日志 UTF-8 字节断言（`E4 B8 AD E6 96 87`）
- 备注（实际行为与计划差异）：
  - 目标进程初始 CP 是系统 ANSI 码（936，ConPTY 初始值），非 65001——不做初始值硬编码断言，只验证 Set/Get 缓存一致性
  - 85：`ModeHooks: CpChange` 日志写 DLL 进程私有日志（`injected_<pid>_<时间戳>.log`，GetTempPathW 目录，见 common/childlog.py），mediator 日志不可见，不在此断言
  - 87：WriteConsoleA 的 written 按 W 字符数计（中文每字 1），与 UTF-8 字节数不等——仅断言 written>0 + 日志字节全量到达
  - 86：`chcp 936 >nul` 子进程调 SetConsoleOutputCP 命中 Hook，目标缓存同步为 936（ConPTY 全局 CP 同步到缓存）

---

# Phase 11：字符宽度（4 文件，`width/`）

目标：Phase 17 wcwidth 集成，CJK/Emoji 双宽光标推进精确。

| # | 文件 | 特性 | 预期 |
|---|------|------|------|
| 88 | `test_ascii_width.py` | ASCII 单宽 | "Hello" 后 POS.X 推进 5 |
| 89 | `test_cjk_width.py` | CJK 双宽 | "测试" 后推进 4 列（2 列/字） |
| 90 | `test_emoji_width.py` | Emoji 代理对 | "😀" 后推进 2 列 |
| 91 | `test_mixed_width.py` | 混合 "A中😀B" | 精确推进 6 列 |

## 验证标准
- [x] 4 个文件全部 PASS（复用项目 phase17 已验证的断言）
- [x] 88-91 结果（ChildExitSync cursor 断言）："Hello"→X=5；"测试"→X=4（2字×2列）；"\U0001f600"→X=2（代理对 wcwidth32）；"A中😀B"→X=6（1+2+2+1）；均 PASS
- 备注（2026-08-02）：断言基线改为 python LazyInit aligned 光标（HelloAck 的 WT 真实位置，见 common/childlog.py find_child_aligned_baseline），期望 X = 基线.X + 推进列数；修复前基线为 ConHost 陈旧快照 (0,4)，断言曾直接假设 X=0
- 备注：
  - 用 ChildExitSync sent cursor=(X,Y) 断言（python 目标 GetConsoleScreenBufferInfo 走 ConHost pass-through，不可直接用）
  - 驱动 print 输出含裸 emoji 会触发 GBK UnicodeEncodeError → 测试意外 FAIL；统一用 \U 转义

---

# Phase 12：滚动缓冲区（3 文件，`scrollback/`）

| # | 文件 | 特性 | 预期 |
|---|------|------|------|
| 92 | `test_scrollback_count.py` | 回滚行数跟踪 | 输出 N 行后回滚计数正确（Phase 18 语义） |
| 93 | `test_user_buffer_height.py` | 用户缓冲区高度保留 | 模式切换后用户高度不丢 |
| 94 | `test_mode_switch_reset.py` | 模式切换重置 | Alt Buffer 进出后回滚状态重置正确 |

## 验证标准
- [x] 3 个文件全部 PASS（复用项目 phase18 已验证断言）
- [x] 92：cmd 输出 40 行满宽 → scrollback=175 → 再 50 行 → 258（单调增）→ 无输出 resize → 保持 258（Phase 18 保留语义）；userBufH=0；子进程段：python 输出 33 行满宽 + resize → 子进程 DLL ApplyWtResize scrollback=57（修复 2 生效）
- [x] 93：python SetConsoleScreenBufferSize(120,1000) → dwSize.Y=1000 + DLL 日志 SetUserBufferHeight height=1000
- [x] 94：设置 1000 后切换输入模式（SetConsoleMode 0x01）→ ResetScrollback → dwSize.Y 恢复屏幕行 30
- 备注（测试经验/工程行为）：
  - scrollback 递增语义已统一（2026-08-02 修复）：VirtualConsoleState::AdvanceCursor 的 `\n` 分支此前只 clamp 不递增，与 ConsoleState 不一致——已加 `m_scrollbackLines++`；修复后空行/换行输出也计数
  - 子进程 resize 同步已修复（2026-08-02）：DllRecvLoop ResizeNotify 分支此前只更新 ConsoleState，不调 VirtualConsoleState::ApplyWtResize → 子进程 bufferSize/scrollback 不随 WT resize 更新；现与主进程 WtStateReport 路径对齐（子进程注入的 python 目标 resize 后 DLL 日志出现 ApplyWtResize）
  - 92 cmd 段须读 DLL 进程私有日志（`injected_<pid>_<时间戳>.log`，路径经 common/childlog.py `latest_injected_log(pid)` 定位），多匹配取 findall[-1]
  - 92 绝对值受 cmd 回显/折行影响，断言用">0 + 单调增 + resize 保留"
  - 目标内 ctypes 调 GetConsoleScreenBufferInfo/SetConsoleScreenBufferSize/SetConsoleMode 命中 DLL Hook 返回缓存（需显式 argtypes，64 位指针否则崩溃）

---

# Phase 13：注入生命周期（15 文件，`lifecycle/`）

| # | 文件 | 特性 | 预期 |
|---|------|------|------|
| 95 | `test_inject_handshake.py` | 注入 + 握手 | 握手成功日志；目标 cmd 输出被劫持到 WT |
| 96 | `test_child_injection.py` | 子进程注入（Phase 12） | cmd 中启动 python 子进程，其输出也被劫持（日志 VtOutput 出现） |
| 97 | `test_unload_clean.py` | 管道断开卸载（Phase 11） | 关闭 WT → DLL 模块消失（Toolhelp 校验）→ cmd 恢复原控制台可操作 |
| 98 | `test_repeat_inject_unload.py` | 反复注入/卸载 10 次 | 每次注入成功、卸载后模块消失；无泄漏（句柄/进程数） |
| 99 | `test_self_protection.py` | Attach/Free/Alloc 静默拦截（Phase 9） | 目标程序调 Alloc/Free/Attach 返回 0（被拦截）；注入状态不受影响 |
| 100 | `test_blankline_accumulation.py` | 同进程反复注入/卸载不累积空行（2026-08-10 新增） | dir 制造滚动历史后循环注入/卸载，卸载后 prompt 上方空行数恒等于 baseline（不得逐轮 +1） |
| 101 | `test_child_cursor_aligned.py` | 子进程注入光标对齐 HelloAck（BUG-001 回归，2026-08-02 新增） | 子进程 DLL 日志含 `child cursor aligned to WT (X,Y) from HelloAck`，且无 LazyInit 重放分支记录 |
| 102 | `test_pipe_security.py` | 管道安全（HIGH #2） | 随机管道名 `\\.\pipe\terminjector_<pid>_<hex>` 每会话不同；server DACL 收紧；DLL 日志 `server identity verified`；预创建旧固定管道名不影响注入 |
| 103 | `test_list_targets.py` | `--list-targets` 进程枚举（2026-08-17 新增） | 默认仅可注入（STATUS 全为 injectable）；`--all` 附带原因标记（access_denied 等）；`--json` 合法且仅含 injectable=true；当前测试进程在可注入列表 |
| 104 | `test_tui_resize_scrollback.py` | 注入后 resize WT 无 scrollback（Bug B 回归，2026-08-18 新增） | vim 注入后 UIA 读 TermControl 基线非空白行数；SetWindowPos 缩窄 0.75 后非空白行增长 ≤ 2（修复前 +28：主 buffer ED 2J 推 scrollback；修复后 0~1：LazyInit 补发 ?1049h 后 ED 2J 在 alt buffer 不推） |
| 105 | `test_tui_unload_restore.py` | 卸载恢复注入几何与画面（BUG-009 回归，2026-08-18 新增） | 100x36 全屏 TUI 注入 → WT 0.6x → 关闭卸载 → ConHost buffer==(100,36)、window==(0,0,99,35)、画面逐行==注入前（旧 DLL FAIL：window 缩到 68x29） |
| 106 | `test_resize_overlay_clean.py` | 注入后 resize 无叠画（BUG-012 回归，2026-08-19 新增） | 自包含 TARGET 按 GCSBI 尺寸整屏画确定性矩阵（行中部与行尾 '|'）；0.6x/1.4x/1.4x 后 UIA 断言每行 '|' ≤2（修复前 3~4 叠画）+ LAYOUT 键 ≥3 |
| 107 | `test_conpty_hosted_target.py` | ConPTY 托管目标的注入重放（2026-09-24 新增） | 目标 shell 控制台由 ConPTY 托管（源终端比目标终端大，复现"尺寸不匹配"）→ DLL 日志 `conptyHosted=1` / `lineShell=1`、不走 TUI 的跳过重放分支、重放字节数 >4、目标终端光标与源终端一致（pywezterm 双 ConPTY，未装记 UNSUPPORTED） |
| 108 | `test_launcher_chain.py` | 跨位数启动链逐环断言（2026-09-24 新增） | x64 目标 → `py.exe`(32) → `python.exe`(64) 六环（L1~L6）分别断言，失败直接指出断在哪一环 |
| 109 | `test_adopt_console_descendants.py` | 接管「注入前已存在的同控制台后代」（2026-09-27 新增） | 源终端里先跑起 Textual TUI（注入前就存在）+ 一个 GUI 型后代（有可见顶层窗口）→ 注入承载它们的 shell：TUI 被接管（画面持续刷新 + 键入标记上屏）、**GUI 型后代被显式跳过**（不被接管、活模块表里没有我们的 DLL）、mediator 只建 1 个子会话 |

## 验证标准
- [x] 97：关闭 WT 后 10s 内 injected.dll 从模块列表消失
- [x] 98：10 次循环无残留进程/句柄泄漏
- [x] 95-99 结果：
  - 95 `inject_handshake`：握手 OK + cmd echo 输出经 DLL→mediator→WT（VtOutput hex 含目标字节）
  - 96 `child_injection`：python 子进程注入生效，WriteConsoleW 输出 ChildVtOutput hex 到达
  - 97 `unload_clean`：WM_CLOSE 关 WT → 10s 内 injected.dll 从 cmd 模块消失（Toolhelp）+ cmd 存活
  - 98 `repeat_inject_unload`：10 轮握手+卸载全 OK，无 terminal_injector 残留、无新增 WT 窗口（快照式比较）
  - 99 `self_protection`：AllocConsole=FALSE/err8、AttachConsole=FALSE/err5、FreeConsole=TRUE，调用后 DLL 缓存仍命中
  - 101 `child_cursor_aligned`：子进程 DLL 日志含 `child cursor aligned to WT (X,Y) from HelloAck`，且无 LazyInit 重放分支记录
  - 102 `pipe_security`：随机管道名每会话不同；DLL 日志 `server identity verified`；旧固定管道名伪服务器不抢占
  - 103 `list_targets`：默认输出全部为 injectable；`--all` 含非 injectable 行且带原因；`--json` 合法且全部 injectable=true；当前测试进程在列表
  - 104 `tui_resize_scrollback`：vim（TI_VIM_EXE 或常见路径）注入握手成功；resize 后 UIA 非空白行数增长 ≤2（修复前 28→56）；无 vim 环境 UNSUPPORTED
- 备注（测试经验）：
  - cmd（主进程）输出走 VtPassThrough `pipe→stdout: VtOutput`；子进程（python）输出走 `ChildVtOutput`——断言方向要区分
  - 字节断言必须限定日志来源行（输入转发日志 stdin→router 与输出 VtOutput hex 都含文本字节，只搜字节会误匹配）
  - 模块枚举用 Toolhelp TH32CS_SNAPMODULE + Module32First/NextW
  - 98 泄漏检查用"循环前快照 WT 窗口、循环后比较新增"——避免历史遗留 WT 窗口误报
  - 100（blankline_accumulation）：必须先执行 `dir` 制造滚动历史（bug 触发条件 = prompt 行进入滚动区、光标在窗口外，重放走光标归位路径）；命令注入用 AttachConsole 后 `WriteConsoleInputW`（CONIN$ CreateFile 在部分宿主环境 PATH_NOT_FOUND，回退 GetStdHandle）；屏幕读取同（CONOUT$ → GetStdHandle）；blanks 定义 = prompt 行（csbi 光标行）上方连续空行数，baseline=1（cmd 原生空行），断言每轮 == baseline

---

# Phase 14：性能与稳定性（4 文件，`performance/`）

| # | 文件 | 特性 | 预期 |
|---|------|------|------|
| 104 | `test_full_screen_redraw.py` | 满屏重绘 60fps | 20 帧满屏 WriteConsoleOutputW 全部成功，总耗时 < 20×50ms（单帧 50ms 容差），无撕裂（日志输出字节 >= 单帧格数） |
| 105 | `test_high_freq_output.py` | 高频输出（cat 大文件） | 5MB 输出完成且内容完整（目标侧 sha256 + 驱动侧字节数精确比对）；无卡死 |
| 106 | `test_mouse_latency.py` | 鼠标端到端延迟 <50ms | 50 次采样点击到目标读到的时间差 P95 < 50ms（GetTickCount64 双端时间戳） |
| 107 | `test_logger_stability.py` | Logger 双路无死锁 | 高频写日志 200 次线程不卡死；子进程 DLL 日志持续增长、行数 >= 调用次数 |

## 验证标准
- [x] 106：50 次采样 P95 < 50ms（实测 P95=47ms 稳定 ×4，平均 ~36ms；50ms 为
  WT 输入节流下留余量的阈值）
- [x] 105：输出哈希与源文件一致（经 DLL 链路无截断）
- [x] 104：20 帧满屏（120x30=3600 格）实测 31ms，单帧平均 1.6ms（远超 60fps）
- [x] 107：200 次调用全部成功，DLL 日志增长 ~16KB / 1200+ 行，worker 持续写入
- 备注（2026-08-05，测试经验/工程行为）：
  - 105：mediator 日志 hex 字段只记前 256 字节/包（Mediator.cpp WriteChildVtOutput），
    5MB 内容无法从日志全量重建 → 完整性 = ChildVtOutput len 总和精确比对
    （实测 5MB 474ms、62 包、补发字节仅 637B）+ 内容头标记在 hex 流中；
    目标 DONE 后 BatchSender 仍可能未 flush 最后一批（进程退出时 Shutdown 最终
    flush），驱动须轮询等待 len 总和达标再断言，否则偶发少 ~130KB
  - 106：测量点必须紧贴 SendInput down 发送——`input_sim.mouse_click` 内部
    move sleep 50ms 会在测量中引入固定偏差（实测 110ms 假延迟）；预热点击的
    down 会被目标计入样本导致配对错位，目标须先 rec FIRST 对齐再开始计数；
    P95=47ms 每轮完全一致，为 WT 鼠标输入固定节流
  - 107：子进程 DLL 日志定位 = 会话开始前 glob 快照，新增文件即子进程日志
    （injected_<pid>_<ts>.log）；200 次调用实测增长 ~16KB（紧凑日志格式）

---

# Phase 15：全量回归与收尾

## 内容
1. `run_all.py` 全量运行（预计 1~1.5h），修复所有 FAIL
2. 汇总报告：107 项 PASS/FAIL/UNSUPPORTED 统计
3. `README.md` 完善：运行方式、特性矩阵、UNSUPPORTED 清单、结果解读
   （2026-08-05 完成：`tests/README.md` 增加 performance 类别、UNSUPPORTED
   清单表——实测 4 个探测：osc_clipboard/osc_color_query 当前 PASS、
   xtversion/kitty_keyboard 当前 UNSUPPORTED；计数更新 107）
4. 与 terminal-injector 回归：跑项目 `tests/runners/run_all.py` 确认无回归（若需）

## 验证标准
- [x] 全量 0 FAIL（UNSUPPORTED 除外）——2026-08-05 实测：
  - 全部 14 个类别逐类回归（vt_output/console_api/cursor_buffer/keyboard/
    line_editor/modes/vt_passthrough/mouse/special_sequences/codepage/width/
    scrollback/lifecycle/performance）：0 FAIL
  - 已知偶发（非 FAIL 断言）：SetForegroundWindow 失败（环境前台锁）、
    目标 READY 偶发超时，重跑即过；不修复环境
  - UNSUPPORTED=2（xtversion、kitty_keyboard），PASS=105
- [x] 2026-08-06 分批次全量复核（7 批 × 2 类别，107 项）：唯一 FAIL 为
  `test_set_text_attribute` LOG_WRITE_X——功能正常（ChildVtOutput hex[1]=58
  已含 'X'），但断言 `" 58 " in content` 依赖 flush 批合并，'X' 单独成批
  （hex[1]=58 行尾）时漏判；已改正则 `[ =]58(?=\s|$)` 同时覆盖行中/行尾；
  `test_fill_output` 同类断言（" 23 "）一并加固，console_api 复验 11/11 PASS
- [x] 报告输出 JSON 汇总文件 `results/summary.json`

---

## 执行顺序说明

- Phase 0 必须最先完成（基建是一切的前提）
- Phase 1-3 为输出侧（无输入依赖，可任意顺序）
- Phase 4-5 为输入侧（依赖 0；4 的 Ctrl+C 依赖 Phase 6 的 processed_input 概念，但测试独立）
- Phase 6-7 模式相关建议在 4-5 之后（行编辑/直通切换会影响输入测试的默认模式）
- Phase 8 鼠标依赖 0 即可（项目 Phase 16 已验证）
- Phase 9 特殊序列可并行开发
- Phase 10-12 依赖 0 即可（复用项目断言）
- Phase 13 生命周期独立，建议靠后（涉及反复注入，避免干扰其他测试）
- Phase 14 性能靠后（其余测试跑通后再做调优验证）

每阶段完成后运行该阶段全部文件 + 冒烟回归，确认无交叉影响。

---

# 已知问题（已迁出）

工程侧的**缺陷与限制登记表**唯一权威在 [`docs/working/BUGS.md`](../../../docs/working/BUGS.md)：
`BUG-001…BUG-029`（工程缺陷）/ `LIM-001…LIM-008`（架构限制）/ `TRAP-001…TRAP-009`（排查陷阱）/
`PIT-001…PIT-010`（平台坑）/ `F-001…F-004`（已证伪假设），每条含现象、根因、修法、判据与回归用例。

本文件**不再维护该表**（历史版本见 git 历史）。新增或更新请改 `docs/working/BUGS.md`。
