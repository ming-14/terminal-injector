---
name: pywezterm-terminal-probe
description: 用 pywezterm 的真 ConPTY 承载 terminal-injector 的 mediator，做端到端只读探针，替代「起 Windows Terminal + SendInput 打前台窗口 + 解析日志」这套高失败率做法。当需要复现/排查注入类 bug（子进程未接管、输出丢失、输入不通、TUI 渲染异常）、验证修复效果、或写不依赖窗口焦点的 e2e 回归时使用。适用于 Windows 上的 DLL 注入式终端劫持项目。
agent_created: true
---

v1版本，已弃用

---

# pywezterm 终端探针

## 这个技能解决什么问题

terminal-injector 原有的 e2e 链路是：

```
起 WT 窗口 → SendInput 打前台窗口发按键 → 解析 mediator 日志反推结果
```

三个固有痛点：

1. **焦点脆弱**：`SetForegroundWindow` 受前台锁约束，实测会抛 `pywintypes.error`；
   多个 WT 窗口并存时窗口归属检测错乱，按键打进错误窗口（曾误操作到用户窗口）。
2. **验证间接**：只能从 mediator 日志的 hex 反推"应该显示了什么"，看不到真实屏幕字节。
3. **慢**：每个用例要起关 WT 窗口 + 等焦点切换，单例耗时以十秒计。

本技能改用 `pywezterm.Pty`——它是**真 ConPTY**（侧载 wezterm 自带 `OpenConsole.exe`），
正好扮演 WT 的角色承载 mediator：

```
目标进程(被注入) ←→ mediator ←─ pywezterm.Pty ─→ 探针脚本
                                 ↑ 直接 write 输入字节
                                 ↓ 直接 read 输出字节
```

- **输入**：`pty.write(bytes)` —— 不经前台窗口，**零焦点问题**
- **输出**：`pty.read()` —— 拿**真实字节**（可选喂 `pywezterm.Terminal` 还原屏幕）
- **快**：秒级完成，无窗口起关

## 何时使用

- 复现/定位注入类 bug：子进程未被接管、输出丢失、输入不通、TUI 渲染异常
- 验证某个修复是否真的生效（对照修复前后）
- 给高失败率的 WT e2e 补充一条"稳定复现"通道
- 需要**直接看输出字节**而不是从日志反推的场景

## 前置条件

| 项 | 要求 |
|---|---|
| pywezterm | 已编译好的包目录（含 `pywezterm/` 子包 + `pywezterm.pyd` + `conpty/`）。设 `PWTERM_DIR` 指向它 |
| terminal-injector | `build/bin/Release/` 下有 `terminal_injector.exe` 与 `injected.dll`。设 `TI_PROJECT_ROOT` |
| Python 依赖 | `psutil`（进程树）；helpers 路径可选（本脚本自带回退实现） |

环境变量（全部可省略，脚本会按相对位置自动探测）：

- `PWTERM_DIR` —— 含 `pywezterm/` 的目录
- `TI_PROJECT_ROOT` —— 项目根（默认由脚本位置推导）
- `TI_E2E_HELPERS` —— 项目 `tests/e2e` 目录

## 怎么用

核心是 `scripts/pwterm.py` 里的 `PwSession`：

```python
import sys; sys.path.insert(0, "<skill>/scripts")
from pwterm import PwSession

with PwSession() as s:                 # 退出自动清理 mediator 与目标进程
    assert s.start("ps7")              # 起目标 + 起 mediator + 等握手
    s.write_line('py -c "print(1)"')   # 敲命令（直接写字节，无焦点问题）
    out = s.drain(5.0)                 # 读真实输出字节
    print(s.tree())                    # [(pid, name, injected?), ...]
    print(s.grep("injected", ("InjectDllToChild", "OnChildProcessCreated")))
```

直接跑自带示例验证环境是否就绪：

```bash
cd <skill>/scripts
TI_PROJECT_ROOT=<项目根> python ab_probe.py          # 注入链路 A/B 对照（需项目产物）
python smoke.py                                       # 纯库自检，不碰注入（先跑这个）
```

期望输出：A 组 `python.exe hooked=True`；B 组 `py.exe hooked=True (relay32.dll)` +
`python.exe hooked=True (injected.dll)`（引入 32 位中继之后）。

> 注：在 relay32 落地**之前**，B 组是 `hooked=False`（x64 DLL 注不进 32 位存根），
> 当时"A 成 B 败"正是探针能正确判别的证据。现在这条判别基准已失效 —— 要逐环断言
> 跨位数链路，用项目里的 `tests/_probe/launcher_chain_probe.py`。
> `ab_probe.py` 现在只作环境自检 + 人工观察（用 `modules(pid)` 看挂的是哪个 DLL）。

## PwSession 接口速查

| 方法 | 说明 |
|---|---|
| `start(shell="ps7", timeout=20)` | 起目标（`ps7`/`cmd`/任意 exe）+ 起 mediator + 等握手，返回 bool |
| `write(data)` / `write_line(text)` | 写输入字节 / 写一行（自动补 CR） |
| `send_key(name)` | 发按键：`enter` `esc` `tab` `backspace` `up/down/left/right` `home/end` `pageup/pagedown` `delete/insert` `ctrl-c/ctrl-d/ctrl-z` |
| `read(timeout, n)` | 读一批输出字节 |
| `drain(seconds)` | 持续读 N 秒，返回这段全部字节 |
| `wait_for(needle, timeout)` | 轮询直到输出出现 needle |
| `all_output()` / `clear_output()` | 取/清内部累积缓冲 |
| `tree()` | `[(pid, name, hooked_bool)]`，判定子进程是否被接管（认 `injected.dll` **与** `relay32.dll`） |
| `modules(pid)` | 该进程已加载模块名集合（小写），用于区分挂的是主 DLL 还是中继 |
| `mediator_log()` / `injected_log()` | 读 mediator / 最新 DLL 日志全文 |
| `grep("injected"\|"mediator", patterns)` | 按关键词过滤日志行（已去掉前缀噪声） |
| `close()` | 清理 pty / 目标 / 残留 mediator |

## 库级自测怎么跑（先确认库没问题，再怀疑项目）

pywezterm 自带 11 个测试文件（`reference/pywezterm-main/tests`），是最快的回归基线：

```bash
cd <pywezterm-main>/tests
PYTHONPATH="<含 pywezterm/ 的目录>" python -m pytest -q
# 实测 64 passed, 4 skipped（跳过的 4 项是 ConsoleInput：当前进程无宿主交互控制台）
# 收集总数 68 = 64 + 4 skip；耗时约 7s

# 建议加超时守卫（本机已装 pytest-timeout，插件列表里能看到 timeout-2.4.0）：
PYTHONPATH="<含 pywezterm/ 的目录>" python -u -m pytest -q --timeout=60 <pywezterm-main>/tests
```

> **全量跑偶发挂起**（2026-09-27 实测一次：>5min 不返回、CPU 仅 1s，子进程残留
> 2×conhost + 1×cmd.exe）。当场重跑与逐文件跑均 7s 全绿，未复现，疑似 ConPTY/
> side-by-side 载入被打断（与杀软首次扫描 `OpenConsole.exe` 同类）。故**务必带
> `--timeout`**；排查时先**逐文件跑**定位（11 个文件各自 <5s，合计恰为 64 passed）。

多数文件带 `__main__` 自跑块，可单独跑，例如 `python test_term.py`。

### 导入路径的坑（必读）

**`sys.path` 要指向「含 `pywezterm/` 子包的那个目录」，不是包目录本身，更不是
`pywezterm-main`。**

- ✅ `sys.path.insert(0, r"...\terminal-injector\reference")` → 里面有 `pywezterm/`
- ❌ `PYTHONPATH=...\reference\pywezterm-main` → 那里也有个 `pywezterm/`，但里面
  只有 `src/` 和 `.rs`，**没有 `.pyd`** → 报 `No module named 'pywezterm.pywezterm'`
  （这个报错看着像包坏了，其实只是指错了目录）

## 库级测试文件都覆盖什么

按用途挑对标文件来抄写法，别重复造轮子：

| 文件 | 覆盖 |
|---|---|
| `test_pty.py` | Pty 起进程 / 读输出 / write / resize / try_wait / kill / close 幂等 |
| `test_term.py` | Terminal 屏幕文本、光标、snapshot 属性、按键编码（含 DECCKM）、鼠标 SGR、scrollback、**resize 光标锚顶语义** |
| `test_edge.py` | CJK 双宽、UTF-8 中文过 pty、10 万字符大输出、spawn 失败、close 后再 read |
| `test_selection.py` | 选区（单行/跨行/倒序/跨 scrollback）、双击选词、三击选行、OSC 52 剪贴板回调、`make_all_lines_dirty` |
| `test_stage1_state.py` | 模式状态查询（alt-screen / mouse grabbed / bracketed paste / title / OSC 7 目录 / OSC 9 进度 / OSC 133 语义区） |
| `test_stage2_render.py` | `current_seqno` / `changed_stable_rows` 增量差分 / `logical_lines` wrap 重组 |
| `test_surface_render.py` | Surface 首帧全量、增量只含变化、无变化空字节 |
| `test_mux_*.py` | Mux 布局矩形、pane 隔离、键盘编码下沉、render 合成与滚动、鼠标命中路由、分隔线/状态栏 |
| `test_console_input.py` | ConsoleInput（Windows 宿主控制台，非交互进程会 skip） |

**写屏幕断言类探针的标准范式**（`test_term.py` / `test_edge.py` 的 `_run`）——应答闭环：

```python
while time.time() < deadline:
    chunk = p.read(4096, timeout=0.2)
    if chunk:
        out += chunk
        t.feed(chunk)
        resp = t.drain_written()      # 子进程可能等 DSR 应答，必须回写
        if resp:
            p.write(resp)
    elif p.try_wait() is not None:
        # 子进程已退出，但管道可能有余量，继续排空到 EOF（read 返回 b""）
        while time.time() < deadline:
            c = p.read(4096, timeout=0.3)
            if not c:
                break
            out += c
            t.feed(c)
        break
```

## 关键注意事项

1. **进程树要在子进程存活期内采样**。被检进程若是 `python -c "...time.sleep(6)"`，
   查得太晚就已退出，拿不到 `injected` 标记。`tree()` 前先 `sleep(2)`。

2. **不要在脚本里出现 PowerShell 7 的字面可执行名**。某些 shell 安全策略会拦截
   含该字符串的命令行。`pwterm.py` 已用 `PS7_EXE = "".join(["p","w","s","h",".exe"])`
   拼接规避，shell 参数名也用 `ps7` 而非原名。

3. **`injected_log()` 取的是"最新"日志**，同一 pid 多次注入会有多个文件，按
   时间戳后缀排序取最后。若要对比两段实验的增量，注意手动做差集。

4. **这只替代 WT，不替代注入本身**。探针验证的是"注入链路 + VT 字节流"，
   若怀疑问题出在 WT 自身的渲染/窗口行为，仍需一条真 WT 通道对照。

5. **判断进程/可执行文件位数，用 `GetBinaryTypeW`，不要手工解析 PE**。
   手工解析在 PE32 与 PE32+ 之间极易错用偏移（本技能作者就曾因此误判 `py.exe`
   为 BOOT_APPLICATION）。`GetBinaryTypeW` 是 OS 权威判定：

   ```python
   import ctypes
   bt = ctypes.c_ulong(0)
   ok = ctypes.windll.kernel32.GetBinaryTypeW(
       ctypes.c_wchar_p(r"C:\Windows\py.exe"), ctypes.byref(bt))
   # ok=1 且 bt.value==0 → 32BIT;  ==6 → 64BIT（注意: 仅对 EXE 有效, DLL 返回 ok=0）
   ```

6. **同步命名管道：挂起的 ReadFile 会堵住同句柄的 WriteFile**。
   这是本链路最隐蔽的一类"卡住不报错"故障，**必须在写 DLL 侧收发循环时按此验算**：

   - Windows 语义：同步（非 overlapped）管道句柄上一次只能有一个 I/O 在飞；
     一个未完成的 `ReadFile` 未返回前，同句柄的 `WriteFile` 不会返回。
   - 典型中招形态：接收线程 `while(1) RecvPacket()`（阻塞 ReadFile 常驻），
     别的线程 `Send()`（WriteFile）去发控制帧 → **永远发不出去**。
     症状是"日志里连发送完成的下一行都没有"，且阻塞时长 = 对端存活时长
     （对端一关，WriteFile 才以 `err=232 ERROR_BROKEN_PIPE` 返回）。
   - 正确做法（本项目既有先例）：接收侧改 `PeekNamedPipe` 轮询，有数据才 `ReadFile`。
     见 `src/mediator/ChildSession.cpp RecvLoop` 与 `VtPassThrough::ForwardPipeToStdout`。
   - `PeekNamedPipe` 对**客户端句柄**同样可用（probe 实测）。

   隔离复现（不含项目源码，最快确认是不是这一类）：
   `tests/_probe/pipe_io_serialize_probe.py` —— A 组挂起 ReadFile（写被堵 1001ms，
   读一被满足立刻放行），B 组改 Peek 轮询（写 0.00ms 通过）。

7. **逐环验证跨位数链路**：`tests/_probe/launcher_chain_probe.py` 把
   x64 目标 → py.exe(32) → python.exe(64) 拆成 L1~L6 六环分别断言，
   失败时会直接指出断在哪一环，比只看"屏幕空白"高效得多。

8. **`Pty.read()` 实测返回 `bytes`**（不是 `list[int]`）；`Pty.write()` 收 `bytes`
   或 `list[int]` 都行。`pwterm.py` 里写 `list(data)` 是历史写法，不必照抄。

9. **这个库不止 Pty**。`Terminal` 是 **wezterm-term 本体**（与 WT 同一份终端模型），
   能还原可见区文本/光标/颜色属性/宽字符/scrollback/alt-screen —— 比第三方仿真器
   （pyte）更贴近"WT 上到底长什么样"。写屏幕内容断言类探针时可优先用它，
   参考项目里 `terminal-ansi-output-probe` 技能的用法，但语义基准换成它。
   另有 `Surface`（增量渲染差分）、`Mux`（多 pane，最多 2 个）、
   `ConsoleInput`（Windows 宿主控制台输入采集）。签名与用法见
   `references/pywezterm-api.md`。库级自测在 `reference/pywezterm-main/tests`
   （`PYTHONPATH=<含 pywezterm/ 的目录> python -m pytest -q`，实测 64 passed / 4 skipped）。

10. **`Terminal` 是"序列语义的权威裁判"**——凡怀疑"某条 VT 序列到底该渲染成什么"，
    先喂 `Terminal` 拿到确定答案，再谈项目对不对。
    实例（2026-09-25 冒号式真彩色 bug，`VtSgrFilter` 把 ':' 当 ';' 重建）：

    ```python
    t = pywezterm.Terminal(40, 4)
    t.feed(b"\x1b[38:2::255:0:0m" + b"A" + b"\x1b[0m")
    fg = [c[2] for c in t.snapshot()[0] if c[1] == "A"][0]   # '#ff0000'
    ```

    这一步把"过滤器改写"与"终端解析"两段责任分离开，根因无法含糊。
    实测四种形态（可直接当判据基线）：

    | 序列 | 解析 |
    |---|---|
    | `38;2;255;0;0` | `#ff0000` ✓ |
    | `38:2::255:0:0` | `#ff0000` ✓ |
    | `38:2:255:0:0` | `#ff0000` ✓ |
    | `38;2:255:0:0`（半全角混用） | `default` ✗ |
    | `38;2;;255;0`（旧重建产物） | `default` ✗ |

    **`snapshot()` 单元格元组里 fg 在索引 2、bg 在索引 3。**
    结论：**冒号语法必须"全冒号"**；混用 `38;2:r:g:b` 是应用侧写法问题，
    WT 同样不认 —— 不要为了"让屏幕好看"去兼容它（会与 WT 真实语义分叉）。

11. **e2e runner 会静默"假 PASS"**（`tests/e2e/common/reporter.py`）：
    `Summary` 默认 status 就是 `"PASS"`，只有子进程输出含 `SUMMARY:` 行才改写。
    测试文件若因缺少 `run()` / `__main__` 而**零输出退出**，会被判为 PASS。
    征兆：`python run_all.py --file xxx.py` 显示 **0.1s 通过**（真跑要起 WT + 注入，
    以秒计）。写完新 e2e 一定要单独跑一次确认有 `SUMMARY:` 输出。

12. **隔离单测可以完全不依赖 CMake 构建**：直接列被测 `.cpp` 给 `cl` 编译即可
    （范例 `tests/unit/test_vt_sgr_filter.cpp` + `run_vt_sgr_filter_test.bat`）。
    改 `translator/` 这类纯逻辑模块时，这条路比整树构建快得多，
    也避免被环境问题（见下）挡住。

13. **写"输入是否到达"的判据之前，先用未注入的对照组标定它**。
    实测踩坑：给 Textual TUI 发 `q` 指望它退出 → 不退出，于是误判"输入不通"；
    真正原因是**焦点在过滤输入框里，可打印字符全被控件吃掉**（方向键同理无效）。
    标定办法：同一个 ConPTY 里直接跑 TUI（不注入、不劫持），逐个试按键并记录
    「进程是否退出 / 屏幕是否变化 / 变化里能否看到该字符」——
    范例见 `tests/_probe/t_hijack_order_probe.py --calib`。
    标定结论（taskboard）：敲进去的**独有标记串会显示在屏幕上**，
    故判据用"屏幕出现该串"，既不受控件焦点影响，也不会被定时器重绘伪造。

14. **真 WT 通道也能在无焦点下验证"输出侧"**：先用
    `wt.exe -w new new-tab --title X <shell> /c <脚本>` 起源会话（跑起目标），
    再用 `wt.exe -w new new-tab <mediator> --mediator --target-pid <pid>`
    （GUI 的 `launch_in_wt` 就是这条命令行）劫持到新 tab，
    靠 **mediator 日志的 VtOutput 流量增长**断言"画面在刷新"、用注入端 DLL 日志的
    `Adopt:`/子会话记录断言机制 —— 全程不需要焦点。
    局限：输入侧注入不进去（真 WT 无焦点收不到键），输入必须回到 Pty 通道验；
    用完记得关掉留下的窗口（按窗口标题 `EnumWindows` + `WM_CLOSE`）。
    实测记一笔：`wt.exe`（WindowsApps 别名）拉起后的实际进程镜像是
    **`WindowsTerminal.exe`**，做"不该被接管的镜像名黑名单"时两个名字都要有。

15. **宿主环境的 PATH/Path/path 三重变体**会让 MSBuild 抛
    `MSB6001 已添加项。字典中的关键字:'PATH' 所添加的关键字:'Path'` ——
    这是宿主机环境问题，不是项目 bug，且三个变体在本进程内**删不掉**。
    绕过办法：`Start-Process -Environment`（PowerShell 7）传一份精简环境启动
    新 PowerShell 再构建。另外链接时偶发 `LNK1104`（扫描不到持有进程）
    是杀软对新链接文件的瞬时锁，**等几秒重试**。

## 已知的验证结论（可作基线）

用本探针在 Windows + PowerShell 7 目标上实测：

| 用例 | 进程树 | DLL 日志 |
|---|---|---|
| `python -c ...` | python.exe `hooked=True` | `InjectDllToChild: success` |
| `py -c ...`（**引入 relay32 之前**） | py.exe / python.exe 均 `hooked=False` | `LoadLibraryW returned 0` |

第二例的根因：`C:\Windows\py.exe` 是**标准 32 位控制台程序**
（`GetBinaryTypeW` 判定 = 32BIT；PE: machine=0x014C/PE32, subsystem=3 Windows CUI,
有 Import 与 Reloc），而 `injected.dll` 是 x64，位宽不匹配导致 `LoadLibraryW` 返回 0。
更关键的是：**py.exe 未被注入 → 它拉起的孙进程 python.exe 也没人拦截 → 一并漏网**。
（`py.exe` 导入表含 `CreateProcessW`，它就是这样拉起 python.exe 的。）

引入 32 位中继（`relay32.dll` + `relay32inject.exe`）之后的预期形态：

| 环节 | 期望 |
|---|---|
| py.exe | `hooked=True`，且 `modules(pid)` 里是 **relay32.dll**（不是 injected.dll） |
| mediator 日志 | `ChildSession Handshake: RelayHello pid=<py.exe> bitness=32` |
| python.exe（孙） | `hooked=True`，`modules(pid)` 里是 **injected.dll** |

> 注意：`ab_probe.py` 里 B 组的注释写的是"期望 injected=False"，那是**引入中继之前**
> 的结论。中继落地后 B 组应变成"py.exe 挂 relay32、python.exe 挂 injected"。
> 用 `modules()` 看具体 DLL，别只看 `tree()` 的布尔位。

## 资源

- `scripts/pwterm.py` —— `PwSession` 会话封装（核心，直接复用）
- `scripts/smoke.py` —— 纯库自检探针（Pty 起进程 + Terminal 还原屏幕并断言标记），不碰注入
- `scripts/ab_probe.py` —— A/B 对照组示例，兼作环境自检
- `references/pywezterm-api.md` —— pywezterm 原生 API 速查（Pty / Terminal / Surface /
  Mux / ConsoleInput 全签名、key mods 位、应答闭环、实测结论、重建方式）

项目内的写法范例（写新探针时先抄这几个，别从零造）：

- `tests/_probe/pywezterm_smoke.py` —— 最小 Pty+Terminal 冒烟
- `tests/_probe/t_sgr_colon_probe.py` —— 真 ConPTY 承载 mediator + 双路取证（日志 hex + Terminal 解析）
- `tests/_probe/t_hijack_order_probe.py` —— ★ 多组对照（A/B/C）+ `--calib` 标定按键判据 +
  卸载恢复测量；组内复用 `Tap` 后台持续读（不读会把 ConPTY 写端堵死）
- `tests/e2e/lifecycle/test_adopt_console_descendants.py` —— 已进官方 e2e 的 pywezterm 用例
  （pywezterm/textual 不可用时记 `SUMMARY: UNSUPPORTED`）
