# Phase 2: 注入器 + 双模式入口

> 本 Phase 实现 `terminal-injector.exe` 的双模式入口与 DLL 注入器。完成后，用户可以通过命令行将 `injected.dll` 注入到任意运行中的控制台进程，并能（以 mediator 模式）启动中介程序等待 DLL 连接。

---

## 1. Phase 目标

1. 实现 `terminal-injector.exe` 命令行参数解析（`--inject <pid>` / `--mediator`）
2. 实现注入器：`CreateRemoteThread` + `LoadLibraryW` 远程加载 DLL
3. 实现进程权限提升（`SeDebugPrivilege`）
4. 实现 mediator 模式启动：创建命名管道服务端，等待 DLL 连接，完成 Hello 握手
5. 实现注入器与 mediator 的协同：注入时把管道名通过 DLL 导出函数 `Inject_Bootstrap` 传给 DLL
6. 验证：能注入 cmd.exe 并在 DebugView 看到 DLL 的 Hello 日志

---

## 2. 前置依赖

- Phase 1 全部完成（Logger、ITransport、Protocol、ConsoleTypes 可用）
- `injected.dll` 能编译（即使内部只有空 DllMain）

---

## 3. 涉及文件清单

```
src/
├── app/
│   └── main.cpp                       # 修改：填充双模式分发
├── injector/
│   ├── Injector.h                     # 修改：完整接口
│   ├── Injector.cpp                   # 新建：注入实现
│   └── ProcessHelper.h                # 新建：权限/进程查询辅助
│   └── ProcessHelper.cpp
├── mediator/
│   ├── Mediator.h                     # 修改：完整接口
│   └── Mediator.cpp                   # 新建：握手中介
└── dll/
    ├── dllmain.cpp                    # 修改：导出 Inject_Bootstrap
    └── exports.h                      # 新建：DLL 导出函数声明
```

---

## 4. 详细任务

### 4.1 用户使用流程（对齐需求）

用户使用方式有两种，都需支持：

**方式 A：先注入，再用 WT 接管**
```
# 1. 用户在普通 cmd 中找到目标 PID（如 cmd.exe 的 PID 1234）
# 2. 注入
terminal-injector.exe --inject 1234

# 3. 在 WT 中启动中介接管
wt.exe terminal-injector.exe --mediator --target-pid 1234
```

**方式 B（推荐，一行搞定）**：
```
# WT 中直接执行，中介内部先调 inject 子命令
wt.exe terminal-injector.exe --mediator --target-pid 1234
```

方式 B 中，mediator 进程会 fork 出自身以 `--inject` 模式执行注入，再回到 mediator 模式等待连接。

### 4.2 命令行参数设计

```
terminal-injector.exe --inject <pid> [--dll <path>]
    注入模式：将 injected.dll 注入到 <pid> 进程
    --dll 可选，默认与 exe 同目录的 injected.dll

terminal-injector.exe --mediator --target-pid <pid> [--pipe <name>]
    中介模式：作为 WT 子进程，建立管道等待 DLL 连接
    --pipe 可选，默认 \\.\pipe\terminjector_<pid>

terminal-injector.exe --version
terminal-injector.exe --help
```

参数解析**不引入第三方库**（CLI 简单），手写解析，避免增加依赖。

### 4.3 `main.cpp` 双模式分发

```cpp
#include "logging/Logger.h"
#include "injector/Injector.h"
#include "mediator/Mediator.h"
#include <windows.h>
#include <string>
#include <vector>

namespace terminjector {

struct CliArgs {
    enum class Mode { None, Inject, Mediator, Help, Version };
    Mode mode = Mode::None;
    uint32_t targetPid = 0;
    std::wstring dllPath;
    std::wstring pipeName;
};

// 简单参数解析（Phase 2 不引入第三方 CLI 库）
static CliArgs ParseArgs(int argc, char* argv[]) {
    CliArgs args;
    // 默认 dll 路径：与 exe 同目录的 injected.dll
    args.dllPath = GetDefaultDllPath();

    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--inject" && i + 1 < argc) {
            args.mode = CliArgs::Mode::Inject;
            args.targetPid = static_cast<uint32_t>(std::stoul(argv[++i]));
        } else if (a == "--mediator") {
            args.mode = CliArgs::Mode::Mediator;
        } else if (a == "--target-pid" && i + 1 < argc) {
            args.targetPid = static_cast<uint32_t>(std::stoul(argv[++i]));
        } else if (a == "--dll" && i + 1 < argc) {
            args.dllPath = ToWide(argv[++i]);
        } else if (a == "--pipe" && i + 1 < argc) {
            args.pipeName = ToWide(argv[++i]);
        } else if (a == "--version") {
            args.mode = CliArgs::Mode::Version;
        } else if (a == "--help" || a == "-h") {
            args.mode = CliArgs::Mode::Help;
        }
    }

    if (args.mode == CliArgs::Mode::Mediator) {
        if (args.pipeName.empty() && args.targetPid != 0) {
            args.pipeName = MakePipeName(args.targetPid);
        }
    }
    return args;
}

int Run(int argc, char* argv[]) {
    Logger::Initialize(L"terminal-injector.log", LogLevel::Debug);
    auto args = ParseArgs(argc, argv);

    switch (args.mode) {
        case CliArgs::Mode::Inject: {
            LOG_INFO("Inject mode, pid=%u dll=%ls", args.targetPid, args.dllPath.c_str());
            Injector injector;
            if (!injector.Inject(args.targetPid, args.dllPath, args.pipeName)) {
                LOG_ERROR("Inject failed");
                return 1;
            }
            LOG_INFO("Inject succeeded");
            return 0;
        }
        case CliArgs::Mode::Mediator: {
            LOG_INFO("Mediator mode, targetPid=%u pipe=%ls",
                     args.targetPid, args.pipeName.c_str());
            Mediator mediator;
            return mediator.Run(args.targetPid, args.pipeName, args.dllPath);
        }
        case CliArgs::Mode::Help:
            PrintHelp();
            return 0;
        case CliArgs::Mode::Version:
            std::printf("terminal-injector 0.1.0\n");
            return 0;
        default:
            std::fprintf(stderr, "Unknown mode. Use --help.\n");
            return 1;
    }
}

} // namespace terminjector

int main(int argc, char* argv[]) {
    return terminjector::Run(argc, argv);
}
```

### 4.4 注入器实现（`CreateRemoteThread` + `LoadLibraryW`）

#### 4.4.1 `Injector.h`

```cpp
#pragma once
#include <windows.h>
#include <string>
#include <cstdint>

namespace terminjector {

// DLL 注入器：将 injected.dll 远程加载到目标进程
class Injector {
public:
    Injector();
    ~Injector();

    // 注入 DLL 到目标进程
    // targetPid: 目标进程 PID
    // dllPath:   injected.dll 绝对路径（短路径，避免空格问题）
    // pipeName:  传给 DLL 的命名管道名（DLL 用此连接 mediator）
    // 返回 true 成功
    bool Inject(uint32_t targetPid, const std::wstring& dllPath,
                const std::wstring& pipeName);

private:
    // 提升 SeDebugPrivilege
    bool EnableDebugPrivilege();

    // 在目标进程分配并写入数据
    bool RemoteWrite(HANDLE hProcess, const void* data, size_t len, void*& outRemoteAddr);

    // 远程调用 LoadLibraryW 加载 DLL
    HMODULE RemoteLoadLibrary(HANDLE hProcess, const std::wstring& dllPath);

    // 远程调用 DLL 导出的 Inject_Bootstrap(pipeName) 传参
    bool RemoteCallBootstrap(HANDLE hProcess, HMODULE hRemoteDll,
                             const std::wstring& pipeName);
};

} // namespace terminjector
```

#### 4.4.2 `Injector.cpp` 核心实现

```cpp
#include "Injector.h"
#include "ProcessHelper.h"
#include "logging/Logger.h"
#include "dll/exports.h"

namespace terminjector {

bool Injector::Inject(uint32_t targetPid, const std::wstring& dllPath,
                      const std::wstring& pipeName) {
    if (!EnableDebugPrivilege()) {
        LOG_WARN("EnableDebugPrivilege failed, continuing anyway");
    }

    // 打开目标进程
    HANDLE hProcess = OpenProcess(
        PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION |
        PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE,
        FALSE, targetPid);
    if (!hProcess) {
        LOG_ERROR("OpenProcess(%u) failed: %lu", targetPid, GetLastError());
        return false;
    }
    auto closeProc = wil::scope_exit([&] { CloseHandle(hProcess); });

    // 校验目标架构（必须 x64）
    if (!ProcessHelper::IsX64Process(hProcess)) {
        LOG_ERROR("Target process is not x64, aborting");
        return false;
    }

    // 1. 远程加载 DLL
    HMODULE hRemoteDll = RemoteLoadLibrary(hProcess, dllPath);
    if (!hRemoteDll) {
        LOG_ERROR("RemoteLoadLibrary failed");
        return false;
    }
    LOG_INFO("Remote DLL loaded at %p", hRemoteDll);

    // 2. 调用 DLL 导出的 Inject_Bootstrap 传入管道名
    //    DLL 在 Bootstrap 中连接管道、读取快照、安装 Hook
    if (!RemoteCallBootstrap(hProcess, hRemoteDll, pipeName)) {
        LOG_ERROR("RemoteCallBootstrap failed");
        return false;
    }

    LOG_INFO("Injection complete, pid=%u", targetPid);
    return true;
}

bool Injector::EnableDebugPrivilege() {
    HANDLE hToken;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, &hToken))
        return false;
    auto closeToken = wil::scope_exit([&] { CloseHandle(hToken); });

    LUID luid;
    if (!LookupPrivilegeValueW(nullptr, SE_DEBUG_NAME, &luid)) return false;

    TOKEN_PRIVILEGES tp{};
    tp.PrivilegeCount = 1;
    tp.Privileges[0].Luid = luid;
    tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED;

    return AdjustTokenPrivileges(hToken, FALSE, &tp, sizeof(tp), nullptr, nullptr);
}

HMODULE Injector::RemoteLoadLibrary(HANDLE hProcess, const std::wstring& dllPath) {
    // 用短路径避免远程进程命令行解析问题
    std::wstring shortPath;
    if (!ProcessHelper::ToShortPath(dllPath, shortPath)) {
        LOG_ERROR("ToShortPath failed for %ls", dllPath.c_str());
        return nullptr;
    }

    // 在目标进程写入路径字符串
    size_t bytes = (shortPath.size() + 1) * sizeof(wchar_t);
    LPVOID remoteStr = VirtualAllocEx(hProcess, nullptr, bytes,
                                      MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!remoteStr) {
        LOG_ERROR("VirtualAllocEx failed: %lu", GetLastError());
        return nullptr;
    }

    SIZE_T written = 0;
    if (!WriteProcessMemory(hProcess, remoteStr, shortPath.c_str(), bytes, &written)) {
        LOG_ERROR("WriteProcessMemory failed: %lu", GetLastError());
        VirtualFreeEx(hProcess, remoteStr, 0, MEM_RELEASE);
        return nullptr;
    }

    // 获取 LoadLibraryW 地址（kernel32 在所有进程同地址，x64 下成立）
    auto pLoadLib = reinterpret_cast<LPTHREAD_START_ROUTINE>(
        GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "LoadLibraryW"));

    // 远程线程调用 LoadLibraryW(remoteStr)
    HANDLE hThread = CreateRemoteThread(
        hProcess, nullptr, 0, pLoadLib, remoteStr, 0, nullptr);
    if (!hThread) {
        LOG_ERROR("CreateRemoteThread(LoadLibraryW) failed: %lu", GetLastError());
        VirtualFreeEx(hProcess, remoteStr, 0, MEM_RELEASE);
        return nullptr;
    }

    WaitForSingleObject(hThread, 10000); // 10s 超时
    DWORD exitCode = 0;
    GetExitCodeThread(hThread, &exitCode);
    CloseHandle(hThread);
    VirtualFreeEx(hProcess, remoteStr, 0, MEM_RELEASE);

    // exitCode 即 HMODULE（远程 LoadLibraryW 返回值）
    return reinterpret_cast<HMODULE>(exitCode);
}

bool Injector::RemoteCallBootstrap(HANDLE hProcess, HMODULE hRemoteDll,
                                   const std::wstring& pipeName) {
    // 远程 DLL 中 Inject_Bootstrap 的地址 = 本地 DLL 的偏移 + 远程基址
    // 但更稳妥的做法：DLL 加载时 DllMain 已运行，Bootstrap 应在 DllMain 中自动触发
    // 这里改为：通过远程 WriteProcessMemory 写入管道名到 DLL 的全局变量
    // 或更优雅：直接在 DLL 的 DllMain 中读取"注入参数块"
    //
    // 方案（推荐）：注入器在目标进程分配一块共享内存，写入 BootstrapParams，
    // DLL DllMain 通过 GetCommandLine 或环境变量拿到参数块地址。
    //
    // 简化方案：DLL DllMain 读取环境变量 TERMINJECTOR_PIPE，注入器在注入前
    // 用 CreateEnvironmentBlock/SetEnvironmentVariable 不可用（那是当前进程的）
    // → 改用：注入器创建管道后，DLL 用约定名称 \\.\pipe\terminjector_<自身PID> 连接
    //          这样注入器无需传参，DLL 用 GetCurrentProcessId 自动构造管道名
    //
    // 采用简化方案：管道名约定为 \\.\pipe\terminjector_<targetPid>
    // DLL DllMain 中 GetCurrentProcessId() 即得 targetPid，构造管道名连接
    // 注入器 Inject() 的 pipeName 参数仅用于校验/日志
    LOG_INFO("Bootstrap via convention: pipe=\\\\.\\pipe\\terminjector_%u (DLL self-discovers)",
             GetCurrentProcessId());
    // DLL 侧在 DllMain 的 DLL_PROCESS_ATTACH 中（Phase 3 实现）：
    //   1. 构造管道名 MakePipeName(GetCurrentProcessId())
    //   2. 连接 mediator
    //   3. 懒加载初始化（首个 Hook 触发时）
    return true;
}

} // namespace terminjector
```

**关键决策**：DLL **不**通过参数接收管道名，而是用约定 `\\.\pipe\terminjector_<GetCurrentProcessId()>` 自动发现。这样：
- 注入器只需 `LoadLibraryW`，不需要二次远程调用导出函数
- DLL DllMain 中 `GetCurrentProcessId()` 返回目标进程 PID（DLL 在目标进程地址空间执行）
- mediator 创建管道时用 `MakePipeName(targetPid)`，与 DLL 自发现的名称一致

#### 4.4.3 `ProcessHelper.h` / `.cpp`

```cpp
#pragma once
#include <windows.h>
#include <string>

namespace terminjector::ProcessHelper {

// 判断目标进程是否 x64
bool IsX64Process(HANDLE hProcess);

// 长路径转短路径（去除空格，便于远程写入）
bool ToShortPath(const std::wstring& longPath, std::wstring& outShort);

// 查找进程 PID（按进程名，可选，用于交互式选择目标）
uint32_t FindProcessByName(const std::wstring& name);

} // namespace terminjector::ProcessHelper
```

`IsX64Process` 用 `IsWow64Process`：x64 进程返回 `!isWow64`。

### 4.5 Mediator 实现（握手中介）

#### 4.5.1 `Mediator.h`

```cpp
#pragma once
#include "transport/ITransport.h"
#include <memory>
#include <cstdint>
#include <string>

namespace terminjector {

// 中介程序：被 WT 启动，桥接 DLL 与 WT 的 ConPTY
class Mediator {
public:
    Mediator();
    ~Mediator();

    // 主循环
    // targetPid: 目标进程 PID（用于构造管道名、触发注入）
    // pipeName:  命名管道名
    // dllPath:   injected.dll 路径（mediator 内部 fork inject 子命令时用）
    // 返回退出码
    int Run(uint32_t targetPid, const std::wstring& pipeName,
            const std::wstring& dllPath);

private:
    // 创建管道服务端并等待 DLL 连接
    bool WaitForDllConnect(uint32_t targetPid);

    // 执行 Hello 握手
    bool Handshake();

    // 桥接循环：stdin(WT) ↔ pipe(DLL)
    void BridgeLoop();

    // 触发注入：fork 自身以 --inject 模式运行
    bool SpawnInjector(uint32_t targetPid, const std::wstring& dllPath,
                       const std::wstring& pipeName);

    std::unique_ptr<ITransport> m_transport;
    uint32_t m_targetPid = 0;
};

} // namespace terminjector
```

#### 4.5.2 `Mediator.cpp` 核心流程

```cpp
#include "Mediator.h"
#include "transport/NamedPipeTransport.h"
#include "transport/TransportFactory.h"
#include "protocol/Message.h"
#include "protocol/MessageSerializer.h"
#include "logging/Logger.h"
#include <windows.h>
#include <cstdio>

namespace terminjector {

int Mediator::Run(uint32_t targetPid, const std::wstring& pipeName,
                  const std::wstring& dllPath) {
    m_targetPid = targetPid;

    // 1. 创建管道服务端（先建好，再注入，避免 DLL 连接时管道还没创建）
    m_transport = CreateTransport(TransportType::NamedPipe, pipeName,
                                  NamedPipeTransport::Role::Server);
    LOG_INFO("Mediator creating pipe: %ls", pipeName.c_str());
    if (!m_transport->Connect()) {
        LOG_ERROR("Pipe Connect (server wait) failed");
        return 1;
    }
    LOG_INFO("Pipe created, waiting for DLL...");

    // 2. 触发注入（fork 自身 --inject 模式）
    if (!SpawnInjector(targetPid, dllPath, pipeName)) {
        LOG_ERROR("SpawnInjector failed");
        return 1;
    }

    // 3. 等待 DLL 连接（Connect 已在 server 模式下等待，这里其实阻塞在 Connect）
    //    注意：上面 Connect 在 server 模式是阻塞等待客户端，
    //    需要调整为：先 CreateNamedPipe，再 SpawnInjector，再 ConnectNamedPipe
    //    见 4.5.3 时序图

    // 4. Hello 握手
    if (!Handshake()) {
        LOG_ERROR("Handshake failed");
        return 1;
    }
    LOG_INFO("Handshake OK, entering bridge loop");

    // 5. 桥接循环（Phase 2 仅占位，Phase 3+ 实现真实桥接）
    BridgeLoop();

    return 0;
}

bool Mediator::Handshake() {
    using namespace protocol;
    // 收 Hello
    std::vector<uint8_t> buf(4096);
    // 阻塞读一个包
    MessageType type;
    std::vector<uint8_t> payload;
    // 读取逻辑见 MessageSerializer，需循环 Recv 凑够一个完整包
    if (!RecvPacket(*m_transport, type, payload)) {
        LOG_ERROR("Recv Hello failed");
        return false;
    }
    if (type != MessageType::Hello) {
        LOG_ERROR("Expected Hello, got %u", static_cast<uint32_t>(type));
        return false;
    }
    HelloPayload hello{};
    if (payload.size() >= sizeof(hello)) {
        std::memcpy(&hello, payload.data(), sizeof(hello));
    }
    LOG_INFO("Hello: pid=%u cols=%u rows=%u mode=0x%04x",
             hello.targetPid, hello.bufferCols, hello.bufferRows, hello.consoleMode);

    // 回 HelloAck
    auto ack = Serialize(MessageType::HelloAck, nullptr, 0);
    if (m_transport->Send(ack.data(), ack.size()) != static_cast<int>(ack.size())) {
        LOG_ERROR("Send HelloAck failed");
        return false;
    }
    return true;
}

void Mediator::BridgeLoop() {
    // Phase 2 占位：仅循环收发，不解析
    // Phase 3+ 实现真实桥接：stdin → pipe，pipe → stdout
    LOG_INFO("BridgeLoop placeholder (Phase 3+ will implement)");
}

bool Mediator::SpawnInjector(uint32_t targetPid, const std::wstring& dllPath,
                             const std::wstring& pipeName) {
    // fork 自身：terminal-injector.exe --inject <pid> --dll <path>
    std::wstring cmd = L"terminal-injector.exe --inject " +
                       std::to_wstring(targetPid) +
                       L" --dll \"" + dllPath + L"\"";

    STARTUPINFOW si{}; si.cb = sizeof(si);
    PROCESS_INFORMATION pi{};
    if (!CreateProcessW(nullptr, const_cast<LPWSTR>(cmd.c_str()),
                        nullptr, nullptr, FALSE, 0, nullptr, nullptr, &si, &pi)) {
        LOG_ERROR("CreateProcessW(injector) failed: %lu", GetLastError());
        return false;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    LOG_INFO("Injector spawned: %ls", cmd.c_str());
    return true;
}

} // namespace terminjector
```

#### 4.5.3 时序图（关键）

```
mediator 进程                 injector 进程              目标进程
    │                              │                         │
    │ 1. CreateNamedPipe           │                         │
    │   (管道创建，未 Connect)       │                         │
    │                              │                         │
    │ 2. SpawnInjector ────────────►│                         │
    │   (fork --inject)            │                         │
    │                              │                         │
    │ 3. ConnectNamedPipe(阻塞)     │ 3. OpenProcess           │
    │   等待 DLL 连接                │   VirtualAllocEx         │
    │                              │   WriteProcessMemory      │
    │                              │   CreateRemoteThread ────►│ LoadLibraryW(injected.dll)
    │                              │                         │   DllMain ATTACH 执行
    │                              │                         │   (Phase 3: 连接管道 + Hook)
    │                              │                         │
    │ ◄─────────────────────────────────────────────────────  │ CreateFile(pipe)
    │   ConnectNamedPipe 返回        │                         │   (DLL 作为 client 连接)
    │                              │ 4. 注入器退出             │
    │                              ◄─                         │
    │ 5. Handshake (收 Hello)        │                         │
    │   回 HelloAck                  │                         │
    │ 6. BridgeLoop                  │                         │
```

**注意**：mediator 必须先 `CreateNamedPipe` 再 `SpawnInjector`，否则 DLL 加载后立刻连接管道会失败。`NamedPipeTransport::Connect()` 在 Server 模式下要拆分为 `Create()` 和 `WaitConnect()` 两步，或文档约定注入器注入后短暂重试连接（DLL 侧已有重试逻辑）。

→ **Phase 1 的 `NamedPipeTransport::Connect` 需要调整**：Server 端拆为 `Create()` + `WaitClient()`，本 Phase 在文档中说明，代码调整在 Phase 2 实施时一并完成。

### 4.6 DLL 导出与 DllMain 调整

#### 4.6.1 `exports.h`

```cpp
#pragma once
#include <windows.h>

namespace terminjector {

// DLL 不再需要显式导出函数（采用约定管道名自发现方案）
// 但保留一个 QueryInterface 风格的导出，便于调试与未来扩展
extern "C" __declspec(dllexport)
const char* Inject_QueryVersion();

} // namespace terminjector
```

#### 4.6.2 `dllmain.cpp`（Phase 2 阶段）

```cpp
#include <windows.h>
#include "logging/Logger.h"
#include "transport/NamedPipeTransport.h"
#include "protocol/Message.h"
#include "protocol/MessageSerializer.h"

namespace terminjector {

// Phase 3 将实现真正的 Hook；此处仅做最小连接 + Hello
extern "C" __declspec(dllexport) const char* Inject_QueryVersion() {
    return "0.1.0";
}

// 全局：DLL 与 mediator 的传输通道
static std::unique_ptr<ITransport> g_transport;

// 在首个 Hook 触发时调用（Phase 3）；Phase 2 在 DllMain 中直接尝试连接验证
static bool ConnectToMediator() {
    auto pipeName = MakePipeName(GetCurrentProcessId());
    g_transport = std::make_unique<NamedPipeTransport>(pipeName,
                                                       NamedPipeTransport::Role::Client);
    if (!g_transport->Connect()) {
        LOG_ERROR("DLL Connect to %ls failed", pipeName.c_str());
        return false;
    }
    LOG_INFO("DLL connected to mediator");

    // 发送 Hello（Phase 2 简化版，状态快照在 Phase 3 完整实现）
    protocol::HelloPayload hello{};
    hello.targetPid = GetCurrentProcessId();
    // 其他字段留 0，Phase 3 用真实快照填充
    auto pkt = protocol::Serialize(protocol::MessageType::Hello,
                                   &hello, sizeof(hello));
    g_transport->Send(pkt.data(), pkt.size());

    // 等 HelloAck
    protocol::MessageType type;
    std::vector<uint8_t> payload;
    if (!RecvPacket(*g_transport, type, payload) || type != protocol::MessageType::HelloAck) {
        LOG_ERROR("HelloAck not received");
        return false;
    }
    LOG_INFO("HelloAck received, handshake done");
    return true;
}

} // namespace terminjector

BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID) {
    using namespace terminjector;
    if (reason == DLL_PROCESS_ATTACH) {
        // 注意：AGENTS.md 与 Phase 1 风险表强调 DllMain 不能干重活（Loader Lock）
        // Phase 2 阶段为验证注入链路，暂在 DllMain 中尝试连接
        // Phase 3 将改为懒加载（首个 Hook 触发 ConnectToMediator）
        // 日志目录：TI_INJECTED_LOG_DIR 优先，否则 GetTempPathW()（不硬编码固定路径）
        Logger::Initialize(GetInjectedLogDir() + L"\\injected.log", LogLevel::Debug);
        LOG_INFO("injected.dll loaded in pid=%u", GetCurrentProcessId());

        // 尝试连接 mediator（Phase 2 验证用，Phase 3 移走）
        ConnectToMediator();
    } else if (reason == DLL_PROCESS_DETACH) {
        LOG_INFO("injected.dll unloaded");
        if (g_transport) g_transport->Disconnect();
        Logger::Shutdown();
    }
    return TRUE;
}
```

> **风险标注**：上述在 DllMain 调用 `CreateFileW`(连接管道) 可能触发 Loader Lock 风险（管道等待期间持有加载器锁）。Phase 2 仅作端到端验证，**Phase 3 必须改为懒加载**：DllMain 仅 `DisableThreadLibraryCalls` + `MH_Initialize`，真正的连接放在第一个 Hook 触发时。

---

## 5. 验证标准（Phase 2 完成判定）

### 5.1 注入器独立验证

```powershell
# 启动一个 cmd.exe（手动）
# 用任务管理器查其 PID，假设 1234
terminal-injector.exe --inject 1234
```

预期：
- 控制台输出 `Inject succeeded`
- 系统临时目录下 `injected_<pid>_<时间戳>.log` 出现 `injected.dll loaded in pid=1234`
- DebugView 看到 DLL 日志
- 此时 cmd.exe 行为不变（Phase 2 未装 Hook）

### 5.2 双模式协同验证

```powershell
# 在 WT 中运行
wt.exe terminal-injector.exe --mediator --target-pid 1234
```

预期：
- mediator 创建管道、spawn injector、注入完成
- mediator 日志显示 `Handshake OK, entering bridge loop`
- DLL 日志显示 `HelloAck received, handshake done`
- WT 窗口停在 `BridgeLoop placeholder` 处（Phase 3 实现真实桥接）

### 5.3 权限验证

- 普通用户注入同权限进程：成功
- 普通用户注入管理员进程：失败并日志提示权限不足（预期行为）
- 管理员运行：可注入任意进程

### 5.4 错误场景

| 场景 | 预期行为 |
|------|----------|
| PID 不存在 | `OpenProcess failed: 87` 日志，退出码 1 |
| PID 是 32 位进程 | `Target process is not x64`，退出码 1 |
| DLL 路径不存在 | `RemoteLoadLibrary` 返回 0，`LoadLibrary failed` |
| mediator 已有同名管道 | `CreateNamedPipe failed: 5`，退出码 1 |

---

## 6. 风险点

| 风险 | 缓解 |
|------|------|
| DllMain 中连接管道触发 Loader Lock | Phase 2 仅验证，Phase 3 改懒加载 |
| 注入器与 mediator 竞态（管道未创建 DLL 已连） | mediator 先 CreateNamedPipe 再 SpawnInjector；DLL 侧 `CreateFile` 失败则 `WaitNamedPipe` 重试 |
| `CreateRemoteThread` 被杀软拦截 | 文档说明需加白名单；可选 Phase 10 换手动映射注入 |
| 远程 `LoadLibraryW` 失败但无错误码 | 用 `GetExitCodeThread` 拿 HMODULE；0 表示失败，检查 `GetLastError` 在远程不可见，需 DLL 内部日志 |
| `wil::scope_exit` 需引入 wil 依赖 | 改用手写 RAII 包装 `struct HandleGuard { HANDLE h; ~HandleGuard(){ if(h) CloseHandle(h); } };` |

---

## 7. 交付物清单

- [ ] `src/app/main.cpp` 双模式分发完整
- [ ] `src/injector/Injector.cpp` 注入实现（含权限提升、架构校验、远程 LoadLibrary）
- [ ] `src/injector/ProcessHelper.cpp` 辅助函数
- [ ] `src/mediator/Mediator.cpp` 握手中介（含 SpawnInjector）
- [ ] `src/dll/exports.h`、`src/dll/dllmain.cpp` Hello 发送
- [ ] `NamedPipeTransport` Server 端拆分 `Create()` + `WaitClient()`（Phase 1 接口调整）
- [ ] `RecvPacket` 工具函数（在 `MessageSerializer` 中补，循环 Recv 凑包）
- [ ] 5.1 ~ 5.4 验证全过
