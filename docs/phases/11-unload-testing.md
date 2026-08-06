# Phase 11: 卸载清理与多目标测试

> 本 Phase 实现干净的卸载机制，并执行反复注入/卸载验证。关闭 WT 标签页或主动卸载时，DLL 能解除所有 Hook、恢复目标程序原 Console 行为、释放资源。

---

## 1. Phase 目标

1. **管道断开检测**：DLL 接收线程检测到 `Recv` 返回 0 或错误时，触发卸载流程
2. **卸载流程**：
   - `HookManager::DisableAll()`（先禁用 Hook，保留 trampoline 避免 AV）
   - `HookManager::UninstallAll()`（DLL_PROCESS_DETACH 时执行，最终释放 trampoline）
   - 恢复 `ConsoleState` 到真实 ConHost 状态（调 `_orig` 读取）
   - 释放 `InputQueue`、`fakeWaitHandle`、`HandleRegistry` 等资源
   - 通过 `FreeLibraryAndExitThread` 安全卸载 DLL（避免 DllMain 中 FreeLibrary 死锁）
3. **ReadDetourGuard**：跟踪活跃的 ReadDetour 线程，确保卸载前所有 Detour 已退出
4. **远程 FreeLibrary**：DLL 内 LoadCount 因 cmd 主线程 LdrpThreadBlob 不能归零，需 mediator 远程线程调 FreeLibrary
5. **反复注入/卸载 10 次**：无泄漏、无崩溃

---

## 2. 当前状态（2026-08-01）

### 已完成
- ✅ 单次卸载流程（Unloader.h/.cpp、DllRecvLoop.cpp、Mediator.cpp）
- ✅ HookManager::DisableAll()（保留 trampoline 避免 AV）
- ✅ ReadDetourGuard（跟踪活跃 ReadDetour 线程，确保卸载前退出）
- ✅ 远程 FreeLibrary（mediator 收到 UnloadComplete 后远程线程调 FreeLibrary）
- ✅ Release CRT 链接（避免 Debug CRT 卸载后内存损坏）
- ✅ 验收 1-3 全部通过（DLL 模块卸载、cmd 可交互、Hook 字节恢复）

### 未完成
- ❌ 验收 4（反复注入/卸载 10 次）：循环 2 cmd 崩溃退出（STATUS_STACK_BUFFER_OVERRUN 0xC0000409）

---

## 3. 崩溃诊断

### 3.1 现象

```
循环 2/10：
  [info] 循环 2 握手成功
  [unload] 关闭 WT 窗口...
  [unload] mediator 已退出
  [unload] 等待 DLL 卸载...
  [unload] cmd 进程在卸载等待期间退出！elapsed=1.62s exitcode=0xc0000409
```

### 3.2 崩溃位置

```
# Child-SP          RetAddr           Call Site
00 0000007a`9b9ff688 00007ff8`b6dbb72f injected!__report_gsfailure+0x5
01 0000007a`9b9ff690 00007ff6`c5518027 injected!terminjector::hooks::ReadConsoleW_Detour+0x5df
02 0000007a`9b9ff7d0 00007ff6`c550d31b cmd!ReadBufFromConsole+0x127
```

- **异常码**：0xC0000409（STATUS_STACK_BUFFER_OVERRUN）
- **子码**：0x2（FAST_FAIL_STACK_COOKIE_CHECK_FAILURE）
- **GS cookie**：期望 `0000b8f84fb6f8a7`，实际 `ffffffffffffffff`
- **源文件**：`InputHooks.cpp:557`（ReadConsoleW_Detour 的 `__security_check_cookie`）

### 3.3 根因分析

GS cookie 在 `[rbp-1]` 被完全覆盖为 `0xFFFFFFFFFFFFFFFF`。此模式表明栈上某个局部变量写入越界。

**已尝试的修复**：
1. 将 `std::wstring lineOut` 和 `std::string vtOut` 从循环内提升到函数作用域，避免循环内反复构造/析构导致 SSO 缓冲区与 TLS 基址缓存栈布局冲突
2. 改用 Release CRT 链接（Debug CRT 在卸载后内存管理不一致）

**修复后崩溃仍然发生**，说明根因不完全在此。`0xFFFFFFFFFFFFFFFF` 写入模式暗示可能是：
- 栈上某个局部变量未初始化，编译器将其置于 GS cookie 位置
- 某处 `memset(ptr, -1, size)` 或类似操作越界
- 栈上 `std::string`/`std::wstring` 的 SSO 缓冲区大小计算错误导致覆盖

### 3.4 关键观察

- 单次卸载（验收 1-3）始终通过，仅在循环 2+ 崩溃 → 可能与首次注入/卸载后的残留状态有关
- 崩溃时 cmd 主线程正在 `ReadConsoleW_Detour` 的行编辑主循环中
- 后台线程 4（KickStartBlockedReaders）仍在运行，试图写 `WriteConsoleInputW` 唤醒阻塞的读取器
- 线程 5（RingBufferLogger）和线程 6（RecvLoopMain）仍在运行

---

## 4. 卸载流程详解

### 4.1 管道断开 → 卸载触发

```cpp
// DllRecvLoop.cpp
// Recv 返回 0 或错误时：
LOG_INFO("DllRecvLoop: pipe error/broken, requesting unload");
Unloader::RequestUnload();
```

### 4.2 Unloader::RequestUnload

```cpp
void Unloader::RequestUnload() {
    if (s_unloading.exchange(true)) return;  // 幂等
    // 在独立线程执行卸载，避免在 recv 线程或 Hook 线程中死锁
    std::thread([]{
        DoUnload();
        // 线程退出时触发 FreeLibraryAndExitThread
    }).detach();
}
```

### 4.3 Unloader::DoUnload

```cpp
void Unloader::DoUnload() {
    // 1. 停止后台轮询
    StatePoller::Instance().Stop();

    // 2. 唤醒所有阻塞在 InputQueue 上的线程（SignalDataReady）
    //    让 ReadDetour 线程从 WaitForSingleObject 中返回
    InputQueue::Instance().SignalDataReady();

    // 3. 等待所有 ReadDetour 线程退出（超时 2s）
    //    循环等待 ReadDetourGuard 的 count 降为 0
    //    若超时，强制 pass-through 并继续

    // 4. 禁用所有 Hook（保留 trampoline，不释放）
    //    DisableAll 而非 UninstallAll：
    //    MH_RemoveHook 会释放 trampoline 内存，
    //    若 ReadDetour 线程尚未完全退出，后续调用 *_orig 会 AV 崩溃
    HookManager::DisableAll();

    // 5. 恢复 ConsoleState 到真实 ConHost 状态

    // 6. 断开传输、关闭日志

    // 7. 发送 UnloadComplete 通知 mediator 远程 FreeLibrary
    //    原因：DLL 内部 FreeLibrary 无法让 LoadCount 归零
    //    （cmd 主线程 LdrpThreadBlob 持引用）
    //    mediator 收到后 CreateRemoteThread 调 FreeLibrary(dllBase)

    // 8. FreeLibraryAndExitThread(hSelf, 0)
    //    安全卸载：减少引用计数 → 触发 DLL_PROCESS_DETACH → 终止当前线程
}
```

### 4.4 远程 FreeLibrary（mediator 侧）

```cpp
// Mediator::OnUnloadComplete()
void Mediator::OnUnloadComplete() {
    HANDLE hProcess = OpenProcess(PROCESS_CREATE_THREAD | PROCESS_VM_OPERATION, FALSE, m_targetPid);
    // 远程线程调用 FreeLibrary(dllBase)
    // 此线程从未进入过 injected.dll 代码，LDR 不会为其持有 ThreadBlob
    // LoadCount 从 1 → 0，触发 DLL_PROCESS_DETACH
    CreateRemoteThread(hProcess, nullptr, 0,
        (LPTHREAD_START_ROUTINE)FreeLibrary,
        (LPVOID)m_dllBase, 0, nullptr);
}
```

---

## 5. 关键修复记录

| 日期 | 修复 | 说明 |
|------|------|------|
| 2026-07-25 | `DisableAll()` 替代 `UninstallAll()` | 保留 trampoline 避免 ReadDetour 线程 AV |
| 2026-07-25 | `ReadDetourGuard` 实现 | 跟踪活跃 ReadDetour 线程，卸载前等待退出 |
| 2026-07-25 | 远程 FreeLibrary 机制 | 解决 LoadCount 无法归零问题 |
| 2026-07-25 | Release CRT 链接 | 避免 Debug CRT 卸载后内存损坏 |
| 2026-07-25 | `lineOut`/`vtOut` 提升到函数作用域 | 避免循环内 SSO 与 TLS 基址缓存冲突 |

---

## 6. 测试套件

### 6.1 验收标准

| 验收项 | 描述 | 状态 |
|--------|------|------|
| 验收 1 | DLL 模块已从 cmd 进程卸载 | ✅ 通过 |
| 验收 2 | cmd 恢复可交互（echo 命令响应） | ✅ 通过 |
| 验收 3 | Hook 字节已恢复（首字节非 E9） | ✅ 通过 |
| 验收 4 | 反复注入/卸载 10 次无泄漏 | ❌ 循环 2 崩溃 |

### 6.2 运行方式

```bash
# 运行 Phase 11 测试
python tests/runners/run_all.py phase11

# 运行循环 2 崩溃诊断脚本
python tests/helpers/diag_cycle2_crash.py
```

### 6.3 诊断辅助

- `tests/helpers/diag_cycle2_crash.py`：最小复现循环 2 崩溃，抓 WER dump
- 崩溃 dump 位置：`<转储目录(默认系统临时目录/terminjector_dumps, 可用 TI_DUMP_DIR 覆盖)>\cmd_dumps\cmd.exe.<pid>.dmp`
- 分析命令：
  ```
  cdb -z <dump_path> -c ".sympath srv*C:\symbols*...;.exepath ...;.reload;~* kn 50;!analyze -v;q"
  ```

---

## 7. 风险点

| 风险 | 影响 | 缓解/状态 |
|------|------|-----------|
| 循环 2 ReadConsoleW_Detour GS cookie 损坏 | cmd 崩溃 | 调试中，已定位到栈布局问题 |
| ReadDetour 线程未及时退出 | 卸载时 trampoline 被释放后 AV | DisableAll 先禁用，保留 trampoline |
| LoadCount 不能归零 | DLL 无法卸载 | 远程 FreeLibrary 解决 |
| 调试 CRT 链接 | 卸载后内存损坏 | 已切 Release CRT |