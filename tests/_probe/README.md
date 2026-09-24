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

渲染终端字节用 `pyte`（`pip install pyte`）；承载真 ConPTY 用 `pywezterm`
（可选，未安装时相关探针会提示并跳过）。
