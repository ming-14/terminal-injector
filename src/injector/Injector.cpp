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
#include "../common/logging/Logger.h"

#include <cstring>

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
                     const std::wstring& pipeName) {
    LOG_INFO("Inject starting, pid=%u dll=%ls pipe=%ls",
             targetPid, dllPath.c_str(), pipeName.c_str());

    if (targetPid == 0) {
        LOG_ERROR("Inject: targetPid is 0");
        return false;
    }
    if (dllPath.empty()) {
        LOG_ERROR("Inject: dllPath is empty");
        return false;
    }

    // 1. 提升 SeDebugPrivilege（非必需，但管理员运行时能注入更多进程）
    if (!EnableDebugPrivilege()) {
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

    // 5. DLL 自发现管道名（约定方案，无需远程调用导出函数）
    //    DLL DllMain 中：
    //    - GetCurrentProcessId() 得到 targetPid
    //    - MakePipeName(targetPid) 构造管道名
    //    - 连接 mediator（Phase 2 验证用，Phase 3 改懒加载）
    LOG_INFO("Injection complete, pid=%u (DLL self-discovers pipe via convention)",
             targetPid);
    return true;
}

bool Injector::EnableDebugPrivilege() {
    HANDLE hToken = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(),
                          TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, &hToken)) {
        LOG_ERROR("OpenProcessToken failed: %lu", GetLastError());
        return false;
    }
    HandleGuard tokenGuard(hToken);

    LUID luid{};
    // 注意：SE_DEBUG_NAME 在未定义 UNICODE 时展开为窄字符串，
    // LookupPrivilegeValueW 要求 LPCWSTR，故显式使用 L"SeDebugPrivilege"
    if (!LookupPrivilegeValueW(nullptr, L"SeDebugPrivilege", &luid)) {
        LOG_ERROR("LookupPrivilegeValueW failed: %lu", GetLastError());
        return false;
    }

    TOKEN_PRIVILEGES tp{};
    tp.PrivilegeCount = 1;
    tp.Privileges[0].Luid = luid;
    tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED;

    // AdjustTokenPrivileges 返回 TRUE 不代表权限真正获得，需检查 GetLastError
    if (!AdjustTokenPrivileges(hToken, FALSE, &tp, sizeof(tp), nullptr, nullptr)) {
        LOG_ERROR("AdjustTokenPrivileges failed: %lu", GetLastError());
        return false;
    }
    DWORD err = GetLastError();
    if (err == ERROR_NOT_ALL_ASSIGNED) {
        LOG_WARN("SeDebugPrivilege not assigned (need admin)");
        return false;
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
    DWORD waitRes = WaitForSingleObject(hThread, 10000);
    if (waitRes != WAIT_OBJECT_0) {
        LOG_ERROR("Remote LoadLibraryW thread wait failed: %lu (res=%lu)",
                  GetLastError(), waitRes);
        VirtualFreeEx(hProcess, remoteStr, 0, MEM_RELEASE);
        return nullptr;
    }

    // 线程退出码即 LoadLibraryW 返回值（HMODULE）
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

    return reinterpret_cast<HMODULE>(exitCode);
}

} // namespace terminjector
