# tests/_probe/ —— 只读排查探针（非回归测试）

这里的脚本都是**排查用**的：把"屏幕上到底发生了什么"量成可复读的数字/字节，
用来定位根因。它们**不进回归套件**（套件在 `tests/e2e/`，由 `run_all.py` 发现）。

约定：

- 探针**只读**、不改工程源码；只在必要时临时加诊断日志，跑完必须撤掉。
- 每个探针自带说明：背景、复现方法、判据（期望值/对照值）。
- **不内置个人路径**：需要外部路径的地方一律用环境变量，未设置时探针应 SKIP 并给出提示，例如
  - `TI_PROJECT_ROOT`：项目根（多数探针会按文件位置自行推导）
  - `PWTERM_DIR` / `TI_PWTERM_SCRIPTS`：pywezterm 包/脚本目录
  - `TI_RUNPY`：要复现的交互脚本路径（`runpy_repro_probe.py` 用）
  - `TI_TUI_TARGET`：要复现的 TUI 目标（`t_textual_asyncio_freeze.py` / `t_shell_child_exit.py` 用）
  - `TI_TERMTEST_DIR`：termlib 包所在目录（`t_enterecho_consistency.py` 用）

## 常用探针

| 探针 | 用途 |
|---|---|
| `console_buffer_shape_probe.py` | 对照"经典 ConHost / ConPTY / WT"× "主屏/备用屏"六形态的控制台属性（缓冲/窗口、窗口类名等）—— BUG-013 判据依据 |
| `console_mode_freeze_probe.py` | 量父 shell 在"读键/执行命令"两态下目标控制台的真实输入模式（BUG-014：模式被冻结） |
| `child_input_probe.py` | 子会话交互输入逐层测量：回显、模式、`input()` 是否返回（原生 vs 注入对照） |
| `child_echo_position_probe.py` | 行编辑回显落在屏幕第几行第几列（用 pyte 还原） |
| `conpty_hosted_screen_probe.py` | ConPTY 托管目标注入后，目标终端的画面与光标（pyte 还原） |
| `runpy_repro_probe.py` | 直接跑 `TI_RUNPY` 指定的交互脚本，还原"滚动缓冲+可见屏幕"，量前导空格与空行区间 |
| `tracker_cursor_probe.py` | 用"标记 + 解析 `CursorSync`"读出 `VtCursorTracker` 的坐标，与 pyte 真值逐构造比对 |
| `launcher_chain_probe.py` | 跨位数启动链逐环断言（L1~L6） |
| `pipe_io_serialize_probe.py` | 同步命名管道"挂起读堵死同句柄写"的隔离复现 |
| `cross_inject.py` | x64 进程向 32 位进程注入 32 位 DLL 的可行性判定（决定是否必须另做 32 位注入器） |
| `direct_inject.py` | 最小变量隔离的 `CreateRemoteThread+LoadLibraryW` 注入（同位数/跨位数对照），并导出 `inject()` 供其他探针复用 |
| `inj_time.py` | 测量"注入一针"本身的耗时（远程线程退出即 Hook 就位，作为"轮询补注入"的硬下限） |
| `inject_suspended.py` | `--inject` 能否注入【挂起中】的 64 位进程（注入前后模块表对照） |
| `inject_suspended_pipe.py` | 挂起进程注入耗时 5.5s 的根因定位（缺管道服务端时 `Connect` 吃满超时） |
| `relay_bootstrap_probe.py` | 跨位数向【挂起的 32 位子进程】注入 32 位中继的路线判定（判据 A~E） |
| `relay_e2e.py` | 32 位中继 DLL 方案端到端（钩 `CreateProcessW` / 冻结 / 注入 / ack 四环节 + 铁证） |
| `suspended32_probe.py` | 挂起的 WOW64 进程能否枚举出 32 位模块表 |
| `mapping_base_probe.py` | 用 `VirtualQueryEx`+`GetMappedFileNameW` 取挂起进程内 DLL 基址（不依赖 KnownDLL 同址） |
| `t_sgr_colon_probe.py` | 真彩色冒号写法 `CSI 38:2::r:g:b` 端到端验证（真 ConPTY 承载 mediator + `pywezterm.Terminal` 解析对照）—— 冒号空保留位 bug 的回归证据 |
| `t_hijack_order_probe.py` | ★「先跑起 TUI 再劫持承载它的 shell」的输出/输入通路取证。A 组 target=shell（复现"画面定格 + 无法输入"）、B 组 target=TUI 自身（对照，正常）、C 组再放一个 GUI 型后代（验证它不被接管）。含 `--calib` 模式标定按键判据（taskboard 焦点在过滤框，可打印字符会上屏 → 用独有标记串判定输入到达） |
| `t_termtest_hijack_probe.py` | termtest(`run.py`) 在劫持下的屏幕形态与输入取证：ctrl（不注入）/ A（run.py 是注入前就存在的后代）/ C（注入后敲出来）。除查询探测、输入回显、空行/空格形态外，还回放**鼠标报文洪水**（复现 2026-09-27「不能输入」的候选机制）。注意：**涉及模式位的判据必须回真 WT 验**（同一 termtest 在 ConPTY 里 `0x3b0`、真 WT 里 `0x200`） |
| `t_readfile_gt2.py` / `t_echo_rules.py` | 测定 ConHost 下 `ReadFile(stdin)`（LINE+ECHO）的按键回显与返回字节规则：Enter → `\r\n`（空行也补）、Ctrl+C → `b""`（成功非 EOF）—— 2026-09-25 行编辑回显修复的判据 |
| `child_echo_position_probe.py`（见上表） | 行编辑回显落在屏幕第几行第几列，验证 `EmitLineEcho` 是否推进 `VtCursorTracker` |
| `t_enterecho_consistency.py` | 端到端对照：注入链跑 termlib 交互尾部，Enter 回显的空行是否与 ConHost 一致 —— 空行丢失修复的验收探针（需 `TI_TERMTEST_DIR` 指向 termlib 目录） |
| `pywezterm_smoke.py` | pywezterm 库自检：`Pty` 起进程 + `Terminal` 还原屏幕 + 断言标记，用于确认测试环境可用 |
| `t_textual_asyncio_freeze.py` | ★ 注入后跑 Textual TUI（需 `TI_TUI_TARGET` 指定脚本，如 taskboard.py）双轨对照：断言**画面仍在更新**且**鼠标生效**。2026-09-26「Detour 污染 `GetLastError` 致 asyncio 事件循环死亡、画面定格」修复的端到端判据 |
| `t_unload_tui_crash.py` | ★ 双轨（不跑 TUI / 跑过 TUI）跑完即卸载，断言**卸载后目标终端不再收到鼠标上报序列**且 shell 存活。2026-09-26「卸载重放把面向 WT 的鼠标序列写进共享 ConHost，致旧 WT 开启鼠标上报、目标 shell `0xc0000005`」修复的端到端判据（需 `TI_TUI_TARGET`） |
| `timing.py` / `slack.py` | 注入窗口/时序测量（`py.exe`→`python.exe` 时间窗、首行输出 slack） |

`timing.py` / `slack.py` 依赖同目录的微型目标 `hello.py` / `firstout.py`。

渲染终端字节用 `pyte`（`pip install pyte`）；承载真 ConPTY 用 `pywezterm`
（可选，未安装时相关探针会提示并跳过）。
