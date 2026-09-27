# 缺陷与限制登记表

> **这是本项目唯一的权威表。** 2026-09-27 由分散记录合并而成。
> 来源：`tests/e2e/docs/PHASES.md`「已知问题」节（BUG-001…017 / LIM-001…007）+
> `.workbuddy/memory/`、` .workbuddy-ai/memory/` 工作日志（2026-09-23…27）+
> `docs/2026-08-08-fix-report.md`、`docs/2026-09-23-child-injection-32bit-report.md` +
> `docs/phases/*` 散记 + e2e_v2 迁移期新发现的陷阱。
>
> **新增/更新请改这里**，`tests/e2e/docs/PHASES.md` 只保留指向本文档的指针。

---

## 编号规则

| 前缀 | 含义 | 处置原则 |
|---|---|---|
| `BUG` | 工程缺陷（我们自己的问题） | 修；修完必须补回归用例并记入"判据/回归"列 |
| `LIM` | 上游 / 架构限制（ConPTY、WT 行为） | **不是 bug**。测试按实际语义断言或 SKIP，不改代码 |
| `TRAP` | 排查陷阱：看着像 bug、其实不是 | 记下来，防止后人当 bug 重查一遍 |
| `PIT` | 平台 / 环境 / 测试基建的坑 | 规避，附规避手段 |
| `F` | 已证伪并撤销的假设 | 保留记录，防止重复劳动（附证伪依据） |

合并时做过两处编号调整（保留原号以便对照）：

- `BUG-003`（Shift 修饰丢失）经判定是**上游限制**而非工程缺陷，已重分类为 `LIM-008`；`BUG-003` 不再使用。
- `BUG-011` 为空号：`docs/phases/06-input-chain.md` 提到 "BUG-010/011"，但 011 从未独立登记。
- `BUG-018` 及以后为本次合并时**新登记**的条目（原散落在工作日志里，没有编号）。

---

## 1. BUG —— 已修复

| ID | 现象 | 根因 | 修法 | 判据 / 回归用例 | 日期 | 来源 |
|---|---|---|---|---|---|---|
| BUG-001 | SGR 前景/背景红蓝互换（0xC 输出 `1;34m` 应为 `1;31m`） | Windows 属性位（bit0=蓝）被直接当 ANSI 色索引（bit0=红），未重映射 | `Color.cpp` 新增 `ToVtIndex()` 位序重映射（bit2=红→ANSI 1、bit0=蓝→ANSI 4） | `test_set_text_attribute` 断言 `1B 5B 31 3B 33 31 3B 34 30 6D` | 08-02 | PHASES / TECHNICAL.md |
| BUG-002 | Alt Buffer 切换完全失效 | `sizeof(const char*)-1` = 7，`?1049h` 被截成 `?1049` | `VtEscape.h` 由 `const char*` 改 `char[]`（数组 sizeof 正确） | `test_dual_buffer` 断言完整 `1B 5B 3F 31 30 34 39 68/6C` | 08-02 | PHASES |
| BUG-004 | 行输入下 Ctrl+Z 永不返回（ReadConsoleW 卡死） | `LineEditor::ProcessKey` 把 `\x1a` 当普通字符插入行缓冲 | ProcessKey 新增 Ctrl+Z 分支：截断行缓冲 + 回显 `^Z\r\n` + 返回 EOF | `test_ctrl_z_eof` 的 READ_RET | 08-02 | PHASES |
| BUG-005 | TUI 永不 resize；GetNumberOfConsoleInputEvents 与读口不一致 | 曾按 `ENABLE_WINDOW_INPUT` 过滤 resize 事件，但真实 ConPTY **不受模式门控** | 移除 `FilterByInputMode`；`DllRecvLoop` 无条件 `EnqueueResizeEvent` | `test_window_input` 断言"关闭后仍收到" | 08-05 | PHASES |
| BUG-006 | 补发序列与内容字节合并，e2e 精确断言被破坏 | `VtOutput` 走 BatchSender 合批，补发与内容边界不可分 | 新增协议类型 `CursorSync=0x0090`（不经 BatchSender，先于内容即时发）；发控制消息前先 Flush | `test_processed_output` / `test_vt_output_mode` | 08-05 | PHASES / phases-19 |
| BUG-007 | 反复注入/卸载后 ConHost 空行逐轮 +1、prompt 下移 | `Unloader` 在光标归位**前**记录 `preReplayCur`，导致惰性重放分支永不触发 | 归位成功**之后**再记 `preReplayCur` | `test_blankline_accumulation`（6 轮 blanks 恒 = baseline） | 08-10 | PHASES |
| BUG-008 | 注入后 resize 出现滚动条 + 双帧（UIA 非空白行 28→56） | 全屏 TUI 的 `?1049h` 发生在注入之前、未达 WT ⇒ ED 2J 落在**主 buffer** 把视口推进 scrollback | `LazyInit` 对全屏 TUI 补发 `?1049h`（`recordReplay=false`） | `test_tui_resize_scrollback`（resize 后 ≤+2 行） | 08-18 | PHASES |
| BUG-009 | 卸载后 ConHost 画面叠画、窗口被永久缩窄 | `ReplaySessionToConHost` 无条件把会话 VT 流叠加重放到冻结快照，并裁剪窗口到会话尺寸 | 按注入类别分流：非行编辑 shell 跳过会话重放，改走 `RestoreInjectionGeometry` | `test_tui_unload_restore`（buffer/window 恢复注入几何、画面逐行一致） | 08-18 | PHASES / phases-22 |
| BUG-010 | 注入前已开鼠标模式的 TUI 注入后鼠标全丢 | 目标注入前就 `SetConsoleMode` ⇒ 不再触发 `ModeChange` ⇒ mediator 从未发 `?1002h/?1006h` | 握手后 `Mediator::ApplyInitialMouseReport` 按 Hello 的 inputMode 补发 | `test_presolve_mouse` | 08-19 | PHASES / phases-06 |
| BUG-012 | 连续 resize 后新旧布局叠画（每行 `\|` 计数 3~4） | 全量渲染路径（`canDiff=false`）跳过"默认空格"cell，假设 WT 屏幕初始为空 | 全量路径不再跳过 `IsDefaultBlank`，输出全部 cell | `test_resize_overlay_clean`（0.6x/1.4x 后每行 `\|` ≤2） | 08-19 | PHASES / phases-04 |
| LBUG-001 | 长命令软折行回车后，WT 光标被拉回折行行首 | 子进程 LazyInit 用 ConHost 陈旧快照覆盖 HelloAck 回传的 WT 真实光标 | 按 `isTarget` 分流：主进程保留重放，子进程只用 HelloAck 对齐 | `test_child_cursor_aligned`（有 aligned 记录、无重放记录） | 08-02 | PHASES |
| BUG-013 | ConPTY 托管的目标（如 WT 里的 pwsh）注入后画面全空、光标停左下角 | `bufMatchesWin` 作"是否 alt buffer/TUI"的代理只在经典 ConHost 成立；ConPTY 视口模型下主缓冲恒等于窗口 ⇒ 普通 shell 被误判成 TUI | 新增 `LazyInit::IsConPtyHosted()`（窗口类名 == `PseudoConsoleWindow`）；`isLineShell = !bufMatchesWin \|\| conptyHosted` | `test_conpty_hosted_target`（`conptyHosted=1`、`lineShell=1`、不走 skip replay、重放 >4 字节） | 09-24 | PHASES / memory-0924 |
| BUG-013b | 同场景第二处：光标被"行首覆盖"拉到 prompt 行首后悬空 | 经典 cmd 会重印 prompt 覆盖旧行，ConPTY 托管的目标**不重印** ⇒ 拉行首就悬空 | `promptOverwrite = isLineShell && !conptyHosted` | 同上用例新增"目标终端光标 == 源终端光标"断言 | 09-24 | PHASES |
| BUG-014 | 子进程里 `input()` 挂住、按键不回显 | `SetConsoleMode` Detour **不调原 API**，真实控制台输入模式被冻结在注入瞬间的值 | 输入句柄分支记录后同时调 `SetConsoleMode_orig`（只镜像输入句柄） | `test_modifier_keys` 等回归；残留：`msvcrt.getwch()` 仍返回 WEOF | 09-24 | PHASES |
| BUG-015 | 子进程行编辑回显落到错误行/列（偏 27 行 + 右移 1 格） | ① 基准取了 `ConsoleState`（缓冲绝对行号），而 VT 直通分支不推进它；② 基准在 `ProcessKey` **之后**才取 | 新增 `m_startCursorUi`（取 `VirtualConsoleState`）；定位基准挪到 ProcessKey **之前** | 回归 line_editor/modes/width/lifecycle 共 33 项 | 09-24 | PHASES |
| BUG-016 | 子进程输出被画到错误行（菜单里冒出 26 行空行） | `CursorSync` 立即发送、越过 BatchSender 里积压的内容 | `SendToMediator` 发控制消息前先 `BatchSender::Flush()`（字节顺序 == 产生顺序） | 回归 68 项；判据 = DLL 日志顺序 vs 终端字节流顺序矛盾 | 09-24 | PHASES |
| BUG-018 | 32 位中继链路死锁：子进程一直冻着、cmd 不出提示符 | relay32 接收循环用**阻塞** `RecvPacket`(ReadFile) 常驻，同同步管道句柄的 `Send`(WriteFile) 永不返回（见 PIT-004） | relay32 接收循环改 `PeekNamedPipe` 轮询（与 `ChildSession::RecvLoop` 同策略） | `test_launcher_chain`（L1~L6 逐环，含 `OnRelayChildNotify`） | 09-24 | memory-0923/0924 |
| BUG-019 | 冒号式真彩色 `38:2::r:g:b` 丢色（渐变整条反相、尾端变黑） | `VtSgrFilter::RebuildSgr` 把 ':' 当 ';' 拍平 ⇒ 冒号组内的**空保留位**被吃掉，重建出 `38;2;;r;g;` | 保留参数原始分隔符；空保留位不占数据位 | `tests/unit/test_vt_sgr_filter.cpp`（20/20）+ `test_sgr_truecolor_colon` | 09-25 | memory-0925 |
| BUG-020 | 劫持后 termtest 空行消失、后续行上移 | `ReadFile(stdin)` 在 LINE+ECHO 下未做行编辑/按键回显；且回显未喂 `VtCursorTracker` ⇒ 后续输出被补发的 `ESC[row;1H` 覆盖 | `ReadFile_Detour` 新增行编辑分支（复用 LineEditor + 新增 `EmitLineEcho`）；Enter → `lineOut+"\r\n"`（**空行也补**）、Ctrl+C → 0 字节但 TRUE（非 EOF） | `test_readfile_line_echo`（`ab`+Enter→`61 62 0d 0a`；空行→`0d 0a`） | 09-26 | workbuddy-ai-0926 (7ac611c) |
| BUG-021 | 注入后 Textual/asyncio TUI 画面定格（事件循环死亡） | `IsInputHandleSlow()` 对任意句柄调 `GetNumberOfConsoleInputEvents`，失败时会**污染调用线程的 `GetLastError()`**（置为 6）；该调用点在 Wait Detour 内，被 CPython 锁/asyncio 高频走到 ⇒ `IocpProactor._poll` 抛 `OSError [WinError 6]` | `IsInputHandleSlow` 保存/恢复 `GetLastError()` | 隔离实验表（7 组，每次只改一处）+ `t_textual_asyncio_freeze.py` | 09-26 | workbuddy-ai-0926 |
| BUG-022 | **只有真 WT 崩**、ConPTY 不崩：`0xc0000409`（ucrtbase） | 卸载重放把面向 WT 的**终端模式序列**写进共享 ConHost：`?1004h`（焦点）、`?2004h`（括号粘贴）、`>1u`（Kitty 键盘）残留 ⇒ WT 改用 Kitty 协议编码按键 ⇒ 目标 shell 解析异常输入 AV。**ConPTY 不支持 `>1u`** ⇒ 本地永远复现不出 | `StripMouseReportSequences` → `StripTerminalModeSequences`（剔除 `CSI ? n h/l`、`CSI > n u`、`CSI = n u`、`CSI < u`） | `t_unload_tui_crash.py` 双轨（重放后目标终端收到的序列为空） | 09-26 | workbuddy-ai-0926 (4f69e9b) |
| BUG-023 | opentui Ctrl+C 退出后同一行出现两个 prompt、光标错位 | mediator 的 VtParser 把**子进程的** cursor/DA 应答**回灌给父进程**（两个"光标"是两回事：ConPTY 光标 ≠ 父 shell 的 ConHost 光标，再由 `ChildExitSync` 灌回） | VtParser 的 cursor/DA 回调加 `hasActiveChild()` 闸门（有活跃子进程时不回灌父进程） | `t_shell_child_exit.py` 双轨（注入轨从 3 行降到 2 行，与原生一致） | 09-26 | workbuddy-ai-0926 |
| BUG-024 | 先跑起 TUI、再劫持承载它的 shell：新终端画面定格、按键无效 | 注入只接管"目标本身"+ 注入之后由它 CreateProcess 出的子进程；**注入前已在运行的后代永远不被接管** | 注入时枚举同控制台后代并接管（判据：父链可回溯 + **无可见顶层窗口** + x64 + 不在工具链黑名单 + 未加载我们的 DLL） | `test_adopt_console_descendants`（TUI 被接管 + GUI 型后代被显式跳过 + mediator 只建 1 个子会话） | 09-27 | memory-0927 |
| BUG-025 | 同一 cmd 反复注入/卸载后卸载恢复偶发停在会话尺寸（3 次里 1 次） | 被接管的后代与目标**共享同一块 ConHost**，两者卸载时都走 `ReplaySessionToConHost`；后代快照取自注入之后 ⇒ 两个都恢复，谁后跑谁赢 | 被接管的后代卸载时跳过 `ReplaySessionToConHost`（`!IsAdoptedProcess()` 门控） | `test_tui_unload_restore` 5/5 | 09-27 | memory-0927 |
| BUG-026 | termtest 交互段在注入下崩溃（`ArgumentError: expected LP_INPUT_RECORD instance instead of INPUT_RECORD_Array_128`） | 测试前导的 `ReadConsoleInputW/PeekConsoleInputW` 第 2 参钉成 `POINTER(INPUT_RECORD)`；**ctypes 的 argtypes 是进程级生效**，污染同进程内 termlib 的调用 | 第 2 参改 `wintypes.LPVOID`（前导也改用独立的 `WinDLL("kernel32")` 实例） | termtest 交互段不再崩；`common/target.py` 注释已记 | 09-26 | workbuddy-ai-0926 |

---

## 2. BUG —— 未修复 / 待定

| ID | 现象 | 当前认知 | 状态 | 来源 |
|---|---|---|---|---|
| BUG-017 | Ctrl+C 未产生 SIGINT、目标不退出（`test_ctrl_c_signal` 2 条断言失败） | 既存失败、非当次改动引起；已做 A/B（撤掉 BUG-014 的模式镜像后照样失败）。怀疑点是 `\x03` → `CTRL_C_EVENT` 的转换条件（与 `ENABLE_PROCESSED_INPUT` 的关系，对照 LIM-001） | **未修复，待查** | PHASES |
| BUG-027 | `test_resize_overlay_clean` 在 lifecycle 全量回归中**稳定失败**（3/3，非 flaky）：`line[5]` 含 4 个 `\|`，99/120/141 三帧叠画 | 与 BUG-012 同类残留，但 BUG-012 已修；已排除"被测产物未重建/路径未改动"。**未定论**：可能是 v1 用例的 UIA 读数偏差，也可能是真的残留叠画 | **待判定**。建议用 e2e_v2 的差分设施重写该用例来区分（差分能直接告出"注入屏幕 vs 原生屏幕"差在哪） | memory-0924 / 本轮 review |
| BUG-028 | `TriggerCtrlC` 用 `GenerateConsoleCtrlEvent(CTRL_C_EVENT, 0)`：组号 0 = **广播给共享该控制台的所有进程**（含目标 shell） | 探针加 Ctrl+C 后仍不崩 ⇒ 非 BUG-022 元凶，但**确实是真实隐患**：会误伤同控制台其它进程 | **未修**，建议改为定向而非广播 | workbuddy-ai-0926 |
| BUG-029 | 热路径上仍有若干 `__declspec(thread)`（`HookWhitelist::t_hookDepth`、`LazyInit::t_inLazyInit`、`CursorHooks::s_callCount`、`VtCursorTracker::s_feedLog`、`relay32::t_inDetour`） | 见 PIT-005；**未被证明**是 `0xc0000409` 的元凶（该假设已证伪撤销），但风险仍在 | 待评估（若要复用 `TlsSlot` 需挪到 `src/common/`） | workbuddy-ai-0926 / memory-0927 |
| BUG-030 | **被接管的后代自己开了鼠标模式，但新终端侧没启用鼠标上报 ⇒ 鼠标完全不可用**（用户现场：WT 里跑 `tests/live/textual/taskboard.py` → 劫持到新 WT → 鼠标键盘均不可用、画面正常刷新、约 10s 后程序自行退出回到 `PS C:\...>`） | `LazyInit` / `ApplyInitialMouseReport` 只看**握手时目标进程自己**的输入模式：本例目标是 pwsh，`inputMode=0x200`（只有 `ENABLE_VIRTUAL_TERMINAL_INPUT`，无 `ENABLE_MOUSE_INPUT=0x10`）⇒ 按 BUG-010 定下的判据判定"不要鼠标"，`wantMouse=0`。而真正要鼠标的是**被接管的后代**（Textual TUI 在注入**之前**就 `SetConsoleMode(MOUSE_INPUT)` ⇒ 注入后不会再触发 `ModeChange`）⇒ 新终端侧拿不到 `?1002h/?1006h` ⇒ 点击被 WT 当默认选择行为吃掉，TUI 收不到任何鼠标事件 | **未修**（修法需设计确认）：接管后代时把**后代的输入模式**也纳入鼠标上报判定（或在 Adopt 时查询后代 console mode 回灌 `ModeChange`） | `test_adopt_tui_input_alive` 断言终端侧应收到 `?1002h`（当前 FAIL，即该缺陷的回归用例）。日志证据：`Mouse: skip mouse re-enable (inputMode=0x200, need VT_INPUT\|MOUSE_INPUT)`、`ApplyInitialMouseReport: inputMode=0x200 wantMouse=0 enabled=0 changed=0` | 09-27（e2e_v2 复现） |
| BUG-031 | **卸载时 TUI 子进程还在跑 ⇒ 源 shell 崩溃 `0xc0000005`，且会话内容被重放回源终端**（用户现场：开 WT → 劫持到新 WT → 在**新** WT 里跑 `tests/live/textual/taskboard.py` → 退出 → 卸载回旧 WT → 随便敲点东西 → pwsh 崩，退出码 3221225477；现场画面里还能看到 TUI 的菜单文本） | **主因已定位（日志实证），崩溃那一步待确认**：`Unloader` 的重放判据只看**目标进程**（`isLineShell` / 是否已有活动提示符），不看**屏幕上当前真正在跑的是谁**。目标是 pwsh（行编辑 shell），但屏幕被子进程 TUI 占着 ⇒ 走到"整屏重放"分支，把 TUI 的整个会话画面写回目标控制台：日志 `Replay: no active line-shell prompt, replay full 2500 bytes` → `replayed 2368/2368 VT bytes to ConHost`。源终端因此出现 TUI 画面（与现场一致）。**崩溃**的下一步怀疑点：仍在跑的子进程（TUI）其线程还在被 hook 的 API 里，卸载却已走到 `Unload: helper process spawned` + `ready for remote FreeLibrary` ⇒ 与既有的 `injected.dll_unloaded`（执行已 FreeLibrary 的代码）签名吻合，**未最终确认**。与 BUG-025 同类：都是"重放/恢复的判据用目标，而屏幕/几何实际属于子进程" | 复现路径（v2）：`test_unload_tui_shell_alive` 场景 B —— pwsh 目标 + 新终端跑 taskboard + **不退出 TUI 直接卸载** ⇒ 稳定复现。同用例场景 A（Tab+q 让 TUI 真正退出后再卸载）**不崩** | 未修复。修法方向：卸载重放前判断"当前屏幕是否属于活跃子进程"（是则跳过整屏重放，或等子进程退出）；并复核活跃子进程仍在 Detour 内时的 FreeLibrary 时序（对照 BUG-029/PIT-005 的卸载前置条件） | 09-28（e2e_v2 复现 + 日志取证） |
| 待定-1 | termtest 256 色段"空行丢失"的另一面：conhost 加工过的流 vs 原样直通，屏幕必然不同 | 是否要在直通入口**模拟 conhost 对 CR/CSI 2K 的规范化**（=放弃部分"忠实直通"原则）？与"VtSgrFilter 剥离删除线"是同一类权衡 | **需拍板，不可擅自改**（记录为已知差异亦可） | memory-0925 |
| 待定-2 | 子进程空 Enter 时 LineEditor 回显 `\r\n`，使父进程 prompt 落到第 1 行（原生不写这行） | 疑为子进程运行时的退出写（`WriteConsoleInputW` 无日志，未确证） | 既有差异，非当次引入 | workbuddy-ai-0926 |

---

## 3. LIM —— 上游 / 架构限制（不是我们的 bug）

| ID | 限制 | 测试怎么处置 | 来源 |
|---|---|---|---|
| LIM-001 | `PROCESSED_INPUT` 清除后 Ctrl+C 仍中断目标：`\x03` 经 ConPTY 按共享输入模式无条件转 `CTRL_C_EVENT`（ConPTY 不按进程区分输入模式） | `test_processed_input` 的 GOT_CHAR 断言 SKIP | PHASES |
| LIM-002 | `PROCESSED_OUTPUT` 的 `\n`→CRLF 不适用：输出模式恒强制 VT_PROCESSING，WriteFile 字节原样直通 | 按 VT 直通语义断言 | PHASES |
| LIM-003 | `WRAP_AT_EOL` 不影响光标推进：ConPTY 侧视口模型下该标志不被尊重（DLL 侧已按标志改，但 ConPTY 恒 wrap） | `test_wrap_at_eol` 新增 WRAP_OFF 段按 ConPTY 实际语义断言 | PHASES |
| LIM-004 | VT_INPUT 输入直通未实现（标志缓存一致 + 通知 mediator，但编码不切换） | `test_vt_input_mode` 只验证 set/get + 通知 | PHASES |
| LIM-005 | WT ConPTY 不输出鼠标**移动**事件（三种驱动方式实测均无效）；按下期间移动按当前按钮状态重复输出 FR0 按下事件 | `test_drag_move` 的 MOVED 断言 SKIP，改断"拖拽按下态 HOLD + 释放坐标跟踪" | PHASES |
| LIM-006 | SGR 1006 无标准横滚编码：WT 发 `CSI <66/67;x;yM`，无法与垂直下滚区分 | `test_hwheel` 按"应出现 MOUSE_HWHEELED"断言（已按 baseBtn 2/3 识别） | PHASES |
| LIM-007 | ConPTY 托管的目标若**本身是全屏 TUI**，注入时按流式 shell 处理（BUG-013 的取舍）：`ESC[2J` 落在主屏可能偶发一次滚动条 | 已知取舍：宁可偶发滚动条，也不要普通 shell 永久空白 | PHASES |
| LIM-008 | Shift 修饰在 WT→ConPTY 文本流中丢失（WT 把按键折叠成大写字符，无法区分 Shift/CapsLock） | `test_modifier_keys` 改断大写字符 | PHASES（原列在 BUG-003） |

---

## 4. TRAP —— 排查陷阱（看着像 bug，其实不是）

| ID | 陷阱 | 实证 / 正确认知 | 来源 |
|---|---|---|---|
| TRAP-001 | 差分对比时"注入终端光标 ≠ 原生终端光标"被当成缺陷 | **光标归属不同**：原生归终端（reflow 重排它 → `(8,24)`）；注入归目标进程（DLL 同步它 → `(5,0)`）。文本与 scrollback 均一致时不是缺陷；正确对照物是目标自己的 `GetConsoleScreenBufferInfo` 光标 | 2026-09-27 e2e_v2 差分实证 |
| TRAP-002 | 用 `"ERROR" in 日志` 提前判"握手失败" | 子会话（relay32 中继、被接管后代）的正常报错也含 ERROR，会把成功握手提前判死。只认主握手的显式失败文案 | 2026-09-27 |
| TRAP-003 | 差分断言"偶发不一致" | 阶段之间必须 `wait_stable`：目标写日志/结果文件早于画屏，日志一到就进下一步 ⇒ 画屏字节还在路上，下一步 resize 作用在错状态上。实测同场景两次跑出 scrollback `32 / 0` | 2026-09-27 |
| TRAP-004 | 差分用例偶发判错 | 两条腿必须各自独立日志（走环境变量，不能进命令行 —— 命令行回显本身在屏幕上）；上一条腿残留进程写同一文件会污染 | 2026-09-27 |
| TRAP-005 | 屏幕断言用子串命中即算通过 | cmd 会把敲进去的命令行**回显**到屏幕上，`"TI_X" in text` 被回显行假命中；必须**整行相等** | 2026-09-27 |
| TRAP-006 | 按列数取 `snapshot()` 的列表下标 | 行内是"每**字符**一格"的稀疏列表，宽字符跳列（喂 `中文AB` 得到列号 `0,2,4,5`）；一律用 `cell[0]` | 2026-09-27 实测 |
| TRAP-007 | 在 ConPTY 里验"控制台模式位"类判据 | **必须在真 WT 下复验**：同一 termtest 在 pywezterm 的 ConPTY 里是 `0x3b0`、真 WT 里是 `0x200`（两套 ConHost 初始模式不同），只在 ConPTY 验会得出相反结论 | memory-0927 |
| TRAP-008 | 焦点依赖型用例在全量跑时"假失败" | 前台被抢导致 SendInput 打空。**归因办法：逐文件单跑 + 新旧 DLL 对照**，不要直接算自己的回归、也不要草率判为抖动 | memory-0927 |
| TRAP-009 | 断"输入是否到达"前没做未注入对照 | 实测踩坑：给 Textual TUI 发 `q` 指望它退出 → 不退出，误判"输入不通"，真因是焦点在过滤输入框里、可打印字符被控件吃掉。**先用未注入的对照组标定判据** | skill / `t_hijack_order_probe.py --calib` |

---

## 5. PIT —— 平台 / 环境 / 测试基建的坑

| ID | 坑 | 规避 |
|---|---|---|
| PIT-001 | 宿主 IDE 进程的环境块里 `PATH` / `Path` / `path` 三键并存 ⇒ MSBuild 抛 `MSB6001`；且三变体在本进程内删不掉 | 用 `build_clean.ps1`（`Start-Process -Environment` 传精简环境） |
| PIT-002 | 链接 `injected.dll` 偶发 `LNK1104`（杀软对新链接文件的瞬时扫描锁） | 等几秒重试 |
| PIT-003 | x64 进程**无法**向 32 位进程注入 `LoadLibraryW`（返回 0）；位数判定用 `GetBinaryTypeW`（OS 权威、与调用方位数无关），不要用 `IsWow64Process`，也不要手工解析 PE（PE32/PE32+ 偏移易错） | 32 位一环由 `relay32.dll` + `relay32inject.exe` 承担；x86 导出名需 `.def` |
| PIT-004 | **同步命名管道：挂起的 ReadFile 堵死同句柄的 WriteFile**（Windows 语义：一次只能一个 I/O 在飞）。症状是"卡住不报错"，阻塞时长 = 对端存活时长 | 接收侧一律改 `PeekNamedPipe` 轮询。既有正确实现：`ChildSession::RecvLoop`、`VtPassThrough::ForwardPipeToStdout`、relay32（BUG-018 后）。隔离复现：`tests/_probe/pipe_io_serialize_probe.py` |
| PIT-005 | `__declspec(thread)` 在**运行时注入**的 DLL 里是禁区：TLS 数组会随任何带静态 TLS 的 DLL 加载/卸载重新分配，MSVC 把基址缓存在栈上，失效后写入落到错误地址（可打穿 GS cookie → `0xC0000409`） | 热路径改用 `TlsAlloc/TlsGetValue` 槽位（见 BUG-029 / F-001） |
| PIT-006 | 命令行里出现 PowerShell 7 的可执行全名会被某些 shell 安全策略拦截 | 拆开拼接：`"".join(["p","w","s","h",".exe"])` |
| PIT-007 | 崩溃归因：别只看屏幕横幅 | ① WER：`Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='Application Error'}`（看 错误模块/异常码/偏移）；② 偏移→函数：`cdb -z injected.dll -y . -c "ln injected+0x<偏移>; q"`（PDB 须与崩溃版本同一次构建）；③ **先查历史 WER**：同一签名若早有，那是老问题不是本次回归 |
| PIT-008 | e2e runner 会"假 PASS"：`Summary` 默认 status 即 `PASS`，零输出退出的用例被判过 | e2e_v2 已修：默认 `ERROR`、必须打 `SUMMARY:` 行；只认**行首**的 `[FAIL]` 标记 |
| PIT-009 | 命令行 token 必须逐个加引号：`C:\Program Files\...\python.exe` 不带引号会被 cmd 按空格切开（`'C:\Program' 不是内部或外部命令`）；且别用 `shlex.split` 的 POSIX 规则处理 Windows 路径（反斜杠被当转义吃掉） | `pwcommon/session.py` 的 `parse_launcher()` / `build_command()` |
| PIT-010 | pywezterm 的 `key_down` 不认 `"ctrl-c"`，会当单字符返回 `b'c'`（**修饰键被静默丢掉、不报错**） | 自己解析 `ctrl-/alt-/shift-` 前缀翻成 mods 位（SHIFT=2, ALT=4, CTRL=8）；见 `pwcommon/session.py::parse_key` |

---

## 6. F —— 已证伪并撤销的假设（防重复劳动）

| ID | 假设 | 证伪依据 | 处置 |
|---|---|---|---|
| F-001 | `0xc0000409` 由 `thread_local` / TLS 缓存基址失效引起（`PromptTracker::tls_lastWriteOffset`） | 三重吻合的**间接**证据（GS cookie 期望值 vs 实收 `0xFFFF…FFFF`、py.exe 加载大量 DLL、"循环 2+ 才崩"）。**用户真机复验：崩溃依旧**（同样的 `coreclr.dll` 栈） | 已 `git revert 9dea9b7`，`TlsSlot.h` 删除，8 处 `thread_local` 恢复原样。**教训：没有可复现路径时，间接证据不足以支撑改动。** 真因是 BUG-022 |
| F-002 | termtest 空行丢失需要"裸 LF→CRLF 归一化"（commit `86a6254`） | 撤销后症状依旧修好（真因是 BUG-020 的行编辑回显）；注入流里裸 LF 后均紧跟绝对定位 CSI，列漂移不发生 | 按 AGENTS.md 撤销无效改动（连带撤销 `test_newline_normalize` / `test_processed_output` 对应断言） |
| F-003 | opentui prompt 重复源于 `LazyInit` 的 `promptOverwrite` 判据 | 字节级实证推翻：`conptyHosted=1` 不足以推出"不会重印 prompt"（cmd 在子进程退出后一定会重印）。真因是 mediator 把子进程应答回灌父进程 | 见 BUG-023 |
| F-004 | 用 `inputMode` 判 `conptyHosted` | 同一 pwsh 注入瞬间的 mode 随 PSReadLine 是否处于 raw 读循环而变（真 WT `0x1f7` / ConPTY `0x1e4`），依赖它会"有时修好有时还空" | 中途否掉，改用窗口类名判据（BUG-013） |

---

## 7. 相关文档

- `docs/phases/00-overview.md` ~ `22-conhost-replay.md`：各环节设计文档（多处引用本表编号）
- `docs/TECHNICAL.md`：架构、Hook、协议、卸载机制、known limitations
- `docs/2026-08-08-fix-report.md`：注入重放 / 卸载回放 / 会话期渲染 的一次性修复报告
- `docs/2026-09-23-child-injection-32bit-report.md`：py.exe 子进程断链的定位报告（对应 BUG-018 前半）
- `tests/e2e/docs/PHASES.md`：测试套件的阶段计划与特性矩阵（**已知问题已迁至本文档**）
- `tests/e2e_v2/README.md`：e2e_v2（pywezterm 化）的方案、迁移矩阵与差分诊断
