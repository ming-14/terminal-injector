# 卸载后源 shell 崩溃 `0xc0000005` —— 根因定位报告

- 日期：2026-09-29
- 关联：`BUG-031`（本报告给出其"崩溃那一步"的最终确认）
- 复现路径：`tests/_probe/repro_taskboard_unload_enter.py` / `scan_unload_settle.py` / `cdb_confirm_wait_detour.py`
- 环境：Windows，`pwsh 7.6.3`，全程 pywezterm（`Pty`，侧载 OpenConsole），**不依赖真 WT**

---

## 1. 现场

用户在真 WT 里操作，终端最后吐出：

```
PS C:\Users\rikka> C:\Users\rikka\Desktop\terminal-injector\tests\live\textual\taskboard.py
PS C:\Users\rikka>
[已退出进程，代码为 3221225477 (0xc0000005)]
现在可以使用Ctrl+D关闭此终端，或按 Enter 重新启动。
```

序列：**开终端 → 劫持 → 新终端跑 `taskboard.py` → 卸载 → 回旧终端操作 → pwsh AV 崩溃**。

## 2. 复现（全程 pywezterm）

拓扑：

```
源终端 = pywezterm.Pty 跑 pwsh          （扮演"旧终端"）
新终端 = pywezterm.Pty 跑 mediator       （扮演"劫持后新开的终端"）
注入   = mediator --mediator --target-pid <源 pwsh pid>
TUI    = 在**新终端**里跑 taskboard.py
然后：关新终端（卸载）→ 回源终端输入
```

`tests/_probe/repro_taskboard_unload_enter.py` 一次命中，退出码与现场一致。

### 2.1 触发条件不是"按回车"

用户在描述里写"旧 wt 按回车"，但**隔离实验证明回车本身不是触发条件**：

| 用例 | 动作 | 结果 |
|---|---|---|
| A | 卸载后连按 3 次 Enter（空行提交） | **不崩** 3/3 |
| B | 卸载后只敲字符 `a` `1` `空格`，不回车 | **不崩** 3/3 |
| C | 卸载后敲 `echo x` + 回车 | 崩（不稳定，见下） |
| D | 卸载后敲 `zzz` + 3×Backspace + 回车 | **崩** |
| E | 卸载后等 8s，再按 Enter | **崩** |

D/E 崩、A/B 不崩 ⇒ 触发条件是"**卸载完成后，目标真的去做了一次终端 I/O**"，
而不是某个特定键。用户现场按的那个 Enter 前后必然已经有过输入（或 Enter
本身落在了一个已有待处理输入的状态上）。

### 2.2 真正的开关是"卸载后到首次输入的时间差"

`tests/_probe/scan_unload_settle.py` 固定动作为 `echo <token>` + 回车，只扫等待时间：

| settle | 复现 / 总数 |
|---|---|
| 0.0s | 0 / 2 |
| 1.0s | 0 / 2 |
| 1.5s | 0 / 3 |
| 2.0s | 0 / 3 |
| 2.5s | 0 / 3 |
| 2.7s | 0 / 3 |
| 2.9s | 0 / 3 |
| **3.2s** | **3 / 3** |
| 8.0s | 复现 |

**这是个 0/1 的硬阈值，卡在 3.0 秒**（2.9 全绿、3.2 全崩，中间无概率性），
不是竞态抖动。这解释了用例 C 的"时崩时不崩"：`close_mediator()` 自带
`time.sleep(2.5)`，加后续开销正好落在阈值边界上。

---

## 3. 根因（cdb 取证，已确认）

### 3.1 崩溃现场

`cdb` 挂到源 pwsh（`-o -g -G`），卸载后触发输入，抓到：

```
(2088.604): Access violation - code c0000005
<Unloaded_injected.dll>+0x3011b:
00007ff8`b777011b ??              ???

rax=0000000000000000 rbx=0000000b58fce460 rcx=ce694515558b0000
rip=00007ff8b777011b  ← injected.dll 基址 0x7ff8b7740000 + 0x3011b
```

`<Unloaded_injected.dll>` 是 WinDbg 对"符号属于**已从模块列表移除**的模块"的标注。
崩溃处内存字节是 `??`（不可读/已被复用），而磁盘上该处是 `8b f8`（`mov edi,eax`）。

### 3.2 `0x3011b` 是什么

```
injected!terminjector::hooks::WaitForMultipleObjectsEx_Detour+0x170:
00000001`80030111  mov  r10,[injected!...WaitForMultipleObjectsEx_orig]
00000001`80030118  call r10                  ← 阻塞调用原 API
00000001`8003011b  mov  edi,eax              ← ★ 崩溃地址正好是这条「返回点」
```

即崩溃地址 = **`WaitForMultipleObjectsEx_Detour` 里 `call orig` 的返回地址**。

### 3.3 崩溃线程栈（决定性证据）

```
   1  Id: 2088.afc
00007ff8`e3750d00 : ... : ntdll!NtWaitForMultipleObjects+0x14
00007ff8`b777011b : ... : KERNELBASE!WaitForMultipleObjectsEx+0xf0
00000000`00000006 : ... : <Unloaded_injected.dll>+0x3011b     ← ★
```

逐层读：

1. `ntdll!NtWaitForMultipleObjects` —— 内核等待中
2. 它的返回地址是 **`<Unloaded_injected.dll>+0x3011b`** —— 即 detour 里
   `call orig` 之后的返回点
3. 再往上一帧是 `KERNELBASE!WaitForMultipleObjectsEx+0xf0` —— 确认这条路径
   确实是"从 `WaitForMultipleObjectsEx` 的 **detour** 进来的"

**结论**：该线程进入 `WaitForMultipleObjectsEx_Detour` → `call orig` 阻塞在
`NtWaitForMultipleObjects` → **此时 `injected.dll` 被远程 `FreeLibrary` 摘除** →
等待返回时要把控制权弹回 `injected.dll+0x3011b`，而那段代码已不存在 → AV。

### 3.4 卸载时序（日志佐证）

`build/bin/Release/logs/terminal-injector-unload.log`：

```
t=278      UnloadRemote: waiting 2000ms for DoUnload + Logger threads to exit...
t=2013050  attempt 1 FreeLibrary → "DLL still loaded after attempt 1"  (t 单位为 0.1ms，约 201ms)
t=2331030  TriggerLdrFlush
t=2636091  attempt 2 FreeLibrary
t=2950615  DLL unloaded after FreeLibrary (attempt 2)   ← 此处 DLL 已真正离开进程
t=2950662  exit code=0
```

崩溃紧跟其后。**DLL 已卸载**这一点由 cdb 的 `<Unloaded_injected.dll>` 独立确认。

---

## 4. 为什么既有的防 AV 机制没拦住

`Unloader::DoUnload` 第 5.5 步（`Unloader.cpp:224-269`）有一套"等所有 Read 类
Detour 线程退出 DLL 代码"的机制，依据 `Unloader::ActiveReadDetours()` 计数。

问题在于**计数只覆盖 Read 类 detour**：

| Hook | 用的 RAII guard | 计入 `ActiveReadDetours` |
|---|---|---|
| `ReadConsoleInputW` / `ReadConsoleW` / `ReadFile` 等 | **`ReadDetourGuard`**（`InputHooks.cpp:70`） | ✅ 是 |
| `WaitForMultipleObjectsEx` / `WaitForMultipleObjects` / `WaitForSingleObject(Ex)` / `GetConsoleInputWaitHandle` | 仅 `HookReentryGuard` | ❌ **否** |

`WaitHooks.cpp` 全文件**没有一处** `ReadDetourGuard`。

因此：

- `DisableAll()` 只恢复 `kernel32`/`kernelbase` 里的**原字节** ⇒ 只影响**新调用**，
  对**已经进入** detour 的线程无效；
- 第 5.5 步等 `ActiveReadDetours()==0` 时，**阻塞在 `WaitForMultipleObjectsEx_Detour`
  里的线程完全不在计数内** ⇒ 等待被"已经归零"骗过 ⇒ 直接走到远程 `FreeLibrary`。

**这正是"卸载后 3 秒才崩"的由来**：detour 内的线程仍在 `NtWaitForMultipleObjects`
上等（`WaitHooks` 把输入句柄换成了 `InputQueue` 事件，超时/事件触发后返回）。
3 秒是个稳定的窗口——超过了 `main.cpp` 里 `waiting 2000ms` + 两次 FreeLibrary
尝试的耗时，即"卸载彻底完成"之后。

---

## 5. 与 BUG-022 的关系（澄清）

BUG-022（`0xc0000409`，ucrtbase，Kitty `>1u`）与本 bug **无关**：

- 退出码不同：`0xc0000409` vs **`0xc0000005`**
- 崩溃模块不同：ucrtbase vs **已卸载的 injected.dll**
- 触发条件不同：重放残留终端模式序列 vs **卸载后目标做一次终端 I/O**

BUG-022 自身的因果表述另有一处待修正（`>1u` 在 WT 1.24 上并不存在 Kitty 实现，
Kitty 是 Preview 1.25 才引入），见后续单独结论。

---

## 6. BUG-031 描述的修正

`docs/working/BUGS.md` 的 BUG-031 记：

> 复现路径：`test_unload_tui_shell_alive` 场景 B —— **不退出 TUI 直接卸载** ⇒ 稳定复现。
> 同用例场景 A（Tab+q 让 TUI 真正退出后再卸载）**不崩**。

本报告实测结果与之不符，需要修正：

- **场景 A（TUI 正常退出后卸载）同样会崩** —— 只要"卸载完成后 ≥3 秒"再做输入；
- 真正决定崩不崩的是 **卸载完成 → 首次终端 I/O 的时间差（阈值 ≈ 3s）**，
  而不是"TUI 有没有退出"；
- 既有的 `test_unload_tui_shell_alive` 之所以在场景 A 显示 PASS，是因为它
  `close_mediator()` 后只 `time.sleep(2.0)` 就立刻 `echo` ⇒ **落在阈值以内**，
  天然避开了崩溃窗口。它的场景 B 崩，也是"多花的时间刚好越过阈值"，而非
  "TUI 活着"这个因素。

⇒ 该用例的判据需要改为**显式跨过阈值**（卸载后等 ≥3.5s 再输入），否则无论
TUI 死活都测不到这个缺陷。

---

## 7. 修复方向（待确认，未实施）

按 AGENTS.md「未查到根本因素时不要直接改工程源码」，本报告只给方向，不改代码：

**核心矛盾**：`FreeLibrary` 前必须保证**没有线程留在 DLL 代码里**，而当前
只对 Read 类 detour 做了这个保证。

可选方向：

1. **把可阻塞的 Wait 类 detour 也纳入活跃计数**：给 `WaitHooks.cpp` 的 detour
   套上与 `ReadDetourGuard` 等价的守卫（或让两者共用一个计数），使第 5.5 步
   的等待真正覆盖它们。
   - 注意：Wait 类 detour 阻塞时长不确定（可能 `INFINITE`），第 5.5 步的
     `kWaitTotalMs=5000` 超时兜底可能不够 ⇒ 需要同时让 `KickStartBlockedReaders`
     之类的手段能唤醒它们（现有实现已对输入句柄做替换，`SignalDataReady` 会
     触发 `InputQueue` 事件，理论上可唤醒）。
2. **卸载时先让所有 Wait detour 的等待返回**：在 `DisableAll` 前先
   `InputQueue::SignalDataReady()` 把阻塞的等待"放出来"，再等计数归零。
3. **不远程 FreeLibrary，改为进程退出时自然卸载**：代价是 DLL 常驻（几百 KB），
   与 BUG-025 的既有取向一致（宁可泄漏也不崩）。

**建议先做 1+2 的组合**（与既有 Read 类方案同构，改动面小、可验证），
并在 `WaitHooks` 里补一条与 `InputHooks` 对应的注释，说明该守卫的卸载语义。

---

## 8. 复现脚本清单

| 脚本 | 作用 |
|---|---|
| `tests/_probe/repro_taskboard_unload_enter.py` | 按现场序列一次复现（`--scenario quit/alive/both`） |
| `tests/_probe/isolate_unload_crash.py` | 隔离 A~E 五个变量（Enter / 字符 / echo / 擦除 / 延时） |
| `tests/_probe/scan_unload_settle.py` | 扫"卸载→首次输入"的时间阈值（定位 3.0s 硬边界） |
| `tests/_probe/cdb_crash_stack.py` | cdb 挂目标，抓崩溃首现场 |
| `tests/_probe/cdb_confirm_wait_detour.py` | cdb `sxe av` + 全线程栈，确认崩溃线程在 Wait detour 内 |
