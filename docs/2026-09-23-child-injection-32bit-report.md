# 子进程注入断链排查报告（py.exe 启动器场景）

> 日期：2026-09-23
> 现象：注入 pwsh 后运行 `run.py`（`.py` 关联 → `py.exe`）→ WT 中一片空白，疑似卡死。
> 本文只做**定位与方案论证**，未修改任何工程源码。

---

## 1. 结论

**根因是跨位数（WOW64）注入失败导致的"注入链断裂"。**

链路：`pwsh(已Hook, x64)` → `CreateProcessW` → `py.exe(32位)` → `CreateProcessW` → `python.exe(x64)`

1. `py.exe` 是 **32 位**进程，而 `injected.dll` 是 **x64** →
   `InjectDllToChild` 的远程 `LoadLibraryW` 返回 0 → 注入失败；
2. `py.exe` 未被 Hook，它后续 `CreateProcessW(python.exe)` **没有任何人拦截**；
3. 孙进程 `python.exe`（x64，本身完全可注入）因此**一并漏网**；
4. 于是 python 的输出全部写进目标进程自己的 ConHost，mediator 侧
   `ChildSession: Run start` 之后永远等不到 `DLL connected` → WT 里什么都看不到。

> **重要更正**：本报告初稿曾判断 `py.exe` 是 `Subsystem=16 (BOOT_APPLICATION)`、
> 无导入表、无重定位的"自包含存根"，并据此认为"注入进去也没有 WT 会话可对接"。
> **该判断是错的**，源于 PE 解析时对 PE32 文件误用了 PE32+ 的数据目录偏移。正确解析见 §3.1。

---

## 2. 影响面

任何"32 位启动器再拉起 64 位真实程序"的链路都会命中，例如：

- `py.exe` → `python.exe`（`.py` 文件关联、`py -3` 等）
- 其他 32 位 launcher / wrapper（32 位安装器、32 位 shell 包装器）

用户态无法通过"再注入一次"绕过，因为断点在**链路中间那一环**。

---

## 3. 证据链（逐项隔离变量）

### 3.1 `py.exe` 到底是什么（权威解析）

```
machine   = 0x014C (x86)          ← 32 位
magic     = 0x10B  (PE32)
subsystem = 3      (WINDOWS_CUI)  ← 普通控制台程序，不是 BOOT_APPLICATION
Import    rva=0x0002C894 size=100 ← 有导入表
Reloc     rva=0x000BA000 size=5852← 有重定位表
```

导入表关键项（`KERNEL32.dll`）：

- **`CreateProcessW`** ← 它就是这样拉起 python.exe 的
- `CreateJobObjectW` / `AssignProcessToJobObject` ← 用 Job 对象管住子进程
- `IsWow64Process` / `GetBinaryTypeW` ← 它自己会挑选 32/64 位解释器
- `WriteConsoleW` / `GetStdHandle` / `SetStdHandle` ← 正常控制台 I/O

结论：**`py.exe` 是标准的 32 位控制台程序**，有完整的 Console 会话，可被 Hook，
其子进程创建走 `CreateProcessW`（我们已 Hook 的那个 API）。

操作系统独立判定（`GetBinaryTypeW`）：

| 文件 | 判定 |
|---|---|
| `C:\Windows\py.exe` | **32BIT** |
| `C:\Program Files\Python311\python.exe` | 64BIT |
| `injected.dll` | x64（见下） |
| `terminal_injector.exe` | 64BIT |

### 3.2 跨位数注入必然失败（最小变量对照）

同一份 x64 `injected.dll`、同一段注入代码，**只改目标进程位数**：

| 目标 | 位数 | 远程 `LoadLibraryW` 退出码 | 结果 |
|---|---|---|---|
| `C:\Windows\System32\cmd.exe` | 64BIT | `0x4E310000`（真实模块基址） | **成功** |
| `C:\Windows\SysWOW64\cmd.exe` | 32BIT | `0x0` | **失败** |

与实际日志完全一致：

```
[ERROR] InjectDllToChild: LoadLibraryW returned 0 in child pid=5292
[WARN ] OnChildProcessCreated: inject failed pid=5292, child runs without hooks
```

### 3.3 孙进程本身可注入（证明问题只在"链断"）

在 `py.exe` 拉起 `python.exe` 之后，**事后手动注入**这个孙进程：

```
进程树:  python.exe (ppid = py.exe)
注入 python.exe(pid=4232) → 成功 (LoadLibraryW exit=0x4F440000)
```

即：`python.exe` 完全可以被接管，**唯一的障碍是没人去注入它**。

### 3.4 现有 e2e 为什么没发现

`tests/e2e/lifecycle/test_child_injection.py` 用的是**直接 `python`**（不经 py.exe），
子进程天然是 x64，链路永远不断。这正是"自测数据未贴近真实分布"的典型漏网。

---

## 4. 为什么两个"看起来省钱"的方案不行

### 4.1 只加位数校验（跳过 32 位子进程）——不构成修复

`OnChildProcessCreated` 的上报是**"已被注入的父进程 DLL"**发出的。
跳过 `py.exe` 注入后，`py.exe` 依然未 Hook，它拉起孙进程时依然无人拦截 →
**孙进程照样漏网**，用户画面依旧空白。只消除了报错日志，没有改变行为。

### 4.2 轮询/事件式"补注入"孙进程——时间窗不够（已实测）

两个关键测量（本机实测）：

| 量 | 实测值 | 说明 |
|---|---|---|
| `python.exe` 创建 → 首行输出 | **64 ~ 80 ms** | 可用的全部宽限期 |
| 注入一针（远程 LoadLibraryW 返回） | **~59 ms** | 硬下限（`DllMain` 装完所有 Hook 才返回） |

59 ms 与 64~80 ms 是同一量级，中间还要塞进"发现子进程"的延迟与 OpenProcess 等开销。
**余量仅 5~20 ms，不具工程可靠性**（首次输出极可能丢在 ConHost，正是本 bug 的症状）。
故"轮询发现 → 事后注入"这条路排除。

> 注：后续重复注入同一进程仅 0.2~1.6 ms，是因为 DLL 已加载、`LoadLibraryW` 只加引用计数；
> 首次加载的 ~59 ms 才是真实成本。

> 补充（2026-09-27）：本节排除的是**进程创建→首行输出的竞态**（晚一步就把首帧丢在 ConHost），
> 它**不**适用于另一种情况：目标进程早已在运行并**持续输出**（典型：WT 里先跑着 TUI，
> 再把承载它的 shell 劫持到新 WT）。那里没有竞态可言，晚注入只是多丢一帧（LazyInit
> 会从共享 ConHost 快照重放当前画面），之后逐帧都是活的。据此新增
> `ProcessHooks::AdoptConsoleDescendants`：注入时枚举同一控制台的后代并逐个补接管，
> 详见 `docs/TECHNICAL.md` §4 与 `tests/e2e/docs/PHASES.md` BUG-018。

---

## 5. 解法选项

**共同前提**：无论选哪条，都绕不开一个事实——
`py.exe` 的 `CreateProcessW` 只有"进程内有 32 位代码"才能被拦截。
而 32 位进程**无法**向 64 位进程注入（与 §3.2 的失败方向对称），
所以孙进程的注入必须由 **x64 侧（mediator 或独立助手进程）** 完成。

### 选项 A：最小 32 位中继存根（推荐，正确性最佳）

新增一个**极小的 32 位 DLL**（`injected32.dll`），只做一件事：

1. Hook `CreateProcessW/A`；
2. 强制 `CREATE_SUSPENDED`；
3. 通过命名管道把"新子进程 PID"告诉 mediator；
4. 等 mediator 回执（mediator 侧完成对孙进程的 x64 注入）；
5. `ResumeThread`。

- **优点**：孙进程在被挂起状态下被注入，**race-free**；存根小、职责单一；
  `py.exe` 生命周期极短也不需要完整 Hook 栈。
- **成本**：新增 32 位构建；新增两个中继消息；**mediator 侧需要获得注入能力**
  （或复用现有 `--unload-remote` 那种"另起 x64 助手进程"的模式，避免 mediator 膨胀）。
- **注**：本方案建成的中继设施，同时是选项 B 的前置条件。

### 选项 B：完整 32 位 `injected.dll`（支持 32 位目标本体）

让 32 位程序也能作为**一等目标**被接管（不只是 launcher 中继）。

- **优点**：能力最完整；32 位 TUI 程序也能接管。
- **成本**：整个 Hook/translator/state/lineedit 栈都要在 32 位下可用；
  MinHook 侧**已具备条件**（`third_party/minhook/src/hde/hde32.c` 已随仓库提供，
  MinHook 自带 CMakeLists 会按 `CMAKE_SIZEOF_VOID_P` 自动选择），
  但本项目自己的 `cmake/FindMinHook.cmake` **硬编码了 `hde64.c`**，需修正；
  构建脚本 `build.ps1` 硬编码 `-arch=x64`、`Platform=x64`，需扩展。
- **仍受同一限制**：32 位 DLL 也无法向 x64 孙进程注入 → **依然依赖选项 A 的中继设施**。

### 选项 C：不可行 / 不采纳

| 方案 | 否决理由 |
|---|---|
| 轮询补注入 | 时间窗不足（§4.2 实测） |
| WMI / Job 通知 + 事后挂起 | 通知时进程已在运行，仍与 ~59ms 注入竞争；且 `py.exe` 用 Job + 可能 breakaway，不可靠 |
| IFEO / AppInit_DLLs 注册表全局劫持 | 全局副作用、需管理员、侵入性过强 |
| 改写命令行绕过 `py.exe`（直接起 python.exe） | 需复刻 py.exe 的版本选择/shebang/环境语义，脆弱，且改变用户语义 |

---

## 6. 推荐

**先做选项 A**：它精确修掉本 bug，race-free，且其"32 位中继 + x64 端注入"设施
是后续任何 32 位能力的公共前置。选项 B 可在 A 之上按需追加。

需要用户决策的关键点（尚未动手）：

1. 是否采用选项 A？
2. 孙进程注入放在 **mediator 内**，还是**另起 x64 助手进程**（沿用 `--unload-remote` 模式）？
3. 是否需要一并规划选项 B（32 位目标本体支持）？

---

## 7. 复现与测量脚本

`tests/_probe/`（临时排查脚本，非正式测试，可随时删除）：

| 脚本 | 用途 |
|---|---|
| `direct_inject.py` | 直接 `CreateRemoteThread+LoadLibraryW` 注入，x64/x86 对照 |
| `inj_time.py` | 测量注入一针的耗时（硬下限） |
| `slack.py` + `firstout.py` | 测量 `python.exe` 创建→首行输出的宽限期 |
| `timing.py` | 测量 `py.exe` → `python.exe` 的存活窗口 |

端到端探针见技能 `pywezterm-terminal-probe`（用真 ConPTY 承载 mediator，
替代 WT + SendInput，无焦点依赖）。

---

## 8. 实施状态（2026-09-24 追记）

本节追记 §6/§7 之后的实施与验证结果；**上文命名以本节为准**。

### 8.1 命名更正

§5 选项 A 里写的 `injected32.dll` 只是当时的占位名，实际落地产物是：

| 产物 | 位数 | 职责 |
|---|---|---|
| `relay32.dll` | 32 位 | 中继本体：hook `CreateProcessW/A` + 强制 `CREATE_SUSPENDED` + 按孙进程位数分派 |
| `relay32inject.exe` | 32 位 | 同位数注入助手：给 32 位子进程注入 `relay32.dll`（x64 侧 fork 它） |

两文件与 `injected.dll` / `terminal_injector.exe` 必须同目录部署（运行时按自身目录定位）。
构建：`build_relay.ps1`（单独 `build32` 树，`-A Win32`），已被 `build.ps1` / `build_dll.ps1` 自动调用。

### 8.2 §6 的三个待决点已定

1. 采用**选项 A**（最小 32 位中继存根）；选项 B（32 位目标本体）保留为 `TODO(32bit-target)`。
2. 孙进程注入由 **mediator** 执行（fork x64 `terminal_injector.exe --inject`，沿用既有注入器）；
   "冻结 → 注入 → 回 `RelayChildAck` → 恢复"消除竞态。32 位子进程则由**父 DLL 内联 fork
   `relay32inject.exe`**（子进程句柄本就在 detour 手里，无需新增协议往返，也不依赖 mediator 存活）。
3. 未一并规划选项 B。

### 8.3 实施过程中发现的第二个缺陷（已修，重要）

中继的接收循环最初用**阻塞** `RecvPacket`(ReadFile) 常驻等待 `RelayChildAck`，
而 `Send`(WriteFile) 走**同一个同步命名管道句柄**。Windows 语义：同步管道句柄上
未完成的 `ReadFile` 会堵住同句柄的 `WriteFile`。

后果：detour 线程的 `RelayChildNotify` **永远发不出去** → 子进程一直冻结 →
cmd 卡住不出提示符（现象与原始 bug 的"空白"一致，极难与断链区分）。

证据与修法：

- 中继日志只有 `child <pid> is 64-bit, asking mediator to inject`，**没有**紧随的
  `RelayChildNotify ... sent=` 那一行（该行在 `SendPacket` 返回之后）⇒ Send 从未返回。
- 阻塞时长 = 对端存活时长：对端进程一关，`WriteFile` 才以 `err=232 ERROR_BROKEN_PIPE` 返回。
- 隔离复现 `tests/_probe/pipe_io_serialize_probe.py`：A 组挂起阻塞读 → 同句柄写 1001ms 不返回；
  B 组改 `PeekNamedPipe` 轮询 → 0.00ms 通过；C 组客户端句柄 Peek 能察觉对端关闭（`err=109`）。
- 修法：接收循环改 `PeekNamedPipe` 轮询（与 `ChildSession::RecvLoop`、
  `VtPassThrough::ForwardPipeToStdout` 同策略 —— 本项目此前已在 mediator 侧踩过同一个坑）。
  修后实测：detect 子进程 → 上报送达 **0.3ms**；notify → ack 往返 **140ms**。

### 8.4 验证结果

| 验证物 | 结果 |
|---|---|
| `tests/_probe/launcher_chain_probe.py`（pywezterm 真 ConPTY，逐环 L1~L6） | **6/6 PASS** |
| `tests/_probe/relay32_32to32_probe.py`（32→32 分支 / `NotifyChildSession`） | **A~F 全过** |
| `tests/_probe/relay32inject_e2e.py`（注入原语） | **10/10 PASS** |
| `tests/e2e/lifecycle/test_launcher_chain.py`（新增，纳入 `run_all.py`） | **SUMMARY PASS** |
| `run_all.py --cat lifecycle`（13 项） | 12 PASS / 1 FAIL（该 FAIL 与本特性无关，见 8.5） |

### 8.5 未处理的既有问题（不在本特性范围内）

`test_resize_overlay_clean` 在 lifecycle 全量回归中**稳定失败**（3/3，非 flaky）：
`line[5]` 含 4 个 `|`（可见 70，其余在 98/119/140）⇒ 99/120/141 三个布局帧叠画，
属 BUG-012 那一类残留。判定与本特性无关：被测 x64 产物未重建、ConsoleToVt 渲染路径零改动、
该用例也不走被修改的测试基建。另：该用例 setup 阶段本身偶发（曾见"目标 python 进程未找到"）。
