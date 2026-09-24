# tests/_probe/ —— 只读排查探针（非回归测试）

这里的脚本都是**排查用**的：把"屏幕上到底发生了什么"量成可复读的数字/字节，
用来定位根因。它们**不进回归套件**（套件在 `tests/e2e/`，由 `run_all.py` 发现）。

约定：

- 探针**只读**、不改工程源码；只在必要时临时加诊断日志，跑完必须撤掉。
- 每个探针自带说明：背景、复现方法、判据（期望值/对照值）。
- **不内置个人路径**：需要外部路径的地方一律用环境变量，例如
  - `TI_PROJECT_ROOT`：项目根（多数探针会按文件位置自行推导）
  - `PWTERM_DIR` / `TI_PWTERM_SCRIPTS`：pywezterm 包/脚本目录
  - `TI_RUNPY`：要复现的交互脚本路径（`runpy_repro_probe.py` 用）

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
| `sgr_colon_filter_probe.py` | 真彩色冒号写法 `CSI 38:2::r:g:b` 在 DLL SGR 过滤器下的字节变化（现场错色 bug） |
| `timing.py` / `slack.py` | 注入窗口/时序测量（`py.exe`→`python.exe` 时间窗、首行输出 slack） |

`timing.py` / `slack.py` 依赖同目录的微型目标 `hello.py` / `firstout.py`。

渲染终端字节用 `pyte`（`pip install pyte`）；承载真 ConPTY 用 `pywezterm`
（可选，未安装时相关探针会提示并跳过）。
