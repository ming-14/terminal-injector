// 进程创建类 API Hook 实现
// 详见 docs/phases/12-child-process-injection.md 4.2-4.3
//
// 流程：
//   1. ENSURE_INITIALIZED() 触发懒加载
//   2. 强制加入 CREATE_SUSPENDED 标志
//   3. 调原始 CreateProcessW/A → 得到子进程 PID + 主线程句柄
//   4. 生成子会话随机管道名，发 ChildProcessNotify 给 mediator
//      （mediator 据此创建子进程管道实例；名字不可预测，防抢占）
//   5. 用 CreateRemoteThread + LoadLibraryW 注入 DLL 到子进程，
//      并经 RemoteCallExport 传注入参数（随机管道名 + mediatorPid）
//   6. ResumeThread（如果原始 flags 没有 SUSPENDED）
//
// 关键设计：
//   - thread_local 重入保护：防止 Detour 内部间接递归调用 CreateProcess
//   - 子进程 DLL 的管道参数由父 DLL 注入时下发（安全审查 HIGH #2 修复，
//     旧方案子 DLL 用 GetCurrentProcessId() 自发现约定名，可被预创建抢占）
//   - 注入失败降级：子进程不被注入，输出走 ConHost（与无注入时一致）
#include "ProcessHooks.h"
#include "HookCommon.h"
#include "HookWhitelist.h"
#include "../HookManager.h"
#include "../RemoteParams.h"
#include "protocol/Message.h"
#include "transport/NamedPipeTransport.h"
#include "remote/RemoteCall.h"
#include "logging/Logger.h"

#include <windows.h>
#include <vector>
#include <algorithm>
#include <cwctype>

#include <psapi.h>   // EnumProcessModulesEx / GetModuleFileNameExW
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

static bool InjectDllToChild(HANDLE hProcess, uint32_t childPid,
                             const std::wstring& pipeName,
                             uint32_t mediatorPid) {
    // 1. 获取当前 DLL 路径（与 InjectDllToChild 函数地址同模块）
    wchar_t dllPath[MAX_PATH] = {0};
    HMODULE hSelf = nullptr;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                            GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            reinterpret_cast<LPCWSTR>(&InjectDllToChild), &hSelf)) {
        LOG_ERROR("InjectDllToChild: GetModuleHandleExW failed: %lu", GetLastError());
        return false;
    }
    if (!GetModuleFileNameW(hSelf, dllPath, MAX_PATH)) {
        LOG_ERROR("InjectDllToChild: GetModuleFileNameW failed: %lu", GetLastError());
        return false;
    }

    // 2. 在子进程分配内存，写入 DLL 路径（含 null 终止符）
    const size_t pathBytes = (wcslen(dllPath) + 1) * sizeof(wchar_t);
    LPVOID remoteBuf = VirtualAllocEx(hProcess, nullptr, pathBytes,
                                      MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!remoteBuf) {
        LOG_ERROR("InjectDllToChild: VirtualAllocEx failed: %lu", GetLastError());
        return false;
    }

    SIZE_T written = 0;
    if (!WriteProcessMemory(hProcess, remoteBuf, dllPath, pathBytes, &written) ||
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
             dllPath, pipeName.c_str());
    return true;
}

// ============================================================
// 子进程创建后处理
// ============================================================
// 通知 mediator + 注入 DLL + 恢复线程
// CreateProcessW/A Detour 共用此逻辑
// lpPi       子进程信息（PID、句柄）
// needResume 是否需要恢复主线程（原始 flags 无 SUSPENDED 时为 true）
static void OnChildProcessCreated(LPPROCESS_INFORMATION lpPi, bool needResume) {
    // 0. 取本进程注入参数（随机管道名 + mediatorPid）
    //    子 DLL 的校验目标与父 DLL 相同（同一 mediator）；
    //    参数未就绪时 mediatorPid=0（跳过校验），pipeName 为空（不注入参数）
    PipeParams parentParams{};
    const bool haveParams = GetPipeParams(parentParams);

    // 0.5 生成子会话随机管道名（名字不可预测，防同会话进程预创建抢占）
    //    父 DLL 既上报 mediator 创建服务端，又传给子 DLL 连接，两侧一致
    const std::wstring childPipe = MakeRandomPipeName(lpPi->dwProcessId);

    // 1. 通知 mediator：子进程已创建 + 随机管道名，请创建管道实例
    //    mediator 收到后创建对应名字的管道并等待连接
    protocol::ChildProcessNotifyPayload notify{};
    notify.childPid = lpPi->dwProcessId;
    notify.parentPid = GetCurrentProcessId();
    wcsncpy_s(notify.pipeName, sizeof(notify.pipeName) / sizeof(wchar_t),
              childPipe.c_str(), _TRUNCATE);
    SendToMediator(&notify, sizeof(notify), protocol::MessageType::ChildProcessNotify);

    // 2. 注入 DLL 到子进程
    //    注入后子进程 DllMain 执行（LazyInit 懒加载，不阻塞）
    //    子进程首个 Console API 调用时触发 LazyInit，连接管道
    if (!InjectDllToChild(lpPi->hProcess, lpPi->dwProcessId, childPipe,
                          haveParams ? parentParams.mediatorPid : 0)) {
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
