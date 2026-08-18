# terminal-injector 使用手册

> 详细使用方法：环境要求、构建、CLI 全参数参考、三种模式、典型场景、日志与调试、故障排查。
> 快速入门见根 [README.md](../README.md)，技术原理见 [TECHNICAL.md](TECHNICAL.md)。

---

## 1. 环境要求

| 项 | 要求 |
|----|------|
| 操作系统 | Windows 10 1809+（x64），需安装 Windows Terminal |
| 构建工具 | Visual Studio 18 Community（MSVC 14.51）、CMake ≥ 3.20 |
| 目标程序 | 任意 x64 控制台/TUI 程序（cmd、powershell、python、vim、opencode 等） |
| 权限 | 注入需对目标进程有 `PROCESS_CREATE_THREAD / PROCESS_VM_* / PROCESS_QUERY_INFORMATION` 权限；同用户普通进程可直接注入，跨用户/提权进程需管理员 |

> 注意：注入器与 DLL 均为 **x64**，目标进程必须是 x64（32 位进程不支持）。

---

## 2. 构建

```powershell
# 配置（首次或 CMake 变更后）
cmake -S . -B build -G "Visual Studio 18 2026" -A x64

# 全量构建（exe + dll）
./build.ps1

# 仅重建 DLL（修改 Hook 后快速迭代）
./build_dll.ps1
```

构建脚本通过 vswhere 自动定位 VS 安装目录，无需硬编码路径。

**产物**（`build/bin/Release/`）：

| 文件 | 说明 |
|------|------|
| `terminal_injector.exe` | 双模式入口（注入器 / 中介） |
| `injected.dll` | 注入到目标进程的 Hook DLL |

---

## 3. CLI 全参数参考

```
terminal_injector.exe --inject <pid> [--dll <path>] [--pipe <name>] [--mediator-pid <pid>]
terminal_injector.exe --mediator --target-pid <pid> [--pipe <name>] [--dll <path>]
terminal_injector.exe --list-targets [--json] [--all]
terminal_injector.exe --unload-remote <pid> <dllBase>
terminal_injector.exe --version
terminal_injector.exe --help
```

| 参数 | 说明 |
|------|------|
| `--inject <pid>` | 注入模式：将 injected.dll 注入目标进程并建立与中介的管道连接 |
| `--mediator` | 中介模式：作为 WT 子进程运行，桥接 WT 与目标进程（必需 `--target-pid`） |
| `--target-pid <pid>` | 目标进程 PID（mediator 模式必需） |
| `--dll <path>` | injected.dll 路径，默认取 exe 同目录的 `injected.dll` |
| `--pipe <name>` | 命名管道名；缺省自动生成随机名 `\\.\pipe\terminjector_<pid>_<hex>`（防同会话进程可预测抢占） |
| `--mediator-pid <pid>` | 期望的管道服务端进程 PID；DLL 连接后校验身份，不匹配则拒绝（0=跳过校验） |
| `--list-targets` | 列出可注入进程（权限 + x64 + 控制台程序判定） |
| `--json` | 与 `--list-targets` 搭配，输出 JSON 数组（pid/name/injectable/x64/console/already_injected/reason/start_time/cmd_line） |
| `--all` | 与 `--list-targets` 搭配，同时列出不可注入进程（附原因：access_denied / not_x64 / not_console） |
| `--unload-remote <pid> <dllBase>` | 远程卸载助手：在目标进程创建远程线程调 `FreeLibrary(dllBase)`（dllBase 支持 10/16 进制） |
| `--version` | 显示版本号 |
| `--help`, `-h` | 显示帮助 |

退出码：`0` = 成功；`1` = 参数错误 / 注入失败 / 卸载未完成。

---

## 4. 三种模式

### 4.1 注入模式（`--inject`）

```powershell
# 先启动目标程序并拿到 PID
# （PowerShell 里拿 PID： (Start-Process cmd).Id，或 Get-Process cmd | Select Id）

# 注入（管道名缺省随机生成）
terminal_injector.exe --inject 1234

# 指定 DLL 路径 / 指定管道名
terminal_injector.exe --inject 1234 --dll "<path>\injected.dll" --pipe mypipe
```

- 注入成功后 DLL 在目标进程内等待连接管道服务端（mediator）。
- **手动执行 `--inject` 且没有对应 mediator 时**，DLL 连接失败属预期——注入器只是把 DLL 放进进程，桥接由 mediator 完成。
- 注入器日志写入 `terminal-injector-inject-<pid>.log`（与 mediator 分文件，避免句柄互斥）。

### 4.2 中介模式（`--mediator`）

```powershell
# 由 Windows Terminal 启动；用 wt.exe 打开一个 Tab，在其内运行：
terminal_injector.exe --mediator --target-pid 1234
```

- mediator 是 **WT 的子进程**：从 stdin 读 WT 的 VT 输入 → 经管道发 DLL；收 DLL 的 VT 输出 → 写 stdout 交给 WT 渲染。
- `--mediator-pid` 自动取自身 PID 传给 DLL 做服务端身份校验（见 §6）。
- 管道名缺省为随机名，**不要**与手动 `--inject` 的管道名混用；如需固定管道，两侧传相同的 `--pipe`。

**典型端到端流程（手动）**：

```powershell
# 1. 启动目标（独立控制台，如 cmd）
Start-Process cmd

# 2. 打开 Windows Terminal 新 Tab，运行 mediator（指向目标 PID）
wt new-tab terminal_injector.exe --mediator --target-pid 1234

# 3. 在另一个 Tab 注入
terminal_injector.exe --inject 1234

# 现在可以在 WT 的 mediator Tab 中操作目标程序了
```

> 顺序说明：mediator 先启动等待连接、注入后 DLL 即握手，顺序可互换（任一先启动均可，DLL 侧有连接重试）。

### 4.3 远程卸载模式（`--unload-remote`）

```powershell
terminal_injector.exe --unload-remote 1234 0x7ffa00000000
```

- 由 DLL 的 Unloader 在管道断开时自动启动，一般**无需手动调用**。
- 原理：远程线程调 `FreeLibrary` 使 LoadCount 归 0，触发 `DLL_PROCESS_DETACH` 清理全部 Hook；配合 LDR flush（远程 `LoadLibraryW("kernel32.dll")`）强制卸载待清理模块。
- 日志写独立文件 `terminal-injector-unload.log`（避免与下一轮 mediator 并发互抢句柄）。

### 4.4 列出可注入进程（`--list-targets`）

```powershell
# 默认只列出可注入进程（PID + 进程名 + 状态）
terminal_injector.exe --list-targets
# 附带不可注入进程及原因（access_denied / not_x64 / not_console）
terminal_injector.exe --list-targets --all
# JSON 输出（供脚本化使用，默认同样只含可注入项）
terminal_injector.exe --list-targets --json
```

- 判定顺序：权限（OpenProcess 注入权限）→ x64 → PE Subsystem=CUI（控制台程序），任一不满足即标原因。
- 可注入项额外标记 `injectable (already injected)`（已注入过 injected.dll）与启动时间/命令行（`--json` 全字段）。
- 运行时会尝试提升 `SeDebugPrivilege`：管理员下能判定更多进程，否则系统进程显示 `access_denied`。

---

## 5. 典型场景

### 5.1 常规接管

见 §4.2 流程。关闭 WT 的 mediator Tab 后：管道断开 → DLL 自动解除全部 Hook → 目标进程恢复原生 Console 行为，**不污染目标进程**。

### 5.2 子进程自动接管

目标程序用 `CreateProcess` 启动的子进程（如 cmd 里再起 python、vim）会被 CreateProcess Hook 自动注入接管，无需逐个子进程手动注入。

### 5.3 多会话

每个会话独立成对：`inject + mediator + 管道名`。多开不同目标/多 Tab 互不干扰（管道名随机、mediator 按目标 PID 分日志文件）。

### 5.4 崩溃/异常后清理

- 若 WT 异常退出导致 DLL 未卸载：执行 `--unload-remote <pid> <dllBase>`（dllBase 可从 `%TEMP%\injected_<pid>_*.log` 或 `terminal-injector-<pid>.log` 中查到）。
- 目标进程被强杀：注入的 DLL 随进程消亡，无需清理。

---

## 6. 安全与权限

- **随机管道名**：`MakeRandomPipeName` 生成 `terminjector_<pid>_<hex>`，防止同会话进程预创建同名管道抢占（欺骗 DLL 连到攻击者服务端）。
- **服务端身份校验**：DLL 连接管道后校验服务端进程 PID == `--mediator-pid`（即 mediator 自身），不匹配立即断开。注入器创建 DLL 时下发该值。
- 注入需要目标进程句柄权限，跨用户/提权目标需管理员运行注入器。

---

## 7. 日志与调试

### 7.1 日志文件（全部默认 Debug 级别）

| 日志 | 路径 | 内容 |
|------|------|------|
| mediator | `<exe目录>\terminal-injector-<pid>.log` | 握手、VT 桥接、ChildVtOutput、OnModeChange、尺寸同步、VtOutput hex |
| 注入器 | `<exe目录>\terminal-injector-inject-<pid>.log` | 注入参数、RemotePipeSetup 返回码、注入结果 |
| 卸载助手 | `<exe目录>\terminal-injector-unload.log` | 远程 FreeLibrary / LDR flush / 模块卸载状态 |
| DLL | `<exe目录>\injected_<pid>_<时间戳>.log` | 目标进程内 Hook、状态缓存、翻译、批发送（每进程每会话独立文件） |

> DLL 日志中 `t=` 时间戳单位为**微秒**（RingBufferLogger `elapsedUs`）。

### 7.2 环境变量

| 变量 | 作用 |
|------|------|
| `TI_INJECTED_LOG_DIR` | 覆盖 DLL 日志目录（默认 exe 所在目录，即 injected.dll 所在目录） |
| `TI_LOG_LEVEL` | DLL 日志级别：`TRACE/DEBUG/INFO/WARN/ERROR/FATAL`（默认 `DEBUG`） |
| `TI_PROJECT_ROOT` | e2e 测试用：覆盖项目根目录（默认由 tests/e2e 路径推导） |
| `TI_CDB_TOOLS` | legacy 调试脚本用：cdb 工具目录 |

### 7.3 常见排查

| 现象 | 排查 |
|------|------|
| 注入失败 "Inject failed" | 看 `terminal-injector-inject-<pid>.log`：目标权限、DLL 路径、位数 |
| 握手超时 | 看 mediator 日志是否出现 `Handshake failed`；确认目标未被清理/未被强杀 |
| 目标程序无响应 | 可能 Hook 等待函数假句柄问题，看 DLL 日志 `injected_<pid>_*.log` |
| 关闭 Tab 后目标未恢复 | 执行 `--unload-remote` 手动卸载（见 §5.4） |

---

## 8. 自动化测试

e2e 套件（109 个测试，14 个类别）使用方式见 [tests/README.md](../tests/README.md)：

```powershell
cd tests/e2e
python run_all.py                        # 全量回归
python run_all.py --list                 # 列出全部测试
python run_all.py --cat keyboard         # 按类别
python run_all.py --file keyboard/test_modifier_keys.py  # 单文件
```

依赖：`build/bin/Release` 产物 + Python 3.8+ + `pywin32`、`psutil`（`pip install pywin32 psutil`）。
测试期间不要手动操作 WT 窗口（SendInput 驱动会被干扰）。

---

## 9. 已知限制

- 用户态劫持天花板：约 5% 行为与内核 ConPTY 有差异（如 `ENABLE_WRAP_AT_EOL` 不被 ConPTY 尊重），测试按实际语义断言。
- 中文输入法激活时 SendInput 注入的组合键可能被输入法吞掉（e2e 测试自动禁用目标窗口 IME）。
- 仅支持 x64 目标进程。
