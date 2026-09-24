// 同位数远程注入原语实现（Phase 23）
// 详见 child_inject.h 的说明
#include "child_inject.h"

#include "logging/Logger.h"
#include "remote/RemoteCall.h"
#include "transport/PipeParams.h"

namespace terminjector::relay32 {

namespace {

// 单次远程调用（LoadLibraryW / 导出函数）等待上限
constexpr DWORD kRemoteWaitMs = 10000;

} // namespace

bool InjectDllSameBitness(HANDLE hProcess, uint32_t targetPid,
                          const std::wstring& dllPath,
                          const std::wstring& pipeName,
                          uint32_t mediatorPid) {
    // 1. 在目标进程里写一份 DLL 路径（含 null 终止符）
    const size_t bytes = (dllPath.size() + 1) * sizeof(wchar_t);
    LPVOID remoteStr = VirtualAllocEx(hProcess, nullptr, bytes,
                                      MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (remoteStr == nullptr) {
        LOG_ERROR("inject: VirtualAllocEx failed: %lu", GetLastError());
        return false;
    }

    SIZE_T written = 0;
    if (!WriteProcessMemory(hProcess, remoteStr, dllPath.c_str(), bytes, &written) ||
        written != bytes) {
        LOG_ERROR("inject: WriteProcessMemory failed: %lu", GetLastError());
        VirtualFreeEx(hProcess, remoteStr, 0, MEM_RELEASE);
        return false;
    }

    // 2. 取本进程 kernel32!LoadLibraryW 地址。
    //    同位数下该地址在目标内同样有效（KnownDLL 同址），见头文件说明。
    HMODULE hK32 = GetModuleHandleW(L"kernel32.dll");
    auto pLoadLib = (hK32 != nullptr)
        ? reinterpret_cast<LPTHREAD_START_ROUTINE>(GetProcAddress(hK32, "LoadLibraryW"))
        : nullptr;
    if (pLoadLib == nullptr) {
        LOG_ERROR("inject: resolve LoadLibraryW failed");
        VirtualFreeEx(hProcess, remoteStr, 0, MEM_RELEASE);
        return false;
    }

    HANDLE hThread = CreateRemoteThread(hProcess, nullptr, 0, pLoadLib,
                                        remoteStr, 0, nullptr);
    if (hThread == nullptr) {
        LOG_ERROR("inject: CreateRemoteThread failed: %lu", GetLastError());
        VirtualFreeEx(hProcess, remoteStr, 0, MEM_RELEASE);
        return false;
    }

    const DWORD waitRes = WaitForSingleObject(hThread, kRemoteWaitMs);
    DWORD exitCode = 0;
    const BOOL gotExit = GetExitCodeThread(hThread, &exitCode);
    CloseHandle(hThread);
    VirtualFreeEx(hProcess, remoteStr, 0, MEM_RELEASE);

    if (waitRes != WAIT_OBJECT_0) {
        LOG_ERROR("inject: LoadLibraryW thread not finished (wait=%lu), pid=%u",
                  waitRes, targetPid);
        return false;
    }
    if (!gotExit) {
        LOG_ERROR("inject: GetExitCodeThread failed: %lu", GetLastError());
        return false;
    }
    if (exitCode == 0) {
        LOG_ERROR("inject: LoadLibraryW returned 0 in pid=%u (load failed)", targetPid);
        return false;
    }

    // 3. 同位数下 HMODULE 就是 32 位，线程退出码即完整基址，
    //    不需要像 x64 那样再枚举模块表补高位。
    const HMODULE hRemoteDll =
        reinterpret_cast<HMODULE>(static_cast<uintptr_t>(exitCode));
    LOG_INFO("inject: dll loaded in pid=%u at 0x%X", targetPid, exitCode);

    // 4. 下发管道参数（目标内 DLL 等这个参数才去连 mediator）
    PipeParams params{};
    wcsncpy_s(params.pipeName, kMaxPipeNameLen, pipeName.c_str(), _TRUNCATE);
    params.mediatorPid = mediatorPid;
    if (!RemoteCallExport(hProcess, hRemoteDll, dllPath, "RemotePipeSetup",
                          &params, sizeof(params), nullptr)) {
        LOG_ERROR("inject: RemotePipeSetup failed in pid=%u", targetPid);
        return false;
    }

    LOG_INFO("inject: done pid=%u pipe=%ls mediatorPid=%u",
             targetPid, pipeName.c_str(), mediatorPid);
    return true;
}

} // namespace terminjector::relay32
