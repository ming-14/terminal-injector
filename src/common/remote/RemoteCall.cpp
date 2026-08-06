// RemoteCall 实现：跨进程调用 DLL 导出函数并传参
// 详见 RemoteCall.h 头注释
//
// 实现要点：
//   - 导出地址用"本地 LoadLibraryExW(DONT_RESOLVE_DLL_REFERENCES) 解析
//     RVA + 远程模块基址"计算，不依赖远程 GetProcAddress stub：
//     远程自定义页 stub 受目标进程 CFG/远程线程防护影响随机崩溃
//     （0xC0000005，见调试记录），且每进程远程线程数量受限，第 3 个
//     远程线程会被拒绝（CreateRemoteThread 返回 5）
//   - 远程线程压缩到 2 个（LoadLibraryW + 导出调用），避开线程数限制
#include "RemoteCall.h"
#include "../logging/Logger.h"

#include <string>

namespace terminjector {

namespace {

// 本地解析 DLL 导出函数的 RVA（不执行 DllMain）
// 返回 false 表示 DLL 加载失败或导出函数不存在
bool ResolveExportRva(const std::wstring& dllPath, const char* exportName,
                      uintptr_t* outRva) {
    HMODULE hLocal = LoadLibraryExW(dllPath.c_str(), nullptr,
                                    DONT_RESOLVE_DLL_REFERENCES);
    if (hLocal == nullptr) {
        LOG_ERROR("RemoteCallExport: LoadLibraryExW(local, noresolve) failed: %lu",
                  GetLastError());
        return false;
    }
    uintptr_t pfn = reinterpret_cast<uintptr_t>(
        GetProcAddress(hLocal, exportName));
    uintptr_t base = reinterpret_cast<uintptr_t>(hLocal);
    FreeLibrary(hLocal);
    if (pfn == 0) {
        LOG_ERROR("RemoteCallExport: local GetProcAddress('%s') failed",
                  exportName);
        return false;
    }
    *outRva = pfn - base;
    return true;
}

// 远程内存守卫：RAII 释放 VirtualAllocEx 分配
struct RemoteBuffer {
    HANDLE hProcess;
    LPVOID addr = nullptr;
    ~RemoteBuffer() {
        if (addr != nullptr) {
            VirtualFreeEx(hProcess, addr, 0, MEM_RELEASE);
        }
    }
};

} // namespace

bool RemoteCallExport(HANDLE hProcess, HMODULE hRemoteDll,
                      const std::wstring& dllPath,
                      const char* exportName,
                      const void* param, size_t paramSize,
                      uintptr_t* outRet) {
    if (hProcess == nullptr || hProcess == INVALID_HANDLE_VALUE ||
        hRemoteDll == nullptr || exportName == nullptr) {
        LOG_ERROR("RemoteCallExport: invalid args");
        return false;
    }

    // 1. 本地解析导出 RVA，换算目标进程地址 = 远程模块基址 + RVA
    uintptr_t rva = 0;
    if (!ResolveExportRva(dllPath, exportName, &rva)) {
        return false;
    }
    uintptr_t pfn = reinterpret_cast<uintptr_t>(hRemoteDll) + rva;
    LOG_INFO("RemoteCallExport: export '%s' resolved at %p in target",
             exportName, reinterpret_cast<void*>(pfn));

    // 2. 远程写参数块（若需要）
    RemoteBuffer paramBuf{hProcess};
    if (param != nullptr && paramSize > 0) {
        paramBuf.addr = VirtualAllocEx(hProcess, nullptr, paramSize,
                                       MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
        if (paramBuf.addr == nullptr) {
            LOG_ERROR("RemoteCallExport: VirtualAllocEx(param) failed: %lu",
                      GetLastError());
            return false;
        }
        if (!WriteProcessMemory(hProcess, paramBuf.addr, param, paramSize,
                                nullptr)) {
            LOG_ERROR("RemoteCallExport: WriteProcessMemory(param) failed: %lu",
                      GetLastError());
            return false;
        }
    }

    // 3. 远程线程调用导出函数(参数)，退出码 = 返回值
    //    目标进程的远程线程防护允许前 2 个远程线程（LoadLibraryW + 本线程）
    HANDLE hT2 = CreateRemoteThread(
        hProcess, nullptr, 0,
        reinterpret_cast<LPTHREAD_START_ROUTINE>(pfn),
        paramBuf.addr, 0, nullptr);
    if (hT2 == nullptr) {
        LOG_ERROR("RemoteCallExport: CreateRemoteThread(export) failed: %lu",
                  GetLastError());
        return false;
    }
    if (WaitForSingleObject(hT2, 10000) != WAIT_OBJECT_0) {
        LOG_ERROR("RemoteCallExport: wait export thread failed: %lu",
                  GetLastError());
        CloseHandle(hT2);
        return false;
    }
    DWORD ret = 0;
    if (!GetExitCodeThread(hT2, &ret)) {
        LOG_ERROR("RemoteCallExport: GetExitCodeThread(export) failed: %lu",
                  GetLastError());
        CloseHandle(hT2);
        return false;
    }
    CloseHandle(hT2);

    LOG_INFO("RemoteCallExport: '%s' returned %lu", exportName, ret);
    if (outRet != nullptr) {
        *outRet = ret;
    }
    return ret != 0;
}

} // namespace terminjector
