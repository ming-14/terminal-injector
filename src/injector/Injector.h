// DLL 注入器：将 injected.dll 远程加载到目标进程
// 详见 docs/phases/02-injector-modes.md 4.4
//
// 注入流程：
//   1. EnableDebugPrivilege（提升 SeDebugPrivilege）
//   2. OpenProcess（PROCESS_CREATE_THREAD | VM_* | QUERY_INFORMATION）
//   3. 架构校验（IsX64Process，拒绝 32 位目标）
//   4. RemoteLoadLibrary（VirtualAllocEx + WriteProcessMemory + CreateRemoteThread）
//
// 管道名约定：
//   DLL 用 GetCurrentProcessId() 自发现管道名
//   \\.\pipe\terminjector_<targetPid>
//   注入器 Inject() 的 pipeName 参数仅用于校验/日志，不传给 DLL
#pragma once

#include <windows.h>
#include <cstdint>
#include <string>

namespace terminjector {

class Injector {
public:
    Injector();
    ~Injector();

    Injector(const Injector&) = delete;
    Injector& operator=(const Injector&) = delete;

    // 注入 DLL 到目标进程
    // targetPid 目标进程 PID
    // dllPath  injected.dll 绝对路径（内部转短路径）
    // pipeName 约定管道名（仅日志用，DLL 自发现）
    // 返回 true 成功
    bool Inject(uint32_t targetPid, const std::wstring& dllPath,
                const std::wstring& pipeName);

private:
    // 提升 SeDebugPrivilege（注入同权限或更低权限进程时非必需，但建议启用）
    bool EnableDebugPrivilege();

    // 远程调用 LoadLibraryW 加载 DLL
    // 返回远程 HMODULE（失败返回 nullptr）
    HMODULE RemoteLoadLibrary(HANDLE hProcess, const std::wstring& dllPath);
};

} // namespace terminjector
