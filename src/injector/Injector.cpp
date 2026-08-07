// Injector 实现
// 详见 docs/phases/02-injector-modes.md 4.4.2
//
// 关键技术点：
//   - kernel32.dll 在所有 x64 进程中加载地址相同，可直接用本地 GetProcAddress
//   - CreateRemoteThread 调用 LoadLibraryW，线程退出码即 HMODULE
//   - 用短路径避免远程进程路径解析问题（空格/中文）
//   - 手写 HandleGuard RAII，不引入 wil 依赖
#include "Injector.h"
#include "ProcessHelper.h"
#include "transport/PipeParams.h"
#include "remote/RemoteCall.h"
#include "../common/logging/Logger.h"

#include <cstring>
#include <cwctype>
#include <algorithm>

namespace terminjector {

namespace {

// 手写 RAII 句柄守卫（替代 wil::scope_exit，避免引入新依赖）
struct HandleGuard {
    HANDLE h = nullptr;
    explicit HandleGuard(HANDLE handle) : h(handle) {}
    ~HandleGuard() { if (h && h != INVALID_HANDLE_VALUE) CloseHandle(h); }
    HandleGuard(const HandleGuard&) = delete;
    HandleGuard& operator=(const HandleGuard&) = delete;
};

} // namespace

Injector::Injector() = default;
Injector::~Injector() = default;

bool Injector::Inject(uint32_t targetPid, const std::wstring& dllPath,
                      const std::wstring& pipeName, uint32_t mediatorPid) {
    LOG_INFO("Inject starting, pid=%u dll=%ls pipe=%ls mediatorPid=%u",
             targetPid, dllPath.c_str(), pipeName.c_str(), mediatorPid);

    if (targetPid == 0) {
        LOG_ERROR("Inject: targetPid is 0");
        return false;
    }
    if (dllPath.empty()) {
        LOG_ERROR("Inject: dllPath is empty");
        return false;
    }

    // 1. 提升 SeDebugPrivilege（非必需，但管理员运行时能注入更多进程）
    if (!ProcessHelper::EnableDebugPrivilege()) {
        LOG_WARN("EnableDebugPrivilege failed (may continue for same-permission targets)");
    }

    // 2. 打开目标进程
    // 所需权限：
    //   PROCESS_CREATE_THREAD — CreateRemoteThread
    //   PROCESS_VM_OPERATION  — VirtualAllocEx / VirtualFreeEx
    //   PROCESS_VM_WRITE      — WriteProcessMemory
    //   PROCESS_VM_READ       — 读取远程内存（备用）
    //   PROCESS_QUERY_INFORMATION — IsWow64Process
    HANDLE hProcess = OpenProcess(
        PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION |
        PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE,
        FALSE, targetPid);
    if (!hProcess) {
        LOG_ERROR("OpenProcess(%u) failed: %lu", targetPid, GetLastError());
        return false;
    }
    HandleGuard procGuard(hProcess);

    // 3. 架构校验（仅支持 x64 目标）
    if (!ProcessHelper::IsX64Process(hProcess)) {
        LOG_ERROR("Target process %u is not x64, aborting", targetPid);
        return false;
    }
    LOG_INFO("Target %u is x64", targetPid);

    // 4. 远程加载 DLL
    HMODULE hRemoteDll = RemoteLoadLibrary(hProcess, dllPath);
    if (!hRemoteDll) {
        LOG_ERROR("RemoteLoadLibrary failed for %ls", dllPath.c_str());
        return false;
    }
    LOG_INFO("Remote DLL loaded at %p in pid=%u", hRemoteDll, targetPid);

    // 5. 跨进程下发管道参数给 DLL（安全加固，防可预测管道名抢占）
    //    注入参数：随机管道名 + mediatorPid（服务端身份校验目标）
    //    DLL 侧 RemotePipeSetup 保存；连接后校验 GetNamedPipeServerProcessId
    if (pipeName.empty()) {
        LOG_ERROR("Inject: pipeName is empty, cannot deliver pipe params");
        return false;
    }
    {
        PipeParams params{};
        wcsncpy_s(params.pipeName, kMaxPipeNameLen, pipeName.c_str(), _TRUNCATE);
        params.mediatorPid = mediatorPid;
        if (!RemoteCallExport(hProcess, hRemoteDll, dllPath, "RemotePipeSetup",
                              &params, sizeof(params), nullptr)) {
            LOG_ERROR("Inject: RemoteCallExport(RemotePipeSetup) failed for "
                      "pid=%u; DLL will not connect to mediator", targetPid);
            return false;
        }
        LOG_INFO("Injection complete, pid=%u (pipe params delivered)", targetPid);
    }
    return true;
}

HMODULE Injector::RemoteLoadLibrary(HANDLE hProcess, const std::wstring& dllPath) {
    // 转短路径（去除空格/中文，避免远程 WriteProcessMemory 后解析问题）
    std::wstring shortPath;
    if (!ProcessHelper::ToShortPath(dllPath, shortPath)) {
        LOG_ERROR("ToShortPath failed for %ls", dllPath.c_str());
        return nullptr;
    }
    LOG_INFO("Short path: %ls -> %ls", dllPath.c_str(), shortPath.c_str());

    // 在目标进程分配内存存放路径字符串（含 null 终止符）
    const size_t bytes = (shortPath.size() + 1) * sizeof(wchar_t);
    LPVOID remoteStr = VirtualAllocEx(hProcess, nullptr, bytes,
                                      MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!remoteStr) {
        LOG_ERROR("VirtualAllocEx failed: %lu", GetLastError());
        return nullptr;
    }

    // 写入路径字符串
    SIZE_T written = 0;
    if (!WriteProcessMemory(hProcess, remoteStr, shortPath.c_str(), bytes, &written)) {
        LOG_ERROR("WriteProcessMemory failed: %lu", GetLastError());
        VirtualFreeEx(hProcess, remoteStr, 0, MEM_RELEASE);
        return nullptr;
    }
    if (written != bytes) {
        LOG_ERROR("WriteProcessMemory partial write: %zu/%zu", written, bytes);
        VirtualFreeEx(hProcess, remoteStr, 0, MEM_RELEASE);
        return nullptr;
    }

    // 获取 LoadLibraryW 地址
    // x64 下 kernel32.dll 在所有进程加载地址相同，可直接用本地地址
    HMODULE hKernel32 = GetModuleHandleW(L"kernel32.dll");
    if (!hKernel32) {
        LOG_ERROR("GetModuleHandleW(kernel32) failed");
        VirtualFreeEx(hProcess, remoteStr, 0, MEM_RELEASE);
        return nullptr;
    }
    auto pLoadLib = reinterpret_cast<LPTHREAD_START_ROUTINE>(
        GetProcAddress(hKernel32, "LoadLibraryW"));
    if (!pLoadLib) {
        LOG_ERROR("GetProcAddress(LoadLibraryW) failed");
        VirtualFreeEx(hProcess, remoteStr, 0, MEM_RELEASE);
        return nullptr;
    }

    // 创建远程线程调用 LoadLibraryW(remoteStr)
    HANDLE hThread = CreateRemoteThread(
        hProcess, nullptr, 0, pLoadLib, remoteStr, 0, nullptr);
    if (!hThread) {
        LOG_ERROR("CreateRemoteThread(LoadLibraryW) failed: %lu", GetLastError());
        VirtualFreeEx(hProcess, remoteStr, 0, MEM_RELEASE);
        return nullptr;
    }
    HandleGuard threadGuard(hThread);

    // 等待远程线程结束（10s 超时）
    LOG_INFO("RemoteLoadLibrary: waiting LoadLibraryW thread (target pid=%u)",
             GetProcessId(hProcess));
    DWORD waitRes = WaitForSingleObject(hThread, 10000);
    LOG_INFO("RemoteLoadLibrary: LoadLibraryW thread finished, res=%lu elapsed=%ldms",
             waitRes,
             (waitRes == WAIT_OBJECT_0) ? 0L : -1L);
    if (waitRes != WAIT_OBJECT_0) {
        LOG_ERROR("Remote LoadLibraryW thread wait failed: %lu (res=%lu)",
                  GetLastError(), waitRes);
        VirtualFreeEx(hProcess, remoteStr, 0, MEM_RELEASE);
        return nullptr;
    }

    // 线程退出码即 LoadLibraryW 返回值（HMODULE）
    // 注意：GetExitCodeThread 只有 32 位，64 位 HMODULE 的高 32 位会被截断
    //（远程 DLL 基址可能 >4G，不能依赖退出码）→ 用 EnumProcessModulesEx
    // 枚举目标进程模块，按文件名匹配 injected.dll，拿完整 64 位基址
    DWORD exitCode = 0;
    if (!GetExitCodeThread(hThread, &exitCode)) {
        LOG_ERROR("GetExitCodeThread failed: %lu", GetLastError());
        VirtualFreeEx(hProcess, remoteStr, 0, MEM_RELEASE);
        return nullptr;
    }

    // 释放远程字符串内存
    VirtualFreeEx(hProcess, remoteStr, 0, MEM_RELEASE);

    if (exitCode == 0) {
        LOG_ERROR("Remote LoadLibraryW returned 0 (DLL load failed in target)");
        return nullptr;
    }

    HMODULE hRemoteDll = ProcessHelper::FindRemoteModuleByPath(hProcess, L"injected.dll");
    if (hRemoteDll == nullptr) {
        LOG_ERROR("Remote LoadLibraryW succeeded but module '%ls' not found "
                  "in target (%lu)", L"injected.dll", GetLastError());
        return nullptr;
    }

    return hRemoteDll;
}

} // namespace terminjector
