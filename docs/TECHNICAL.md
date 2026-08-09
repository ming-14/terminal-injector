# terminal-injector 技术说明

> 架构、数据流、关键机制与设计取舍。面向阅读源码或修改本项目的开发者。
> 使用层面见 [USAGE.md](USAGE.md)，各 Phase 详细设计见 [phases/](phases/00-overview.md)。

---

## 1. 系统架构

```
┌─────────────────────────── Windows Terminal ───────────────────────────┐
│  WT 渲染 VT 输出；将键盘/鼠标转换为 VT 输入                              │
└──────────────┬──────────────────────────────▲──────────────────────────┘
               │ stdin/stdout (VT 字节流)      │
┌──────────────▼──────────────────────────────┴──────────────────────────┐
│  terminal_injector.exe --mediator（由 WT 启动）                          │
│  - stdin ← WT 输入，桥接为管道消息                                      │
│  - 管道消息 → 写 stdout（VT 字节给 WT 渲染）                             │
│  - WtSizeWatcher 监听窗口尺寸；VtParser 解析 DSR/DA 应答                 │
└──────────────┬──────────────────────────────▲──────────────────────────┘
               │ Named Pipe (\\.\pipe\terminjector_<pid>_<hex>)            │
┌──────────────▼──────────────────────────────┴──────────────────────────┐
│  目标进程（cmd / python / vim ...，含自动注入的子进程）                   │
│  injected.dll：                                                            │
│  - MinHook 劫持全部 Console API（输出/输入/状态/模式/缓冲/信号/进程/保护）│
│  - 输出 → ConsoleToVt 翻译 → 批发送 → 管道                                │
│  - 输入 ← VtToInputRecord / 直通 → 伪造 INPUT_RECORD                     │
│  - 状态 → ConsoleState 缓存欺骗；VirtualConsoleState 与 WT 双向同步       │
└──────────────────────────────────────────────────────────────────────────┘
```

## 2. 数据流

**输出流（目标 → WT）**：

```
WriteConsoleW / WriteConsoleOutput / FillConsoleOutput / WriteConsoleOutputAttribute ...
  → Hook 拦截 → ConsoleToVt 翻译为 VT 序列
  → BatchSender 攒批（约 16ms 或阈值）→ NamedPipe 发送
  → mediator 收消息 → 写 stdout → WT ConPTY 渲染
```

**输入流（WT → 目标）**：

```
用户在 WT 按键/点击/滚轮 → WT 生成 VT 序列（\x1b[A、\x1b[<0;10;20M ...）
  → mediator 从 stdin 读取 → VtInput 消息 → DLL
  → DLL 按目标程序当前 ConsoleMode 分流：
      ENABLE_VIRTUAL_TERMINAL_INPUT 开 → 字节直通（透传）
      老式模式 → VtToInputRecord 翻译为 INPUT_RECORD 伪造给 ReadConsoleInput/ReadFile
```

**控制流（双向）**：resize（中介→DLL）、模式/代码页切换（DLL→中介）、DSR/DA 终端查询（中介应答→DLL 反向同步虚拟状态）。

## 3. 模块划分（干净架构）

依赖只能由外层指向内层，禁止环：

```
app（双模式入口）
 ├──► injector（CreateRemoteThread + LoadLibrary 注入）
 ├──► mediator（VT 桥接、子会话管理）
 └──► common（logging / transport / protocol / console / remote）

injected_dll（运行时独立编译注入）
 ├──► common
 ├──► minhook（third_party）
 └──► hooks ─► state ─► translator ─► transport
```

| 模块 | 职责 |
|------|------|
| `src/app` | `main.cpp`：手写参数解析、日志文件按模式分流（mediator/inject/unload 分文件） |
| `src/injector` | 打开目标进程 → 注入 DLL → 下发管道参数 → 等 DLL 连接握手 |
| `src/mediator` | stdin↔管道桥接、`ChildSession`（子进程会话）、`WtSizeWatcher`（尺寸）、`VtParser`（DSR/DA/应答）、`VtPassThrough`（输出直通） |
| `src/dll` | Hook 全集 + 状态缓存 + 翻译器 + 行编辑 + 卸载 |
| `src/common` | 不依赖任何业务模块的基础设施 |

## 4. 注入机制

1. `OpenProcess` 目标进程（需 `PROCESS_CREATE_THREAD | PROCESS_VM_* | PROCESS_QUERY_INFORMATION`）。
2. `VirtualAllocEx` 分配参数块（`PipeParams`：管道名、mediator 身份 PID、DLL 路径等）。
3. `CreateRemoteThread` + `LoadLibraryW(injected.dll)`。
4. DLL 的 `LazyInit` 在连接线程中读取远程参数、建立管道客户端、发送 `Hello`（含目标 PID、初始 Console 状态快照、dllBase）。
5. mediator 回 `HelloAck`（WT 侧尺寸 + **ConPTY 当前光标**）；DLL 用 ConPTY 光标覆盖本地缓存，使坐标系统一对齐。
6. 安全：管道名随机生成（`terminjector_<pid>_<hex>`），DLL 连接后校验服务端进程 PID 等于注入参数中的 mediator PID。

**RemoteCall 框架**（`src/common/remote`）：注入器在目标进程内执行远程函数调用的封装（参数经远程内存传递、远程线程退出码回读），用于注入后置步骤（如 `RemotePipeSetup`、LDR flush 等）。

**KickStart**：注入目标进程（非子进程）注入前可能已阻塞在旧 `ReadConsoleW`，握手后需 KickStart 唤醒使其改走 Hook 链路；子进程由父进程 CreateProcess 创建、Hook 已就位，禁止 KickStart（否则 ENTER 残留队列被误读）——由 `HelloAckPayload.isTarget` 区分。

## 5. Hook 体系

- **库**：MinHook（inline hook，支持 x64，可在 DllMain 初始化）。
- **HookManager**：统一管理 Hook 的创建/启用/禁用/恢复，卸载时全部 `MH_DisableHook` + `MH_RemoveHook`。
- **按类别分文件**（禁止 god 文件）：

| 文件 | 覆盖 API |
|------|----------|
| `OutputHooks` | WriteConsoleA/W、WriteFile（CONOUT$）、WriteConsoleOutput(A/W)、FillConsoleOutput(A/W)、WriteConsoleOutputAttribute、SetConsoleCursorPosition(Attribute)、ScrollConsoleScreenBuffer 等 |
| `InputHooks` | ReadConsoleInputW/A、PeekConsoleInputW/A、ReadConsoleW/A、ReadFile（CONIN$）、GetNumberOfConsoleInputEvents 等 |
| `CursorHooks` | GetConsoleScreenBufferInfo、Get/SetConsoleCursorPosition、SetConsoleCursorInfo 等 |
| `ModeHooks` | Get/SetConsoleMode、Get/SetConsoleCP、Get/SetConsoleTitle 等 |
| `BufferHooks` | SetActiveScreenBuffer（Alt Buffer）、SetConsoleScreenBufferSize、SetConsoleWindowInfo 等 |
| `SignalHooks` | SetConsoleCtrlHandler、GenerateConsoleCtrlEvent（Ctrl+C/Ctrl+Break 传递） |
| `ProcessHooks` | CreateProcessW/A（子进程自动注入）、CreateProcessAsUser 等 |
| `ProtectionHooks` | AttachConsole、FreeConsole、AllocConsole、GetConsoleWindow、CloseHandle（假句柄拦截） |
| `WaitHooks` | WaitForSingleObject/Ex、WaitForMultipleObjects（假句柄 → 手动重置事件映射，防假死） |
| `FontHooks` | SetConsoleFont 等字体相关 |
| `HookWhitelist` | 受保护 API 白名单（防止对自身/关键函数误 hook） |

## 6. 状态管理（`src/dll/state`）

| 组件 | 职责 |
|------|------|
| `ConsoleState` | 目标程序视角的 Console 状态缓存：Mode、光标、缓冲区尺寸、代码页、标题、窗口信息。所有状态类 Get 接口从此返回（欺骗目标程序），Set 接口同步更新并上报 |
| `StateSnapshot` | 注入瞬间读取目标进程全部 Console 状态填入 `Hello`，保证接管后界面不闪变 |
| `StatePoller` | 后台轮询线程补全状态（注入后一段时间内的状态差异） |
| `VirtualConsoleState` | WT/ConPTY 侧真实状态：经 `WtStateReport`（resize / DSR CPR / DA 应答）反向同步，供翻译器校正 VT 序列（光标对齐、滚动等） |
| `VtCursorTracker` | 解析 VT 直通字节流维护**语义光标**：直通模式下输出不逐条翻译，但光标位置必须跟踪，否则"输出后查询光标/重定位"类行为错误 |
| `HandleRegistry` | 假句柄映射表：等待类 Hook 将伪句柄映射为手动重置事件 |
| `InputQueue` | 伪造输入队列的缓冲（ReadConsoleInput 的返回数据） |

## 7. 翻译器（`src/dll/translator`）

| 组件 | 职责 |
|------|------|
| `ConsoleToVt` | Console API 调用 → VT 序列（SGR 颜色、光标定位、擦除、滚动、Alt Buffer 切换 `?1049h/l`） |
| `Color` | Windows 颜色位序（bit0=蓝 bit1=绿 bit2=红）→ VT 索引重映射（BUG-001：0x4 修复前被译为 34 蓝，应为 31 红） |
| `VtToInputRecord` | VT 输入序列 → `INPUT_RECORD`（含鼠标 `\x1b[<...M/m`、修饰键、组合键） |
| `VtInputParser` | VT 输入字节流解析状态机（CSI/SS2/SS3/OSC/转义序列分类） |
| `VtEscape` | VT 转义序列常量与构造工具 |
| 字符宽度 | wcwidth 集成：CJK/Emoji 双宽字符正确推进光标（Phase 17） |

**行编辑（`src/dll/lineedit`）**：`LineEditor` 接管 `ReadConsole` 的交互式行编辑（回显、退格、方向键历史导航），`TabCompleter` 实现 Tab 补全——输入回显经 DLL 直接翻译成 VT 输出，不依赖 ConHost 内部实现。

## 8. IPC 协议（`src/common/protocol`）

二进制紧凑消息，`#pragma pack(1)` 保证 DLL 与 mediator 跨进程（可能不同 CRT）内存布局一致。固定 32 位消息类型：

| 方向 | 消息 | 含义 |
|------|------|------|
| DLL→ | `Hello` | 握手：PID、位数、初始模式/代码页/缓冲区/光标、dllBase |
| →DLL | `HelloAck` | 握手确认：WT 尺寸、ConPTY 光标、isTarget（KickStart 控制） |
| DLL→ | `VtOutput` | 已翻译的输出字节流（变长） |
| →DLL | `VtInput` | WT 输入字节流（变长） |
| →DLL | `ResizeNotify` | WT 窗口尺寸变化 |
| DLL→ | `ModeChange` / `CpChange` | 目标程序切换模式/代码页 |
| DLL→ | `ModeSwitchNotify` | VT 直通模式 ↔ 行编辑模式切换 |
| →DLL | `WtStateReport` | WT 状态反向同步（resize / DSR CPR / DA） |
| →DLL | `Shutdown` | 要求卸载 Hook |
| DLL→ | `UnloadComplete` | 卸载完成，请求远程 FreeLibrary |
| DLL→ | `ChildProcessNotify` / `ChildExitNotify` | 子进程创建/退出 |
| →DLL | `ChildExitSync` | 子进程退出后 ConPTY 光标同步给父进程 DLL |
| DLL→ | `CursorSync` | 子进程直通写/行编辑回显前补发的光标定位（独立消息，不经 BatchSender 合并，保证紧随 VtOutput 原样） |
| 双向 | `Ping` / `Pong` | 心跳保活 |

**批发送**：DLL 侧高频输出（鼠标事件、流式输出）攒批打包（约 16ms 或事件数阈值），避免每事件一次 IPC 导致 CPU 100% / 延迟。

**传输抽象**：`ITransport` 接口 + `NamedPipeTransport` 实现，预留共享内存路径（`TransportFactory`）。

## 9. mediator 内部

- `Mediator` 主循环：stdin（WT 输入）→ `VtInput`；管道消息 → stdout（VT 输出）。
- `VtPassThrough`：把 DLL 来的 VT 输出原样写 stdout（记录 `pipe→stdout: VtOutput` hex 日志，测试据此断言字节）。
- `WtSizeWatcher`：检测 stdout 缓冲尺寸变化 → `ResizeNotify`。
- `VtParser`：解析 WT 对 DSR/DA 查询的应答 → `WtStateReport` 回 DLL。
- `ChildSession`：每个子进程一个管道实例与会话，输出经 `InputRecordToVt`/直通合成到主 ConPTY 流；子进程退出时发 `ChildExitSync` 对齐父进程 DLL 光标缓存（否则父进程新 prompt 的定位序列会把 ConPTY 光标拉回旧位置覆盖子进程输出）。

## 10. 卸载机制（Phase 11）

1. WT Tab 关闭 → mediator 退出 → **管道断开**。
2. DLL 检测断开 → `DoUnload`：禁用全部 Hook、恢复原 API、等待在途翻译完成、释放状态/队列。
3. **关键约束**：DLL 内部无法让 LoadCount 归 0——cmd 主线程的 `LdrpThreadBlob` 对 DLL 持引用，直接 `FreeLibrary` 后 LoadCount 仍 ≥1，`DLL_PROCESS_DETACH` 不触发，DLL 无法真正卸载。
4. DLL 发送 `UnloadComplete` → 由 DLL 以 cmd 子进程身份启动 **`--unload-remote` 助手进程**（独立于 WT 生命周期，WT 已死也不受影响）。
5. 助手等待 2s（DoUnload 线程 + Logger worker 退出、LDR 释放 ThreadBlob 后 LoadCount 降到 1）→ 重试最多 3 次远程 `FreeLibrary(dllBase)`。
6. LDR 可能延迟卸载（模块处于 `LdrModulesReadyToUnload`）→ 触发 **LDR flush**：远程 `LoadLibraryW("kernel32.dll")` 调起 `LdrpFlushUnloadCompleteProcessing`，模块真正从模块列表消失。
7. `DLL_PROCESS_DETACH` → MinHook 清理 → 目标进程恢复原生 Console 行为。
8. **卸载前画面恢复**（`ReplaySessionToConHost`，详见 [Phase 22](phases/22-conhost-replay.md)）：把会话期增量 VT 流重放到 ConHost 冻结缓冲之上。关键点：
   - 重放终点截断到最后一次 prompt 写入起点（`PromptTracker` 写-读序列语义：行编辑读入口之前的最后一次内容写入即该读的 prompt，无内容猜测）→ prompt 文本不进 ConHost；重放前确定性擦除快照 prompt 行。
   - 光标归位后 **`preReplayCur` 记录归位后光标**（2026-08-10 修复：此前记录归位前位置，空会话 SGR 重放不移动光标 → 惰性判定恒失败 → 每轮 prompt 下移一行、空行累积，`test_blankline_accumulation` 回归覆盖）。
   - 惰性重放（空会话，重放前后光标未动）：光标抬到擦除行上一行，KickStart 回车回显 `\r\n` 恰好把 cmd 新 prompt 推回注入前原位。

## 11. 自保护（Phase 9）

目标程序可能调用 `AttachConsole / FreeConsole / AllocConsole / GetConsoleWindow / CloseHandle` 破坏接管链路：这些 API 被拦截并返回"已满足"的假结果（如 `GetConsoleWindow` 返回当前进程假窗口、`AttachConsole` 返回成功但不切换），防止 TUI 程序自行重建 ConHost 造成"越狱"。

## 12. 日志系统

- **双路**：进程内异步环形缓冲（`RingBufferLogger`）+ 文件落盘。日志线程独立于 Hook 调用链，Hook 内绝不调用被 Hook 的 Console API，避免重入死锁。
- 文件按模式分派（`main.cpp`）：mediator = `terminal-injector-<pid>.log`、注入器 = `terminal-injector-inject-<pid>.log`、卸载助手 = `terminal-injector-unload.log`（分文件原因：并发进程日志句柄不共享 write，共用会互斥失败）。
- DLL 日志：`%TEMP%\injected_<pid>_<时间戳>.log`（`TI_INJECTED_LOG_DIR` 可覆盖目录，`TI_LOG_LEVEL` 可调级别）。
- `t=` 时间戳为自会话开始的**微秒数**（`RingBufferLogger` elapsedUs）。

## 13. 性能设计

- 输出：翻译+批发送，满屏重绘 60fps 不撕裂。
- 输入：鼠标事件攒批（16ms / 20 事件），端到端延迟 < 50ms。
- 等待假句柄映射：目标程序 `WaitForSingleObject(假句柄)` 不真实等待，防 TUI 假死。
- Hook 内零堆分配热路径（复用缓冲区）。

## 14. 已知限制

- 用户态劫持天花板：约 5% 行为与内核 ConPTY 有差异（如 `ENABLE_WRAP_AT_EOL` 在 ConPTY 下不被尊重），测试按 ConPTY 实际语义断言而非理想语义。
- 中文输入法激活时 SendInput 组合键可能被 IME 吞掉（测试自动禁用目标窗口 IME）。
- 仅支持 x64 目标进程。

## 15. 相关文档

- 各 Phase 设计：[phases/00-overview.md](phases/00-overview.md) ~ [22-conhost-replay.md](phases/22-conhost-replay.md)
- 使用手册：[USAGE.md](USAGE.md)
- 测试套件：[tests/README.md](../tests/README.md)、[tests/e2e/docs/PHASES.md](../tests/e2e/docs/PHASES.md)
