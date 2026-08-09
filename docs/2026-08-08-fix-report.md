# terminal-injector 修复报告

- 日期:2026-08-08
- 状态:已修复问题均已实现、构建、验证;改动未提交 git
- 范围:注入重放(滚动历史到 WT)、卸载回放(ConHost 恢复)、会话期渲染、快照/句柄可靠性

---

## 一、已修复的问题

### 1. 注入侧:滚动历史(Scrollback)到 WT

| 问题 | 根因 | 修复 | 文件 |
|---|---|---|---|
| 注入后 WT 无历史,长输出错乱 | 原 `WriteConsoleOutput` 用绝对行号逐行输出,行号超 ConPTY 视口(≈30行)被 clamp/覆盖,内容错位重叠 | 新增 `ReplayScreenStreamed`:`\r\n` 流式推进,ConPTY 自然滚动,历史进 WT scrollback,视口停在缓冲底部 | `src/dll/translator/ConsoleToVt.cpp/.h` |
| 注入后 prompt 与几百行空行 | 流式重放把 9000+ 默认空格行也推入 WT scrollback | 尾部空行裁剪:只输出到最后一个内容行 | 同上 |
| 注入时 srWindow.Top>0 顶部几行重复 | 注入重流被记入卸载重放缓冲,卸载回放时与冻结快照重复 | 注入重流 `recordReplay=false`(只发 WT,不进 VtReplayBuffer) | `src/dll/LazyInit.cpp` |

### 2. 卸载侧:ConHost 画面恢复

| 问题 | 根因 | 修复 | 文件 |
|---|---|---|---|
| 卸载后 scrollback 丢失("没回放 rollback") | `ReplaySessionToConHost` 用 `SetConsoleScreenBufferSize(targetBuf)` 把 9001 行缓冲缩到视口高,丢弃顶部历史 | 缓冲 resize 只放大、绝不缩小 | `src/dll/Unloader.cpp` |
| 解除后双 prompt / dir 截断 | LazyInit Phase 14 用 WT 尺寸把 VirtualConsoleState 窗口覆盖成顶部锚定 `[0..rows-1]`,卸载时 ConHost 窗口被移到顶部 → 重放 CUP 映射错绝对行 | 卸载窗口顶部保持**注入时 srWindow.Top**(记录注入窗口),尺寸用会话尺寸 | `Unloader.cpp` + `state/VirtualConsoleState.h/.cpp` |
| 解除后 prompt 拼到输出末行(`或批处理文件。>`) | 每次 WriteConsoleW 的 cursor sync、LazyInit 的 line-start sync 都是视口相对 CUP,记入重放缓冲后在 ConHost 落错位置 | 两类 sync 改 `recordReplay=false`;ConHost 重放靠光标归位 + 纯文本(`\r\n` 自然推进) | `OutputHooks.cpp` + `LazyInit.cpp` |
| 卸载竞态:cmd 先写新 prompt 再重放 → 双 prompt | DoUnload 原顺序:DisableAll → 先 KickStart 唤醒 cmd(写新 prompt)→ 再重放。cmd 在重放前直接写 ConHost | **重排:先重放,再唤醒 cmd**(cmd 的新 prompt 落在重放后的正确位置) | `Unloader.cpp::DoUnload` |
| 多次注入/解除累积双 prompt | 会话末 prompt + cmd 卸载后重新绘制的 prompt 叠加 | 三层机制：**①重放终点截断到最后一次 prompt 写入起点**（写-读序列语义：行编辑读入口之前的最后一次输出即该读的 prompt，由 `PromptTracker` 记录；仅当行编辑读当前阻塞时生效）→ prompt 文本不进 ConHost；**②重放前擦除快照旧 prompt 行**（注入光标行——截断后重放不含 prompt 文本，快照该行永不被重放覆盖）；**③惰性重放（空会话，重放前后光标未动）光标抬到擦除行上一行**，KickStart 回车回显 `\r\n` 把 cmd 新 prompt 推回注入前原位 → 卸载后画面与注入前逐像素一致 | `Unloader.cpp` + `state/PromptTracker.h/.cpp` |

### 3. 会话期渲染

| 问题 | 根因 | 修复 | 文件 |
|---|---|---|---|
| dir 长输出插行错乱 | WriteConsoleW 每次输出前的 cursor sync 用绝对行号(0~9000),ConPTY/WT 按视口相对解释,超视口后落错行 → 内容互相覆盖 | `CursorPosition` 同步行/列 **clamp 到视口尺寸**(min(绝对行, 视口高-1)) | `src/dll/hooks/OutputHooks.cpp` |

### 4. 快照 / 句柄可靠性

| 问题 | 根因 | 修复 | 文件 |
|---|---|---|---|
| cmd 批处理等待子进程时快照/卸载失败(err=6) | 该状态下 std 输出句柄对 console API 失效(GetConsoleScreenBufferInfo err=6),CONOUT$ 始终可用 | `GetConsoleOutHandle()` 优先 std,失败回退 `CreateFileW("CONOUT$")`;快照 + 卸载重放均使用 | `state/StateSnapshot.cpp` + `Unloader.cpp` |

### 5. 早期会话修复(已并入本报告范围)

- DSR(`\x1b[6n`)/DA(`\x1b[c`) 校准查询 `recordReplay=false`,避免卸载重放时 ConHost 自答出字面 VT 文本(`^[[6;1R` / `^[[?1;0c`)。
- 卸载重放代码页切换 936→65001(中文不再乱码),恢复顺序固定:先 `SetConsoleMode` 关 VT,再 `SetConsoleOutputCP` 还原代码页。
- 滚动历史全量抓取:编译期常量 `kCaptureFullScrollback = true`,`CaptureRegion` 读整缓冲。

---

## 二、验证证据

> 注：下表多数为 2026-08-08 一次性验证脚本（完成后已清理，不再存在于 tests/e2e），
> 验证点与结果保留为历史回归记录；仅标注「现存」的两项为仓库内可复跑测试。

### 回归测试(全部通过)

| 测试 | 验证点 |
|---|---|
| 注入流式重放（一次性脚本，已清理） | 80 行 scrollback 按序送达 WT,绝对定位 max row=30(视口内),无尾部空行带 |
| 卸载回放（一次性脚本，已清理） | 30 行 + 中文内容正确、无乱码、无字面 VT 序列、无重复 |
| 无 resize 卸载（一次性脚本，已清理） | 100 行内容保留、窗口/光标正确 |
| resize + 会话输出（一次性脚本，已清理） | 100 行增量正确叠到快照,不缩缓冲 |
| cmd/pwsh 握手（一次性脚本，已清理） | 注入握手 + prompt 行首覆盖正常 |
| 逐像素一致性（一次性脚本，无输入 3 轮，已清理） | 注入→卸载后画面与注入前逐像素一致：单 prompt、无多余空行（nz=[0,1,3]、prompt@3、光标 (41,3)），3/3 PASS |
| `lifecycle/test_repeat_inject_unload.py`（现存，含单 prompt 断言） | 10/10 轮：握手+卸载 OK、卸载后 prompt 行数恒为 1（max=1）、无 terminal_injector 残留、无新增 WT 窗口 |
| `lifecycle/test_unload_clean.py`（现存） | 注入后模块存在、10s 内卸载、cmd 进程存活（未误杀）、恢复原生后台行为 |

### 压测 / 专项

> 以下均为一次性验证脚本（已清理），结果保留为历史回归记录。

| 测试 | 结果 |
|---|---|
| cmd 卸载 10 次压测（一次性脚本，已清理） | 10/10:`prompt_cnt=1、concat=False、dir 完整(82 日期行)`,零方差 |
| 同一 cmd 反复注入/解除 3 轮（一次性脚本，已清理） | 3/3 全 PASS,每轮 ConHost 保持干净,不累积 |
| 深目录 `%SystemRoot%\System32\drivers`（一次性脚本，已清理） | 单 prompt、无拼接 |
| cmd 批处理含子进程 WT 场景 3 次（一次性脚本，已清理） | 3/3:batch 输出经 VT passthrough 拦截到 WT,会话期不进 ConHost,卸载后完整回放 |
| python WT 场景（一次性脚本，已清理） | python 输出经翻译路径拦截(PROBE=60),卸载后回放完整 |
| WT 截图 45 行超视口（一次性脚本，已清理） | 渲染干净,行顺序正确,历史进 WT scrollback |

全量构建成功(injected_dll + mediator + injector)。

---

## 三、仍存在的问题 / 风险(诚实说明)

### 1. mediator 脱离 WT 单独运行时拦截不一致(测试环境,真实使用不受影响)

- **现象**:mediator 以 `CREATE_NO_WINDOW` / `stdin=PIPE` 直接启动(不经 wt.exe)时,目标(cmd/python)的输出有时**不被拦截**(注入日志零 PROBE、输出直接进 ConHost)。
- **已确认**:连接完整(Hello/HelloAck 正常)、`hooksInstalled=1 registered=58`(Hook 全装),但目标未调用被 Hook 的 WriteConsoleW/WriteFile。
- **真实 WT 场景(mediator 在 WT 标签内)已验证拦截正常**:cmd 批处理(含子进程)+ python 都正确拦截。
- **根因未 100% 定位**:疑似涉及 mediator 无控制台时与目标交互的深层 Windows 控制台行为(可能的目标句柄/写路径差异),非直接 Hook 缺陷。测试环境可复现,真实使用不受影响。如需彻底解决,建议用 API Monitor 级工具跟踪目标在两种场景下的实际写 API。

### 2. 会话末 prompt 处理依赖写-读序列语义（已替代旧启发式擦除）

- 旧方案：重放后读回末行，**以 `>` 结尾**才擦除——需猜测内容，`echo x > file` 之类输出末行若以 `>` 结尾会被误擦。
- 现方案（`PromptTracker`）：**行编辑读（ReadConsoleW/A，ECHO_INPUT+LINE_INPUT）入口之前的最后一次内容写入起点**即该读的 prompt，无内容猜测；重放终点截断在该起点，prompt 文本不进 ConHost。实测（2026-08-08）修正两处设计误判：
  - **快照旧 prompt 行不会被重放覆盖**：重放内容 = `vt[0, prompt起点)`，只写当前光标处，快照里的旧 prompt 行（注入光标行）永在画面中 → 重放前（4.1）确定性擦除该行。
   - **cmd 自绘的新 prompt 不落在原位**：KickStart 回车回显 `\r\n` 会把光标推到下一行，新 prompt 落在旧行之后 → 惰性重放（重放前后光标未动 = 空会话无可视内容）时光标抬到擦除行上一行（5.5），回显恰好把新 prompt 推回注入前原位。最终画面与注入前逐像素一致（2026-08-08 一次性验证实测 3 轮：nz=[0,1,3]、prompt@3、光标 (41,3)，脚本已清理）。
- 残余边界：
  - **命令执行中卸载**（无行编辑读阻塞）不截断，保留全部输出（无 prompt 可截）。
  - **批处理 `set /p` 等非重绘 prompt**：截断会少该 prompt 行（该读已被 KickStart 回车消费，prompt 文本不会重绘）——语义正确、视觉损失极小。
  - 重放缓冲达 4MB 上限时 `Append` 返回 -1 不记录，退化为全量重放（旧双 prompt 风险仅在该资源上限场景）。

### 3. 卸载回放依赖 ConHost 的 VT 行为

- 重放精确性基于 ConHost 对 CUP 的"视口相对"解释。不同 Windows 版本 / ConHost 版本可能有细微差异,当前验证均在本机(Windows 10 19045)完成。

### 4. 仓库状态

- `gui.py` 已在工作区删除,并将以本次提交记录（从仓库根移除,已迁移至 `build/bin/Release/` 本地使用,不受版本管理）。
- 本轮全部代码改动随本次提交入库;一次性验证脚本已清理,验证证据保留在本报告。

---

## 四、本轮改动的文件清单

- `src/dll/translator/ConsoleToVt.cpp/.h` — ReplayScreenStreamed、尾部裁剪
- `src/dll/LazyInit.cpp` — 注入重流/光标同步/行首同步 recordReplay 控制、日志 hooksInstalled
- `src/dll/Unloader.cpp` — 缓冲不缩、窗口注入基准、光标归位、重放终点 prompt 截断（替代旧擦除）、4.1 重放前快照 prompt 行擦除、5.5 惰性重放光标抬升、DoUnload 重排、CONOUT$ 回退
- `src/dll/hooks/OutputHooks.cpp` — CursorPosition clamp、每写 cursor sync recordReplay=false
- `src/dll/state/StateSnapshot.cpp/.h` — CONOUT$ 回退、CaptureRegion
- `src/dll/state/VirtualConsoleState.h/.cpp` — 注入窗口/光标记录（m_inputMode 已随旧擦除删除）
- `src/dll/state/PromptTracker.h/.cpp` — 写-读序列 prompt 追踪（新增）
- `src/dll/state/VtReplayBuffer.h/.cpp` — Append 返回追加起始偏移
- `src/dll/BatchSender.cpp` — 内容写入起点记录到 PromptTracker
- `src/dll/hooks/InputHooks.cpp` — 行编辑读入口/出口 PromptTracker 作用域
- `src/dll/BatchSender.cpp/.h`、`src/dll/hooks/HookCommon.h/.cpp` — recordReplay 参数贯通(早期)

---

## 五、建议的后续工作

1. **(可选)查清 mediator-direct 拦截不一致根因**:需 API Monitor 级工具跟踪目标写 API。
2. **(可选)多版本兼容性验证**:在不同 Windows 版本 / ConHost 上回归卸载重放。
