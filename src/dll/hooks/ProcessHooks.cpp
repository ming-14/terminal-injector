// 进程创建类 API Hook 实现
// 详见 docs/phases/12-child-process-injection.md 4.2-4.3
//
// 流程：
//   1. ENSURE_INITIALIZED() 触发懒加载
//   2. 强制加入 CREATE_SUSPENDED 标志
//   3. 调原始 CreateProcessW/A → 得到子进程 PID + 主线程句柄
//   4. 判定子进程位数（决定注入哪个 DLL，也决定 mediator 的握手方式）
//   5. 生成子会话随机管道名，发 ChildProcessNotify 给 mediator
//      （mediator 据此创建子进程管道实例；名字不可预测，防抢占）
//   6. 按位数分派注入：
//        64 位 → 本 DLL 自己用 CreateRemoteThread + LoadLibraryW 注入 injected.dll
//        32 位 → fork 同目录 relay32inject.exe 代劳注入 relay32.dll（见下）
//      两者都经 RemoteCallExport 传注入参数（随机管道名 + mediatorPid）
//   7. ResumeThread（如果原始 flags 没有 SUSPENDED）
//
// 关键设计：
//   - thread_local 重入保护：防止 Detour 内部间接递归调用 CreateProcess
//   - 跨位数注入不可能：本地 kernel32!LoadLibraryW 是 64 位地址，在 32 位目标
//     里没有意义（实测跨位数注入返回 0）。同位数注入才是可行路径，因此 32 位
//     子进程由 32 位的 relay32inject.exe 注入 —— 它复用 relay32.dll 同一份
//     注入原语（src/relay32/child_inject.cpp），不是另写一遍
//   - 子进程 DLL 的管道参数由父 DLL 注入时下发（安全审查 HIGH #2 修复，
//     旧方案子 DLL 用 GetCurrentProcessId() 自发现约定名，可被预创建抢占）
//   - 注入失败降级：子进程不被注入，输出走 ConHost（与无注入时一致）
#include "ProcessHooks.h"
#include "HookCommon.h"
#include "HookWhitelist.h"
#include "../HookManager.h"
#include "../RemoteParams.h"
#include "process/ProcessBitness.h"
#include "protocol/Message.h"
#include "transport/NamedPipeTransport.h"
#include "remote/RemoteCall.h"
#include "logging/Logger.h"

#include <windows.h>
#include <string>
#include <vector>
#include <unordered_map>
#include <algorithm>
#include <cwctype>

#include <psapi.h>     // EnumProcessModulesEx / GetModuleFileNameExW
#include <tlhelp32.h>  // CreateToolhelp32Snapshot（同控制台后代判定的父子表）
#pragma comment(lib, "psapi.lib")

namespace terminjector::hooks {

// ============================================================
// 原函数指针定义
// ============================================================
DEFINE_ORIG_PTR(CreateProcessW, BOOL WINAPI(LPCWSTR, LPWSTR, LPSECURITY_ATTRIBUTES,
    LPSECURITY_ATTRIBUTES, BOOL, DWORD, LPVOID, LPCWSTR,
    LPSTARTUPINFOW, LPPROCESS_INFORMATION));

DEFINE_ORIG_PTR(CreateProcessA, BOOL WINAPI(LPCSTR, LPSTR, LPSECURITY_ATTRIBUTES,
    LPSECURITY_ATTRIBUTES, BOOL, DWORD, LPVOID, LPCSTR,
    LPSTARTUPINFOA, LPPROCESS_INFORMATION));

// ============================================================
// 重入保护
// ============================================================
// thread_local 标志防止 Detour 内部间接递归调用 CreateProcess
// （如 Logger 初始化、LazyInit 等场景若触发 CreateProcess）
namespace {
thread_local bool t_inCreateProcess = false;

// 等 32 位注入助手（relay32inject.exe）退出的上限。
// 正常路径 ~40ms；助手内部对 LoadLibraryW 与 RemoteCallExport 各有 10s 超时，
// 这里取足够覆盖最坏情况的值，同时避免异常时把父进程冻得太久。
constexpr DWORD kRelayInjectWaitMs = 30000;
}

// ============================================================
// InjectDllToChild
// ============================================================
// 复用 Injector 的 CreateRemoteThread + LoadLibraryW 模式
// 在子进程 SUSPENDED 状态下注入 DLL
// hProcess 子进程句柄（需 PROCESS_CREATE_THREAD 等权限）
// childPid  子进程 PID（仅用于日志）
// pipeName  子会话随机管道名（父 DLL 生成，与上报 mediator 的一致）
// mediatorPid 管道服务端进程 PID（子 DLL 连接后校验身份；0=跳过）
// 返回 true 表示注入成功

// 枚举目标进程模块，按文件名（不区分大小写）匹配，返回完整 64 位基址
// （LoadLibraryW 线程退出码只有 32 位，不能直接当 HMODULE 用）
static HMODULE FindChildModuleByPath(HANDLE hProcess,
                                     const std::wstring& name) {
    DWORD cb = 0;
    if (!EnumProcessModulesEx(hProcess, nullptr, 0, &cb,
                              LIST_MODULES_ALL)) {
        LOG_ERROR("FindChildModuleByPath: EnumProcessModulesEx(query) failed: %lu",
                  GetLastError());
        return nullptr;
    }
    std::vector<HMODULE> mods(cb / sizeof(HMODULE));
    if (!EnumProcessModulesEx(hProcess, mods.data(), cb, &cb,
                              LIST_MODULES_ALL)) {
        LOG_ERROR("FindChildModuleByPath: EnumProcessModulesEx failed: %lu",
                  GetLastError());
        return nullptr;
    }

    std::wstring lower(name);
    std::transform(lower.begin(), lower.end(), lower.begin(),
                   [](wchar_t c) { return static_cast<wchar_t>(::towlower(c)); });
    for (HMODULE hMod : mods) {
        wchar_t path[MAX_PATH] = {0};
        if (GetModuleFileNameExW(hProcess, hMod, path, MAX_PATH) == 0) {
            continue;
        }
        std::wstring file = path;
        const size_t slash = file.find_last_of(L'\\');
        if (slash != std::wstring::npos) {
            file = file.substr(slash + 1);
        }
        std::wstring fileLower(file);
        std::transform(fileLower.begin(), fileLower.end(), fileLower.begin(),
                       [](wchar_t c) { return static_cast<wchar_t>(::towlower(c)); });
        if (fileLower == lower) {
            LOG_INFO("FindChildModuleByPath: '%ls' found at %p in pid=%u",
                     name.c_str(), reinterpret_cast<void*>(hMod), GetProcessId(hProcess));
            return hMod;
        }
    }
    return nullptr;
}

// 取本 DLL 的所在目录与文件名（地址取自本函数，保证落在本模块内）。
// 同目录下放着全部协作产物：injected.dll、relay32.dll、relay32inject.exe。
static bool GetSelfModuleLocation(std::wstring& outDir, std::wstring& outName) {
    HMODULE hSelf = nullptr;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                            GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            reinterpret_cast<LPCWSTR>(&GetSelfModuleLocation), &hSelf)) {
        LOG_ERROR("GetSelfModuleLocation: GetModuleHandleExW failed: %lu", GetLastError());
        return false;
    }
    wchar_t dllPath[MAX_PATH] = {0};
    if (!GetModuleFileNameW(hSelf, dllPath, MAX_PATH)) {
        LOG_ERROR("GetSelfModuleLocation: GetModuleFileNameW failed: %lu", GetLastError());
        return false;
    }
    std::wstring path(dllPath);
    const size_t pos = path.find_last_of(L"\\/");
    if (pos == std::wstring::npos) {
        LOG_ERROR("GetSelfModuleLocation: no separator in %ls", dllPath);
        return false;
    }
    outDir = path.substr(0, pos);
    outName = path.substr(pos + 1);
    return true;
}

static bool InjectDllToChild(HANDLE hProcess, uint32_t childPid,
                             const std::wstring& pipeName,
                             uint32_t mediatorPid) {
    // 1. 获取当前 DLL 全路径（远程 LoadLibraryW 的入参）
    std::wstring dllDir, selfName;
    if (!GetSelfModuleLocation(dllDir, selfName)) {
        return false;
    }
    const std::wstring dllPath = dllDir + L"\\" + selfName;

    // 2. 在子进程分配内存，写入 DLL 路径（含 null 终止符）
    const size_t pathBytes = (dllPath.size() + 1) * sizeof(wchar_t);
    LPVOID remoteBuf = VirtualAllocEx(hProcess, nullptr, pathBytes,
                                      MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!remoteBuf) {
        LOG_ERROR("InjectDllToChild: VirtualAllocEx failed: %lu", GetLastError());
        return false;
    }

    SIZE_T written = 0;
    if (!WriteProcessMemory(hProcess, remoteBuf, dllPath.c_str(), pathBytes,
                            &written) ||
        written != pathBytes) {
        LOG_ERROR("InjectDllToChild: WriteProcessMemory failed: %lu", GetLastError());
        VirtualFreeEx(hProcess, remoteBuf, 0, MEM_RELEASE);
        return false;
    }

    // 3. CreateRemoteThread 调用 LoadLibraryW
    // x64 下 kernel32.dll 在所有进程加载地址相同，可直接用本地地址
    HMODULE hK32 = GetModuleHandleW(L"kernel32.dll");
    if (!hK32) {
        LOG_ERROR("InjectDllToChild: GetModuleHandleW(kernel32) failed");
        VirtualFreeEx(hProcess, remoteBuf, 0, MEM_RELEASE);
        return false;
    }
    auto pLoadLib = reinterpret_cast<LPTHREAD_START_ROUTINE>(
        GetProcAddress(hK32, "LoadLibraryW"));
    if (!pLoadLib) {
        LOG_ERROR("InjectDllToChild: GetProcAddress(LoadLibraryW) failed");
        VirtualFreeEx(hProcess, remoteBuf, 0, MEM_RELEASE);
        return false;
    }

    HANDLE hThread = CreateRemoteThread(hProcess, nullptr, 0, pLoadLib,
                                        remoteBuf, 0, nullptr);
    if (!hThread) {
        LOG_ERROR("InjectDllToChild: CreateRemoteThread failed: %lu", GetLastError());
        VirtualFreeEx(hProcess, remoteBuf, 0, MEM_RELEASE);
        return false;
    }

    // 4. 等待 LoadLibrary 完成（DllMain 执行完毕）
    // 5s 超时：DllMain 应快速返回（LazyInit 是懒加载的，不在 DllMain 中执行）
    DWORD waitRes = WaitForSingleObject(hThread, 5000);
    if (waitRes != WAIT_OBJECT_0) {
        LOG_ERROR("InjectDllToChild: WaitForSingleObject res=%lu err=%lu",
                  waitRes, GetLastError());
        CloseHandle(hThread);
        VirtualFreeEx(hProcess, remoteBuf, 0, MEM_RELEASE);
        return false;
    }

    // 检查 LoadLibraryW 返回值（线程退出码 = HMODULE，0 表示加载失败）
    DWORD exitCode = 0;
    if (!GetExitCodeThread(hThread, &exitCode)) {
        LOG_ERROR("InjectDllToChild: GetExitCodeThread failed: %lu", GetLastError());
        CloseHandle(hThread);
        VirtualFreeEx(hProcess, remoteBuf, 0, MEM_RELEASE);
        return false;
    }

    CloseHandle(hThread);
    VirtualFreeEx(hProcess, remoteBuf, 0, MEM_RELEASE);

    if (exitCode == 0) {
        LOG_ERROR("InjectDllToChild: LoadLibraryW returned 0 in child pid=%u", childPid);
        return false;
    }
    // 线程退出码只有 32 位，64 位 HMODULE 会被截断 → 枚举子进程模块拿完整基址
    const HMODULE hRemoteDll = FindChildModuleByPath(hProcess, L"injected.dll");
    if (hRemoteDll == nullptr) {
        LOG_ERROR("InjectDllToChild: injected.dll not found in child pid=%u", childPid);
        return false;
    }

    // 5. 跨进程下发管道参数给子 DLL（安全加固，防可预测管道名抢占）
    //    与注入器 Inject() 的 RemotePipeSetup 调用一致：
    //    随机管道名 + mediatorPid（服务端身份校验目标）
    {
        PipeParams params{};
        wcsncpy_s(params.pipeName, kMaxPipeNameLen, pipeName.c_str(), _TRUNCATE);
        params.mediatorPid = mediatorPid;
        if (!RemoteCallExport(hProcess, hRemoteDll, dllPath, "RemotePipeSetup",
                              &params, sizeof(params), nullptr)) {
            LOG_ERROR("InjectDllToChild: RemotePipeSetup failed in child pid=%u, "
                      "child DLL will not connect", childPid);
            return false;
        }
    }

    LOG_INFO("InjectDllToChild: success pid=%u dll=%ls pipe=%ls", childPid,
             dllPath.c_str(), pipeName.c_str());
    return true;
}

// ============================================================
// 跨位数注入：32 位子进程交给 32 位注入器
// ============================================================
// 为什么本 DLL 不能自己注入 32 位子进程：
//   远程 LoadLibraryW 的入口地址取自【本进程】的 kernel32，靠的是"kernel32 是
//   KnownDLL、同一 boot 内同位数进程共享同一基址"。本 DLL 是 64 位进程，取到的
//   是 64 位地址，写进 32 位目标毫无意义 —— 实测该远程线程会起来但 LoadLibraryW
//   返回 0（加载失败）。所以必须由 32 位进程来注入。
//
// 为什么是 fork 一个短命助手而不是让 mediator 代劳：
//   子进程句柄本来就在本 Detour 手里（lpPi->hProcess，mediator 只有 PID），
//   注入的成败也由本 Detour 决定何时 ResumeThread。放这里就无需新增协议往返，
//   也不会因为 mediator 异常而让子进程白冻到超时。
//
// 助手与 relay32.dll 共用同一份注入原语（src/relay32/child_inject.cpp），
// 不是把注入逻辑再写一遍。
static bool InjectRelayInto32BitChild(uint32_t childPid,
                                      const std::wstring& pipeName,
                                      uint32_t mediatorPid) {
    std::wstring dir, selfName;
    if (!GetSelfModuleLocation(dir, selfName)) {
        return false;
    }
    const std::wstring exePath = dir + L"\\relay32inject.exe";
    const std::wstring relayPath = dir + L"\\relay32.dll";

    std::wstring cmd = L"\"" + exePath + L"\" --child " + std::to_wstring(childPid) +
                       L" --dll \"" + relayPath + L"\" --pipe \"" + pipeName + L"\"";
    if (mediatorPid != 0) {
        cmd += L" --mediator-pid " + std::to_wstring(mediatorPid);
    }
    LOG_INFO("InjectRelayInto32BitChild: pid=%u cmd=%ls", childPid, cmd.c_str());

    // CreateProcessW 的 lpCommandLine 必须是可写缓冲
    std::vector<wchar_t> cmdBuf(cmd.begin(), cmd.end());
    cmdBuf.push_back(L'\0');

    STARTUPINFOW si{};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi{};
    // 直接走原始入口，不经过本 Detour：助手进程自己不该被我们注入（否则会递归
    // 成"起注入器去注入注入器"）。CreateProcessW_orig 天然绕开这条路径。
    // CREATE_NO_WINDOW：助手是控制台程序，不能让它占用/弹出一个控制台 ——
    // 目标进程的输出正被接管到 WT，多一个控制台会污染显示。
    if (!CreateProcessW_orig(nullptr, cmdBuf.data(), nullptr, nullptr, FALSE,
                             CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi)) {
        LOG_ERROR("InjectRelayInto32BitChild: CreateProcessW failed: %lu",
                  GetLastError());
        return false;
    }

    const DWORD waitRes = WaitForSingleObject(pi.hProcess, kRelayInjectWaitMs);
    DWORD exitCode = 0;
    GetExitCodeProcess(pi.hProcess, &exitCode);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);

    if (waitRes != WAIT_OBJECT_0) {
        LOG_ERROR("InjectRelayInto32BitChild: helper not finished (wait=%lu), pid=%u; "
                  "child will be resumed without relay", waitRes, childPid);
        return false;
    }
    if (exitCode != 0) {
        LOG_ERROR("InjectRelayInto32BitChild: helper exit code=%lu, pid=%u",
                  exitCode, childPid);
        return false;
    }
    LOG_INFO("InjectRelayInto32BitChild: relay injected into 32-bit child %u", childPid);
    return true;
}

// ============================================================
// 通知 mediator + 按位数注入
// ============================================================
// CreateProcess 捕获路径与「接管同控制台已有后代」路径共用：
//   1. 生成子会话随机管道名（名字不可预测，防同会话进程预创建抢占）
//   2. 判对端位数（决定注入 injected.dll 还是 relay32.dll，也决定 mediator
//      用哪种握手），必须在上报 mediator 之前定
//   3. 通知 mediator 创建子会话管道实例 + 等待连接
//   4. 按位数分派注入
// pid/hProcess：目标后代进程；hProcess 需已具备注入权限
//               （32 位走外部助手，只需 pid 与位数判定）
static bool NotifyMediatorAndInject(uint32_t pid, HANDLE hProcess) {
    // 取本进程注入参数（随机管道名 + mediatorPid）
    // 子 DLL 的校验目标与父 DLL 相同（同一 mediator）；
    // 参数未就绪时 mediatorPid=0（跳过校验）
    PipeParams parentParams{};
    const bool haveParams = GetPipeParams(parentParams);

    const std::wstring childPipe = MakeRandomPipeName(pid);

    const ProcessBitness bits = QueryProcessBitness(hProcess);
    const bool peerIsRelay = (bits == ProcessBitness::X86);

    protocol::ChildProcessNotifyPayload notify{};
    notify.childPid = pid;
    notify.parentPid = GetCurrentProcessId();
    wcsncpy_s(notify.pipeName, sizeof(notify.pipeName) / sizeof(wchar_t),
              childPipe.c_str(), _TRUNCATE);
    notify.peerIsRelay = peerIsRelay ? 1u : 0u;
    SendToMediator(&notify, sizeof(notify), protocol::MessageType::ChildProcessNotify);

    const uint32_t mediatorPid = haveParams ? parentParams.mediatorPid : 0;
    if (peerIsRelay) {
        // 32 位：本 DLL 是 64 位，跨位数注入不可能，由 32 位的
        // relay32inject.exe 注入 relay32.dll（中继捕获它拉起的子进程；
        // 32 位程序本体接管是 TODO(32bit-target)）
        return InjectRelayInto32BitChild(pid, childPipe, mediatorPid);
    }
    if (bits == ProcessBitness::Unknown) {
        // 位数判不出来（拿不到镜像路径等）：沿用旧行为按 64 位处理，
        // 失败也只是进程不被注入，与无注入时一致
        LOG_WARN("NotifyMediatorAndInject: pid=%u bitness unknown, assuming 64-bit", pid);
    }
    return InjectDllToChild(hProcess, pid, childPipe, mediatorPid);
}

// ============================================================
// 子进程创建后处理
// ============================================================
// 通知 mediator + 注入 DLL + 恢复线程
// CreateProcessW/A Detour 共用此逻辑
// lpPi       子进程信息（PID、句柄）
// needResume 是否需要恢复主线程（原始 flags 无 SUSPENDED 时为 true）
static void OnChildProcessCreated(LPPROCESS_INFORMATION lpPi, bool needResume) {
    // 0. 通知 mediator + 按位数注入（与「接管同控制台已有后代」共用同一流程）
    //    注入后子进程 DllMain 执行（LazyInit 懒加载，不阻塞）
    //    子进程首个 Console API 调用时触发 LazyInit，连接管道
    const bool injected = NotifyMediatorAndInject(lpPi->dwProcessId, lpPi->hProcess);
    if (!injected) {
        LOG_WARN("OnChildProcessCreated: inject failed pid=%u, child runs without hooks",
                 lpPi->dwProcessId);
        // 降级：子进程不被注入，输出走 ConHost（与无注入时一致）
    }

    // 3. 恢复子进程主线程（如果原始 flags 没有 SUSPENDED）
    //    即使注入失败也要 Resume，否则子进程永远挂起
    if (needResume) {
        ResumeThread(lpPi->hThread);
    }
}

// ============================================================
// 接管同控制台上「注入前已存在」的后代进程
// ============================================================
namespace {

// 进程父子关系表（Toolhelp32）。用 PPID 而不是别的信号：
// 同一个控制台上可能同时挂着 shell 与它拉起的 TUI，只有父子链能区分
// 「目标的后代」（要接管）与「目标的祖先/无关进程」（不能接管）。
// 注：Windows 不保证 PPID 一定可用（父进程退出后可能残留），此处只用于
// 过滤与排序，即使表不准也只是多注入/少注入一个进程，不影响已接管链路。
using ParentMap = std::unordered_map<uint32_t, uint32_t>;

ParentMap BuildParentMap() {
    ParentMap map;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) {
        LOG_WARN("Adopt: CreateToolhelp32Snapshot failed err=%lu", GetLastError());
        return map;
    }
    PROCESSENTRY32W pe{};
    pe.dwSize = sizeof(pe);
    if (Process32FirstW(snap, &pe)) {
        do {
            map[pe.th32ProcessID] = pe.th32ParentProcessID;
        } while (Process32NextW(snap, &pe));
    }
    CloseHandle(snap);
    return map;
}

// 从 pid 沿父链回溯到 self 所需的层数；不是 self 的后代返回 -1。
// 深度上限防父链成环（异常数据）导致死循环。
int DescendantDepth(const ParentMap& map, uint32_t pid, uint32_t self) {
    uint32_t cur = pid;
    for (int depth = 1; depth <= 32; ++depth) {
        const auto it = map.find(cur);
        if (it == map.end()) return -1;
        const uint32_t parent = it->second;
        if (parent == 0 || parent == cur) return -1;  // 0/自环 = 链断
        cur = parent;
        if (cur == self) return depth;
    }
    return -1;
}

} // namespace

void AdoptConsoleDescendants() {
    // 仅注入目标进程执行：后代进程是子会话（isTarget=0），不重复接管。
    // 否则每个后代都会反过来枚举并注入它的祖先，互相注入成环。
    if (!IsTargetProcess()) {
        return;
    }
    // 幂等：重复注入会二次下发管道参数（子会话连到第二条管道）
    static LONG s_adoptStarted = 0;
    if (InterlockedCompareExchange(&s_adoptStarted, 1, 0) != 0) {
        return;
    }

    const DWORD self = GetCurrentProcessId();

    // 1. 取同控制台进程表。返回值 > 容量时表示所需数量，扩容重试一次。
    //    GetConsoleProcessList 作用于【调用进程所属控制台】，本进程已被注入、
    //    正挂在目标控制台上，所以拿到的正是用户看到的那个会话。
    std::vector<DWORD> pids(64);
    DWORD count = GetConsoleProcessList(pids.data(), static_cast<DWORD>(pids.size()));
    if (count == 0) {
        LOG_WARN("Adopt: GetConsoleProcessList returned 0 (no console?) err=%lu",
                 GetLastError());
        return;
    }
    if (count > pids.size()) {
        pids.resize(count);
        count = GetConsoleProcessList(pids.data(), static_cast<DWORD>(pids.size()));
        if (count > pids.size()) {
            count = static_cast<DWORD>(pids.size());  // 静态截断，注入已取到的部分
        }
    }

    // 2. 只取本进程的后代（“为什么按 PPID 过滤”见 DescendantDepth 注释），
    //    按深度升序注入（“为什么升序”见头文件）。
    const ParentMap parents = BuildParentMap();
    struct Candidate {
        DWORD pid;
        int depth;
    };
    std::vector<Candidate> candidates;
    candidates.reserve(count);
    for (DWORD i = 0; i < count; ++i) {
        if (pids[i] == self) continue;
        const int depth = DescendantDepth(parents, pids[i], self);
        if (depth < 0) {
            LOG_INFO("Adopt: skip pid=%lu (not a descendant of target)", pids[i]);
            continue;
        }
        candidates.push_back({pids[i], depth});
    }
    std::sort(candidates.begin(), candidates.end(),
              [](const Candidate& a, const Candidate& b) { return a.depth < b.depth; });

    LOG_INFO("Adopt: console has %lu process(es), %zu descendant(s) to take over",
             count, candidates.size());

    for (const Candidate& c : candidates) {
        HANDLE h = OpenProcess(PROCESS_CREATE_THREAD | PROCESS_VM_OPERATION |
                               PROCESS_VM_WRITE | PROCESS_VM_READ |
                               PROCESS_QUERY_INFORMATION, FALSE, c.pid);
        if (h == nullptr) {
            LOG_WARN("Adopt: OpenProcess failed pid=%u err=%lu (runs without hooks)",
                     c.pid, GetLastError());
            continue;
        }
        // 已在接管链路里的不能再来一次：二次 LoadLibrary 会二次下发管道参数，
        // 子会话连到第二条管道，原会话被顶掉。
        // （并发场景：adopt 与 CreateProcess 钩子可能同时命同一个进程）
        if (FindChildModuleByPath(h, L"injected.dll") != nullptr ||
            FindChildModuleByPath(h, L"relay32.dll") != nullptr) {
            LOG_INFO("Adopt: skip pid=%u (already injected)", c.pid);
            CloseHandle(h);
            continue;
        }
        const bool injected = NotifyMediatorAndInject(c.pid, h);
        LOG_INFO("Adopt: pid=%u depth=%d injected=%d", c.pid, c.depth, injected ? 1 : 0);
        if (!injected) {
            LOG_WARN("Adopt: inject failed pid=%u, it keeps drawing to the old ConHost",
                     c.pid);
        }
        CloseHandle(h);
    }
}

// ============================================================
// CreateProcessW Hook
// ============================================================
BOOL WINAPI CreateProcessW_Detour(LPCWSTR lpApplicationName, LPWSTR lpCommandLine,
    LPSECURITY_ATTRIBUTES lpProcessAttributes,
    LPSECURITY_ATTRIBUTES lpThreadAttributes,
    BOOL bInheritHandles, DWORD dwCreationFlags,
    LPVOID lpEnvironment, LPCWSTR lpCurrentDirectory,
    LPSTARTUPINFOW lpStartupInfo, LPPROCESS_INFORMATION lpProcessInfo) {

    ENSURE_INITIALIZED();
    ASSERT_IN_HOOK();          // 关键 Detour：子进程注入复杂路径，重入风险
    HookReentryGuard guard;

    // 重入保护：防止 Detour 内部间接递归
    if (t_inCreateProcess) {
        return CreateProcessW_orig(lpApplicationName, lpCommandLine,
            lpProcessAttributes, lpThreadAttributes, bInheritHandles,
            dwCreationFlags, lpEnvironment, lpCurrentDirectory,
            lpStartupInfo, lpProcessInfo);
    }

    // 1. 强制加入 CREATE_SUSPENDED，以便注入后再恢复
    //    如果原 flags 已有 SUSPENDED，注入后不 Resume（尊重调用方意图）
    const bool needResume = !(dwCreationFlags & CREATE_SUSPENDED);
    const DWORD modifiedFlags = dwCreationFlags | CREATE_SUSPENDED;

    // 2. 调原始 CreateProcessW（带 SUSPENDED）
    t_inCreateProcess = true;
    BOOL ok = CreateProcessW_orig(lpApplicationName, lpCommandLine,
        lpProcessAttributes, lpThreadAttributes, bInheritHandles,
        modifiedFlags, lpEnvironment, lpCurrentDirectory,
        lpStartupInfo, lpProcessInfo);
    t_inCreateProcess = false;

    if (!ok) return FALSE;

    // 3. 通知 mediator + 注入 DLL + 恢复线程
    OnChildProcessCreated(lpProcessInfo, needResume);

    return TRUE;
}

// ============================================================
// CreateProcessA Hook
// ============================================================
BOOL WINAPI CreateProcessA_Detour(LPCSTR lpApplicationName, LPSTR lpCommandLine,
    LPSECURITY_ATTRIBUTES lpProcessAttributes,
    LPSECURITY_ATTRIBUTES lpThreadAttributes,
    BOOL bInheritHandles, DWORD dwCreationFlags,
    LPVOID lpEnvironment, LPCSTR lpCurrentDirectory,
    LPSTARTUPINFOA lpStartupInfo, LPPROCESS_INFORMATION lpProcessInfo) {

    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    // 重入保护
    if (t_inCreateProcess) {
        return CreateProcessA_orig(lpApplicationName, lpCommandLine,
            lpProcessAttributes, lpThreadAttributes, bInheritHandles,
            dwCreationFlags, lpEnvironment, lpCurrentDirectory,
            lpStartupInfo, lpProcessInfo);
    }

    const bool needResume = !(dwCreationFlags & CREATE_SUSPENDED);
    const DWORD modifiedFlags = dwCreationFlags | CREATE_SUSPENDED;

    t_inCreateProcess = true;
    BOOL ok = CreateProcessA_orig(lpApplicationName, lpCommandLine,
        lpProcessAttributes, lpThreadAttributes, bInheritHandles,
        modifiedFlags, lpEnvironment, lpCurrentDirectory,
        lpStartupInfo, lpProcessInfo);
    t_inCreateProcess = false;

    if (!ok) return FALSE;

    OnChildProcessCreated(lpProcessInfo, needResume);

    return TRUE;
}

// ============================================================
// 注册进程创建类 Hook
// ============================================================
void RegisterProcessHooks() {
    // 优先 kernelbase，回退 kernel32（与其他 Hook 同策略）
    HMODULE hKBase = GetModuleHandleW(L"kernelbase.dll");
    HMODULE hK32   = GetModuleHandleW(L"kernel32.dll");

    auto resolve = [hKBase, hK32](const char* name) -> void* {
        if (hKBase != nullptr) {
            void* p = GetProcAddress(hKBase, name);
            if (p != nullptr) return p;
        }
        if (hK32 != nullptr) {
            return GetProcAddress(hK32, name);
        }
        return nullptr;
    };

    std::vector<HookEntry> entries;
    entries.push_back({"CreateProcessW",
        resolve("CreateProcessW"),
        reinterpret_cast<void*>(&CreateProcessW_Detour),
        reinterpret_cast<void**>(&CreateProcessW_orig)});
    entries.push_back({"CreateProcessA",
        resolve("CreateProcessA"),
        reinterpret_cast<void*>(&CreateProcessA_Detour),
        reinterpret_cast<void**>(&CreateProcessA_orig)});

    for (const auto& e : entries) {
        if (e.target == nullptr) {
            LOG_ERROR("RegisterProcessHooks: failed to resolve %s", e.name);
            return;
        }
    }

    HookManager::RegisterBatch(entries);
    LOG_INFO("ProcessHooks registered (%zu hooks)", entries.size());
}

} // namespace terminjector::hooks
