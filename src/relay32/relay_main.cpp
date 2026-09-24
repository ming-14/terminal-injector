// relay32.dll —— 32 位中继 DLL（Phase 23）
// 详见 docs/2026-09-23-child-injection-32bit-report.md
//
// 为什么需要它：
//   32 位进程（如 C:\Windows\py.exe）无法被 x64 injected.dll 注入 ——
//   跨位数 LoadLibraryW 必然失败（实测 x64 cmd 成功 / SysWOW64 cmd 返回 0）——
//   于是它拉起的 64 位孙进程漏网，输出写进目标自己的 ConHost，WT 里一片空白。
//
// 职责（只做四件事，不接管 Console）：
//   1. 钩住 CreateProcessW / CreateProcessA
//   2. 强制 CREATE_SUSPENDED，把子进程冻住
//   3. 按子进程位数分派：
//        32 位 → 中继自己注入 relay32.dll（同位数，本地 kernel32 地址有效），
//                注入完成后立即恢复，无需等回执
//        64 位 → 上报 mediator（只有 x64 侧有注入能力），等 RelayChildAck 再恢复
//   4. 上报前先用 ChildProcessNotify 让 mediator 把会话管道建好
//
// 为什么必须"冻结 + 等回执"而不是"先恢复再补注入"：
//   实测子进程从创建到首行输出只有 64~80ms，而注入一针（首次 LoadLibraryW
//   装完所有 Hook 才返回）约 59ms —— 余量仅 5~20ms，"轮询发现 + 事后注入"
//   必然偶发丢首屏。冻结子进程把这段竞态彻底消除：实测 notify→ack 往返 10ms，
//   整个 detour 持有时长 79ms；对挂起进程注入（含管道握手）实测 70ms。
//
// 不接管 Console 的原因：
//   中继所在的启动器（py.exe 之流）本身不产出需要显示的终端内容。
//   "32 位程序本体被接管"是另一件事，属 TODO(32bit-target)，本文件不做。
#include <windows.h>
#include <MinHook.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <mutex>
#include <set>
#include <string>
#include <vector>

#include "child_inject.h"
#include "RemoteParams.h"
#include "logging/Logger.h"
#include "logging/SafeOutputDebugString.h"
#include "protocol/Message.h"
#include "protocol/MessageSerializer.h"
#include "process/ProcessBitness.h"
#include "transport/NamedPipeTransport.h"
#include "transport/PipeParams.h"

namespace terminjector::relay32 {
namespace {

// ============================================================
// 常量
// ============================================================

// detour 内等"与 mediator 建连"的上限
// 正常情况下参数在注入后立刻下发、连接在 ms 级建立；此超时只兜底异常
constexpr DWORD kConnectWaitMs = 8000;
// worker 等注入参数（RemotePipeSetup）的上限，与 injected.dll 的 LazyInit 一致
constexpr DWORD kPipeParamsWaitMs = 10000;
// 等 mediator 回执的上限。正常路径 ~70ms；只在 mediator 异常时才走到这里
constexpr DWORD kAckTimeoutMs = 10000;

// ============================================================
// 全局状态
// ============================================================

HMODULE g_hSelf = nullptr;

// 与 mediator 的连接（manual-reset，置位后不再复位）
HANDLE g_connectedEvent = nullptr;

std::atomic<bool> g_connected{false};
std::atomic<bool> g_stopping{false};
std::atomic<uint32_t> g_mediatorPid{0};
// 只警告一次"连接未就绪，按无中继透传"，避免刷日志
std::atomic<bool> g_passThroughWarned{false};

// 传输对象：一旦建成就不再析构（进程内只建一次，随进程退出回收）
//
// 为什么故意不 delete：detour 在任意线程上按裸指针使用它，而 worker 在管道
// 断开时会走到回收路径；若在此处 delete，detour 侧就存在悬垂窗口。
// 本 DLL 只活在一个短命启动器进程内，泄漏一个管道句柄无实际代价。
// 这条与本项目既有的"trampoline 池随进程退出回收"是同一取舍。
std::atomic<NamedPipeTransport*> g_transport{nullptr};

std::mutex g_ackLock;
std::condition_variable g_ackCv;
std::set<uint32_t> g_ackedChildren;

// DllMain 阶段装的钩子结果（Logger 尚未就绪，改由 worker 线程补记日志）
// 0=未尝试 1=成功 -1=失败
std::atomic<int> g_hookW{0};
std::atomic<int> g_hookA{0};

// detour 重入保护（防止 detour 内部间接再进 CreateProcess）
thread_local bool t_inDetour = false;

// ============================================================
// 日志
// ============================================================

// 初始化日志：优先 TI_INJECTED_LOG_DIR（测试/诊断覆盖），否则 <DLL 目录>\logs。
// 与被注入进程的日志集中在一处，便于按 pid 时间线对照排查。
//
// 刻意不在 DllMain 里初始化：DllMain 持 Loader Lock，此时打开文件若触发
// 任何隐式 DLL 加载会死锁。injected.dll 同样把 Logger 初始化推迟到工作线程。
void InitLogger() {
    wchar_t dllPath[MAX_PATH] = {0};
    if (GetModuleFileNameW(g_hSelf, dllPath, MAX_PATH) == 0) {
        return;
    }
    std::wstring dir(dllPath);
    const size_t pos = dir.find_last_of(L"\\/");
    if (pos == std::wstring::npos) {
        return;
    }
    dir.resize(pos);

    std::wstring logDir;
    wchar_t envBuf[MAX_PATH] = {0};
    const DWORD n = GetEnvironmentVariableW(L"TI_INJECTED_LOG_DIR", envBuf, MAX_PATH);
    if (n > 0 && n < MAX_PATH) {
        logDir.assign(envBuf);
    } else {
        logDir = dir + L"\\logs";
    }
    CreateDirectoryW(logDir.c_str(), nullptr);

    SYSTEMTIME st{};
    GetLocalTime(&st);
    wchar_t name[128] = {0};
    swprintf_s(name, L"relay32_%lu_%04d%02d%02d-%02d%02d%02d.log",
               GetCurrentProcessId(), st.wYear, st.wMonth, st.wDay,
               st.wHour, st.wMinute, st.wSecond);
    const std::wstring logPath = logDir + L"\\" + name;
    Logger::Initialize(logPath, LogLevel::Debug);
}

// ============================================================
// 基础工具
// ============================================================

bool GetSelfDllPath(std::wstring& out) {
    wchar_t path[MAX_PATH] = {0};
    if (GetModuleFileNameW(g_hSelf, path, MAX_PATH) == 0) {
        return false;
    }
    out.assign(path);
    return true;
}

// 子进程位数判定在 common/process/ProcessBitness.h（与 x64 侧 injected.dll
// 共用同一份：IsWow64Process 的语义随调用方位数而变，两边必须用同一判据）。

// ============================================================
// 与 mediator 的收发
// ============================================================

bool SendPacket(protocol::MessageType type, const void* payload, uint32_t len) {
    NamedPipeTransport* t = g_transport.load();
    if (t == nullptr || !t->IsConnected()) {
        return false;
    }
    const auto pkt = protocol::Serialize(type, payload, len);
    // NamedPipeTransport::Send 内部有 SRWLOCK 串行化，多线程并发调 CreateProcess
    // 时不会出现两帧交错
    const int sent = t->Send(pkt.data(), pkt.size());
    return sent == static_cast<int>(pkt.size());
}

// 等某个子进程的回执。响应已经先到（插入集合）也不会漏。
bool WaitForChildAck(uint32_t childPid, DWORD timeoutMs) {
    std::unique_lock<std::mutex> lock(g_ackLock);
    // 连接断了就等不到回执了，立即返回（子进程照样必须恢复）
    g_ackCv.wait_for(lock, std::chrono::milliseconds(timeoutMs), [childPid] {
        return g_ackedChildren.count(childPid) != 0 || !g_connected.load();
    });
    return g_ackedChildren.erase(childPid) != 0;
}

// worker：等注入参数 -> 建连 -> RelayHello -> 接收循环
//
// 用 CreateThread 而非 std::thread：本线程不需要 join（进程退出时随进程终止），
// 避免 DllMain(DETACH) 内 join 死锁。与 injected.dll 的 DllMain 做法一致。
DWORD WINAPI ConnectWorkerProc(LPVOID /*unused*/) {
    InitLogger();
    LOG_INFO("=== relay32 attached, pid=%lu bitness=32 ===", GetCurrentProcessId());
    LOG_INFO("hooks: CreateProcessW=%s CreateProcessA=%s",
             g_hookW.load() == 1 ? "ok" : "FAILED",
             g_hookA.load() == 1 ? "ok" : "FAILED");

    // 1. 等注入参数（父 DLL 注入完成后立刻经 RemotePipeSetup 下发）
    PipeParams params{};
    bool haveParams = false;
    for (DWORD waited = 0; waited < kPipeParamsWaitMs; waited += 50) {
        if (g_stopping.load()) {
            return 0;
        }
        if (GetPipeParams(params)) {
            haveParams = true;
            break;
        }
        Sleep(50);
    }
    if (!haveParams) {
        LOG_ERROR("pipe params not received within %lu ms; relay inactive "
                  "(children will run without hijack)", kPipeParamsWaitMs);
        return 0;
    }
    g_mediatorPid.store(params.mediatorPid);

    // 2. 连接 mediator 的会话管道（Client 端内置 5s 重试）
    auto* transport = new NamedPipeTransport(params.pipeName,
                                             NamedPipeTransport::Role::Client);
    if (!transport->Connect()) {
        LOG_ERROR("connect to mediator failed, pipe=%ls", params.pipeName);
        delete transport;
        return 0;
    }

    // 3. 服务端身份校验（与 injected.dll 同策略，防同名管道抢占/中间人）
    if (params.mediatorPid != 0) {
        const uint32_t serverPid = transport->GetServerProcessId();
        if (serverPid == 0 || serverPid != params.mediatorPid) {
            LOG_ERROR("server identity check FAILED: serverPid=%u expected=%u, disconnect",
                      serverPid, params.mediatorPid);
            transport->Disconnect();
            delete transport;
            return 0;
        }
    }
    LOG_INFO("connected to mediator: pipe=%ls mediatorPid=%u",
             params.pipeName, params.mediatorPid);

    g_transport.store(transport);

    // 4. RelayHello：本帧必须是会话首帧（mediator 侧按中继模式握手）
    protocol::RelayHelloPayload hello{};
    hello.pid = GetCurrentProcessId();
    hello.bitness = 32;
    const bool helloOk = SendPacket(protocol::MessageType::RelayHello,
                                    &hello, sizeof(hello));
    LOG_INFO("RelayHello sent=%d (pid=%u)", helloOk ? 1 : 0, hello.pid);

    // 5. 置位连接就绪 —— 必须在 RelayHello 之后，否则 detour 可能抢先发出
    //    RelayChildNotify，让 mediator 收到非 RelayHello 的首帧
    g_connected.store(true);
    SetEvent(g_connectedEvent);

    // 6. 接收循环：中继会话只会收到 RelayChildAck
    //
    // 必须用 Peek 轮询，不能用阻塞 RecvPacket：本会话的管道句柄**同时**承载
    // detour 线程的 Send（RelayChildNotify / ChildProcessNotify）。同步命名管道
    // 一次只能有一个 I/O 在飞，常驻的阻塞 ReadFile 会把同句柄的 WriteFile 永远
    // 堵住 —— 现象是 detour 卡死不返回、子进程一直冻着（实测阻塞时长 = 对端存活
    // 时长，对端一关才以 err=232 返回）。
    // 与 mediator 的 ChildSession::RecvLoop 同一策略（那边踩过同一个坑）。
    // 隔离复现见 tests/_probe/pipe_io_serialize_probe.py。
    uint8_t peekBuf[1];
    while (!g_stopping.load()) {
        NamedPipeTransport* t = g_transport.load();
        if (t == nullptr || !t->IsConnected()) {
            break;
        }
        const int peeked = t->Peek(peekBuf, 1);
        if (peeked < 0) {
            // Peek=-1：管道断开（含对端关闭）
            LOG_INFO("recv loop: pipe error/broken (Peek=%d), relay inactive", peeked);
            break;
        }
        if (peeked == 0) {
            // 无数据：短暂休眠后重试（有数据时 RecvPacket 不会长时间阻塞）
            Sleep(10);
            continue;
        }
        protocol::MessageType type{};
        std::vector<uint8_t> payload;
        if (!protocol::RecvPacket(t, type, payload)) {
            LOG_INFO("recv loop: pipe closed, relay inactive");
            break;
        }
        if (type == protocol::MessageType::RelayChildAck) {
            if (payload.size() >= sizeof(protocol::RelayChildAckPayload)) {
                protocol::RelayChildAckPayload ack{};
                std::memcpy(&ack, payload.data(), sizeof(ack));
                LOG_INFO("RelayChildAck: child=%u ok=%u", ack.childPid, ack.ok);
                {
                    std::lock_guard<std::mutex> lock(g_ackLock);
                    g_ackedChildren.insert(ack.childPid);
                }
                g_ackCv.notify_all();
            } else {
                LOG_WARN("RelayChildAck payload too small: %zu", payload.size());
            }
        } else {
            LOG_INFO("recv loop: unhandled msg type=0x%08X len=%zu",
                     static_cast<uint32_t>(type), payload.size());
        }
    }

    g_connected.store(false);
    g_ackCv.notify_all();
    return 0;
}

// ============================================================
// 同位数远程注入（32 位中继 -> 32 位子进程）
// ============================================================

// 注入原语在 child_inject.{h,cpp}（relay32inject.exe 也用同一份，
// 避免两条注入路径各写一遍而走样）。这里只负责补上"注的是自己"和
// "用本进程持有的 mediatorPid"两件事。
bool InjectRelayIntoChild(HANDLE hProcess, uint32_t childPid,
                          const std::wstring& pipeName) {
    std::wstring dllPath;
    if (!GetSelfDllPath(dllPath)) {
        LOG_ERROR("GetSelfDllPath failed");
        return false;
    }
    return InjectDllSameBitness(hProcess, childPid, dllPath, pipeName,
                                g_mediatorPid.load());
}

// ============================================================
// 上报 mediator
// ============================================================

// "我（中继）刚创建了 32 位子进程并已注入好 relay32，请建会话"
// peerIsRelay=1 让 mediator 用中继模式握手（等 RelayHello 而非 Hello）
bool NotifyChildSession(uint32_t childPid, const std::wstring& pipeName) {
    protocol::ChildProcessNotifyPayload notify{};
    notify.childPid = childPid;
    notify.parentPid = GetCurrentProcessId();
    wcsncpy_s(notify.pipeName, std::size(notify.pipeName),
              pipeName.c_str(), _TRUNCATE);
    notify.peerIsRelay = 1;

    const bool ok = SendPacket(protocol::MessageType::ChildProcessNotify,
                               &notify, sizeof(notify));
    LOG_INFO("ChildProcessNotify child=%u pipe=%ls sent=%d",
             childPid, pipeName.c_str(), ok ? 1 : 0);
    return ok;
}

// "我（中继）捕获到 64 位子进程并已冻结，请建会话 + 注入 + 回执"
// payload 与 ChildProcessNotify 相同（peerIsRelay 对本消息无意义，填 0）
bool NotifyRelayChild(uint32_t childPid, const std::wstring& pipeName) {
    protocol::ChildProcessNotifyPayload notify{};
    notify.childPid = childPid;
    notify.parentPid = GetCurrentProcessId();
    wcsncpy_s(notify.pipeName, std::size(notify.pipeName),
              pipeName.c_str(), _TRUNCATE);
    notify.peerIsRelay = 0;

    const bool ok = SendPacket(protocol::MessageType::RelayChildNotify,
                               &notify, sizeof(notify));
    LOG_INFO("RelayChildNotify child=%u pipe=%ls sent=%d",
             childPid, pipeName.c_str(), ok ? 1 : 0);
    return ok;
}

// ============================================================
// 子进程处理
// ============================================================

// 子进程已由调用方以 CREATE_SUSPENDED 创建，本函数负责注入/上报并恢复它。
// 任何失败路径都必须走到 ResumeThread —— 让子进程永远挂起比漏注入更糟。
void HandleSuspendedChild(LPPROCESS_INFORMATION pi, const char* apiName) {
    const ProcessBitness bits = QueryProcessBitness(pi->hProcess);
    // 子会话管道名由本中继生成，与上报 mediator / 下发给子 DLL 的名字必须一致
    const std::wstring childPipe = MakeRandomPipeName(pi->dwProcessId);

    if (bits == ProcessBitness::X86) {
        // 32 位子进程：中继自己能注入（同位数）。
        // 注入成功后子进程内也有中继，它后续拉起的 64 位进程同样会被接管。
        // 注入本身会等 LoadLibraryW 返回（即 relay32 的 DllMain 装完钩子），
        // 所以无需等 mediator 回执即可恢复 —— 不存在"跑在无钩子状态下"的窗口。
        LOG_INFO("%s: child %u is 32-bit, injecting relay32 (self)", apiName,
                 pi->dwProcessId);
        if (InjectRelayIntoChild(pi->hProcess, pi->dwProcessId, childPipe)) {
            // 先通知 mediator 建会话，子进程内的中继才有管道可连
            NotifyChildSession(pi->dwProcessId, childPipe);
        } else {
            LOG_ERROR("%s: relay injection into 32-bit child %u failed; "
                      "child runs without relay", apiName, pi->dwProcessId);
        }
    } else if (bits == ProcessBitness::X64) {
        // 64 位子进程：中继是 32 位，无法注入（跨位数必然失败），
        // 只能交给 mediator —— 只有 x64 侧有注入能力。
        // 子进程保持冻结直到回执，注入 race-free。
        LOG_INFO("%s: child %u is 64-bit, asking mediator to inject", apiName,
                 pi->dwProcessId);
        if (NotifyRelayChild(pi->dwProcessId, childPipe)) {
            const ULONGLONG t0 = GetTickCount64();
            const bool acked = WaitForChildAck(pi->dwProcessId, kAckTimeoutMs);
            LOG_INFO("%s: child %u ack=%d after %llu ms", apiName, pi->dwProcessId,
                     acked ? 1 : 0, GetTickCount64() - t0);
            if (!acked) {
                LOG_ERROR("%s: no RelayChildAck for child %u within %lu ms; "
                          "resuming anyway (child may run without hooks)",
                          apiName, pi->dwProcessId, kAckTimeoutMs);
            }
        } else {
            LOG_ERROR("%s: RelayChildNotify for child %u failed "
                      "(relay disconnected?); child runs without hooks",
                      apiName, pi->dwProcessId);
        }
    } else {
        LOG_WARN("%s: child %u bitness unknown; resuming without hijack",
                 apiName, pi->dwProcessId);
    }

    if (ResumeThread(pi->hThread) == static_cast<DWORD>(-1)) {
        LOG_ERROR("%s: ResumeThread failed for child %u: %lu",
                  apiName, pi->dwProcessId, GetLastError());
    }
}

// 等与 mediator 建连。返回 false 表示不可用，调用方应按"无中继"透传。
bool EnsureConnected(DWORD timeoutMs) {
    if (g_connected.load()) {
        return true;
    }
    if (g_connectedEvent == nullptr) {
        return false;
    }
    WaitForSingleObject(g_connectedEvent, timeoutMs);
    return g_connected.load();
}

void WarnPassThroughOnce() {
    bool expected = false;
    if (g_passThroughWarned.compare_exchange_strong(expected, true)) {
        LOG_WARN("relay not connected to mediator; passing CreateProcess through "
                 "WITHOUT suspending (children run as if relay absent)");
    }
}

// ============================================================
// CreateProcessW / A Detour
// ============================================================

using CreateProcessW_t = BOOL(WINAPI*)(LPCWSTR, LPWSTR, LPSECURITY_ATTRIBUTES,
    LPSECURITY_ATTRIBUTES, BOOL, DWORD, LPVOID, LPCWSTR,
    LPSTARTUPINFOW, LPPROCESS_INFORMATION);
using CreateProcessA_t = BOOL(WINAPI*)(LPCSTR, LPSTR, LPSECURITY_ATTRIBUTES,
    LPSECURITY_ATTRIBUTES, BOOL, DWORD, LPVOID, LPCSTR,
    LPSTARTUPINFOA, LPPROCESS_INFORMATION);

CreateProcessW_t g_origCreateProcessW = nullptr;
CreateProcessA_t g_origCreateProcessA = nullptr;

// CreateProcessW 与 A 的差异只在字符集；中继只读写 dwCreationFlags 与
// lpProcessInfo，其余参数原样透传，因此两个 detour 逻辑完全一致。
//
// callOrig 是"用给定 flags 调原函数"的 lambda：创建标志在参数表里位于中段，
// 不能用变参打包后追加，交给 lambda 保证落在正确位置。
template <typename CallFn>
BOOL RunDetour(CallFn&& callOrig, DWORD dwCreationFlags,
               LPPROCESS_INFORMATION lpProcessInfo, const char* apiName) {
    // 调用方自己就要挂起：尊重其意图，不介入协调
    if (dwCreationFlags & CREATE_SUSPENDED) {
        return callOrig(dwCreationFlags);
    }
    // 重入：detour 内部（注入/日志）若间接再进 CreateProcess，直接透传
    if (t_inDetour) {
        return callOrig(dwCreationFlags);
    }
    // 未与 mediator 建连：按"无中继"透传，不冻结子进程 ——
    // 否则会造出一个我们无法处理、又必须挂着的子进程
    if (!EnsureConnected(kConnectWaitMs)) {
        WarnPassThroughOnce();
        return callOrig(dwCreationFlags);
    }

    t_inDetour = true;
    const BOOL ok = callOrig(dwCreationFlags | CREATE_SUSPENDED);
    if (ok) {
        HandleSuspendedChild(lpProcessInfo, apiName);
    }
    t_inDetour = false;
    return ok;
}

BOOL WINAPI CreateProcessW_Detour(LPCWSTR lpApplicationName, LPWSTR lpCommandLine,
    LPSECURITY_ATTRIBUTES lpProcessAttributes,
    LPSECURITY_ATTRIBUTES lpThreadAttributes,
    BOOL bInheritHandles, DWORD dwCreationFlags,
    LPVOID lpEnvironment, LPCWSTR lpCurrentDirectory,
    LPSTARTUPINFOW lpStartupInfo, LPPROCESS_INFORMATION lpProcessInfo) {
    auto callOrig = [&](DWORD flags) {
        return g_origCreateProcessW(lpApplicationName, lpCommandLine,
            lpProcessAttributes, lpThreadAttributes, bInheritHandles, flags,
            lpEnvironment, lpCurrentDirectory, lpStartupInfo, lpProcessInfo);
    };
    return RunDetour(callOrig, dwCreationFlags, lpProcessInfo, "CreateProcessW");
}

BOOL WINAPI CreateProcessA_Detour(LPCSTR lpApplicationName, LPSTR lpCommandLine,
    LPSECURITY_ATTRIBUTES lpProcessAttributes,
    LPSECURITY_ATTRIBUTES lpThreadAttributes,
    BOOL bInheritHandles, DWORD dwCreationFlags,
    LPVOID lpEnvironment, LPCSTR lpCurrentDirectory,
    LPSTARTUPINFOA lpStartupInfo, LPPROCESS_INFORMATION lpProcessInfo) {
    auto callOrig = [&](DWORD flags) {
        return g_origCreateProcessA(lpApplicationName, lpCommandLine,
            lpProcessAttributes, lpThreadAttributes, bInheritHandles, flags,
            lpEnvironment, lpCurrentDirectory, lpStartupInfo, lpProcessInfo);
    };
    return RunDetour(callOrig, dwCreationFlags, lpProcessInfo, "CreateProcessA");
}

// ============================================================
// Hook 安装
// ============================================================

void* ResolveProcessApi(const char* name) {
    // 与其他 Hook 同策略：优先 kernelbase，回退 kernel32
    HMODULE hKBase = GetModuleHandleW(L"kernelbase.dll");
    if (hKBase != nullptr) {
        void* p = GetProcAddress(hKBase, name);
        if (p != nullptr) {
            return p;
        }
    }
    HMODULE hK32 = GetModuleHandleW(L"kernel32.dll");
    if (hK32 != nullptr) {
        return GetProcAddress(hK32, name);
    }
    return nullptr;
}

void InstallProcessHooks() {
    void* targetW = ResolveProcessApi("CreateProcessW");
    if (targetW != nullptr &&
        MH_CreateHook(targetW, reinterpret_cast<void*>(&CreateProcessW_Detour),
                      reinterpret_cast<void**>(&g_origCreateProcessW)) == MH_OK &&
        MH_EnableHook(targetW) == MH_OK) {
        g_hookW.store(1);
    } else {
        g_hookW.store(-1);
    }

    void* targetA = ResolveProcessApi("CreateProcessA");
    if (targetA != nullptr &&
        MH_CreateHook(targetA, reinterpret_cast<void*>(&CreateProcessA_Detour),
                      reinterpret_cast<void**>(&g_origCreateProcessA)) == MH_OK &&
        MH_EnableHook(targetA) == MH_OK) {
        g_hookA.store(1);
    } else {
        g_hookA.store(-1);
    }
}

} // namespace
} // namespace terminjector::relay32

// ============================================================
// DLL 入口
// ============================================================

BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID /*reserved*/) {
    using namespace terminjector::relay32;

    switch (reason) {
        case DLL_PROCESS_ATTACH: {
            g_hSelf = hModule;
            DisableThreadLibraryCalls(hModule);

            // 无命名事件，不触发 Loader Lock
            g_connectedEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);

            // 装钩子（只调 MinHook 与 GetProcAddress，不触发 Loader Lock）
            if (MH_Initialize() != MH_OK) {
                terminjector::SafeOutputDebugStringW(
                    L"[terminjector] relay32: MH_Initialize failed");
                return TRUE;  // 不拒绝加载：无中继时行为等同未注入
            }
            InstallProcessHooks();

            // 建连与日志交给工作线程（Logger 初始化会打开文件，不宜在
            // DllMain 的 Loader Lock 内做）
            HANDLE hWorker = CreateThread(nullptr, 0, ConnectWorkerProc,
                                          nullptr, 0, nullptr);
            if (hWorker != nullptr) {
                CloseHandle(hWorker);
            }
            break;
        }
        case DLL_PROCESS_DETACH: {
            // 只置标志并断开管道（中断阻塞的 RecvPacket），不 join 线程：
            // DllMain 内 join 会死锁。进程退出时工作线程已被系统终止。
            g_stopping.store(true);
            g_connected.store(false);
            terminjector::NamedPipeTransport* t = g_transport.load();
            if (t != nullptr) {
                t->Disconnect();
            }
            g_ackCv.notify_all();
            break;
        }
        default:
            break;
    }
    return TRUE;
}

// 导出探针：便于外部确认加载的是中继 DLL 而非 injected.dll
extern "C" __declspec(dllexport) int Relay_QueryBitness(void) {
#if defined(_WIN64)
    return 64;
#else
    return 32;
#endif
}
