# terminal-injector 测试

本目录包含两套测试：**`e2e/`**（现行端到端测试套件，日常使用）与 **`legacy/`**（早期阶段测试与调试脚本归档，仅调试参考）。

---

## 一、e2e 端到端测试套件（现行）

### 是什么

在真实链路上验证劫持效果：

```
目标 cmd ──注入──► injected.dll ──NamedPipe──► mediator ──ConPTY──► Windows Terminal
```

每项特性一个测试文件：自动化启动目标 + 注入 + WT、驱动输入/输出、断言结果文件与 mediator 日志字节、自动清理。测试不依赖手工操作，可全量回归。

### 运行前提

- 已构建产物：`<project>\build\bin\Release\`（`terminal_injector.exe` + `injected.dll`），见根 README「构建」
- Python 3.8+，依赖：`pywin32`（win32gui/win32con/win32api）、`psutil`
  ```powershell
  pip install pywin32 psutil
  ```
- 项目根目录解析：默认取 `tests/e2e` 的上上级；可用环境变量覆盖
  ```powershell
  $env:TI_PROJECT_ROOT = "C:\path\to\terminal-injector"
  ```
- 测试期间**不要手动操作 WT 窗口/鼠标**（SendInput 驱动会被干扰）

### 运行方式

在 `tests/e2e/` 目录下执行：

```powershell
python run_all.py                        # 全量回归（108 个测试）
python run_all.py --list                 # 列出全部测试文件
python run_all.py --cat mouse            # 运行指定类别（vt_output / keyboard / mouse / lifecycle ...）
python run_all.py --phase 8              # 按 PHASES.md 阶段运行（0-15）
python run_all.py --file lifecycle/test_self_protection.py   # 单文件
```

单个测试文件亦可独立运行（结果一致）：

```powershell
python lifecycle/test_self_protection.py
```

退出码：`0` = 全部 PASS（UNSUPPORTED 不算失败），`1` = 存在 FAIL，`2` = 参数/文件错误。

### 测试文件结构

每个测试文件 `e2e/<类别>/test_<特性>.py`，统一三段：

```python
"""特性: <名称>    类别: <类别>

链路: WT → mediator → DLL → 目标程序 ReadConsoleInputW
预期:
  - <断言 1>
  - <断言 2>
验证方式: 目标程序自检结果文件 + mediator 日志 VtOutput 字节
"""

def run() -> int:   # 返回失败数；0 = 全过
    ...

if __name__ == "__main__":
    sys.exit(run())
```

运行链路（`common/session.py` 封装）：

1. `injector.start_target_cmd()` 启动注入目标 cmd（独立控制台）
2. `injector.start_wt_mediator(pid)` 启动 WT 并在其中运行 mediator
3. `injector.wait_for_handshake()` 等 DLL 注入 + 握手完成
4. 在目标 cmd 中运行内嵌目标脚本（`python _targets/<name>.py`，脚本正文内嵌于测试文件）
5. 目标脚本用 Console API 自检 → 写入 `results/<name>.txt`（`KEY=VALUE` 协议）
6. runner 用 SendInput 驱动输入 + 轮询结果文件断言
7. `vt_capture.py` 解析 mediator 日志（`terminal-injector-<pid>.log`）验证 VT 字节流
8. `injector.cleanup()` 清理 cmd / mediator / WT（只清理本测试启动的窗口）

### 目录约定

```
e2e/
├── run_all.py            # 统一 runner（--list / --cat / --phase / --file / 全量）
├── common/               # 测试基建：session / result / target / paths / reporter
├── helpers/              # 复用 helpers：injector.py / input_sim.py / vt_capture.py
├── docs/PHASES.md        # 阶段实施计划、特性矩阵、已知问题清单（BUG/LIM 记录）
├── _targets/             # 运行时生成的目标脚本（勿手改，被测试文件正文覆盖）
├── results/              # 运行时结果文件 + summary.json（勿提交）
└── <类别>/               # vt_output / console_api / cursor_buffer / keyboard /
                          # line_editor / modes / vt_passthrough / mouse /
                          # special_sequences / codepage / width / scrollback /
                          # lifecycle / performance
```

类别与阶段映射见 `docs/PHASES.md`（如 Phase 8 = mouse、Phase 13 = lifecycle）。

### 三层验证方式

1. **目标程序自检**（主要）：目标脚本用 Console API 读写 + 查询虚拟状态，写结果文件
2. **mediator 日志字节**（输出侧）：`vt_capture.py` 解析 `pipe→stdout: VtOutput` 的 hex，验证 VT 序列到达链路
3. **UNSUPPORTED 探测**：OSC 52 / Sixel / Kitty 图形等 WT 不支持的特性，无响应则记 UNSUPPORTED，不算 FAIL

### 结果解读

- 结果文件协议：`PASS` / `FAIL:<原因>` / `UNSUPPORTED=<原因>` / `<KEY>=<值>` / `DONE=1`
- `results/summary.json`：全量汇总（PASS/FAIL/UNSUPPORTED 计数）
- DLL 日志：`<GetTempPathW()>\injected_<pid>_<时间戳>.log`（每进程每会话独立文件，
  路径经 `common/childlog.py` 定位；`TI_INJECTED_LOG_DIR` 环境变量可覆盖目录，
  `TI_LOG_LEVEL` 可调级别，默认 Debug）——目标进程私有，调试用
- 已知问题（架构限制 vs 工程 bug）记录在 `docs/PHASES.md` 末节：
  - `BUG-xxx`：工程缺陷（修复后测试恢复断言）
  - `LIM-xxx`：上游/架构限制（ConPTY、WT 行为），测试按实际语义断言或 SKIP

### UNSUPPORTED 清单（WT 不支持 → 探测记 UNSUPPORTED，不算 FAIL）

| 测试 | 特性 | 行为 |
|------|------|------|
| `osc_color_query` | OSC 11;? 背景色查询 | 4s 无响应 → UNSUPPORTED（当前 WT 响应 ST 终止符 → PASS） |
| `osc_clipboard` | OSC 52 剪贴板写 | 剪贴板读不到自己写的内容 → UNSUPPORTED（当前 WT 支持 → PASS） |
| `xtversion` | `CSI > 0 q` | 4s 无应答 → UNSUPPORTED（当前 WT 无应答） |
| `kitty_keyboard` | `CSI > 1 u` | 4s 无应答 → UNSUPPORTED（当前 WT 无应答） |

探测结果随时点变化（WT 版本），每轮以实际输出为准，不允许 FAIL。

### 新增测试

1. 在对应类别目录新建 `test_<特性>.py`，按「测试文件结构」写 docstring + `run()`
2. 目标脚本正文内嵌在测试文件（`TARGET_BODY` 字符串），经 `common/target.py` 运行时生成
3. 断言走 `session.wait_result()` + `input_sim` 驱动 + `vt_capture.MediatorLog` 字节匹配
4. 先单文件运行通过，再 `python run_all.py --cat <类别>` 确认无交叉影响

---

## 二、legacy 测试与调试脚本（归档）

早期阶段测试，**不作为回归套件**，仅排查历史问题时参考：

- `legacy/runners/`：按 Phase 划分的早期 e2e（test_phase8 ~ phase18），手工驱动较多
- `legacy/helpers/`：调试工具（cdb 附加/转储、注入诊断、卸载诊断、pty_agent 等）
- `legacy/manual/`：手工验证脚本（光标位置、ConHost 内容等）
- `legacy/targets/`：早期目标脚本

如需调试崩溃/卸载问题，`legacy/helpers/diag_*.py` 与 cdb 附加脚本仍可用（cdb 位于
`legacy/paths.cdb_tools()`，可用 `TI_CDB_TOOLS` 环境变量覆盖）。

---

## 三、快速上手

```powershell
# 1. 构建
./build.ps1

# 2. 冒烟验证（握手 + 输出链路）
cd tests/e2e
python vt_output/test_sgr_basic_colors.py

# 3. 局部回归（如改了输入 Hook）
python run_all.py --cat keyboard

# 4. 全量回归
python run_all.py
```
