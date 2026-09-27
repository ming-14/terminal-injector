# e2e_v2 —— 以 pywezterm 为宿主的端到端测试（脚手架 + 迁移方案）

> 状态：**脚手架阶段**。本文是先落地的设计稿，迁移按类别分批推进；
> `lifecycle/` 为第一批试点。v1（`tests/e2e/`）保持原样不动，直到 v2 覆盖完。

---

## 1. 为什么要迁

v1 的链路是：

```
目标 cmd（经典 ConHost） ──注入──► injected.dll ──NamedPipe──► mediator ──ConPTY──► Windows Terminal
                                                                                      ▲
驱动侧 TestSession ── SendInput 打前台窗口 ───────────────────────────────────────────┘
```

三个固有代价（实测，不是理论担忧）：

| 代价 | 具体表现 |
|---|---|
| **焦点脆弱** | `SetForegroundWindow` 受前台锁约束会被拒；多 WT 窗口并存时归属检测错乱，按键打进用户窗口（曾误操作用户窗口并可被 cleanup 关掉）。setup 还得轮询 Win+Space 切英文布局绕中文 IME。 |
| **验证间接** | 只能解析 mediator 日志的 hex 反推"屏幕上应该显示了什么"，或靠 UIA 读 WT 的 TermControl（要窗口可见）。 |
| **慢/脏** | 每例起关一个 WT 窗口，十秒级耗时；残留窗口会污染下一例的窗口归属检测。 |

v2 把 **WT 换成 `pywezterm.Pty`**（真 ConPTY，侧载 wezterm 自带 `OpenConsole.exe`）：

```
目标进程 ──注入──► injected.dll ──NamedPipe──► mediator ──ConPTY──► pywezterm.Pty
                                                                      ▲ 直接 write 输入字节
                                                                      ▼ 直接 read 真实输出字节
                                                                    pywezterm.Terminal（wezterm-term 本体，与 WT 同源）
```

- **零焦点**：输入是 `pty.write(bytes)`，不经前台窗口，没有 IME 问题。
- **直接看屏幕**：`Terminal` 还原可见区文本/光标/样式/scrollback/alt-screen，不再从日志反推。
- **快**：秒级，不再起关窗口。

---

## 2. 一条硬规则：目标宿主拓扑必须显式选择，不能一律换成 Pty

`LazyInit` 判"目标的控制台是不是由终端托管"用的是 `IsConPtyHosted()`
（判据 = 真实控制台窗口类名 `PseudoConsoleWindow`），并据此走两条不同分支：

| 目标宿主 | `conptyHosted` | 走的路径 |
|---|---|---|
| 经典 ConHost（`CREATE_NEW_CONSOLE`） | 0 | 主进程：屏幕重放 + 行首覆盖；TUI：`?1049h` + 几何恢复 |
| ConPTY 托管（WT/pty 里跑的 shell） | 1 | 按流式 shell 处理，不做行首覆盖，**不做** TUI 的跳过重放 |

**所以不能把所有用例的目标都塞进 Pty** —— 那等于把整批用例 silently 挪到另一条代码路径，
v1 覆盖的那些回归点会静默失效。v2 因此提供两种拓扑，用例显式声明：

```python
PwSession(host="conhost")   # 默认。目标 = 经典 ConHost（窗口可隐藏）；mediator 在 Pty。
PwSession(host="conpty")    # 目标本身也跑在 Pty 里（两个 Pty）。用于 conptyHosted 场景。
```

并且**每个依赖拓扑的用例都要自证一次**（`s.assert_topology("conhost"/"conpty")`，
内部读 DLL 日志的 `conptyHosted=0/1`）：声明错或环境变了都会让用例
**静默覆盖到另一条路径，看起来还是绿的**。

> 补充（2026-09-27 实测）：v1 里除 `test_conpty_hosted_target`（用的是 **pwsh**）外，
> 其余用例全跑在经典 ConHost 上 —— 而**现实里用户的目标绝大多数是"WT 里开的 cmd"**，
> 覆盖率是偏的。v2 因此新增 `test_conpty_hosted_cmd` 补上 cmd 版：
> pwsh 与 cmd 的关键行为不同（ConPTY 托管的 pwsh **不重印 prompt**，cmd **会重印**），
> "conptyHosted=1 但程序会重印 prompt"这个组合此前从未被覆盖
> （opentui 那次 prompt 重复就是踩在它上面，见 `docs/working/BUGS.md` BUG-023）。
> 实测 cmd 在 Pty 里同样拿到 `conptyHosted=1` + `lineShell=1`，echo 与输入都通。

### 真 WT 是必需的吗：绝大多数不需要，留一条窄通道

| 要验的东西 | pywezterm 能不能替代真 WT | 依据 |
|---|---|---|
| **目标宿主的"形态"**（经典 ConHost vs 由终端托管） | **能，完全等价** | pywezterm 侧载的是同一个 ConPTY，目标进程的控制台窗口类名同为 `PseudoConsoleWindow`，DLL 判出来就是 1 —— `test_conpty_hosted_cmd` 已断言 |
| 输入 / 输出 / 子进程 / 卸载等链路行为 | 能（而且更好：零焦点依赖） | 见 §1 |
| **WT 自身的渲染与窗口行为**（UIA 读屏、DWM、窗口尺寸联动） | 不能 | 需要真窗口 |
| **WT 支持而 ConPTY 不支持的协议** | 不能 | 硬证据 `docs/working/BUGS.md` BUG-022：卸载残留 `>1u`（Kitty 键盘）让 WT 改用 Kitty 编码按键 → 目标 shell 崩溃；**ConPTY 不支持 `>1u` ⇒ 本地永远复现不出来** |

结论：

1. v1 里所谓"依赖真 WT"的用例，绝大多数依赖的其实是"**目标由终端托管**"这个**形态**，
   而形态可以被 pywezterm 忠实复现 ⇒ 它们**不需要真 WT**。
2. 真 WT 保留一条**窄通道**，只验"WT 自身的差异"，且不进默认全量跑（依赖 WT 安装）。
   输出侧**无焦点也能验**（`wt.exe -w new new-tab <mediator> --mediator --target-pid <pid>`）；
   输入侧仍然要回 Pty（真 WT 无焦点收不到键）。

---

## 3. 断言基准（v2 的四条通道）

| 通道 | 手段 | 替代了 v1 的什么 |
|---|---|---|
| **屏幕**（主） | `Terminal.text() / cursor() / snapshot() / scrollback_count() / is_alt_screen_active()` | UIA 读 WT TermControl、pyte 离线渲染 |
| **字节** | `pty.read()` 拿真实输出字节（可喂 `Terminal`） | mediator 日志 hex 反推 |
| **日志** | mediator 日志（`Handshake OK` / `peerIsRelay=1` / `ChildSession started` / `OnRelayChildNotify`）+ DLL 日志（`conptyHosted=1` / `child cursor aligned` / `Adopt:`） | 同 v1（这层本就可靠，保留） |
| **结果文件** | 目标自检 `KEY=VALUE`（`rec/check/done`，协议与 `TARGET_PREAMBLE` 完全沿用 v1） | 无变化 → 目标脚本零迁移成本 |

屏幕断言优先：**"程序看到的语义"用结果文件，"屏幕上真的长什么样"用 Terminal**，
两者交叉。日志只用于"机制是否被触发"这类没有屏幕表征的断言（比如中继握手）。

### 3.1 必须内建的三个机制

1. **应答闭环**：`Terminal` 会自发产生应答（DSR/DA 等），必须 `drain_written()` 回写 pty，
   否则子进程卡在等应答上。session 的泵循环内建。
2. **持续泵**：ConPTY 写端不被读会堵死；session 起后台线程持续 `read → feed(Terminal)`。
3. **屏幕等待**：断言"屏幕上出现了 X"要轮询（TUI 有重绘延迟），用 `wait_screen(needle, timeout)`。

---

## 4. 目录与命名

```
tests/e2e_v2/
├── README.md              # 本文
├── run_all.py             # 运行器（--list / --cat / --file / 全量）
├── pwcommon/              # 公共库（**刻意不叫 common/helpers**，避免与 v1 同名包互相顶掉）
│   ├── paths.py           # 路径解析（pywezterm 目录、BUILD_BIN、日志路径）
│   ├── pyterm.py          # pywezterm 引导（sys.path + 不可用时 UNSUPPORTED 协议）
│   ├── session.py         # PwSession：启动链路 / 输入 / 屏幕 / 日志 / 清理
│   ├── result.py          # 结果文件协议（沿用 v1）
│   └── reporter.py        # SUMMARY 解析与汇总（修掉 v1 的假 PASS 与重复计数）
├── _targets/              # 运行期生成的目标脚本（gitignore）
├── _results/              # 运行期结果文件（gitignore）
└── lifecycle/             # 试点类别
```

`pwcommon` 这个名字是刻意的：v1 里 `tests/e2e/keyboard/` 曾被同名第三方库顶掉、
9 个用例在导入期就死（BUG-017 记录）。v2 不与 v1 共用 `common` / `helpers` 这些通用名。

---

## 5. lifecycle 迁移矩阵（15/15 可迁，3 项断言需改写）

| 用例 | v1 依赖 | v2 方案 | 迁移成本 |
|---|---|---|---|
| `inject_handshake` | 目标 ConHost + WT + SendInput + 日志 hex | `host="conhost"`；`write("echo X\r")`；**断言 `Terminal.text()` 含 X** + 日志 | 低 |
| `child_injection` | 同上 + 结果文件 | 同上，子进程输出断屏幕 + 结果文件 | 低 |
| `self_protection` | 结果文件 | 只需要"目标被注入"这个前提 → `host="conhost"`；断言全在结果文件 | 低 |
| `launcher_chain` | 结果文件 + mediator 日志 | 同上（纯日志 + 结果文件） | 低 |
| `child_cursor_aligned` | DLL 日志 | 同上 | 低 |
| `pipe_security` | 两轮会话 + mediator/DLL 日志 | 同上；"预创建固定名伪造服务端"逻辑不变 | 低 |
| `list_targets` | 纯本地 CLI，不用链路 | 原样搬 | 极低 |
| `unload_clean` | `PostMessage(WM_CLOSE)` 关 WT + Toolhelp | **`pty.close()`** 关 ConPTY → mediator 退出 → 管道断开；断言不变 | 低 |
| `blankline_accumulation` | 起关 WT + `AttachConsole` 读**目标 ConHost** | 目标仍在真 ConHost，`AttachConsole` 读回逻辑原样复用；关 WT → 关 Pty | 中 |
| `tui_unload_restore` | ConHost 几何 + `AttachConsole` dump | 同上；resize 由 **`pty.resize()`** 驱动 | 中 |
| `tui_resize_scrollback` | vim + **UIA 读 WT TermControl** + `SetWindowPos` | `pty.resize()` + **`Terminal.scrollback_count()` / `text()`**（wezterm-term 与 WT 同源，语义等价且更精确） | 中 |
| `resize_overlay_clean` | 自绘矩阵 + **UIA 读 WT** + `SetWindowPos` | 同上：`pty.resize()` + `Terminal.text()` 数每行 `\|` | 中 |
| `repeat_inject_unload` | 10 轮起关 **WT 窗口** + 窗口残留计数 | 10 轮 Pty 开关；**"无新增 WT 窗口"这条断言在 v2 没有对象** → 改为断言无 mediator/目标残留 | 中 |
| `conpty_hosted_target` | 已是 Pty（用 pyte 渲染） | 改用 `pwcommon`；pyte → `Terminal` | 低 |
| `adopt_console_descendants` | 已是 Pty（用 pyte 渲染） | 同上 | 低 |

迁移后不再需要的依赖：`win32gui` / `SetForegroundWindow` / `ensure_english_layout` /
`uiautomation` / `input_sim`（SendInput）/ `vt_capture`（hex 解析）/ `pyte`。

仍需 Windows API 的地方：`AttachConsole` + `ReadConsoleOutputCharacterW` 读**目标 ConHost** 的
全屏缓冲（`blankline_accumulation`、`tui_unload_restore`）——这是 v1 的正确做法，保留。

---

## 6. 与 v1 的关系

- v1 **不动**，继续可跑；v2 覆盖到哪一类，哪一类在 v1 侧标记为"由 v2 对应用例取代"。
- v2 的 `reporter` 修掉 v1 的两个已知缺陷（见 §7），所以 v2 的 PASS 更可信。
- 未装 pywezterm / 缺前提（vim、textual 等）时，统一记 `SUMMARY: UNSUPPORTED`（不算失败），
  与 v1 约定一致。
- pywezterm 默认取 `<project>/tests/vendor`（AGENTS.md 指定的位置），
  回退 `<project>/reference`；可用 `PWTERM_DIR` 覆盖。

## 7. 顺手修掉的 v1 缺陷（v2 不继承）

1. **假 PASS**：v1 的 `Summary` 默认 `status="PASS"`，只有输出含 `SUMMARY:` 行才改写 ⇒
   零输出退出的用例被判 PASS。v2 改为**默认 `ERROR`**，必须有 `SUMMARY:` 行才算数；
   并把 checks 数写进汇总。
2. **重复计数**：v1 的 `total_failures = FAILURES + ERROR + FAIL`，而 `FAILURES` 已经含
   了 FAIL 项的断言数，等于把 FAIL 又加一遍。v2 分开统计 `fail_items` 与 `fail_asserts`。
3. **假 FAIL（v2 自己踩的，已修）**：v1 与 v2 初版都用"整行**包含** `[FAIL]`"当兜底判据，
   于是用例只要把 `[FAIL]` 作为**文本**打印出来（失败信息里引用它、或用例名里带这两个
   字符），就会被从 PASS 翻成 FAIL。实测：脚手架自检用例打了 `SUMMARY: PASS`，运行器判 FAIL。
   现在只认**行首**标记（`line.strip().startswith("[FAIL]")`），并把这条固化成回归用例。
4. **握手早退判据不可靠**：v1 用 `"ERROR" in 日志` 提前判失败，但**子会话**
   （relay32 中继、被接管后代）的正常报错日志同样含 ERROR，不影响主握手，
   却会让等待提前返回 ⇒ 用例假报"握手失败"（跨位数/接管类最容易踩）。
   v2 只认主握手的显式失败：`Mediator: Handshake failed` / `Mediator: targetPid is 0`。

判定逻辑已实测过（8 组输入 × 状态/计数 + 行首/行中两种标记位置，见
`harness/test_harness_selfcheck.py`，26 条断言）：

| 输入 | v2 判定 | v1 判定 |
|---|---|---|
| `SUMMARY: PASS (0 failures)` + 2 行 `[PASS]` | PASS，checks=2 | PASS |
| 只有 `[PASS] a`，无 SUMMARY | **ERROR** | PASS（错） |
| 零输出 + 退出码 0 | **ERROR** | PASS（错） |
| 行首 `[FAIL]` + 自称 PASS | FAIL | FAIL |
| 中间提到 `[FAIL]` 字样（行首是 `[PASS]`） | **PASS** | FAIL（错） |
| 自称 PASS 但退出码 1 | **ERROR** | PASS（错） |
| `SUMMARY: UNSUPPORTED (…)` | UNSUPPORTED | UNSUPPORTED |

---

## 8. 已落地（本轮）

```
tests/e2e_v2/
├── README.md                       # 本文
├── run_all.py                      # --list / --cat / --file / --preflight / 全量
├── pwcommon/{paths,pyterm,result,target,childlog,reporter,session,diff}.py
├── harness/
│   └── test_harness_selfcheck.py     # 脚手架自检（不起终端，秒级）：命令行引号 / 按键修饰 / 汇总判定
└── lifecycle/
    ├── test_inject_handshake.py        # host=conhost
    ├── test_child_injection.py         # host=conhost + 子进程注入
    ├── test_conpty_hosted_target.py    # host=conpty（双 Pty，目标 pwsh）
    ├── test_conpty_hosted_cmd.py       # host=conpty（双 Pty，目标 cmd = "WT 里开的 cmd"）
    └── test_screen_matches_native.py   # 差分诊断：注入屏幕 vs 原生屏幕
```

五个试点 + 一个自检用例 **全量 6 PASS / 59 断言 / 31.9s / 0 残留进程**（跑前跑后进程快照对比），
每个用例再各自连跑 3 轮稳定。单例耗时：

| 用例 | v2 耗时 | 说明 |
|---|---|---|
| `test_harness_selfcheck` | **0.2s** | 不起终端：命令行引号 / 按键修饰 / 汇总判定 |
| `test_inject_handshake` | **0.7~0.9s** | 起目标 + 起 mediator + 握手 + echo 往返 + 屏幕断言 |
| `test_child_injection` | ~2.5s | 多一个 python 子进程注入 + 2s 采样窗 |
| `test_screen_matches_native` | ~18s | 3 个场景 ×（原生基准 2 遍 + 注入 1 遍） |
| `test_conpty_hosted_target` | ~9s | 源终端 drain 4s + 重放等待（目标 pwsh） |
| `test_conpty_hosted_cmd` | ~2s | 双 Pty，目标 cmd（"WT 里开的 cmd"） |

对照：v1 同类用例以十秒计（大头是 wt.exe 起窗口与焦点切换），且需要"别碰鼠标"。

比 v1 多出来的断言（v2 才做得到的）：

- `test_inject_handshake`：屏幕上的**输出行**等于标记（v1 只能看日志 hex）；
  并用 `conptyHosted=0` **自证拓扑**是经典 ConHost。
- `test_child_injection`：子进程**自己的活模块表**里有 injected.dll（直接证据，
  v1 只能从"输出字节到了"间接推断）。
- `test_conpty_hosted_target`：光标一致性直接读两个通道的 `Terminal.cursor()`
  （v1 靠 pyte 手算 1-based 换算），实测 (`(2, 45) == (2, 45)`)。

### 脚手架阶段踩到/定下的规矩

1. **命令行里每个 token 都要加引号**。`sys.executable` 是
   `C:\Program Files\Python311\python.exe`，不加引号会被 cmd 按空格切开，
   报 `'C:\Program' 不是内部或外部命令`。`run_target` 现在按 token 加引号，
   `launcher` 支持传 list（跨位数用例传 `["py", "-3"]`）。
2. **目标 ConHost 窗口默认隐藏**（`STARTF_USESHOWWINDOW` + `SW_HIDE`）：
   `conptyHosted=0` 证明隐藏后仍是真经典 ConHost，不会变成别的形态。
3. **屏幕断言要用整行相等**：cmd 会把敲进去的命令行回显到屏幕上，
   `needle in text` 会被"echo TI_X"里的 TI_X 假命中 → 用 `wait_line(exact=True)`。
4. **Terminal 的应答必须回写**（`drain_written()`），泵循环里内建；否则目标等 DSR 卡死。
5. **`snapshot()` 的行按"字符"排不是按"列"排**：宽字符占两列、续列不在列表里
   （喂 `中文AB` 得到列号 `0,2,4,5`）→ 取列位置一律用 `cell[0]`，别用列表下标。
6. **`resize()` 必须模型与 pty 一起改，且先模型后 pty**（`order="term_first"`）：
   缩小尺寸时模型要对"当前屏幕内容"做 reflow（溢出进 scrollback），若 pty 先改，
   目标可能已按新尺寸重绘完，reflow 的对象就变了 ⇒ 同一场景两次跑出不同 scrollback。
7. **带修饰的按键必须自己翻成 mods 位**：pywezterm 的 `key_down` 不认 `"ctrl-c"`，
   会当单字符处理返回 `b'c'` —— **Ctrl 被静默丢掉**（不报错！）。
   走 `parse_key()`（`press("ctrl-c")` 已接好），实测 `key_down("c", CTRL)` == `b'\x03'`。
8. **launcher 路径不能丢给 `shlex.split` 的 POSIX 规则**：它会把
   `C:\Program Files\...\python.exe` 的反斜杠当转义吃掉。走 `parse_launcher()`
   （存在的路径 → 单 token；否则 `posix=False` 再剥引号）。

### 还没被跑到过的代码（诚实标注）

`pwcommon/childlog.py` 里的 `aligned_baseline` / `wait_child_exit_cursor` /
`wait_module_state` / `snapshot_injected_logs` 是为下一批迁移（`child_cursor_aligned`、
`scrollback` 类）准备的，**目前没有任何用例执行过它们** —— 迁移到那些用例时必须先验证，
不要当成已经可靠的工具。

---

## 8.5 差分诊断：注入屏幕 vs 原生屏幕（"最终诊断"）

注入应当是**透明**的：同一个程序、同样的输入、同样的尺寸，走不走 mediator，
屏幕上应该长成一样。于是不必为每个用例手写期望值 —— 拿**原生腿**当基准：

```
基准腿  pywezterm.Pty ─ cmd.exe ← 直接写输入字节（不经 mediator）
被测腿  pywezterm.Pty ─ mediator ─ 目标 cmd（经典 ConHost，窗口隐藏）
```

两条腿跑**同一个场景脚本**（`pwcommon/diff.py` 的 `drive`），比
`lines / cursor / scrollback / is_alt_screen`。用例：
`lifecycle/test_screen_matches_native.py`。

### 实测（3 个场景，每场景连跑 3 轮稳定）

| 场景 | 结果 |
|---|---|
| echo 一行 | 屏幕逐行一致，cursor `(6,41,True)` 也一致 |
| 满屏长输出 300 行 | 逐行一致 + **scrollback 都是 276**（DLL 的回滚跟踪与原生完全对齐） |
| 自绘全屏矩阵 + resize | 逐行一致 + scrollback 都是 32 + alt=False；尺寸序列都是 `['120x30','72x18']`；光标另断 |

### 三条硬规矩（都是实测踩出来的）

1. **两条腿的启动上下文必须一致**。原生腿也要经 cmd 壳；否则命令行回显/初始光标不同，
   cursor 必然不等，比出来全是噪声。
2. **等"安静"再比，不要固定 sleep**。用 `wait_stable(quiet=0.6)`，且**每个阶段边界都要有一步**：
   目标"写日志/写结果文件"通常发生在画屏之前，日志一到就进入下一步，
   画屏字节可能还在路上 ⇒ 下一步（尤其 resize）作用在对不上的屏幕状态上。
   实测踩坑：同一场景两次跑出 scrollback `32 / 0` 两种结果，根因就是这个竞态。
3. **先做基准自检**（`run_native` 跑两遍必须一致）。基准自己不稳（时间戳、随机数、
   定时器动画）的场景**不该用差分**，该老实用定点断言。
4. **每条腿用独立的日志/结果文件**（本用例走 `TI_DIFF_LOG` 环境变量而不是命令行参数）：
   上一条腿的进程可能还没退干净、写进同一条日志 ⇒ 偶发判错。
   走环境变量还能保证两条腿**敲进去的命令行逐字节相同**（命令行回显本身也在屏幕上）。

### 光标为什么在 resize 场景不能拿原生当基准

`cursor` 的归属在两种语义下不同：

| 场景 | 光标归谁 | 实测 |
|---|---|---|
| 无 reflow（普通输入输出） | 两边一致 | echo / 300 行场景完全一致 |
| reflow（缩尺寸）**且程序不设光标** | 原生归**终端**（reflow 重排它）；注入归**目标程序**（DLL 同步它） | 原生 `(8,24)` vs 注入 `(5,0)`，**文本与 scrollback 都一样** |

第二种**不是缺陷**：注入架构里光标本来就以目标进程为准（程序才是权威），
原生那个 `(8,24)` 是终端"替"程序算出来的。所以这类场景的正确对照物是
**目标程序自己的 `GetConsoleScreenBufferInfo` 光标** —— 本用例就是这么断的
（`compare(check_cursor=False)` + "注入光标 == 目标自检光标"，实测 `(5,0)`）。

### 适用边界（差分不能替代什么）

- **机制类事实**（`conptyHosted` / `Adopt: pid=` / `peerIsRelay`）—— 注入侧独有，原生腿没有对手
- **目标自检结果文件** —— 那是"程序内部的账"（DLL 虚拟状态）
- **注入侧故意偏离原生的行为**（如 ConPTY 托管目标不做行首覆盖）—— 差分必然"不等"，且不等才是对的

---

## 9. 后续批次（建议顺序）

1. **lifecycle 剩余 12 个**（按 §5 矩阵，先做代价低的）：
   `list_targets`（纯 CLI）→ `self_protection` / `launcher_chain` / `child_cursor_aligned`
   / `pipe_security` / `unload_clean`（都在结果文件 + 日志层）→ `repeat_inject_unload`
   （需改"WT 窗口残留"断言）→ `blankline_accumulation` / `tui_unload_restore`
   （保留 `AttachConsole` 读目标 ConHost）→ `tui_resize_scrollback` / `resize_overlay_clean`
   （UIA → `Terminal`）→ `adopt_console_descendants`（已是 Pty 形态，改用 `pwcommon`）。
2. **输出侧类别**（`vt_output` / `console_api` / `cursor_buffer`）：v2 里可以直接断言
   真实字节 + 屏幕，比 v1 的 hex 反推强；`common/vtbyte.py` 的批量套路可平移。
3. **输入侧类别**（`keyboard` / `line_editor` / `modes` / `mouse`）：改成 `pty.write`
   + `Terminal.key_down/mouse` 编码，彻底摆脱 SendInput 与 IME。
4. 全部覆盖后再决定 v1 的去留（`legacy` 化或删除），本轮不动 v1。

> 覆盖进度守恒：迁移一个用例前先确认它在 v2 里的断言**不弱于** v1（尤其是
> "机制是否被触发"这类只有日志能答的断言要保留），否则就是静默降级。

