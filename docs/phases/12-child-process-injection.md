# Phase 12: 子进程注入（CreateProcess Hook）

> 本 Phase 实现 DLL 对目标进程子进程的自动注入。完成后，cmd.exe 启动的 mode.com / python / vim / less 等子进程也会被注入，其输出与输入经过同一套 Hook 链路，解决 Phase 5 暴露的"mode con: 无输出"问题，并为 Phase 6/8 的 vim/python 交互奠定基础。
>
> **实际开发顺序**：本 Phase 应在 Phase 5 完成后、Phase 6 开始前实施。编号为 12 仅是为了不打乱已有 Phase 6-11 的编号。

---

## 1. 背景与动机

### 1.1 问题

Phase 5 测试发现：cmd.exe 调用 `mode con: cols=80 lines=25` 后，mediator 收不到 mode 命令的输出，BufferHooks（SetConsoleScreenBufferSize / SetConsoleWindowInfo）也不被触发。

**根因**：`mode` 是外部命令（`mode.com`），作为 cmd.exe 的**子进程**运行。DLL 只注入了 cmd.exe，没有注入子进程。子进程的 Console API 调用不经过 Hook，输出直接写到 ConHost，mediator 收不到。

### 1.2 影响

| Phase | 受影响验证项 | 说明 |
|-------|-------------|------|
| 5 | `mode con: cols=120 lines=40` | mode.com 不被注入，BufferHooks 不触发 |
| 5 | Python `print(f"\033[10;5H*")` | python.exe 不被注入，光标定位不经过 Hook |
| 6 | python TUI 鼠标交互 | python.exe 不被注入，输入链路不通 |
| 8 | vim/less Alt Buffer | vim/less 不被注入，Alt Buffer 切换不经过 Hook |
| 11 | 多目标测试（python/vim/less） | 所有子进程程序都无法验证 |

### 1.3 目标

Hook `CreateProcessW/A`，在目标进程启动子进程时自动注入 DLL，使子进程的 Console API 也经过 Hook 链路，输出经 IPC 发送给 mediator。

---

## 2. 前置依赖

- Phase 5 完成（光标/缓冲区 Hook 可用，ConsoleState 缓存机制成熟）
- Phase 3 的 LazyInit / 管道连接 / Handshake 协议可复用

---

## 3. 涉及文件清单

```
src/
├── common/protocol/
│   └── Message.h                    # 扩展：ChildProcessNotify / ChildExitNotify 消息
├── mediator/
│   ├── Mediator.h                   # 扩展：多客户端管理
│   ├── Mediator.cpp                 # 扩展：多管道实例 + VtOutput 合并 + VtInput 分发
│   └── ChildSession.h               # 新建：子进程会话管理（管道实例 + 线程）
│   └── ChildSession.cpp
├── dll/
│   ├── hooks/
│   │   ├── ProcessHooks.h           # 新建：CreateProcess Hook 声明
│   │   └── ProcessHooks.cpp         # 新建：CreateProcessW/A Hook 实现
│   ├── LazyInit.cpp                 # 扩展：从环境变量读管道名（子进程模式）
│   └── dllmain.cpp                  # 扩展：注册 ProcessHooks
└── injector/
    └── Injector.cpp                 # 复用：CreateRemoteThread + LoadLibrary 注入逻辑
```

---

## 4. 详细任务

### 4.1 整体架构

```
cmd.exe (已注入 DLL)
  │
  │  CreateProcessW("mode.com ...")
  │  → ProcessHooks 拦截
  │
  │  1. 强制加入 CREATE_SUSPENDED 标志
  │  2. 调原始 CreateProcessW → 得到子进程 PID + 主线程句柄
  │  3. 发送 ChildProcessNotify(pid) 给 mediator
  │  4. mediator 创建管道实例 \\.\pipe\terminjector_<child_pid>，等待连接
  │  5. DLL 用 CreateRemoteThread + LoadLibrary 注入到子进程
  │  6. 子进程 DLL LazyInit：读环境变量获取管道名，连接，Handshake
  │  7. ResumeThread（如果原始 flags 没有 SUSPENDED）
  │
  ▼
mode.com (已注入 DLL)
  │
  │  子进程 DLL Hook 生效
  │  → SetConsoleScreenBufferSize 被 BufferHooks 拦截
  │  → WriteConsoleW 被 OutputHooks 拦截 → VtOutput 发 mediator
  │
  ▼
mediator (多管道实例)
  │
  │  合并所有 DLL 的 VtOutput → 写 stdout → WT 渲染
  │  VtInput → 分发给当前前台子进程 DLL
```

### 4.2 CreateProcessW/A Hook

```cpp
// ProcessHooks.cpp

DEFINE_ORIG_PTR(CreateProcessW, BOOL WINAPI(LPCWSTR, LPWSTR, LPSECURITY_ATTRIBUTES,
    LPSECURITY_ATTRIBUTES, BOOL, DWORD, LPVOID, LPCWSTR,
    LPSTARTUPINFOW, LPPROCESS_INFORMATION));

BOOL WINAPI CreateProcessW_Detour(LPCWSTR lpApplicationName, LPWSTR lpCommandLine,
    LPSECURITY_ATTRIBUTES lpProcessAttributes,
    LPSECURITY_ATTRIBUTES lpThreadAttributes,
    BOOL bInheritHandles, DWORD dwCreationFlags,
    LPVOID lpEnvironment, LPCWSTR lpCurrentDirectory,
    LPSTARTUPINFOW lpStartupInfo, LPPROCESS_INFORMATION lpProcessInfo) {

    ENSURE_INITIALIZED();

    // 1. 强制加入 CREATE_SUSPENDED，以便注入后再恢复
    //    注意：如果原 flags 已有 SUSPENDED，注入后不 Resume（尊重调用方意图）
    bool needResume = !(dwCreationFlags & CREATE_SUSPENDED);
    DWORD modifiedFlags = dwCreationFlags | CREATE_SUSPENDED;

    // 2. 设置环境变量：让子进程 DLL 知道自己是子进程模式
    //    TERMINJECTOR_CHILD=1 表示子进程模式
    //    TERMINJECTOR_PARENT_PIPE=\\.\pipe\terminjector_<parent_pid> 供 mediator 关联
    SetEnvironmentVariableW(L"TERMINJECTOR_CHILD", L"1");

    // 3. 调原始 CreateProcessW（带 SUSPENDED）
    BOOL ok = CreateProcessW_orig(lpApplicationName, lpCommandLine,
        lpProcessAttributes, lpThreadAttributes, bInheritHandles,
        modifiedFlags, lpEnvironment, lpCurrentDirectory,
        lpStartupInfo, lpProcessInfo);

    if (!ok) return FALSE;

    // 4. 通知 mediator：子进程即将创建，请准备管道实例
    //    mediator 收到后创建 \\.\pipe\terminjector_<child_pid> 并等待连接
    protocol::ChildProcessNotifyPayload notify{};
    notify.childPid = lpProcessInfo->dwProcessId;
    notify.parentPid = GetCurrentProcessId();
    SendToMediator(&notify, sizeof(notify), protocol::MessageType::ChildProcessNotify);

    // 5. 注入 DLL 到子进程（复用 Injector 的 CreateRemoteThread + LoadLibrary）
    //    注入完成后子进程 DllMain 触发 LazyInit，连接管道
    InjectDllToChild(lpProcessInfo->hProcess, lpProcessInfo->dwProcessId);

    // 6. 恢复子进程主线程（如果原始 flags 没有 SUSPENDED）
    if (needResume) {
        ResumeThread(lpProcessInfo->hThread);
    }

    return TRUE;
}
```

`CreateProcessA` Hook 同理，内部转换为 W 版本或直接 Hook A 版本。

### 4.3 注入策略

复用 Phase 2 的 `Injector::Inject` 逻辑（CreateRemoteThread + LoadLibraryW）：

```cpp
// ProcessHooks.cpp 内部辅助函数
static bool InjectDllToChild(HANDLE hProcess, uint32_t childPid) {
    // 1. 获取 DLL 路径（与当前 DLL 同路径）
    wchar_t dllPath[MAX_PATH] = {0};
    HMODULE hSelf = nullptr;
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                       GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                       reinterpret_cast<LPCWSTR>(&InjectDllToChild), &hSelf);
    GetModuleFileNameW(hSelf, dllPath, MAX_PATH);

    // 2. 在子进程分配内存，写入 DLL 路径
    size_t pathBytes = (wcslen(dllPath) + 1) * sizeof(wchar_t);
    LPVOID remoteBuf = VirtualAllocEx(hProcess, nullptr, pathBytes,
                                      MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!remoteBuf) return false;
    WriteProcessMemory(hProcess, remoteBuf, dllPath, pathBytes, nullptr);

    // 3. CreateRemoteThread 调用 LoadLibraryW
    HMODULE hK32 = GetModuleHandleW(L"kernel32.dll");
    FARPROC loadLib = GetProcAddress(hK32, "LoadLibraryW");
    HANDLE hThread = CreateRemoteThread(hProcess, nullptr, 0,
        reinterpret_cast<LPTHREAD_START_ROUTINE>(loadLib),
        remoteBuf, 0, nullptr);
    if (!hThread) {
        VirtualFreeEx(hProcess, remoteBuf, 0, MEM_RELEASE);
        return false;
    }

    // 4. 等待 LoadLibrary 完成（DllMain 执行完毕）
    WaitForSingleObject(hThread, 5000);  // 5s 超时
    CloseHandle(hThread);
    VirtualFreeEx(hProcess, remoteBuf, 0, MEM_RELEASE);

    LOG_INFO("ChildProcess injected: pid=%u dll=%ls", childPid, dllPath);
    return true;
}
```

**关键时序**：
```
CreateProcessW(SUSPENDED) → ChildProcessNotify → InjectDll → [子进程 DllMain + LazyInit + Handshake] → ResumeThread
```

子进程在 SUSPENDED 状态下被注入，DllMain 执行 LazyInit 连接管道。但 DllMain 中不能做重活（Loader Lock），所以 LazyInit 是懒加载的（首个 Hook 触发）。

**问题**：子进程在 SUSPENDED 状态下，LazyInit 不会触发（没有 API 调用）。

**解决方案**：ResumeThread 后，子进程开始执行，首个 Console API 调用触发 LazyInit。此时管道已就绪（mediator 在收到 ChildProcessNotify 后已创建管道实例）。

### 4.4 管道连接方案

#### 4.4.1 子进程管道命名

子进程 DLL 在 LazyInit 时，通过 `GetCurrentProcessId()` 获取子进程 PID，构造管道名：
```
\\.\pipe\terminjector_<child_pid>
```

与父进程管道名格式一致，仅 PID 不同。

#### 4.4.2 环境变量传播

父进程 DLL 在 `CreateProcessW_Detour` 中设置环境变量：
- `TERMINJECTOR_CHILD=1`：标记子进程模式
- `TERMINJECTOR_PARENT_PID=<parent_pid>`：父进程 PID，供 mediator 关联

子进程 DLL 在 LazyInit 时检查环境变量：
- 若 `TERMINJECTOR_CHILD=1`，按 `\\.\pipe\terminjector_<self_pid>` 连接管道
- 若无此变量（顶层目标进程），按原有逻辑连接

环境变量通过 CreateProcess 的 `lpEnvironment` 继承。若调用方传入自定义环境块，DLL 需在 Hook 中合并 `TERMINJECTOR_*` 变量到环境块。

#### 4.4.3 mediator 多管道实例

Windows 命名管道支持**多实例**：同一管道名可创建多个实例。mediator 利用此特性管理多个 DLL 连接。

```cpp
// Mediator 扩展：多客户端管理
class Mediator {
    // 主管道（父进程 DLL）
    std::unique_ptr<NamedPipeTransport> m_mainTransport;

    // 子进程会话列表（线程安全）
    std::mutex m_childMutex;
    std::vector<std::unique_ptr<ChildSession>> m_childSessions;

    // 收到 ChildProcessNotify 时创建新会话
    void OnChildProcessNotify(uint32_t childPid, uint32_t parentPid) {
        auto session = std::make_unique<ChildSession>(childPid);
        session->Start();  // 创建管道实例 + 接收线程
        std::lock_guard lock(m_childMutex);
        m_childSessions.push_back(std::move(session));
    }
};
```

```cpp
// ChildSession：单个子进程的管道实例 + 接收线程
class ChildSession {
    uint32_t m_childPid;
    std::unique_ptr<NamedPipeTransport> m_transport;
    std::thread m_recvThread;
    std::atomic<bool> m_running{false};

    void RecvLoop() {
        // 收 VtOutput → 转发给 mediator 主 stdout
        // 收 ByeAck → 清理会话
    }

public:
    void Start() {
        // 创建管道实例 \\.\pipe\terminjector_<child_pid>
        // 等待 DLL 连接
        // 启动接收线程
    }

    void SendVtInput(const uint8_t* data, size_t len) {
        // 转发 mediator 收到的 VtInput 给子进程 DLL
    }
};
```

### 4.5 VtOutput 合并

所有子进程的 VtOutput 都由 mediator 合并写入 stdout：
```
父进程 DLL VtOutput ─┐
子进程 DLL VtOutput ─┤── mediator stdout ── WT 渲染
子进程 DLL VtOutput ─┘
```

**合并策略**：直接顺序写入。子进程通常在前台运行（cmd.exe 在 WaitForSingleObject 等待），其输出与父进程不会交错。

### 4.6 VtInput 分发

mediator 收到 WT 的 VtInput 后，需决定发给哪个 DLL：
- **当前前台进程**：cmd.exe 启动子进程后会阻塞等待（WaitForSingleObject），此时输入应给子进程
- **判断方式**：mediator 维护"当前前台 PID"，子进程 Hello 时设为前台，ByeAck 时恢复父进程

```cpp
// mediator VtInput 分发
void Mediator::OnVtInput(const uint8_t* data, size_t len) {
    uint32_t foreground = m_foregroundPid.load();
    if (foreground == m_mainPid) {
        m_mainTransport->Send(...);  // 发给父进程
    } else {
        auto* session = FindChildSession(foreground);
        if (session) session->SendVtInput(data, len);
    }
}
```

### 4.7 退出处理

子进程退出时：
1. 子进程 DLL 检测到 pipe 断开（RecvPacket 失败）
2. DLL 发送 ByeAck 消息给 mediator（Phase 11 卸载逻辑）
3. mediator 收到 ByeAck，清理 ChildSession
4. mediator 将前台 PID 恢复为父进程

### 4.8 新增消息类型

```cpp
// Message.h 扩展
enum class MessageType : uint32_t {
    // ... 已有消息 ...

    // 子进程管理（Phase 12）
    ChildProcessNotify = 0x0060,  // DLL→mediator：子进程即将创建
    ChildExitNotify    = 0x0061,  // DLL→mediator：子进程已退出
};

// ChildProcessNotify payload（DLL→mediator）
struct ChildProcessNotifyPayload {
    uint32_t childPid;    // 子进程 PID
    uint32_t parentPid;   // 父进程 PID
};
static_assert(sizeof(ChildProcessNotifyPayload) == 8, "...");
```

---

## 5. 验证标准

| 测试 | 预期 | 说明 |
|------|------|------|
| cmd `mode con: cols=80 lines=25` | mediator 收到 mode.com 的 VtOutput，BufferHooks 触发日志 | mode.com 被注入 |
| cmd `echo hello` | 正常输出（回归） | 内部命令不受影响 |
| cmd `python -c "print('test')"` | mediator 收到 python 的 VtOutput | python.exe 被注入 |
| cmd `python -c "import os; os.system('echo nested')"` | 嵌套子进程输出可见 | 递归注入（python→cmd） |
| cmd 启动 vim，按 `:q` 退出 | vim 输出可见，退出后 cmd 恢复 | 子进程生命周期管理 |
| cmd `ver` | 正常输出（回归） | 无子进程场景不受影响 |

---

## 6. 风险点

| 风险 | 影响 | 缓解 |
|------|------|------|
| CreateProcessW Hook 导致无限递归 | 进程卡死 | thread_local 重入保护（同 LazyInit 模式） |
| 注入失败（权限不足） | 子进程不被注入，输出丢失 | 降级：子进程输出走 ConHost（与无注入时一致），日志告警 |
| ResumeThread 时机竞态 | 子进程首个 API 触发 LazyInit 时管道未就绪 | mediator 收到 ChildProcessNotify 后同步创建管道实例，再注入 DLL |
| 子进程不继承环境变量（自定义 lpEnvironment） | 子进程 DLL 无法识别子进程模式 | Hook 中合并 TERMINJECTOR_* 到 lpEnvironment |
| 递归注入深度失控（python→cmd→python...） | 资源泄漏 | 限制注入深度（环境变量 TERMINJECTOR_DEPTH 计数，>5 不注入） |
| WaitForSingleObject（cmd 等子进程）与 Hook 竞态 | 死锁 | Phase 8 的 Wait 句柄假映射提前部分实现，或本 Phase 简化处理 |
| mediator 多线程管道管理复杂 | 竞态/死锁 | ChildSession 独立线程 + 互斥锁保护会话列表 |

---

## 7. 交付物清单

- [ ] `ProcessHooks.cpp`：CreateProcessW/A Hook（CREATE_SUSPENDED + 注入 + Resume）
- [ ] `InjectDllToChild`：复用 Injector 逻辑注入子进程
- [ ] `Message.h` 扩展：ChildProcessNotify / ChildExitNotify 消息类型
- [ ] `ChildSession.h/.cpp`：子进程管道实例 + 接收线程
- [ ] `Mediator.cpp` 扩展：多客户端管理 + VtOutput 合并 + VtInput 分发
- [ ] `LazyInit.cpp` 扩展：环境变量检测（子进程模式）
- [ ] 验证 mode con: / python / vim 子进程注入

---

## 8. 与其他 Phase 的关系

```
Phase 5 (光标/Buffer) ──完成──► Phase 12 (子进程注入) ──► Phase 6 (输入链路)
                                    │
                                    ▼
                              子进程的输入/输出经过 Hook 链路
                              Phase 6 的 python/vim 交互可验证
                                    │
                                    ▼
                              Phase 8 (Alt Buffer) 的 vim/less 可验证
                                    │
                                    ▼
                              Phase 11 (多目标测试) 的 python/vim/less 可验证
```

**注**：Phase 12 的 Wait 句柄处理（cmd.exe 等待子进程）与 Phase 8 的 Wait 句柄假映射有重叠。若 Phase 8 尚未实现，Phase 12 可简化处理：不 Hook WaitForSingleObject，让 cmd.exe 正常等待（ConHost 的 wait handle 仍可用，因为子进程共享同一 ConHost session）。
