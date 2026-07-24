# Terminal-Injector 项目总览

> 本文档是 terminal-injector 项目的顶层规划文档，定义项目目标、架构决策、目录结构、Phase 划分与技术栈。
> 所有后续 Phase 文档（`01-*.md` ~ `11-*.md`）均以本文档为基线。

---

## 1. 项目目标

编写一个 C++ 程序（`terminal-injector.exe`），通过 **DLL 注入**方式劫持桌面上**已在运行**的任意控制台/TUI 程序，将其输入输出接管到由 Windows Terminal（WT）启动的**中介程序**上，使用户在 WT 中操作该程序时，交互体验与 WT 原生 ConPTY 创建的会话**完全一致**（键盘、鼠标、VT 序列、窗口缩放、Ctrl+C、Alt Buffer 等全部一致）。

### 核心指标

| 指标 | 目标 |
|------|------|
| 劫持后交互一致性 | ≥ 95% 等同 WT 原生创建（用户态极限） |
| 支持目标程序类型 | 任意控制台/TUI 程序（cmd/powershell/python/opencode/vim/less 等） |
| 输入延迟 | 鼠标/键盘事件端到端 < 50ms |
| 输出吞吐 | 满屏重绘 60fps 不撕裂 |
| 卸载干净度 | 管道断开后 Hook 全部解除，目标进程恢复原 Console 行为 |

---

## 2. 架构决策汇总

| 维度 | 决策 | 理由 |
|------|------|------|
| 整体架构 | DLL 注入目标进程 + 中介程序被 WT 启动 | 用户态可达 95% 一致性，符合"劫持已运行进程"需求 |
| 目标范围 | 任意控制台程序，Hook 全集，全量翻译 | 通用性优先，不限定特定 TUI |
| IPC 通道 | 命名管道起步，`ITransport` 抽象可切换共享内存 | 先简单可用，保留性能升级路径 |
| 构建系统 | CMake + MSVC 14.51 + x64 | 现代工程实践，cl 路径已知 |
| Hook 库 | MinHook | 开源免费、支持 x64、可在 DllMain 初始化 |
| 注入方式 | `CreateRemoteThread` + `LoadLibrary` | 经典稳定，够用 |
| 使用方式 | 统一程序双模式（`--inject <pid>` / `--mediator`） | 单一二进制，用户协调简单 |
| 日志策略 | `OutputDebugString` + 文件双路 | Hook 内不调被 Hook API，避免重入死锁 |
| 退出策略 | 管道断开 → 卸载 Hook → 恢复原 API | 干净退出，不污染目标进程 |
| C++ 标准 | C++17 | MSVC 14.51 完整支持，生态成熟 |
| 架构原则 | 干净架构，禁止 god 文件 | 遵循 AGENTS.md 强制规则 |

---

## 3. 系统架构与数据流

### 3.1 角色划分

```
┌─────────────────────────────────────────────────────────────────┐
│  Windows Terminal (wt.exe)                                       │
│  ┌───────────────────────────────────────────────┐              │
│  │  ConPTY (内核级伪终端)                         │              │
│  │  WT 渲染 VT 序列，发送键盘/鼠标 VT 输入        │              │
│  └───────────────────┬───────────────────────────┘              │
│                      │ stdin/stdout (VT 字节流)                  │
│  ┌───────────────────▼───────────────────────────┐              │
│  │  mediator.exe (中介程序，被 WT 启动)           │              │
│  │  - 从 stdin 读 WT 发来的 VT 输入 → IPC 发 DLL │              │
│  │  - 从 IPC 读 DLL 发来的 VT 输出 → 写 stdout   │              │
│  │  - 监听 WT 窗口尺寸变化 → 通知 DLL            │              │
│  └───────────────────┬───────────────────────────┘              │
└──────────────────────┼──────────────────────────────────────────┘
                       │ Named Pipe (\\.\pipe\terminjector_<pid>)
┌──────────────────────┼──────────────────────────────────────────┐
│  目标进程 (cmd.exe / opencode / vim ...)                         │
│  ┌───────────────────▼───────────────────────────┐              │
│  │  injected.dll (本项目 DLL)                     │              │
│  │  - Hook 全部 Console API                       │              │
│  │  - 输出类 API → 翻译成 VT 序列 → IPC 发中介    │              │
│  │  - 输入类 API ← IPC 收中介 VT ← 翻译成结构体   │              │
│  │  - 状态类 API → 返回缓存状态（欺骗目标程序）   │              │
│  │  - 自保护类 API → 拦截 Attach/Free/Alloc       │              │
│  └───────────────────────────────────────────────┘              │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 数据流方向

**输出流（目标 → WT）**：
```
目标程序调用 WriteConsoleW / WriteConsoleOutput / FillConsoleOutput / ...
  → DLL Hook 拦截参数
  → translator 模块将 Console API 调用翻译成 VT 序列字节流
  → ITransport (NamedPipe) 发送给中介
  → 中介写入自身 stdout
  → WT 的 ConPTY 渲染到屏幕
```

**输入流（WT → 目标）**：
```
用户在 WT 按键 / 点击 / 滚轮
  → WT 生成 VT 输入序列（如 \x1b[A 上箭头, \x1b[<0;10;20M 鼠标）
  → 中介从自身 stdin 读取
  → ITransport (NamedPipe) 发送给 DLL
  → DLL 根据目标程序当前 ConsoleMode 决定：
      - 若目标开启 ENABLE_VIRTUAL_TERMINAL_INPUT → 直接透传 VT 字节
      - 若目标为老式模式 → translator 将 VT 翻译成 INPUT_RECORD 结构
  → 目标程序的 ReadConsoleInput / ReadFile (CONIN$) Hook 返回伪造数据
```

**控制流（双向）**：
```
WT 窗口 resize
  → 中介检测到 stdout 的 CONSOLE_SCREEN_BUFFER_INFO 变化
  → IPC 通知 DLL 新尺寸
  → DLL 更新内部缓存，目标程序下次 GetConsoleScreenBufferInfo 拿到新值

DLL 状态变化（如目标调用 SetConsoleMode 切换 VT 模式）
  → IPC 通知中介当前模式
  → 中介据此决定输入翻译策略
```

---

## 4. 工程目录结构（干净架构）

遵循 AGENTS.md "采用干净架构，不准 god 文件" 规则，按职责分层，每个文件单一职责。

```
terminal-injector/
├── CMakeLists.txt                     # 顶层 CMake，定义项目与子模块
├── cmake/
│   ├── FindMinHook.cmake              # MinHook 查找/下载模块
│   ├── MSVCSetup.cmake                # cl.exe 路径配置 (VS18 / MSVC 14.51)
│   └── CompilerFlags.cmake            # 编译选项 (/W4 /utf-8 等)
│
├── docs/
│   ├── phases/                        # Phase 文档（本目录）
│   │   ├── 00-overview.md
│   │   ├── 01-scaffold.md
│   │   ├── 02-injector-modes.md
│   │   ├── 03-dll-framework.md
│   │   ├── 04-output-chain.md
│   │   ├── 05-cursor-buffer.md
│   │   ├── 06-input-chain.md
│   │   ├── 07-mode-signal.md
│   │   ├── 08-advanced-features.md
│   │   ├── 09-self-protection.md
│   │   ├── 10-state-sync-stability.md
│   │   ├── 11-unload-testing.md
│   │   └── 12-child-process-injection.md
│   └── architecture/                  # 架构设计文档（后续补充）
│
├── src/
│   ├── common/                        # 跨模块共享基础设施
│   │   ├── logging/
│   │   │   ├── Logger.h               # 日志接口
│   │   │   ├── Logger.cpp             # OutputDebugString + 文件双路实现
│   │   │   └── LogLevel.h             # 日志级别定义
│   │   ├── transport/
│   │   │   ├── ITransport.h           # IPC 传输抽象接口
│   │   │   ├── NamedPipeTransport.h   # 命名管道实现
│   │   │   ├── NamedPipeTransport.cpp
│   │   │   └── TransportFactory.h     # 工厂（后续支持 SharedMemory）
│   │   ├── protocol/
│   │   │   ├── Message.h              # DLL↔中介消息类型枚举
│   │   │   ├── MessageSerializer.h    # 序列化接口
│   │   │   ├── MessageSerializer.cpp  # 二进制序列化实现
│   │   │   └── PacketDefs.h           # 包头/魔数/版本定义
│   │   └── console/
│   │       ├── ConsoleTypes.h         # Console 结构体重定义（#pragma pack）
│   │       └── ConsoleConstants.h     # 模式标志/常量
│   │
│   ├── injector/                      # 注入器模块
│   │   ├── Injector.h
│   │   ├── Injector.cpp               # CreateRemoteThread + LoadLibrary
│   │   └── ProcessHelper.h            # OpenProcess/权限提升辅助
│   │
│   ├── mediator/                      # 中介程序模块
│   │   ├── Mediator.h
│   │   ├── Mediator.cpp               # 主循环：stdin↔IPC 桥接
│   │   ├── WtSizeWatcher.h            # WT 窗口尺寸监听线程
│   │   └── VtPassThrough.h            # VT 透传逻辑（中介侧）
│   │
│   ├── dll/                           # 注入到目标进程的 DLL
│   │   ├── dllmain.cpp                # DLL 入口（极简，仅初始化 Hook）
│   │   ├── HookManager.h              # Hook 生命周期管理
│   │   ├── HookManager.cpp            # MH_Initialize/CreateHook/EnableHook
│   │   ├── state/
│   │   │   ├── ConsoleState.h         # 状态缓存（Mode/Cursor/Buffer/CP...）
│   │   │   ├── ConsoleState.cpp
│   │   │   ├── StateSnapshot.h        # 注入瞬间快照
│   │   │   └── StateSnapshot.cpp
│   │   ├── translator/
│   │   │   ├── ConsoleToVt.h          # Console API → VT 序列翻译
│   │   │   ├── ConsoleToVt.cpp
│   │   │   ├── VtToInputRecord.h      # VT 输入 → INPUT_RECORD 翻译
│   │   │   ├── VtToInputRecord.cpp
│   │   │   └── VtEscape.h             # VT 转义序列常量与构造工具
│   │   └── hooks/                     # Hook 实现，按类别分文件（禁止 god 文件）
│   │       ├── OutputHooks.h          # 输出类 Hook 声明
│   │       ├── OutputHooks.cpp        # WriteConsole/WriteFile/WriteConsoleOutput...
│   │       ├── InputHooks.h           # 输入类 Hook 声明
│   │       ├── InputHooks.cpp         # ReadConsoleInput/Peek/ReadFile(CONIN$)...
│   │       ├── CursorHooks.h          # 光标类 Hook 声明
│   │       ├── CursorHooks.cpp        # SetCursorPosition/GetScreenBufferInfo...
│   │       ├── ModeHooks.h            # 模式类 Hook 声明
│   │       ├── ModeHooks.cpp          # Get/SetConsoleMode/CP/Title...
│   │       ├── BufferHooks.h          # 缓冲区类 Hook 声明
│   │       ├── BufferHooks.cpp        # SetActiveScreenBuffer/ScreenBufferSize...
│   │       ├── WaitHooks.h            # 等待类 Hook 声明
│   │       ├── WaitHooks.cpp          # WaitForSingleObject/MultipleObjects...
│   │       └── ProtectionHooks.h      # 自保护 Hook 声明
│   │       └── ProtectionHooks.cpp    # Attach/Free/Alloc/GetStdHandle/CloseHandle
│   │
│   └── app/                           # terminal-injector.exe 双模式入口
│       └── main.cpp                   # 参数解析：--inject <pid> / --mediator
│
├── tests/                             # 多目标测试
│   ├── targets/                       # 测试用目标程序脚本
│   │   ├── test_cmd.bat
│   │   ├── test_powershell.ps1
│   │   ├── test_python_tui.py
│   │   └── test_vim.sh
│   ├── runners/                       # 自动化测试脚本
│   │   └── run_all.py
│   └── manual/                        # 手动测试清单
│       └── checklist.md
│
└── third_party/
    └── minhook/                       # MinHook 源码（镜像下载）
```

### 4.1 模块依赖关系（单向，禁止环依赖）

```
app  ──►  injector
   ──►  mediator
   ──►  common (logging/transport/protocol/console)

dll  ──►  common (logging/transport/protocol/console)
    ──►  minhook (third_party)
    ──►  内部: hooks ──► state ──► translator ──► transport
```

`common` 是最底层，不依赖任何业务模块。`dll` 和 `app`/`injector`/`mediator` 互不依赖（DLL 编译为独立模块，运行时才注入）。

---

## 5. Phase 划分总览

共 12 个 Phase，每个 Phase 有明确的交付物、验证标准与依赖关系。详细文档见对应 `0X-*.md`。

> **注意**：Phase 12（子进程注入）在实际开发顺序中位于 Phase 5 之后、Phase 6 之前。编号为 12 仅是为了不打乱已有 Phase 6-11 的编号。

| Phase | 名称 | 核心交付 | 前置依赖 |
|-------|------|----------|----------|
| 1 | 工程脚手架与依赖准备 | CMake 工程、MinHook 集成、Logger、ITransport+NamedPipe、Protocol | 无 |
| 2 | 注入器 + 双模式入口 | `terminal-injector.exe` 双模式、CreateRemoteThread 注入、握手协议 | Phase 1 |
| 3 | DLL 核心 Hook 框架 + 状态快照 | DllMain、HookManager、状态快照、管道连接、单个 Hook 验证（WriteConsoleW） | Phase 2 |
| 4 | 输出链路（Console API → VT 翻译） | 全部输出类 Hook + ConsoleToVt 翻译器 | Phase 3 |
| 5 | 光标与缓冲区信息 | 光标/Buffer Info 类 Hook + resize 双向同步 | Phase 4 |
| 12 | 子进程注入（CreateProcess Hook） | CreateProcessW/A Hook、自动注入子进程、mediator 多管道实例 | Phase 5 |
| 6 | 输入链路（VT → INPUT_RECORD） | 输入类 Hook + VtToInputRecord 翻译器 + 鼠标双向翻译 | Phase 5, 12 |
| 7 | 模式与信号 | Get/SetConsoleMode 状态机、Ctrl+C 信号传递 | Phase 6 |
| 8 | 高级特性 | Alt Buffer、ConsoleTitle、ConsoleCP、ConsoleFont、Wait 句柄假映射 | Phase 7 |
| 9 | 自保护 | Attach/Free/Alloc/GetStdHandle/CloseHandle Hook | Phase 8 |
| 10 | 状态同步优化与稳定性 | 后台轮询线程、鼠标攒批、死锁防护、性能调优 | Phase 9 |
| 11 | 卸载清理与多目标测试 | 管道断开卸载、cmd/powershell/python/opencode/vim 全量测试 | Phase 10 |

### 5.1 Phase 依赖图

```
Phase 1 (脚手架)
   │
   ▼
Phase 2 (注入器)
   │
   ▼
Phase 3 (DLL框架) ──► 首次端到端验证：WriteConsoleW 劫持 cmd 输出
   │
   ├──────────────► Phase 4 (输出链路)
   │                   │
   │                   ▼
   │                Phase 5 (光标/Buffer)
   │                   │
   │                   ▼
   │                Phase 12 (子进程注入) ──► 子进程输出/输入经过 Hook 链路
   │                   │
   │                   ▼
   │                Phase 6 (输入链路) ──► 键盘鼠标可用
   │                   │
   │                   ▼
   │                Phase 7 (模式/信号) ──► Ctrl+C/VT模式可用
   │                   │
   │                   ▼
   │                Phase 8 (高级特性) ──► Vim/Alt Buffer 可用
   │                   │
   │                   ▼
   │                Phase 9 (自保护) ──► 防越狱
   │                   │
   │                   ▼
   │                Phase 10 (同步优化) ──► 性能达标
   │                   │
   │                   ▼
   │                Phase 11 (卸载/测试) ──► 全量验收
   │
   └─► 每个 Phase 完成后回归测试前序功能
```

---

## 6. 技术栈与依赖

### 6.1 编译工具链

- **编译器**：MSVC 14.51（cl.exe 路径：`C:\Program Files\Microsoft Visual Studio\18\Community\VC\Tools\MSVC\14.51.36231\bin`）
- **构建系统**：CMake ≥ 3.20
- **目标架构**：x64（Phase 1 仅编 x64，后续如需 x86 再扩展）
- **C++ 标准**：C++17
- **Windows SDK**：10（ConPTY/Console API）

### 6.2 第三方依赖

| 依赖 | 版本 | 用途 | 获取方式 |
|------|------|------|----------|
| MinHook | 最新稳定版 | Inline Hook 库 | GitHub 镜像下载源码，放入 `third_party/minhook/` |

**MinHook 下载方式**（遵循 download-by-mirror 规则，不修改全局 git 配置）：
```powershell
# 下载源码 zip（镜像）
Invoke-WebRequest -Uri "https://v4.gh-proxy.org/https://github.com/TsudaKageyu/minhook/archive/refs/heads/master.zip" -OutFile "minhook.zip"
# 或 git clone 走镜像（仅项目级临时配置）
$env:GIT_CONFIG_COUNT = "1"
$env:GIT_CONFIG_KEY_0 = "url.https://v4.gh-proxy.org/https://github.com/.insteadOf"
$env:GIT_CONFIG_VALUE_0 = "https://github.com/"
git clone https://github.com/TsudaKageyu/minhook.git third_party/minhook
Remove-Item Env:GIT_CONFIG_COUNT, Env:GIT_CONFIG_KEY_0, Env:GIT_CONFIG_VALUE_0
```

### 6.3 Windows API 依赖

- `kernel32.dll`：Console API、进程/线程、文件
- `user32.dll`：窗口枚举（可选，用于查找目标窗口）
- `dbghelp.dll`：符号解析（调试用，可选）

所有 Console API 通过 `windows.h` + `<windowsconsole.h>` 引用，Hook 时需同时处理 ANSI (A) 和 Unicode (W) 两个版本。

---

## 7. 验证策略

遵循 AGENTS.md "测试要覆盖功能与用户舒适度/流畅度" 要求，采用**多目标程序测试**。

### 7.1 测试目标矩阵

| 目标程序 | 验证重点 | 对应 Phase |
|----------|----------|-----------|
| cmd.exe | 基础输出、VT 颜色、resize | 3, 4, 5 |
| powershell.exe | VT 序列、进度条、Tab 补全 | 4, 5, 6 |
| python (REPL) | VT 颜色、键盘交互 | 4, 6 |
| python TUI (curses/textual) | 全屏重绘、鼠标、Alt Buffer | 6, 8 |
| opencode (Go TUI) | 复杂 TUI、流式输出、鼠标、分屏 | 6, 8, 10 |
| vim | Alt Buffer、光标、鼠标、Ctrl+C | 5, 6, 7, 8 |
| less | Alt Buffer、滚轮、q 退出 | 8 |

### 7.2 测试维度

每个目标程序验证以下维度：

1. **输出正确性**：文本、颜色、光标位置、滚屏
2. **输入响应**：键盘、组合键、Tab、方向键
3. **鼠标交互**：点击、滚轮、拖拽选择、双击
4. **窗口缩放**：拖动 WT 边框，目标程序重绘正确
5. **信号处理**：Ctrl+C 中断、Ctrl+Break
6. **Alt Buffer**：vim 进入/退出后屏幕恢复
7. **中文/Unicode**：UTF-8 输出、chcp 65001 切换
8. **卸载清理**：关闭 WT tab 后目标进程恢复
9. **性能**：满屏输出无撕裂、鼠标无延迟

### 7.3 测试方式

- **手动测试清单**：`tests/manual/checklist.md`（每个功能点勾选验证）
- **自动化脚本**：`tests/runners/run_all.py`（启动目标程序 + 模拟输入 + 对比输出）
- **Python TUI 辅助**：用 Python 写特定 TUI 场景触发边界条件

---

## 8. 关键风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| Hook 内调用被 Hook API 导致重入死锁 | 进程卡死 | Logger 仅用 OutputDebugString + 独立文件句柄，禁用任何 Console API |
| Loader Lock 死锁（DllMain 干重活） | DLL 加载失败 | DllMain 仅 DisableThreadLibraryCalls + MH_Initialize，其余懒加载 |
| 状态快照不完整 | 劫持后界面错乱 | 注入瞬间读取全部 Console 状态 + 后台轮询线程补全 |
| 鼠标高频事件 IPC 性能崩 | CPU 100% / 延迟 | DLL 内攒批（16ms 或 20 事件）后打包发送 |
| WaitForSingleObject 假句柄 | 程序假死 | Hook 等待函数，映射到手动重置事件 |
| 用户态 vs 内核 ConPTY 差异 | 5% 不可达 | 接受极限，文档说明用户态劫持的天花板 |

---

## 9. 后续文档索引

- [Phase 1: 工程脚手架与依赖准备](01-scaffold.md)
- [Phase 2: 注入器 + 双模式入口](02-injector-modes.md)
- [Phase 3: DLL 核心 Hook 框架 + 状态快照](03-dll-framework.md)
- [Phase 4: 输出链路](04-output-chain.md)（待写）
- [Phase 5: 光标与缓冲区信息](05-cursor-buffer.md)（待写）
- [Phase 6: 输入链路](06-input-chain.md)（待写）
- [Phase 7: 模式与信号](07-mode-signal.md)（待写）
- [Phase 8: 高级特性](08-advanced-features.md)（待写）
- [Phase 9: 自保护](09-self-protection.md)（待写）
- [Phase 10: 状态同步优化与稳定性](10-state-sync-stability.md)（待写）
- [Phase 11: 卸载清理与多目标测试](11-unload-testing.md)（待写）
- [Phase 12: 子进程注入（CreateProcess Hook）](12-child-process-injection.md)
