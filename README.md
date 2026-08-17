# terminal-injector

[English](#english) · [中文](#中文)

---

## English

A DLL-injection terminal hijacker: takes over **already-running** console/TUI programs (cmd, powershell, python, vim, ...) into Windows Terminal (WT), making their I/O behavior match native WT ConPTY sessions (keyboard, mouse, VT sequences, resize, Ctrl+C, Alt Buffer, CJK double-width characters, scrollback, etc.).

```
┌──────────────────────────────── Windows Terminal ──────────────────────────────┐
│  WT renders VT output; converts keyboard/mouse into VT input                    │
└──────────────┬──────────────────────────────▲──────────────────────────────────┘
               │ stdin/stdout (VT byte stream) │
┌──────────────▼──────────────────────────────┴──────────────────────────────────┐
│  mediator (spawned by WT)                                                       │
│  stdin ↔ IPC bridging, VT input parsing, window-size monitoring                 │
└──────────────┬──────────────────────────────▲──────────────────────────────────┘
               │ Named Pipe (\\.\pipe\terminjector_<pid>)                          │
┌──────────────▼──────────────────────────────┴──────────────────────────────────┐
│  target process (cmd / opencode / vim ...)                                      │
│  injected.dll: hooks ALL Console APIs                                           │
│  output → translated to VT byte stream → sent via IPC                           │
│  input  ← VT received via IPC → translated to INPUT_RECORD / passthrough        │
│  state  → cached responses (fooling the target); unload → original APIs restored │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### Features

- **Output chain**: all output APIs (WriteConsole / WriteFile / WriteConsoleOutput / FillConsoleOutput / WriteConsoleOutputAttribute, ...) → VT sequence translation
- **Input chain**: ReadConsoleInput / ReadFile(CONIN$) / ReadConsole, ... ← VT input reverse-translated into INPUT_RECORD; VT input mode auto-detection and byte passthrough
- **Mode state machine**: full Get/SetConsoleMode flag set (incl. ENABLE_WINDOW_INPUT, ENABLE_WRAP_AT_EOL, ...) synced flag-by-flag
- **Cursor & buffers**: ScreenBufferInfo / cursor position / dual buffer (Alt Buffer) / bidirectional resize sync
- **Line editing**: ReadConsole line editing, Tab completion, history navigation, echo
- **Advanced sequences**: DSR/DA terminal attribute queries, OSC title/hyperlink/clipboard, SGR colors (incl. 256/truecolor), mouse event translation, focus events, synchronized output
- **Character width**: wcwidth integration, correct double-width advancement for CJK/Emoji
- **Scrollback**: scrollback line tracking, user buffer height preserved, reset on mode switch
- **Child process injection**: CreateProcess hook — child processes spawned by the target are auto-taken-over
- **Self-protection & clean unload**: Attach/Free/Alloc/GetConsoleWindow interception (isolates ConHost window operations); on pipe disconnect → all hooks removed → target resumes native console behavior

### Directory Structure

```
terminal-injector/
├── CMakeLists.txt              # top-level CMake (MSVC 14.51 / x64 / C++17)
├── cmake/                      # MSVCSetup / CompilerFlags / FindMinHook
├── build/                      # build artifacts (bin/Release: exe + dll + logs)
├── src/
│   ├── common/                 # logging / transport(NamedPipe) / protocol / console
│   ├── injector/               # CreateRemoteThread + LoadLibrary injection
│   ├── mediator/               # WT child process: VT bridge, size monitoring, child sessions
│   ├── dll/                    # injected.dll: hook management, state cache, translators, line editing, unload
│   │   ├── hooks/              # Output/Input/Cursor/Mode/Buffer/Wait/Protection/Signal/Process
│   │   ├── state/              # ConsoleState / VirtualConsoleState / StateSnapshot / HandleRegistry
│   │   ├── translator/         # ConsoleToVt / VtToInputRecord / Color / VtEscape
│   │   └── lineedit/           # LineEditor / TabCompleter
│   └── app/                    # terminal-injector.exe dual-mode entry point
├── tests/
│   ├── e2e/                    # end-to-end test suite (run_all.py + 14 categories, 109 test files)
│   │   ├── common/             # test infrastructure (injector / input_sim / vt_capture / paths)
│   │   ├── _targets/           # self-check scripts running inside target processes
│   │   └── docs/PHASES.md      # test suite design doc
│   └── legacy/                 # archived early-phase tests and debug scripts
├── docs/
│   ├── USAGE.md                # detailed usage manual (full CLI reference, scenarios, troubleshooting)
│   ├── TECHNICAL.md            # technical notes (architecture, mechanisms, protocol, limitations)
│   └── phases/                 # Phase 1–19 design docs
└── third_party/minhook/        # MinHook injection library
```

### Build

Requirements: Visual Studio 18 Community (MSVC 14.51), CMake ≥ 3.20, x64.

```powershell
# Configure (Visual Studio generator, x64)
cmake -S . -B build -G "Visual Studio 18 2026" -A x64

# Full build (exe + dll)
./build.ps1

# Rebuild DLL only (fast iteration after hook changes)
./build_dll.ps1
```

Artifacts: `build/bin/Release/terminal_injector.exe`, `build/bin/Release/injected.dll`.

### Usage

```powershell
# 1. Launch the target program first (any console program, e.g. cmd / python / vim), get its PID

# 2. Inject mode: inject injected.dll into the target process
terminal_injector.exe --inject <pid>

# 3. Mediator mode (spawned by WT): create the pipe, wait for the DLL to connect, bridge WT and target
terminal_injector.exe --mediator --target-pid <pid>

# 4. Remote unload (auto-invoked by the DLL on pipe disconnect; can also be manual)
terminal_injector.exe --unload-remote <pid> <dllBase>

# 5. List injectable processes (default: injectable only; add --all for reasons)
terminal_injector.exe --list-targets [--json] [--all]
```

| Argument | Description |
|----------|-------------|
| `--inject <pid>` | Inject mode; PID of the target process |
| `--mediator --target-pid <pid>` | Mediator mode; WT child process, default pipe `\\.\pipe\terminjector_<pid>` |
| `--dll <path>` | injected.dll path (default: same directory as the exe) |
| `--pipe <name>` | Custom pipe name |
| `--unload-remote <pid> <dllBase>` | Remote unload helper: FreeLibrary via remote thread |
| `--list-targets` | List injectable processes (permission + x64 + console checks), `--json` for JSON output, `--all` includes non-injectable with reasons |

After the WT tab is closed: pipe disconnects → the DLL automatically removes all hooks → the target resumes native console behavior (clean unload, no pollution).

### Testing

e2e suite (109 test files across 14 categories), requires the `build/bin/Release` artifacts + Python 3.8+ / pywin32 / psutil:

```powershell
cd tests/e2e
python run_all.py            # full regression
python run_all.py --list     # list all tests
python run_all.py --cat mouse        # run a category
python run_all.py --file keyboard/test_modifier_keys.py   # run a single test
python run_all.py --phase 6          # run by PHASES.md phase
```

Each test file can also run standalone: `python keyboard/test_modifier_keys.py`.
Results are aggregated into `tests/e2e/results/summary.json`; category mapping is in `tests/e2e/docs/PHASES.md`.

### Documentation

- [docs/USAGE.md](docs/USAGE.md): usage manual — full CLI reference, the three modes, typical scenarios, logging & debugging, troubleshooting
- [docs/TECHNICAL.md](docs/TECHNICAL.md): technical notes — architecture & data flow, hooks/state/translators/protocol, unload mechanism, known limitations
- [docs/phases/00-overview.md](docs/phases/00-overview.md): architecture overview, data flow, directory conventions, phase breakdown
- [docs/phases/01-scaffold.md](docs/phases/01-scaffold.md) ~ [19-vt-cursor-tracker.md](docs/phases/19-vt-cursor-tracker.md): per-phase design docs
- [tests/README.md](tests/README.md): e2e suite usage & extension
- [tests/e2e/docs/PHASES.md](tests/e2e/docs/PHASES.md): test suite design, feature matrix, known issues

### Known Limitations

- User-mode hijacking has a natural ceiling (~5% of behaviors differ from kernel-level ConPTY); typical differences are annotated by tests (e.g. `ENABLE_WRAP_AT_EOL` is not honored under ConPTY — tests assert against actual ConPTY semantics)

---

## 中文

# terminal-injector

DLL 注入式终端劫持器：将**已在运行**的任意控制台/TUI 程序（cmd、powershell、python、vim 等）接管到 Windows Terminal（WT）中，使其输入输出行为与 WT 原生 ConPTY 会话一致（键盘、鼠标、VT 序列、resize、Ctrl+C、Alt Buffer、CJK 双宽字符、滚动缓冲区等）。

```
┌─────────────────────────── Windows Terminal ───────────────────────────┐
│  WT 渲染 VT 输出；将键盘/鼠标转换为 VT 输入                              │
└──────────────┬──────────────────────────────▲──────────────────────────┘
               │ stdin/stdout (VT 字节流)      │
┌──────────────▼──────────────────────────────┴──────────────────────────┐
│  mediator（中介程序，由 WT 启动）                                        │
│  stdin ↔ IPC 桥接、VT 输入解析、窗口尺寸监听                              │
└──────────────┬──────────────────────────────▲──────────────────────────┘
               │ Named Pipe (\\.\pipe\terminjector_<pid>)                  │
┌──────────────▼──────────────────────────────┴──────────────────────────┐
│  目标进程（cmd / opencode / vim ...）                                   │
│  injected.dll：Hook 全部 Console API                                    │
│  输出 → 翻译为 VT 字节流 → IPC 发送                                     │
│  输入 ← IPC 收 VT → 翻译为 INPUT_RECORD / 直通                          │
│  状态 → 返回缓存（欺骗目标程序）；卸载 → 恢复原 API                       │
└────────────────────────────────────────────────────────────────────────┘
```

## 特性

- **输出链路**：WriteConsole / WriteFile / WriteConsoleOutput / FillConsoleOutput / WriteConsoleOutputAttribute 等全部输出 API → VT 序列翻译
- **输入链路**：ReadConsoleInput / ReadFile(CONIN$) / ReadConsole 等 ← VT 输入反翻译为 INPUT_RECORD；VT 输入模式自动检测与字节直通
- **模式状态机**：Get/SetConsoleMode 全标志（含 ENABLE_WINDOW_INPUT、ENABLE_WRAP_AT_EOL 等）逐项同步
- **光标与缓冲区**：ScreenBufferInfo / 光标位置 / 双缓冲（Alt Buffer）/ resize 双向同步
- **行编辑**：ReadConsole 行编辑、Tab 补全、历史导航、回显
- **高级序列**：DSR/DA 终端属性查询、OSC 标题/超链接/剪贴板、SGR 颜色（含 256/truecolor）、鼠标事件翻译、聚焦事件、同步输出
- **字符宽度**：wcwidth 集成，CJK/Emoji 双宽字符正确推进
- **滚动缓冲区**：回滚行数跟踪、用户缓冲区高度保留、模式切换重置
- **子进程注入**：CreateProcess Hook，目标程序创建的子进程自动接管
- **自保护与卸载**：Attach/Free/Alloc/GetConsoleWindow 拦截（隔离 ConHost 窗口操作）；管道断开 → 卸载全部 Hook → 目标进程恢复原生 Console 行为

## 目录结构

```
terminal-injector/
├── CMakeLists.txt              # 顶层 CMake（MSVC 14.51 / x64 / C++17）
├── cmake/                      # MSVCSetup / CompilerFlags / FindMinHook
├── build/                      # 构建产物（bin/Release 下 exe + dll + 日志）
├── src/
│   ├── common/                 # logging / transport(NamedPipe) / protocol / console
│   ├── injector/               # CreateRemoteThread + LoadLibrary 注入
│   ├── mediator/               # WT 子进程：VT 桥接、尺寸监听、子会话
│   ├── dll/                    # injected.dll：Hook 管理、状态缓存、翻译器、行编辑、卸载
│   │   ├── hooks/              # Output/Input/Cursor/Mode/Buffer/Wait/Protection/Signal/Process
│   │   ├── state/              # ConsoleState / VirtualConsoleState / StateSnapshot / HandleRegistry
│   │   ├── translator/         # ConsoleToVt / VtToInputRecord / Color / VtEscape
│   │   └── lineedit/           # LineEditor / TabCompleter
│   └── app/                    # terminal-injector.exe 双模式入口
├── tests/
│   ├── e2e/                    # 端到端测试套件（run_all.py + 14 类 109 个测试文件）
│   │   ├── common/             # 测试基建（injector / input_sim / vt_capture / paths）
│   │   ├── _targets/           # 目标进程内自检脚本
│   │   └── docs/PHASES.md      # 测试套件设计文档
│   └── legacy/                 # 早期阶段测试与调试脚本归档
├── docs/
│   ├── USAGE.md                # 详细使用手册（CLI 全参数、场景、故障排查）
│   ├── TECHNICAL.md            # 技术说明（架构、机制、协议、限制）
│   └── phases/                 # Phase 1–19 设计文档
└── third_party/minhook/        # MinHook 注入库
```

## 构建

依赖：Visual Studio 18 Community（MSVC 14.51）、CMake ≥ 3.20、x64。

```powershell
# 配置（Visual Studio 生成器，x64）
cmake -S . -B build -G "Visual Studio 18 2026" -A x64

# 全量构建（exe + dll）
./build.ps1

# 仅重建 DLL（修改 Hook 后快速迭代）
./build_dll.ps1
```

产物：`build/bin/Release/terminal_injector.exe`、`build/bin/Release/injected.dll`。

## 使用

```powershell
# 1. 先启动目标程序（任意控制台程序，如 cmd / python / vim），拿到 PID

# 2. 注入模式：将 injected.dll 注入目标进程
terminal_injector.exe --inject <pid>

# 3. 中介模式（由 WT 启动）：建立管道等待 DLL 连接，桥接 WT 与目标
terminal_injector.exe --mediator --target-pid <pid>

# 4. 远程卸载（管道断开后由 DLL 自动调用，也可手动）
terminal_injector.exe --unload-remote <pid> <dllBase>

# 5. 列出可注入进程（默认仅可注入项；加 --all 附原因）
terminal_injector.exe --list-targets [--json] [--all]
```

| 参数 | 说明 |
|------|------|
| `--inject <pid>` | 注入模式，指定目标进程 PID |
| `--mediator --target-pid <pid>` | 中介模式，WT 子进程，默认管道 `\\.\pipe\terminjector_<pid>` |
| `--dll <path>` | injected.dll 路径（默认 exe 同目录） |
| `--pipe <name>` | 自定义管道名 |
| `--unload-remote <pid> <dllBase>` | 远程卸载助手：远程线程 FreeLibrary |
| `--list-targets` | 列出可注入进程（权限 + x64 + 控制台判定），`--json` 输出 JSON，`--all` 附带不可注入原因 |

关闭 WT Tab 后：管道断开 → DLL 自动解除全部 Hook → 目标进程恢复原生控制台行为（干净卸载，不污染进程）。

## 测试

e2e 套件（109 个测试文件，覆盖 14 个类别），依赖 `build/bin/Release` 产物 + Python 3.8+ / pywin32 / psutil：

```powershell
cd tests/e2e
python run_all.py            # 全量回归
python run_all.py --list     # 列出全部测试
python run_all.py --cat mouse        # 运行指定类别
python run_all.py --file keyboard/test_modifier_keys.py   # 单个测试
python run_all.py --phase 6          # 按 PHASES.md 阶段运行
```

每个测试文件亦可独立运行：`python keyboard/test_modifier_keys.py`。
结果汇总写入 `tests/e2e/results/summary.json`，类别目录见 `tests/e2e/docs/PHASES.md`。

## 文档

- [docs/USAGE.md](docs/USAGE.md)：详细使用手册——CLI 全参数参考、三种模式、典型场景、日志与调试、故障排查
- [docs/TECHNICAL.md](docs/TECHNICAL.md)：技术说明——架构与数据流、Hook/状态/翻译器/协议、卸载机制、已知限制
- [docs/phases/00-overview.md](docs/phases/00-overview.md)：架构总览、数据流、目录规范、Phase 划分
- [docs/phases/01-scaffold.md](docs/phases/01-scaffold.md) ~ [19-vt-cursor-tracker.md](docs/phases/19-vt-cursor-tracker.md)：各 Phase 设计
- [tests/README.md](tests/README.md)：e2e 测试套件使用与扩展
- [tests/e2e/docs/PHASES.md](tests/e2e/docs/PHASES.md)：测试套件设计、特性矩阵、已知问题清单

## 已知限制

- 用户态劫持存在天然天花板（约 5% 行为与内核 ConPTY 有差异），典型差异已由测试标注（如 `ENABLE_WRAP_AT_EOL` 在 ConPTY 下不被尊重，测试按 ConPTY 实际语义断言）